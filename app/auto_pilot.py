"""Auto-Pilot — the daily 1-3 trade picks.

Removes discretion from the daily decision. Reads the unified scanner
results, filters to the highest-conviction tier available, caps to
``MAX_PICKS`` names, and pre-computes everything needed to place the
trade (entry, SL, qty, R:R, total risk).

Surfaced as the FIRST panel on /cockpit. The panel is intentionally
prescriptive: "Buy N at X, SL Y, qty Z. Open at next session." Or,
when no qualifying confluence exists today: "Stay in cash."

Aligned with the user's motto: "Let machine do the hard work."
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.orm import Session

from . import dashboard as dash_svc
from .scanner import risk as risk_svc
from .scanner import runner as scanner_runner
from .scanner.patterns import SCAN_TYPES

log = logging.getLogger("journal.auto_pilot")

# Hard caps tuned to the user's psychology + capacity:
#   - Working professional → can act on max 3 trades a day
#   - Won't take any trade unless A+ (3+ scanners) or Minervini-Stage-2-leader
MAX_PICKS = 3
TARGET_R_MULTIPLE = 3.0      # default profit target = 3R
DEFAULT_RISK_FLOOR_PCT = 0.0025   # 0.25% per trade if no Setting present
DEFAULT_RISK_CEIL_PCT = 0.005     # 0.5% per trade


@dataclass
class DailyPick:
    rank: int
    symbol: str
    setup_label: str           # "Minervini Stage 2 leader" / "3 scanners agree"
    confluence: list[str]      # human labels of scanners that fired
    rs_rating: int | None
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

    @property
    def has_picks(self) -> bool:
        return len(self.picks) > 0


def _is_qualifying_tier(tier: str) -> bool:
    """A+ always qualifies. A qualifies only when it's a Minervini hit
    or a multi-scanner confluence — single-scanner score-based A's are
    too discretionary for the auto-pilot rule."""
    return tier == "A+"


def _confluence_from_scans(scans: list[dict]) -> list[str]:
    return [s["label"] for s in sorted(scans, key=lambda s: s["score"], reverse=True)]


def build_daily_picks(db: Session) -> AutoPilotState:
    """Read the unified scanner cache, build the day's picks. Always
    returns a state — empty picks + no_trade_reason if nothing qualifies.
    """
    state = AutoPilotState()
    state.capital = dash_svc.current_capital(db)

    # Reuse the runner's all-scans cache so we don't recompute. Returns
    # None if not all scanners have a fresh entry — auto-pilot stays empty.
    cached = scanner_runner.latest_cached_all(db, max_age_minutes=24 * 60)
    if cached is None:
        state.no_trade_reason = "Scanner cache empty — click Run all live on /scanners first."
        return state
    results, rows = cached

    if rows:
        oldest_run_at = min(r.run_at for r in rows.values())
        state.cached_at = oldest_run_at
        state.cache_age_minutes = int((datetime.utcnow() - oldest_run_at).total_seconds() / 60)

    # Group by symbol to compute scan_count + collect scans (mirrors the
    # logic in routers/scanners._build_unified_results, kept independent
    # because cockpit shouldn't import from a router).
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

    # Tier each candidate (replicates routers/scanners._compute_tier
    # without importing from a router module — minimal duplication, but
    # the rule is canonical here too).
    qualifiers: list[dict] = []
    for slot in grouped.values():
        scans = slot["scans"]
        if len(scans) >= 3:
            slot["tier"] = "A+"
            slot["tier_reason"] = f"{len(scans)} scanners agree"
            qualifiers.append(slot)

    if not qualifiers:
        state.no_trade_reason = (
            "No setup met A+ confluence today (3+ scanners agreeing). "
            "Stay in cash. Markets will be there tomorrow."
        )
        return state

    # Rank by (scan_count desc, primary score desc) and cap.
    qualifiers.sort(
        key=lambda s: (len(s["scans"]), s["primary"].score),
        reverse=True,
    )
    qualifiers = qualifiers[:MAX_PICKS]

    # Position-size each pick. We use the user's lower risk tier (0.25%)
    # by default — auto-pilot is for the WORKING-PROFESSIONAL conservative
    # mode, not maximum-risk mode.
    risk_low, _risk_high = risk_svc.get_user_risk_tiers(db)
    risk_pct_used = risk_low or DEFAULT_RISK_FLOOR_PCT
    state.risk_pct_used = risk_pct_used

    for i, slot in enumerate(qualifiers, start=1):
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
            setup_label=slot.get("tier_reason", ""),
            confluence=_confluence_from_scans(slot["scans"]),
            rs_rating=c.extras.get("rs_rating"),
            entry=round(entry, 2),
            sl=round(sl, 2),
            sl_pct=round(sl_pct * 100, 2),
            target=target,
            qty=qty,
            risk_rs=round(qty * sl_distance, 2),
            notional_rs=round(notional, 2),
            sl_method=c.extras.get("sl_method", "—"),
            score=c.score,
            tier="A+",
        ))

    if not state.picks:
        state.no_trade_reason = "All A+ candidates failed sizing (SL too wide or qty 0)."
    return state
