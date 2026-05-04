"""Auto-Pilot — the daily 1-3 trade picks.

Removes discretion from the daily decision. Reads the unified scanner
results, ranks by **composite score** (stock × confluence × RS × Minervini
× sector × regime), surfaces the top names above a quality threshold.

Surfaced as the FIRST panel on /cockpit. The panel is intentionally
prescriptive: "Buy N at X, SL Y, qty Z. Open at next session." Or,
when no qualifying composite exists today (or regime is hard-blocked):
"Stay in cash."

Aligned with the user's motto: "Let machine do the hard work."

Composite scoring spec lives in ``app.scanner.scoring``.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.orm import Session

from . import breadth as breadth_mod
from . import dashboard as dash_svc
from . import sector_rotation as sector_rotation_mod
from .scanner import entry_types as entry_types_mod
from .scanner import intraday_ltp as ltp_mod
from .scanner import risk as risk_svc
from .scanner import runner as scanner_runner
from .scanner import scoring as scoring_mod
from .scanner.patterns import SCAN_TYPES

log = logging.getLogger("journal.auto_pilot")

# Hard caps tuned to the user's psychology + capacity:
#   - Working professional → can act on max 5 trades a day
#   - Picks are ranked by composite score; trader can act on top N
MAX_PICKS = 5
TARGET_R_MULTIPLE = 3.0      # default profit target = 3R
DEFAULT_RISK_FLOOR_PCT = 0.0025   # 0.25% per trade if no Setting present
DEFAULT_RISK_CEIL_PCT = 0.005     # 0.5% per trade

# Minimum composite score to surface as a daily pick. A stock at exactly
# this threshold has e.g. (Minervini + RS 70 + Improving sector + base score
# in mid-band) — strong-enough single-scanner leader. Below this is too
# discretionary for the auto-pilot rule.
MIN_COMPOSITE_FOR_PICK = 65.0


@dataclass
class DailyPick:
    rank: int
    symbol: str
    setup_label: str           # "Minervini Stage 2 leader" / "3 scanners agree"
    confluence: list[str]      # human labels of scanners that fired
    rs_rating: int | None
    sector_quadrant: str | None
    composite_score: float
    entry: float
    sl: float
    sl_pct: float              # SL as % of entry (display)
    target: float              # entry + 3R
    qty: int
    risk_rs: float             # ₹ at risk if SL hits
    notional_rs: float         # qty * entry
    sl_method: str             # "3-bar low" / "2×ATR(7)" / "soft cap" — for trust
    score: float
    tier: str                  # "A+" / "A"
    # Phase A reality-check — populated when intraday LTP is available.
    today_open: float | None = None
    today_high: float | None = None
    today_low: float | None = None
    today_ltp: float | None = None
    today_status: str = "unknown"   # 'reachable' / 'chasing' / 'invalidated' / 'unknown'
    today_status_reason: str = ""   # human-readable note for the badge
    # Phase B entry-type recommender — replaces "buy at" with a typed trigger.
    entry_type: str = "PivotBreak"  # one of entry_types_mod constants
    entry_type_label: str = "Pivot Break"
    trigger_price: float = 0.0      # the actual BUY level (overrides scanner's suggested_entry)
    entry_rationale: str = ""


@dataclass
class AutoPilotState:
    picks: list[DailyPick] = field(default_factory=list)
    no_trade_reason: str = ""
    capital: float = 0.0
    risk_pct_used: float = 0.0
    total_risk_rs: float = 0.0
    cache_age_minutes: int | None = None
    market_verdict_level: str = ""    # piggybacks on existing market verdict
    cached_at: datetime | None = None
    # Hard-block context — populated when regime gate fires.
    regime_blocked: bool = False
    regime_label: str = ""
    regime_reason: str = ""
    regime_mood: float | None = None
    # Phase A reality-check — counts that summarise the LTP refresh layer.
    dropped_invalidated: int = 0      # picks whose SL was already breached today
    intraday_fetch_failed: int = 0    # picks where yfinance couldn't fetch

    @property
    def has_picks(self) -> bool:
        return len(self.picks) > 0


def _confluence_from_scans(scans: list[dict]) -> list[str]:
    return [s["label"] for s in sorted(scans, key=lambda s: s["score"], reverse=True)]


def build_daily_picks(db: Session) -> AutoPilotState:
    """Read the unified scanner cache, build the day's picks. Always
    returns a state — empty picks + no_trade_reason if nothing qualifies
    or the regime is hard-blocked.
    """
    state = AutoPilotState()
    state.capital = dash_svc.current_capital(db)

    # ----------------------------------------------------------------------
    # 1. Regime gate — checked BEFORE we even read scan results. When
    # markets are red, the answer is "cash" regardless of what fired.
    # ----------------------------------------------------------------------
    breadth_row = breadth_mod.latest(db, universe="all")
    mood = breadth_mod.mood_score(breadth_row) if breadth_row is not None else {"score": None}
    regime = scoring_mod.regime_multiplier_from_breadth(
        mood_score=mood.get("score") if mood else None,
        pct_above_50ema=float(breadth_row.pct_above_50ema) if breadth_row else None,
        pct_above_200ema=float(breadth_row.pct_above_200ema) if breadth_row else None,
    )
    state.regime_label = regime.label
    state.regime_mood = regime.mood_score

    if regime.is_blocked:
        state.regime_blocked = True
        state.regime_reason = regime.block_reason
        state.no_trade_reason = (
            f"Regime hard-block: {regime.block_reason}. "
            "Stay in cash. The market will be there when it's ready."
        )
        return state

    # ----------------------------------------------------------------------
    # 2. Read scan cache. Empty = scanner hasn't run yet.
    # ----------------------------------------------------------------------
    cached = scanner_runner.latest_cached_all(db, max_age_minutes=24 * 60)
    if cached is None:
        state.no_trade_reason = "Scanner cache empty — click Run all live on /scanners first."
        return state
    results, rows = cached

    if rows:
        oldest_run_at = min(r.run_at for r in rows.values())
        state.cached_at = oldest_run_at
        state.cache_age_minutes = int((datetime.utcnow() - oldest_run_at).total_seconds() / 60)

    # ----------------------------------------------------------------------
    # 3. Group by symbol. Mirrors routers/scanners._build_unified_results
    # so the cockpit shows the same rolled-up rows.
    # ----------------------------------------------------------------------
    grouped: dict[str, dict] = {}
    for scan_type, candidates in results.items():
        label = SCAN_TYPES[scan_type][0]
        for c in candidates:
            slot = grouped.setdefault(c.symbol, {
                "symbol": c.symbol,
                "scans": [],
                "primary": None,
                "max_score": 0.0,
            })
            slot["scans"].append({
                "type": scan_type, "label": label, "score": c.score,
                "extras": c.extras,
            })
            if c.score > slot["max_score"]:
                slot["max_score"] = c.score
                slot["primary"] = c

    # ----------------------------------------------------------------------
    # 4. Composite-score every grouped slot. Rank by composite desc.
    # ----------------------------------------------------------------------
    quadrant_map = sector_rotation_mod.symbol_quadrant_map(db)

    scored: list[tuple[float, dict, scoring_mod.CompositeBreakdown]] = []
    for slot in grouped.values():
        c = slot["primary"]
        rs_rating = (c.extras or {}).get("rs_rating") if c.extras else None
        breakdown = scoring_mod.composite_score(
            scans=slot["scans"],
            rs_rating=rs_rating,
            sector_quadrant=quadrant_map.get(slot["symbol"]),
            regime=regime,
        )
        scored.append((breakdown.composite, slot, breakdown))

    scored.sort(key=lambda x: x[0], reverse=True)

    qualifiers = [(c, slot, b) for c, slot, b in scored if c >= MIN_COMPOSITE_FOR_PICK]
    if not qualifiers:
        # Show what the top composite WAS so the user understands. Empty
        # but informative beats empty + opaque.
        if scored:
            top_c, top_slot, top_b = scored[0]
            state.no_trade_reason = (
                f"No setup cleared the {MIN_COMPOSITE_FOR_PICK:.0f} composite threshold today "
                f"(best was {top_slot['symbol']} at {top_c:.0f}, tier {top_b.tier}). "
                "Stay in cash."
            )
        else:
            state.no_trade_reason = "Scanner cache exists but no candidates surfaced."
        return state

    # Pull a small overflow buffer so SL-breached drops can be backfilled
    # from the next-best qualifiers — the user still sees MAX_PICKS rows
    # if enough candidates exist.
    OVERFLOW = 3
    qualifiers = qualifiers[: MAX_PICKS + OVERFLOW]

    # ----------------------------------------------------------------------
    # 5. Position-size each qualifier. Floor risk tier for auto-pilot.
    # ----------------------------------------------------------------------
    risk_low, _risk_high = risk_svc.get_user_risk_tiers(db)
    risk_pct_used = risk_low or DEFAULT_RISK_FLOOR_PCT
    state.risk_pct_used = risk_pct_used

    # Phase A reality-check — fetch today's OHLC for each qualifier so we
    # can drop SL-breached candidates and tag chasing ones BEFORE assigning
    # ranks. Failed fetches fall through with today_status='unknown'.
    qualifier_symbols = [slot["symbol"] for _, slot, _ in qualifiers]
    today_ohlc_by_sym = ltp_mod.fetch_many(qualifier_symbols)

    # Phase B — fetch ascending daily closes per qualifier (last 30 bars
    # is enough for 10/20-EMA); used by the entry-type recommender for
    # Pullback triggers and prev-bar reference.
    from .models import DailyBar as _DailyBar
    bars_by_sym: dict[str, list[_DailyBar]] = {}
    if qualifier_symbols:
        rows = (
            db.query(_DailyBar)
            .filter(_DailyBar.symbol.in_(qualifier_symbols))
            .order_by(_DailyBar.symbol, _DailyBar.date.asc())
            .all()
        )
        for r in rows:
            bars_by_sym.setdefault(r.symbol.upper(), []).append(r)

    rank = 0
    for composite, slot, breakdown in qualifiers:
        if rank >= MAX_PICKS:
            break  # buffer was for SL-drop backfill; not for showing >5
        c = slot["primary"]
        scanner_entry = float(c.suggested_entry or c.close)
        sl = float(c.suggested_sl)
        if scanner_entry <= 0 or sl <= 0 or sl >= scanner_entry:
            continue

        # Reality-check against today's intraday OHLC.
        ohlc = today_ohlc_by_sym.get(slot["symbol"])
        today_status = "unknown"
        today_status_reason = "intraday LTP unavailable — using prior close"
        if ohlc is None:
            state.intraday_fetch_failed += 1
        else:
            # SL breached intraday → setup is dead. Drop entirely.
            if ohlc.low <= sl:
                state.dropped_invalidated += 1
                log.info(
                    "auto_pilot drop %s: SL breached intraday (low %.2f ≤ SL %.2f)",
                    slot["symbol"], ohlc.low, sl,
                )
                continue

        # Phase B — entry-type recommender. Replaces scanner's suggested_entry
        # with a typed trigger appropriate to the setup (PDH for institutional
        # buying, Pullback for Minervini, Pivot Break for HR/base-on-base, etc).
        symbol_bars = bars_by_sym.get(slot["symbol"].upper(), [])
        prev_bar = symbol_bars[-1] if symbol_bars else None
        prev_high = float(prev_bar.high) if prev_bar else scanner_entry
        prev_low = float(prev_bar.low) if prev_bar else sl
        daily_closes = [float(b.close) for b in symbol_bars]

        rec = entry_types_mod.recommend_entry_for_pick(
            scan_types_fired=[s["type"] for s in slot["scans"]],
            primary_scan_type=c.scan_type,
            candidate_extras=c.extras or {},
            daily_closes=daily_closes,
            prev_high=prev_high,
            prev_low=prev_low,
            today_open=ohlc.open if ohlc else None,
            today_high=ohlc.high if ohlc else None,
            today_low=ohlc.low if ohlc else None,
            today_ltp=ohlc.ltp if ohlc else None,
            fallback_entry=scanner_entry,
        )
        # Trigger price is the AUTHORITATIVE entry (decision #4 from spec).
        entry = rec.trigger_price

        # Re-tag reachable/chasing using the new trigger price (the gap
        # math depends on the actual buy level, not the scanner's stale
        # close suggestion).
        if ohlc is not None:
            if ohlc.low > entry:
                today_status = "chasing"
                gap = (ohlc.ltp - entry) / entry * 100
                today_status_reason = (
                    f"LTP ₹{ohlc.ltp:.2f} is {gap:+.1f}% above {rec.entry_type} "
                    f"trigger ₹{entry:.2f} — chasing. Wait for pullback or skip."
                )
            else:
                today_status = "reachable"
                gap = (ohlc.ltp - entry) / entry * 100
                today_status_reason = (
                    f"plan reachable · LTP ₹{ohlc.ltp:.2f} ({gap:+.1f}% vs trigger)"
                )

        if entry <= sl:
            # Anticipation/Pullback can produce a trigger BELOW the SL on
            # already-broken setups. Skip cleanly.
            continue

        sl_distance = entry - sl
        sl_pct = sl_distance / entry
        risk_rs = state.capital * risk_pct_used
        qty = int(risk_rs // sl_distance) if sl_distance > 0 else 0
        if qty <= 0:
            continue
        notional = qty * entry
        target = round(entry + TARGET_R_MULTIPLE * sl_distance, 2)
        state.total_risk_rs += qty * sl_distance

        rank += 1
        state.picks.append(DailyPick(
            rank=rank,
            symbol=slot["symbol"],
            setup_label=breakdown.reason,
            confluence=_confluence_from_scans(slot["scans"]),
            rs_rating=c.extras.get("rs_rating") if c.extras else None,
            sector_quadrant=quadrant_map.get(slot["symbol"]),
            composite_score=round(composite, 1),
            entry=round(entry, 2),
            sl=round(sl, 2),
            sl_pct=round(sl_pct * 100, 2),
            target=target,
            qty=qty,
            risk_rs=round(qty * sl_distance, 2),
            notional_rs=round(notional, 2),
            sl_method=c.extras.get("sl_method", "—") if c.extras else "—",
            score=c.score,
            tier=breakdown.tier,
            today_open=ohlc.open if ohlc else None,
            today_high=ohlc.high if ohlc else None,
            today_low=ohlc.low if ohlc else None,
            today_ltp=ohlc.ltp if ohlc else None,
            today_status=today_status,
            today_status_reason=today_status_reason,
            entry_type=rec.entry_type,
            entry_type_label=entry_types_mod.ENTRY_TYPE_LABELS.get(rec.entry_type, rec.entry_type),
            trigger_price=rec.trigger_price,
            entry_rationale=rec.rationale,
        ))

    if not state.picks:
        state.no_trade_reason = "All composite-qualifying candidates failed sizing (SL too wide or qty 0)."
    return state
