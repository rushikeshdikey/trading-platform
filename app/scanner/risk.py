"""Risk-sized position suggestion for a scanner candidate.

Reuses the journal's existing sizing math (``calculations.size_by_risk``) and
the live capital figure from the dashboard, so scan results are never sized
on a stale starting-capital number.

Every candidate is sized at **two risk tiers** so the user picks per-trade
based on conviction:
- ``conservative``: 0.25% of current capital — default for lower-conviction
  or choppy-market entries.
- ``standard``: 0.50% of current capital — upper bound for clean, textbook
  setups. This matches the ``default_risk_pct`` setting.

Hardcoded tiers (not settings) because the 0.25–0.5% range is an explicit
risk-management rule, not a per-run preference.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from .. import calculations as calc
from .. import dashboard as dash_svc
from .patterns import Candidate

RISK_PCT_CONSERVATIVE = 0.0025   # 0.25%
RISK_PCT_STANDARD = 0.005        # 0.50%


def size_candidate(db: Session, c: Candidate, capital: float | None = None) -> dict:
    """Return a dict of risk-sized fields at both tiers, keyed for the UI.

    Pass ``capital`` from the caller when sizing many candidates in a batch
    (e.g. /scanners) — computing it walks the entire trade history. Default
    None → fetch on demand (per-call O(N) trades, fine for one-off uses).

    Keys:
      capital                 — live capital (starting + realised P&L + events)
      risk_per_share          — entry - sl, shared across tiers
      qty_low / qty_high      — qty at 0.25% / 0.5% risk
      risk_rs_low / risk_rs_high — ₹ risk at each tier
      position_size_rs_high   — allocation at the 0.5% tier (for display)
      allocation_pct_high     — same as ratio of capital
    """
    if capital is None:
        capital = dash_svc.current_capital(db)
    low = calc.size_by_risk(capital, RISK_PCT_CONSERVATIVE, c.suggested_entry, c.suggested_sl)
    high = calc.size_by_risk(capital, RISK_PCT_STANDARD, c.suggested_entry, c.suggested_sl)
    return {
        "capital": round(capital, 2),
        "risk_per_share": round(high.risk_per_share, 2),
        "qty_low": low.qty,
        "qty_high": high.qty,
        "risk_rs_low": round(low.risk_rs, 2),
        "risk_rs_high": round(high.risk_rs, 2),
        "risk_pct_low": RISK_PCT_CONSERVATIVE,
        "risk_pct_high": RISK_PCT_STANDARD,
        "position_size_rs_high": round(high.position_size_rs, 2),
        "allocation_pct_high": round(high.allocation_pct, 4),
    }
