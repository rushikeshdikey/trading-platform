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

    qualifiers = qualifiers[:MAX_PICKS]

    # ----------------------------------------------------------------------
    # 5. Position-size each qualifier. Floor risk tier for auto-pilot.
    # ----------------------------------------------------------------------
    risk_low, _risk_high = risk_svc.get_user_risk_tiers(db)
    risk_pct_used = risk_low or DEFAULT_RISK_FLOOR_PCT
    state.risk_pct_used = risk_pct_used

    for i, (composite, slot, breakdown) in enumerate(qualifiers, start=1):
        c = slot["primary"]
        entry = float(c.suggested_entry or c.close)
        sl = float(c.suggested_sl)
        if entry <= 0 or sl <= 0 or sl >= entry:
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

        state.picks.append(DailyPick(
            rank=i,
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
        ))

    if not state.picks:
        state.no_trade_reason = "All composite-qualifying candidates failed sizing (SL too wide or qty 0)."
    return state
