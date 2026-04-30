"""TSL ladder runner — automated SL ratchet on Kite-managed trades.

Runs once per day after the EOD bhavcopy lands (15:50 IST cron + boot
catchup). For every open Trade with a non-null kite_trigger_id, decides
whether the SL should ratchet up per the ladder, and if so, calls
modify_gtt at Kite.

The cure for the GRSE failure mode: trader has a winning position, price
moves up, but emotionally can't bring themselves to move SL up. Result:
profit gives back, sometimes turns into a loss. With this daemon, the SL
moves up on rule, no human in the loop.

LADDER (SL only, no trim/scale-out yet):

  At +1R unrealised (close basis):
    → lock SL to entry  (0R floor — Minervini's "never give back a
                          full-1R loser after being a winner" rule)

  At +2R:
    → trail SL = max(current SL, anchor_EMA × buffer)
       PDL  → 0.995 × prior bar low
       10EMA → 0.995 × 10-period EMA of close
       5EMA  → 0.997 × 5-period EMA  (tighter cushion for late stage)

  At +3R or higher:
    → keep trailing per anchor; ratchet aggressively if anchor moves up
    (trim/scale-out comes in Phase E2.2 — separate work)

Hard exit (placed at SL, not as a separate logic): if anchor breaks on
EOD basis, the SL leg of the OCO will have already fired the next day
when price gaps below it — the bracket handles it.

Decisions written to TslDecision regardless of outcome (HOLD logged too)
so we have a forensic trail.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterable

from sqlalchemy.orm import Session

from ..models import DailyBar, Trade, TslDecision, User
from . import kite_audited

log = logging.getLogger("journal.trading_engine.tsl")


# ---------------------------------------------------------------------------
# Anchor math
# ---------------------------------------------------------------------------

PDL_BUFFER = 0.005      # 0.5% below previous-day low
EMA_BUFFER_LOOSE = 0.005   # 10 EMA - 0.5%
EMA_BUFFER_TIGHT = 0.003   # 5 EMA - 0.3%


def _ema(values: list[float], period: int) -> float | None:
    """Standard EMA. Seeds with SMA of the first ``period`` values, then
    chains the multiplier. Returns None if insufficient data."""
    if len(values) < period:
        return None
    multiplier = 2.0 / (period + 1)
    ema_val = sum(values[:period]) / period
    for v in values[period:]:
        ema_val = (v - ema_val) * multiplier + ema_val
    return ema_val


def _compute_anchor_value(anchor: str, bars: list) -> float | None:
    """Return the price level the anchor recommends as the trailing stop
    (already buffered down by the relevant cushion).

    bars: list of objects with .high/.low/.close, ascending date order.
    """
    if not bars:
        return None
    if anchor == "PDL":
        # Previous-day low. ``bars[-1]`` is today (just closed); for an
        # EOD ratchet, "previous day" means the last completed bar.
        return bars[-1].low * (1.0 - PDL_BUFFER)
    if anchor == "10EMA":
        closes = [b.close for b in bars[-30:]]
        ema10 = _ema(closes, 10)
        return ema10 * (1.0 - EMA_BUFFER_LOOSE) if ema10 else None
    if anchor == "5EMA":
        closes = [b.close for b in bars[-15:]]
        ema5 = _ema(closes, 5)
        return ema5 * (1.0 - EMA_BUFFER_TIGHT) if ema5 else None
    return None


# ---------------------------------------------------------------------------
# Per-trade decision
# ---------------------------------------------------------------------------


@dataclass
class TradeDecision:
    """Pure-function output of the ladder decision for one trade.

    The runner takes this and either calls modify_gtt + writes a TslDecision,
    or just writes a HOLD TslDecision.
    """
    action: str                  # "HOLD" / "MOVED_SL" / "ERROR"
    cmp: float
    current_r: float | None
    anchor: str | None
    anchor_value: float | None
    current_stop: float
    proposed_stop: float | None
    reason: str


def _raw_anchor_value(anchor: str, bars: list) -> float | None:
    """Like _compute_anchor_value but WITHOUT the buffer — used for the
    anchor-break check. We compare close vs the actual anchor (PDL,
    10EMA, 5EMA), not the buffered trail target."""
    if not bars:
        return None
    if anchor == "PDL":
        return float(bars[-1].low)
    if anchor == "10EMA":
        closes = [float(b.close) for b in bars[-30:]]
        return _ema(closes, 10)
    if anchor == "5EMA":
        closes = [float(b.close) for b in bars[-15:]]
        return _ema(closes, 5)
    return None


def decide(trade: Trade, bars: list, *, today: date | None = None) -> TradeDecision:
    """Compute the ladder decision for a single trade against the latest bars.

    Pure function — does NOT call Kite, does NOT mutate the Trade. Caller
    handles persistence + side effects.

    bars: list of DailyBar-shaped rows with .high/.low/.close, ascending.
          The newest bar is "today's close" — the EOD reference price.

    Three possible actions:
      HOLD              — no ratchet AND anchor not broken. Most days.
      MOVED_SL          — ratchet SL up (broker.modify_gtt called by caller).
      EXIT_RECOMMENDED  — EOD close < anchor. Caller does NOT cancel the GTT
                          (broker SL still protects); just surfaces a red
                          banner so the trader exits manually at next open.
                          Graduates to auto-exit once trust is established.
    """
    if not bars:
        return TradeDecision(
            action="HOLD", cmp=0.0, current_r=None,
            anchor=trade.tsl_anchor, anchor_value=None,
            current_stop=trade.sl, proposed_stop=None,
            reason="no bars cached for symbol",
        )

    latest = bars[-1]
    cmp = float(latest.close)

    # Use trade.tsl as the live trailing stop if set (post-ratchet); fall
    # back to trade.sl (the original entry-time stop). The "effective stop"
    # is what's currently at the broker.
    current_stop = float(trade.tsl) if trade.tsl else float(trade.sl)

    entry = float(trade.initial_entry_price)
    initial_sl = float(trade.sl)
    risk_per_share = abs(entry - initial_sl)
    if risk_per_share < 0.01:
        return TradeDecision(
            action="HOLD", cmp=cmp, current_r=None,
            anchor=trade.tsl_anchor, anchor_value=None,
            current_stop=current_stop, proposed_stop=None,
            reason="risk-per-share too small (entry==sl)",
        )

    # Direction-aware R-multiple.
    sign = 1.0 if trade.side == "B" else -1.0
    current_r = (cmp - entry) * sign / risk_per_share

    anchor = trade.tsl_anchor or "PDL"
    anchor_value = _compute_anchor_value(anchor, bars)
    raw_anchor = _raw_anchor_value(anchor, bars)

    # ----------------------------------------------------------------------
    # Anchor-break check — runs FIRST, takes precedence over ratchet.
    #
    # Definition: EOD close < raw anchor (no buffer). For PDL anchor, we
    # use today's bar's low as the anchor reference (i.e., did today's
    # close print below today's own low? trivially never true — so for
    # PDL we look at the prior bar's low instead, which IS the "previous
    # day low" by name).
    #
    # Only fires when R > -0.5 — if the trade is already deeply underwater,
    # the original SL would have fired and we'd not be in this code path.
    # The R>-0.5 guard avoids redundant exit signals on positions about to
    # be auto-stopped anyway.
    # ----------------------------------------------------------------------
    break_value = None
    if anchor == "PDL" and len(bars) >= 2:
        # Use bar[-2].low (the actual "previous day low") for the break check.
        break_value = float(bars[-2].low)
    elif anchor in ("5EMA", "10EMA"):
        break_value = raw_anchor

    if break_value is not None and current_r > -0.5:
        broken = cmp < break_value if sign > 0 else cmp > break_value
        if broken:
            return TradeDecision(
                action="EXIT_RECOMMENDED", cmp=cmp, current_r=current_r,
                anchor=anchor, anchor_value=break_value,
                current_stop=current_stop, proposed_stop=None,
                reason=(
                    f"EOD close ₹{cmp:.2f} broke {anchor} "
                    f"@ ₹{break_value:.2f} (R={current_r:+.2f}). "
                    f"Recommendation: exit at next session open. "
                    f"GTT remains active — broker SL ₹{current_stop:.2f} "
                    f"still protects against further drawdown."
                ),
            )

    # Below +1R: do nothing. The original SL is doing its job.
    if current_r < 1.0:
        return TradeDecision(
            action="HOLD", cmp=cmp, current_r=current_r,
            anchor=anchor, anchor_value=anchor_value,
            current_stop=current_stop, proposed_stop=None,
            reason=f"R={current_r:.2f} below +1R floor — no ratchet yet",
        )

    # +1R to +2R: lock SL to entry.
    if current_r < 2.0:
        target_stop = entry
    else:
        # +2R and above: trail per anchor. If anchor calc failed (insufficient
        # bars for EMA, etc.), still advance SL to entry as a fallback.
        if anchor_value is None or anchor_value <= entry:
            target_stop = entry
        else:
            target_stop = anchor_value

    # SL only ratchets UP for longs (down for shorts).
    if sign > 0:
        new_stop = max(current_stop, target_stop)
        moves = new_stop > current_stop + 0.01
    else:
        new_stop = min(current_stop, target_stop)
        moves = new_stop < current_stop - 0.01

    if not moves:
        anchor_str = f"{anchor_value:.2f}" if anchor_value else "n/a"
        return TradeDecision(
            action="HOLD", cmp=cmp, current_r=current_r,
            anchor=anchor, anchor_value=anchor_value,
            current_stop=current_stop, proposed_stop=target_stop,
            reason=(
                f"R={current_r:.2f}, anchor={anchor}@{anchor_str} — "
                f"target {target_stop:.2f} not better than current SL {current_stop:.2f}"
            ),
        )

    return TradeDecision(
        action="MOVED_SL", cmp=cmp, current_r=current_r,
        anchor=anchor, anchor_value=anchor_value,
        current_stop=current_stop, proposed_stop=new_stop,
        reason=(
            f"R={current_r:.2f} → ratchet SL "
            f"₹{current_stop:.2f} → ₹{new_stop:.2f} "
            f"(anchor={anchor})"
        ),
    )


# ---------------------------------------------------------------------------
# The runner — applies decisions across every Kite-managed open trade
# ---------------------------------------------------------------------------


def _bars_for_trade(db: Session, trade: Trade, lookback_days: int = 60) -> list:
    """Load DailyBars for a trade's instrument, ascending date order."""
    from datetime import timedelta
    cutoff = date.today() - timedelta(days=lookback_days)
    return (
        db.query(DailyBar)
        .filter(DailyBar.symbol == trade.instrument.upper())
        .filter(DailyBar.date >= cutoff)
        .order_by(DailyBar.date.asc())
        .all()
    )


def _open_qty(trade: Trade) -> int:
    """Initial + pyramids - exits. Same calc as portfolio but inlined to
    avoid pulling the whole metrics machinery for one number."""
    pyr = sum(p.qty for p in trade.pyramids)
    ex = sum(e.qty for e in trade.exits)
    return trade.initial_qty + pyr - ex


def run_for_user(db: Session, user: User, *, today: date | None = None) -> dict:
    """Walk every Kite-managed open trade for ``user``, decide, persist.

    Returns a summary dict suitable for log lines / status pages:
      {evaluated: N, moved: N, held: N, errored: N, skipped: [...]}
    """
    today = today or date.today()
    trades = (
        db.query(Trade)
        .filter(Trade.user_id == user.id)
        .filter(Trade.status == "open")
        .filter(Trade.kite_trigger_id.isnot(None))
        .all()
    )

    summary = {
        "evaluated": 0, "moved": 0, "held": 0, "errored": 0,
        "exit_recommended": 0, "skipped": [],
        "user_id": user.id, "date": today.isoformat(),
    }

    for trade in trades:
        # Idempotence guard — composite unique index on (trade_id, decision_date)
        # prevents double-running, but we'd rather avoid the IntegrityError
        # noise. Skip cleanly if today's row already exists.
        already = (
            db.query(TslDecision)
            .filter(TslDecision.trade_id == trade.id)
            .filter(TslDecision.decision_date == today)
            .first()
        )
        if already is not None:
            summary["skipped"].append(trade.instrument)
            continue

        bars = _bars_for_trade(db, trade)
        decision = decide(trade, bars, today=today)
        summary["evaluated"] += 1

        # Persist the decision (HOLD or MOVED_SL or ERROR — all get a row).
        row = TslDecision(
            user_id=user.id, trade_id=trade.id,
            decision_date=today, decided_at=datetime.utcnow(),
            cmp=decision.cmp, current_r=decision.current_r,
            anchor=decision.anchor, anchor_value=decision.anchor_value,
            current_stop=decision.current_stop,
            proposed_stop=decision.proposed_stop,
            action=decision.action, reason=decision.reason,
        )

        if decision.action == "EXIT_RECOMMENDED":
            # No Kite call — broker SL still protects. We just log + alert.
            # Per Path A (notification-only): trader sees red banner on
            # /cockpit + EXIT_RECOMMENDED row on /trading/kite, exits manually
            # at next session open. Path C (auto-exit) is a future graduation.
            db.add(row)
            db.commit()
            summary["exit_recommended"] += 1
            log.info(
                "tsl exit recommended for %s: %s",
                trade.instrument, decision.reason,
            )
            continue

        if decision.action != "MOVED_SL":
            db.add(row)
            db.commit()
            summary["held"] += 1
            continue

        # Action: ratchet SL via modify_gtt at Kite.
        try:
            qty = _open_qty(trade)
            if qty <= 0:
                row.action = "HOLD"
                row.reason = f"open_qty={qty} — fully exited, nothing to modify"
                db.add(row)
                db.commit()
                summary["held"] += 1
                continue

            target = trade.kite_target_price or (decision.proposed_stop * 1.5)  # fallback
            resp = kite_audited.modify_gtt(
                db, user, trade.kite_trigger_id,
                symbol=trade.instrument, qty=qty,
                stop_price=decision.proposed_stop,
                target_price=target,
                transaction_type="BUY" if trade.side == "B" else "SELL",
                last_price=decision.cmp,
            )
            row.modify_response = json.dumps(resp, default=str)[:8000]
            # Update the journal's tsl field — leaves the original sl intact
            # as the immutable 1R reference.
            trade.tsl = decision.proposed_stop
            db.add(row)
            db.commit()
            summary["moved"] += 1
            log.info(
                "tsl ratcheted %s: ₹%.2f → ₹%.2f (R=%.2f, anchor=%s)",
                trade.instrument, decision.current_stop, decision.proposed_stop,
                decision.current_r, decision.anchor,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("modify_gtt failed for trade=%s", trade.id)
            row.action = "ERROR"
            row.error = f"{type(exc).__name__}: {exc}"
            db.add(row)
            db.commit()
            summary["errored"] += 1

    return summary


def run_for_all_users(db: Session) -> list[dict]:
    """Walk every active user; produce one summary per user. Used by the
    APScheduler hook + boot catchup."""
    summaries: list[dict] = []
    users = db.query(User).filter(User.is_active.is_(True)).all()
    for user in users:
        try:
            summaries.append(run_for_user(db, user))
        except Exception as exc:  # noqa: BLE001
            log.exception("tsl run failed for user_id=%s", user.id)
            summaries.append({"user_id": user.id, "error": str(exc)})
    return summaries
