"""Indian equity trading cost estimator — intraday vs delivery.

Zerodha (and every other Indian broker) charges very different rates for
intraday square-off (MIS — bought & sold same trading day) vs delivery
(CNC — held overnight or longer). The biggest wedge is STT: 0.025% on the
sell side only for intraday vs 0.1% on *both* sides for delivery. On a
round-trip that's ~8× more STT for delivery, which on small edges (like
the 100 qty × ₹4.2 move example) is the difference between ₹80 and ₹200
total charges.

Heuristic: a trade is treated as **intraday** when it is fully closed and
every leg (entry + pyramids + exits) shares the same calendar date. Any
overnight carry flips it to **delivery**. Open trades default to delivery
for a conservative estimate — they'll be recomputed once closed.

Rates current as of Apr 2026. They move with Budget revisions / SEBI
circulars, so all the percentages live as constants at the top of this
file — change once, applies everywhere.
"""
from __future__ import annotations

from . import calculations as calc
from .models import Trade


# ---------------------------------------------------------------------------
# Shared rates (apply to both intraday and delivery)
# ---------------------------------------------------------------------------
EXCHANGE_FEE_NSE_PCT = 0.0000297   # 0.00297% of turnover
EXCHANGE_FEE_BSE_PCT = 0.0000375   # 0.00375%  (BSE slightly higher)
SEBI_FEE_PCT = 0.000001            # ₹10 per crore (10 / 1,00,00,000)
GST_PCT = 0.18                     # 18% on brokerage + exchange + SEBI

# ---------------------------------------------------------------------------
# Delivery (CNC)
# ---------------------------------------------------------------------------
DEL_STT_BUY_PCT = 0.0010           # 0.10% on buy value
DEL_STT_SELL_PCT = 0.0010          # 0.10% on sell value
DEL_STAMP_BUY_PCT = 0.00015        # 0.015% on buy only
DEL_BROKERAGE_RS_PER_ORDER = 0.0   # Zerodha delivery = ₹0
DP_FEE_PER_SELL_RS = 13.5          # Zerodha's DP charge (per sell per scrip)
DP_GST_PCT = 0.18

# ---------------------------------------------------------------------------
# Intraday (MIS)
# ---------------------------------------------------------------------------
INTRA_STT_SELL_PCT = 0.00025       # 0.025% on sell side only (no buy-side STT)
INTRA_STAMP_BUY_PCT = 0.00003      # 0.003% on buy side only (much lower)
INTRA_BROKERAGE_PCT = 0.0003       # 0.03% of leg value ...
INTRA_BROKERAGE_MAX_RS = 20.0      # ... capped at ₹20 per order


def _is_bse(trade: Trade) -> bool:
    """BSE scrip codes are numeric. Everything else → NSE for fee rates."""
    sym = (trade.instrument or "").strip()
    return sym.isdigit()


def _buy_value(trade: Trade) -> float:
    """Total rupees on the opening + pyramid legs."""
    base = (trade.initial_entry_price or 0) * (trade.initial_qty or 0)
    for p in trade.pyramids:
        base += (p.price or 0) * (p.qty or 0)
    return base


def _sell_value(trade: Trade) -> float:
    return sum((e.price or 0) * (e.qty or 0) for e in trade.exits)


def is_intraday(trade: Trade) -> bool:
    """True when every leg fires on the same calendar date and trade is closed.

    Partial / still-open trades never qualify — we can't know if the rest
    will exit today, so we default to the more expensive delivery estimate.
    """
    if trade.status != "closed":
        return False
    if trade.entry_date is None:
        return False
    day = trade.entry_date
    if trade.close_date is not None and trade.close_date != day:
        return False
    for p in trade.pyramids:
        if p.date is not None and p.date != day:
            return False
    for e in trade.exits:
        if e.date is not None and e.date != day:
            return False
    # Must have had actual exits to be called intraday (otherwise it's open).
    return bool(trade.exits)


def _intraday_buy_legs(trade: Trade) -> list[float]:
    """Per-order buy leg values (initial + each pyramid)."""
    legs = []
    base = (trade.initial_entry_price or 0) * (trade.initial_qty or 0)
    if base:
        legs.append(base)
    for p in trade.pyramids:
        v = (p.price or 0) * (p.qty or 0)
        if v:
            legs.append(v)
    return legs


def _intraday_sell_legs(trade: Trade) -> list[float]:
    """Per-order sell leg values (one per exit row)."""
    return [(e.price or 0) * (e.qty or 0) for e in trade.exits if (e.price or 0) * (e.qty or 0) > 0]


def _intraday_brokerage(leg_values: list[float]) -> float:
    """Zerodha MIS: ``min(0.03% × leg, ₹20)`` per executed order, summed."""
    return sum(min(v * INTRA_BROKERAGE_PCT, INTRA_BROKERAGE_MAX_RS) for v in leg_values)


def breakdown(trade: Trade) -> dict:
    """Full Zerodha-style breakdown. Branches on intraday vs delivery.

    Returns the same shape regardless of mode so the UI can render one
    template — ``mode`` is ``"intraday"`` or ``"delivery"``.
    """
    if trade.side == "S":
        sell_val = _buy_value(trade)
        buy_val = _sell_value(trade)
        buy_legs = _intraday_sell_legs(trade)
        sell_legs = _intraday_buy_legs(trade)
    else:
        buy_val = _buy_value(trade)
        sell_val = _sell_value(trade)
        buy_legs = _intraday_buy_legs(trade)
        sell_legs = _intraday_sell_legs(trade)
    turnover = buy_val + sell_val
    ex_pct = EXCHANGE_FEE_BSE_PCT if _is_bse(trade) else EXCHANGE_FEE_NSE_PCT
    exchange = turnover * ex_pct
    sebi = turnover * SEBI_FEE_PCT

    intraday = is_intraday(trade)
    if intraday:
        brokerage = _intraday_brokerage(buy_legs) + _intraday_brokerage(sell_legs)
        stt = sell_val * INTRA_STT_SELL_PCT
        stamp = buy_val * INTRA_STAMP_BUY_PCT
        dp = 0.0
    else:
        brokerage = DEL_BROKERAGE_RS_PER_ORDER
        stt = buy_val * DEL_STT_BUY_PCT + sell_val * DEL_STT_SELL_PCT
        stamp = buy_val * DEL_STAMP_BUY_PCT
        # DP charged once per sell of a delivery scrip (long trades only).
        dp = DP_FEE_PER_SELL_RS * (1 + DP_GST_PCT) if (trade.exits and trade.side == "B") else 0.0

    gst = (brokerage + exchange + sebi) * GST_PCT
    total = brokerage + stt + stamp + exchange + sebi + gst + dp
    return {
        "mode": "intraday" if intraday else "delivery",
        "buy_value": round(buy_val, 2),
        "sell_value": round(sell_val, 2),
        "turnover": round(turnover, 2),
        "brokerage": round(brokerage, 2),
        "stt": round(stt, 2),
        "exchange": round(exchange, 2),
        "sebi": round(sebi, 2),
        "stamp": round(stamp, 2),
        "gst": round(gst, 2),
        "dp": round(dp, 2),
        "total": round(total, 2),
    }


def estimate_charges(trade: Trade) -> float:
    """Aggregate ₹ charge estimate. Safe to call on open trades (returns the
    conservative delivery figure until the trade is closed)."""
    return breakdown(trade)["total"]


def charges_for(trade: Trade) -> float:
    """Authoritative number to subtract from gross P&L.

    Uses the user-set ``charges_rs`` when present, otherwise estimates.
    A value of exactly 0 is respected (user explicitly said zero).
    """
    if trade.charges_rs is not None:
        return float(trade.charges_rs)
    return estimate_charges(trade)


def net_pnl(trade: Trade) -> float:
    """Gross realised P&L minus charges."""
    return calc.pnl_rs(trade) - charges_for(trade)


def backfill_estimates(trades) -> int:
    """Populate ``charges_rs`` on trades that don't have it set."""
    n = 0
    for t in trades:
        if t.charges_rs is None:
            t.charges_rs = estimate_charges(t)
            n += 1
    return n
