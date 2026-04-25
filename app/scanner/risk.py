"""Risk-sized position suggestion for a scanner candidate.

Both tiers are now user-configurable via /settings:
  - ``risk_pct_low``        — conservative tier (default 0.25%)
  - ``default_risk_pct``    — standard tier     (default 0.5%)

The scanner UI shows BOTH so the user picks per-row based on conviction.
Cockpit signals also call ``size_candidate`` and inherit these values.

When sizing many candidates in one render (scanner page), the caller
should pre-fetch ``capital``, ``risk_low``, ``risk_high`` once and pass
them in — otherwise we'd hit the DB N times for the same three numbers.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from .. import calculations as calc
from .. import dashboard as dash_svc
from .. import settings as app_settings
from .patterns import Candidate

# Hardcoded fallbacks if the settings table doesn't yet have the keys —
# for the very first request after a fresh DB. settings.DEFAULTS already
# contains these but the Setting row isn't created until the user saves.
_FALLBACK_LOW = 0.0025
_FALLBACK_HIGH = 0.005


def get_user_risk_tiers(db: Session) -> tuple[float, float]:
    """Return (risk_pct_low, risk_pct_high) — the conservative and standard
    tiers from the user's settings. Reads via the settings module which
    falls back to ``DEFAULTS`` when the row hasn't been written yet."""
    low = app_settings.get_float(db, "risk_pct_low", _FALLBACK_LOW)
    high = app_settings.get_float(db, "default_risk_pct", _FALLBACK_HIGH)
    return low, high


def size_candidate(
    db: Session,
    c: Candidate,
    capital: float | None = None,
    risk_low: float | None = None,
    risk_high: float | None = None,
) -> dict:
    """Return a dict of risk-sized fields at both tiers.

    ``capital``, ``risk_low``, ``risk_high`` should be passed in by callers
    sizing many candidates in a batch (computing each is non-trivial).
    Defaults pull live values from the DB — fine for one-off uses.
    """
    if capital is None:
        capital = dash_svc.current_capital(db)
    if risk_low is None or risk_high is None:
        rl, rh = get_user_risk_tiers(db)
        risk_low = rl if risk_low is None else risk_low
        risk_high = rh if risk_high is None else risk_high

    low = calc.size_by_risk(capital, risk_low, c.suggested_entry, c.suggested_sl)
    high = calc.size_by_risk(capital, risk_high, c.suggested_entry, c.suggested_sl)
    return {
        "capital": round(capital, 2),
        "risk_per_share": round(high.risk_per_share, 2),
        "qty_low": low.qty,
        "qty_high": high.qty,
        "risk_rs_low": round(low.risk_rs, 2),
        "risk_rs_high": round(high.risk_rs, 2),
        "risk_pct_low": risk_low,
        "risk_pct_high": risk_high,
        "position_size_rs_high": round(high.position_size_rs, 2),
        "allocation_pct_high": round(high.allocation_pct, 4),
    }
