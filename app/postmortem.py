"""What-if analysis for a single trade.

Takes the trade's OHLC bars between entry and close (or today for open trades)
and computes:

- **MFE** (Maximum Favorable Excursion) — the best price reached while holding
  in the trade's direction. Tells you how much was theoretically available.
- **MAE** (Maximum Adverse Excursion) — the worst price reached. Answers
  "did the position ever come close to stopping out?"
- **P&L if I'd held to MFE**, **P&L if SL had hit at planned SL**, **P&L at 3R target**.
- **Left on table** = MFE-implied P&L − realised P&L. Only relevant for closed
  trades; for winners, it's "how much more was on offer"; for losers, it's
  "how much you gave back from the peak."

All numbers are R-multiples against **initial planned risk** (|initial entry −
SL| × initial qty), the same R anchor used in the Edge analytics.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from . import calculations as calc
from . import charges as charges_svc
from .models import Trade


@dataclass
class WhatIf:
    has_data: bool
    # Price extremes within the holding window, in the trade's favour/against.
    mfe_price: float | None
    mfe_date: str | None
    mae_price: float | None
    mae_date: str | None
    # R-multiples (sign matches favour direction)
    mfe_r: float | None
    mae_r: float | None
    realised_r: float | None
    # Rupee-denominated P&L for each scenario
    pnl_realised: float
    pnl_if_hit_sl: float | None        # if the trade had stopped out at planned SL
    pnl_if_held_mfe: float | None      # if you'd sold at MFE
    pnl_if_hit_3r: float | None        # if you'd held to a 3R target
    pnl_left_on_table: float | None    # MFE pnl − realised pnl (winners), or peak-to-close give-back
    mae_tested_sl: bool                # did price ever touch SL?


def _favour_sign(trade: Trade) -> int:
    return 1 if trade.side == "B" else -1


def _initial_risk_per_share(trade: Trade) -> float:
    return abs(trade.initial_entry_price - trade.sl)


def _r_of_price(trade: Trade, price: float) -> float | None:
    """Express a hypothetical exit at ``price`` in R-multiples of initial risk."""
    rps = _initial_risk_per_share(trade)
    if rps < 0.01:
        return None
    sign = _favour_sign(trade)
    return ((price - trade.initial_entry_price) * sign) / rps


def compute(trade: Trade, bars: list[dict[str, Any]]) -> WhatIf:
    entry = trade.initial_entry_price
    qty = trade.initial_qty
    rps = _initial_risk_per_share(trade)
    realised_pnl = calc.pnl_rs(trade) - charges_svc.charges_for(trade)

    # Window: entry_date … close_date (or today if still open). Filter bars.
    end = trade.close_date or date.today()
    in_window = [b for b in bars if trade.entry_date.isoformat() <= b["date"] <= end.isoformat()]

    if not in_window or rps < 0.01:
        return WhatIf(
            has_data=False,
            mfe_price=None, mfe_date=None,
            mae_price=None, mae_date=None,
            mfe_r=None, mae_r=None,
            realised_r=None,
            pnl_realised=realised_pnl,
            pnl_if_hit_sl=None,
            pnl_if_held_mfe=None,
            pnl_if_hit_3r=None,
            pnl_left_on_table=None,
            mae_tested_sl=False,
        )

    # For longs: favour = higher prices. MFE=max(High), MAE=min(Low).
    # For shorts: flipped.
    if trade.side == "B":
        mfe_bar = max(in_window, key=lambda b: b["high"])
        mae_bar = min(in_window, key=lambda b: b["low"])
        mfe_price = mfe_bar["high"]
        mae_price = mae_bar["low"]
        sl_tested = mae_price <= trade.sl
    else:
        mfe_bar = min(in_window, key=lambda b: b["low"])
        mae_bar = max(in_window, key=lambda b: b["high"])
        mfe_price = mfe_bar["low"]
        mae_price = mae_bar["high"]
        sl_tested = mae_price >= trade.sl

    sign = _favour_sign(trade)
    pnl_if_hit_sl = (trade.sl - entry) * sign * qty
    pnl_if_held_mfe = (mfe_price - entry) * sign * qty
    target_3r = entry + 3 * rps * sign
    pnl_if_hit_3r = (target_3r - entry) * sign * qty  # == 3 × rps × qty

    # "Left on table" — for a closed trade, how much of the MFE gain we missed.
    # If trade is open we report MFE − current (unrealised) P&L.
    if trade.status == "closed":
        left_on_table = pnl_if_held_mfe - realised_pnl
    else:
        left_on_table = pnl_if_held_mfe - realised_pnl

    return WhatIf(
        has_data=True,
        mfe_price=round(mfe_price, 2),
        mfe_date=mfe_bar["date"],
        mae_price=round(mae_price, 2),
        mae_date=mae_bar["date"],
        mfe_r=_r_of_price(trade, mfe_price),
        mae_r=_r_of_price(trade, mae_price),
        realised_r=(realised_pnl / (rps * qty)) if rps >= 0.01 and qty else None,
        pnl_realised=round(realised_pnl, 2),
        pnl_if_hit_sl=round(pnl_if_hit_sl, 2),
        pnl_if_held_mfe=round(pnl_if_held_mfe, 2),
        pnl_if_hit_3r=round(pnl_if_hit_3r, 2),
        pnl_left_on_table=round(left_on_table, 2),
        mae_tested_sl=sl_tested,
    )
