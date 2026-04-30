"""Trading engine HTTP surface.

Phase E0 (read-only): /trading/kite — broker session health + holdings +
positions + GTT list + reconciliation against journal Trade rows. ZERO
write actions. Every Kite call goes through ``trading_engine.kite_audited``
so each one writes a ``broker_audit`` row.

Future phases:
  /trading/orders   (E1+: list, place, modify, cancel)
  /trading/gtts     (E1+: per-pick GTT submit, modification log)
  /trading/audit    (any phase: paginated broker_audit viewer)
  /trading/halt     (kill switch toggle)
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from datetime import datetime, timedelta

from fastapi import Form, HTTPException
from fastapi.responses import RedirectResponse

from .. import auth as auth_mod
from .. import dashboard as dash_svc
from .. import kite as kite_mod
from ..db import get_db
from ..deps import templates
from ..models import BrokerAudit, Trade, User
from ..trading_engine import kite_audited

log = logging.getLogger("journal.trading")

router = APIRouter(prefix="/trading")

# Phase E1 hard daily cap on cumulative new risk. Refuses GTT submissions
# once the sum of (entry - stop) × qty for *today's* placed GTTs exceeds
# this fraction of total capital. Server-side enforcement so the user
# can't override even by forging the form.
DAILY_RISK_CAP_PCT = 0.025   # 2.5% of capital — see user_north_star memory


# Symbol-alias loader — maps journal-symbol → broker-symbol so SSEGL in
# the journal reconciles against SSEGL-SM at the broker. File is hand-
# curated (data/symbol_aliases.csv); reload on mtime change.

from pathlib import Path as _Path
import csv as _csv

_ALIASES_FILE = _Path(__file__).resolve().parent.parent.parent / "data" / "symbol_aliases.csv"
_alias_cache: dict[str, str] | None = None
_alias_mtime: float = 0.0


def _load_aliases() -> dict[str, str]:
    """Return {journal_symbol → broker_symbol} dict. Empty if file missing."""
    global _alias_cache, _alias_mtime
    if not _ALIASES_FILE.exists():
        return {}
    mtime = _ALIASES_FILE.stat().st_mtime
    if _alias_cache is not None and mtime == _alias_mtime:
        return _alias_cache
    out: dict[str, str] = {}
    try:
        with _ALIASES_FILE.open() as f:
            for row in _csv.reader(f):
                if not row or len(row) < 2:
                    continue
                first = row[0].strip()
                if not first or first.startswith("#") or first.lower() == "journal_symbol":
                    continue
                journal_sym = first.upper()
                broker_sym = row[1].strip().upper()
                if journal_sym and broker_sym:
                    out[journal_sym] = broker_sym
    except Exception as exc:  # noqa: BLE001
        log.warning("symbol_aliases load failed: %s", exc)
        return _alias_cache or {}
    _alias_cache = out
    _alias_mtime = mtime
    return out


def _reconcile(holdings: list[dict], net_positions: list[dict],
               open_trades: list[Trade]) -> dict:
    """Cross-check journal open trades against broker state.

    Three buckets returned:
      - matched:    trade exists in broker (qty agrees within tolerance)
      - mismatched: trade exists in broker but qty differs (suspicious)
      - missing:    trade exists in journal but NOT in broker (orphan)
      - extra:      broker has a position with no journal Trade (manual?)

    Reconciliation uses tradingsymbol as the join key. Holdings (T1+) and
    net positions (intraday) are unioned — a stock you bought today shows
    up in net positions only until settlement.
    """
    # Symbol aliases: journal-symbol → broker-symbol. SSEGL in journal but
    # SSEGL-SM at broker is the same stock. Apply forward (journal → broker)
    # AND reverse (broker → journal) so either-direction lookups merge cleanly.
    aliases = _load_aliases()
    reverse_aliases = {v: k for k, v in aliases.items()}

    def _canonical(sym: str) -> str:
        """Normalise a symbol to its journal-side canonical form, so two
        rows that are actually the same stock collapse into one bucket."""
        s = (sym or "").upper()
        return reverse_aliases.get(s, s)

    # Build broker-side qty per symbol. Holdings = settled, positions.net = today.
    broker_qty: dict[str, int] = {}
    for h in holdings:
        sym = _canonical(h.get("tradingsymbol") or "")
        broker_qty[sym] = broker_qty.get(sym, 0) + int(h.get("quantity") or 0)
    for p in net_positions:
        sym = _canonical(p.get("tradingsymbol") or "")
        broker_qty[sym] = broker_qty.get(sym, 0) + int(p.get("quantity") or 0)

    # Journal-side qty per symbol = sum(initial_qty + pyramids) - sum(exits).
    # Trades with entry_status='pending' are split out separately — they're
    # *expected* to be missing at the broker (BUY trigger hasn't fired yet).
    journal_qty: dict[str, int] = {}
    pending: list[dict] = []
    for t in open_trades:
        if getattr(t, "entry_status", "filled") == "pending":
            pending.append({
                "symbol": t.instrument,
                "qty": t.initial_qty,
                "trade_id": t.id,
                "trigger_price": t.initial_entry_price,
                "buy_trigger_id": t.kite_buy_trigger_id,
            })
            continue
        held = t.initial_qty + sum(p.qty for p in t.pyramids)
        held -= sum(e.qty for e in t.exits)
        sym = _canonical(t.instrument)
        journal_qty[sym] = journal_qty.get(sym, 0) + held

    matched, mismatched, missing, extra = [], [], [], []
    all_syms = set(broker_qty) | set(journal_qty)
    for sym in sorted(all_syms):
        b = broker_qty.get(sym, 0)
        j = journal_qty.get(sym, 0)
        if b == j and b > 0:
            matched.append({"symbol": sym, "qty": b})
        elif b > 0 and j > 0:
            mismatched.append({"symbol": sym, "broker_qty": b, "journal_qty": j})
        elif j > 0:
            missing.append({"symbol": sym, "journal_qty": j})
        elif b > 0:
            extra.append({"symbol": sym, "broker_qty": b})

    return {
        "matched": matched,
        "mismatched": mismatched,
        "missing": missing,
        "extra": extra,
        "pending": pending,
        "total_broker_symbols": len([s for s in broker_qty if broker_qty[s] > 0]),
        "total_journal_symbols": len([s for s in journal_qty if journal_qty[s] > 0]),
    }


@router.get("/kite", response_class=HTMLResponse)
def kite_dashboard(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(auth_mod.require_user),
):
    """Phase E0 dashboard. Read-only broker mirror."""
    auth = kite_mod.auth_status(user)
    profile, margins, holdings, positions, gtts = {}, {}, [], {"net": [], "day": []}, []
    fetch_error: str | None = None

    if auth["authed"]:
        try:
            profile = kite_audited.fetch_profile(db, user)
            margins = kite_audited.fetch_margins(db, user)
            holdings = kite_audited.fetch_holdings(db, user)
            positions = kite_audited.fetch_positions(db, user)
            gtts = kite_audited.fetch_gtts(db, user)
        except Exception as exc:  # noqa: BLE001
            log.exception("kite read-only fetch failed")
            fetch_error = f"{type(exc).__name__}: {exc}"

    # Reconcile against journal open trades.
    open_trades = (
        db.query(Trade)
        .filter(Trade.status == "open")
        .all()
    )
    # Eager-load pyramids/exits so .qty access doesn't N+1.
    for t in open_trades:
        _ = t.pyramids
        _ = t.exits
    reconciliation = _reconcile(holdings, positions["net"], open_trades)

    # Recent audit log entries for this user (last 20).
    recent_audit = (
        db.query(BrokerAudit)
        .filter(BrokerAudit.user_id == user.id)
        .order_by(BrokerAudit.created_at.desc())
        .limit(20)
        .all()
    )

    # Recent TSL ladder decisions (last 30) — joined with the trade so the
    # template can show instrument + side + tsl_anchor without N+1.
    from ..models import TslDecision
    recent_tsl = (
        db.query(TslDecision, Trade)
        .join(Trade, TslDecision.trade_id == Trade.id)
        .filter(TslDecision.user_id == user.id)
        .order_by(TslDecision.decided_at.desc())
        .limit(30)
        .all()
    )

    return templates.TemplateResponse(
        request,
        "trading/kite.html",
        {
            "auth": auth,
            "profile": profile,
            "margins": margins,
            "holdings": holdings,
            "positions": positions,
            "gtts": gtts,
            "reconciliation": reconciliation,
            "fetch_error": fetch_error,
            "recent_audit": recent_audit,
            "recent_tsl": recent_tsl,
        },
    )


# ---------------------------------------------------------------------------
# Phase E1 — one-click GTT submit from Auto-Pilot picks
# ---------------------------------------------------------------------------


def _today_placed_risk(db: Session, user: User) -> float:
    """Sum of (entry - stop) × qty across all GTT placements made today
    by this user. Read from broker_audit — that's the canonical record
    of every Kite call we made.
    """
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    rows = (
        db.query(BrokerAudit)
        .filter(
            BrokerAudit.user_id == user.id,
            BrokerAudit.endpoint == "place_gtt",
            BrokerAudit.created_at >= today_start,
            BrokerAudit.status == 200,
        )
        .all()
    )
    total_risk = 0.0
    for r in rows:
        try:
            payload = json.loads(r.request_json or "{}")
            tv = payload.get("trigger_values") or []
            orders = payload.get("orders") or []
            if not tv or not orders:
                continue
            qty = orders[0].get("quantity") or 0
            last_price = payload.get("last_price") or 0
            stop = min(tv) if tv else 0
            # For BUY: risk = (last_price - stop) × qty; entry ≈ last_price.
            risk = max(0.0, (last_price - stop) * qty)
            total_risk += risk
        except Exception:  # noqa: BLE001
            continue
    return total_risk


@router.post("/gtt/submit")
def submit_gtt(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(auth_mod.require_user),
    symbol: str = Form(...),
    qty: int = Form(...),
    entry_price: float = Form(...),
    stop_price: float = Form(...),
    target_price: float = Form(...),
    transaction_type: str = Form("BUY"),
    entry_mode: str = Form("now"),         # 'now' | 'trigger'
    auto_pilot_rank: str = Form(""),       # cosmetic, for the audit comment
):
    """Phase E1.1: hybrid entry submission.

    ``entry_mode='now'`` (default for Auto-Pilot picks) — place a regular
    MARKET BUY immediately, then place the GTT-OCO bracket. Entry fills
    in seconds; bracket sits as the exit.

    ``entry_mode='trigger'`` (planned breakout/pullback entries) — place a
    GTT-single BUY at ``entry_price``. The OCO bracket is NOT placed here;
    the TSL daemon (15:50 IST) checks for fills, then places the OCO once
    a position actually exists at the broker.

    Server-side guards apply uniformly to both paths:
      1. User must be Kite-authed
      2. qty > 0, prices in correct order
      3. Cumulative day risk + this trade's risk ≤ DAILY_RISK_CAP_PCT × capital
    """
    # Manual submissions (from /trading/kite) round-trip back there;
    # Auto-Pilot picks (from /cockpit) round-trip to cockpit.
    return_path = "/trading/kite" if auto_pilot_rank == "manual" else "/cockpit"

    if entry_mode not in ("now", "trigger"):
        entry_mode = "now"

    auth = kite_mod.auth_status(user)
    if not auth["authed"]:
        return RedirectResponse(
            url=f"{return_path}?gtt_err=not_authed", status_code=303,
        )

    # Daily-cap check (server-side, hard). Risk is the same regardless of
    # entry_mode because both paths arrive at the same SL.
    capital = dash_svc.current_capital(db)
    proposed_risk = max(0.0, (entry_price - stop_price) * qty) if transaction_type == "BUY" \
        else max(0.0, (stop_price - entry_price) * qty)
    today_risk = _today_placed_risk(db, user)
    cap_rs = capital * DAILY_RISK_CAP_PCT
    if today_risk + proposed_risk > cap_rs:
        return RedirectResponse(
            url=(
                f"{return_path}?gtt_err=daily_cap_exceeded"
                f"&cap=₹{int(cap_rs)}"
                f"&already=₹{int(today_risk)}"
                f"&proposed=₹{int(proposed_risk)}"
            ),
            status_code=303,
        )

    from datetime import date as _date
    from urllib.parse import quote

    if entry_mode == "now":
        # Path A — fire a LIMIT entry at the user's stated entry_price, then
        # attach the bracket. LIMIT (not MARKET) preserves the planned R math:
        # a 0.5% slippage from planned entry on a tight-SL setup costs 20-50%
        # of edge before the trade has begun. If LTP is currently above
        # entry_price for a BUY, the order sits open until LTP touches the
        # limit — the trader can monitor and cancel if they change their mind.
        try:
            buy_resp = kite_audited.place_order_limit(
                db, user,
                symbol=symbol, qty=qty, transaction_type=transaction_type,
                limit_price=entry_price,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("place_order LIMIT BUY failed")
            return RedirectResponse(
                url=f"{return_path}?gtt_err=buy_failed_{quote(str(exc))[:160]}",
                status_code=303,
            )
        buy_order_id = buy_resp.get("order_id")

        # Now place the OCO bracket. If it fails the BUY already fired —
        # surface the error but still create the Trade row so the user
        # sees it on /positions and can retry the bracket via UI.
        oco_trigger_id: int | None = None
        bracket_err: str | None = None
        try:
            oco_resp = kite_audited.place_gtt_oco(
                db, user,
                symbol=symbol, qty=qty,
                entry_price=entry_price,
                stop_price=stop_price,
                target_price=target_price,
                transaction_type=transaction_type,
            )
            oco_trigger_id = oco_resp.get("trigger_id") if isinstance(oco_resp, dict) else None
        except Exception as exc:  # noqa: BLE001
            log.exception("place_gtt_oco bracket failed (BUY already filled)")
            bracket_err = f"{type(exc).__name__}: {exc}"

        trade = Trade(
            user_id=user.id,
            instrument=symbol.upper(),
            side="B" if transaction_type == "BUY" else "S",
            entry_date=_date.today(),
            initial_entry_price=entry_price,
            initial_qty=qty,
            sl=stop_price,
            setup="Auto-Pilot E1" if auto_pilot_rank != "manual" else "Manual E1",
            status="open",
            kite_trigger_id=oco_trigger_id,
            kite_target_price=target_price,
            tsl_anchor="PDL",
            entry_status="filled",
            kite_buy_order_id=buy_order_id,
        )
        db.add(trade)
        db.commit()

        suffix = ""
        if bracket_err:
            suffix = f"&bracket_err={quote(bracket_err)[:160]}"
        return RedirectResponse(
            url=(
                f"{return_path}?gtt_ok=1&symbol={symbol}"
                f"&trigger_id={oco_trigger_id or 'pending'}"
                f"&order_id={buy_order_id or 'unknown'}"
                f"&trade_id={trade.id}{suffix}"
            ),
            status_code=303,
        )

    # Path B — entry_mode == 'trigger'. Place GTT-single BUY only.
    try:
        gtt_resp = kite_audited.place_gtt_single_buy(
            db, user,
            symbol=symbol, qty=qty, trigger_price=entry_price,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("place_gtt_single_buy failed")
        return RedirectResponse(
            url=f"{return_path}?gtt_err={quote(str(exc))[:200]}",
            status_code=303,
        )
    buy_trigger_id = gtt_resp.get("trigger_id") if isinstance(gtt_resp, dict) else None

    trade = Trade(
        user_id=user.id,
        instrument=symbol.upper(),
        side="B" if transaction_type == "BUY" else "S",
        entry_date=_date.today(),
        initial_entry_price=entry_price,
        initial_qty=qty,
        sl=stop_price,
        setup="Manual E1 (pending)" if auto_pilot_rank == "manual" else "Auto-Pilot E1 (pending)",
        status="open",
        kite_target_price=target_price,
        tsl_anchor="PDL",
        entry_status="pending",
        kite_buy_trigger_id=buy_trigger_id,
        # kite_trigger_id (the OCO) stays null — TSL daemon places it on fill
    )
    db.add(trade)
    db.commit()

    return RedirectResponse(
        url=(
            f"{return_path}?gtt_ok=1&symbol={symbol}"
            f"&pending=1"
            f"&buy_trigger_id={buy_trigger_id or 'unknown'}"
            f"&trade_id={trade.id}"
        ),
        status_code=303,
    )


@router.post("/gtt/{trigger_id}/cancel")
def cancel_gtt(
    request: Request,
    trigger_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(auth_mod.require_user),
):
    """Cancel a GTT trigger by ID. Refuses if not Kite-authed."""
    auth = kite_mod.auth_status(user)
    if not auth["authed"]:
        return RedirectResponse(url="/trading/kite", status_code=303)
    try:
        kite_audited.cancel_gtt(db, user, trigger_id)
    except Exception as exc:  # noqa: BLE001
        log.exception("cancel_gtt failed")
        from urllib.parse import quote
        return RedirectResponse(
            url=f"/trading/kite?gtt_err={quote(str(exc))[:200]}",
            status_code=303,
        )
    return RedirectResponse(url="/trading/kite?gtt_cancelled=1", status_code=303)


@router.post("/pending/{trade_id}/cancel")
def cancel_pending_entry(
    trade_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(auth_mod.require_user),
):
    """Cancel a pending GTT-single BUY trigger and remove the journal row.

    Only acts on Trades with entry_status='pending' AND a non-null
    kite_buy_trigger_id. The OCO bracket can't exist yet (we defer it
    until fill), so there's nothing else to clean up at the broker.
    """
    auth = kite_mod.auth_status(user)
    if not auth["authed"]:
        return RedirectResponse(url="/positions?cancel_err=not_authed", status_code=303)

    trade = db.get(Trade, trade_id)
    if trade is None or trade.user_id != user.id:
        raise HTTPException(404)
    if trade.entry_status != "pending" or trade.kite_buy_trigger_id is None:
        return RedirectResponse(
            url=f"/positions?cancel_err=not_pending&trade_id={trade_id}",
            status_code=303,
        )

    # Idempotent cancel — Kite returns an error if the trigger is already
    # gone, but we still want to drop the journal row.
    try:
        kite_audited.cancel_gtt(db, user, trade.kite_buy_trigger_id)
    except Exception:  # noqa: BLE001
        log.exception("cancel_pending_entry: cancel_gtt failed (continuing)")

    from ..models import Pyramid, Exit
    db.query(Pyramid).filter(Pyramid.trade_id == trade_id).delete(synchronize_session=False)
    db.query(Exit).filter(Exit.trade_id == trade_id).delete(synchronize_session=False)
    db.delete(trade)
    db.commit()

    return RedirectResponse(
        url=f"/positions?cancel_ok=1&trade_id={trade_id}", status_code=303,
    )


@router.post("/exit/{trade_id}")
def exit_at_market(
    trade_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(auth_mod.require_user),
    qty: int = Form(...),
):
    """Place a MARKET order at Kite to flatten ``qty`` of an open position
    and write the corresponding journal Exit row.

    Only acts on Kite-managed trades (``kite_trigger_id`` non-null) — manual
    journal trades have no broker leg to close. If this exits the trade
    fully, also cancels the residual GTT (the SL+target legs) so it
    doesn't fire on a phantom position later.
    """
    from .. import calculations as calc
    from ..models import Exit
    from datetime import date as _date

    auth = kite_mod.auth_status(user)
    if not auth["authed"]:
        return RedirectResponse(url="/positions?exit_err=not_authed", status_code=303)

    trade = db.get(Trade, trade_id)
    if trade is None or trade.user_id != user.id:
        raise HTTPException(404)
    if trade.kite_trigger_id is None:
        return RedirectResponse(
            url=f"/positions?exit_err=not_kite_managed&trade_id={trade_id}",
            status_code=303,
        )

    remaining = calc.open_qty(trade)
    if qty <= 0 or qty > remaining:
        return RedirectResponse(
            url=f"/positions?exit_err=bad_qty&want={qty}&open={remaining}",
            status_code=303,
        )

    sell_side = "SELL" if trade.side == "B" else "BUY"
    try:
        resp = kite_audited.place_order_market(
            db, user,
            symbol=trade.instrument, qty=qty,
            transaction_type=sell_side,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("exit_at_market place_order failed")
        from urllib.parse import quote
        return RedirectResponse(
            url=f"/positions?exit_err={quote(str(exc))[:200]}&trade_id={trade_id}",
            status_code=303,
        )

    order_id = resp.get("order_id") if isinstance(resp, dict) else None

    # Journal Exit row. We don't know the actual fill price yet — MARKET
    # orders fill near LTP but we can't ask Kite for `ltp()` (your app
    # lacks the Quote subscription). Use the trade's current cmp as a
    # reasonable placeholder; user can edit the row after the fact.
    fill_price = trade.cmp or trade.initial_entry_price
    seq = (max((e.sequence for e in trade.exits), default=0) + 1) if trade.exits else 1
    trade.exits.append(Exit(
        sequence=seq, price=fill_price, qty=qty, date=_date.today(),
    ))

    fully_exited = (calc.open_qty(trade) == 0)
    if fully_exited:
        # Cancel residual GTT — without this the SL/target legs fire later
        # on a position that no longer exists at the broker.
        try:
            kite_audited.cancel_gtt(db, user, trade.kite_trigger_id)
        except Exception:  # noqa: BLE001
            log.exception("exit_at_market: cancel_gtt failed (continuing)")
        trade.kite_trigger_id = None
        trade.status = "closed"
        trade.close_date = _date.today()

    db.commit()

    return RedirectResponse(
        url=(
            f"/positions?exit_ok=1&trade_id={trade_id}&qty={qty}"
            f"&order_id={order_id or 'unknown'}"
        ),
        status_code=303,
    )
