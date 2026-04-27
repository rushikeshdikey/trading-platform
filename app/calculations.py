"""Derived metrics for a trade and for position sizing.

All functions are pure and take a `Trade` ORM object (with its pyramids/exits
eagerly loaded) or primitive inputs. They return None when a value cannot be
computed (e.g. no exits yet, no CMP set).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Iterable

from .models import Trade


def _sum_qty(items: Iterable) -> int:
    return sum(i.qty for i in items)


def _weighted_avg(items: Iterable) -> float:
    total_qty = _sum_qty(items)
    if total_qty == 0:
        return 0.0
    total_cost = sum(i.price * i.qty for i in items)
    return total_cost / total_qty


def total_qty(trade: Trade) -> int:
    return trade.initial_qty + sum(p.qty for p in trade.pyramids)


def avg_entry(trade: Trade) -> float:
    tq = total_qty(trade)
    if tq == 0:
        return 0.0
    cost = trade.initial_entry_price * trade.initial_qty + sum(
        p.price * p.qty for p in trade.pyramids
    )
    return cost / tq


def exited_qty(trade: Trade) -> int:
    return _sum_qty(trade.exits)


def open_qty(trade: Trade) -> int:
    return total_qty(trade) - exited_qty(trade)


def realised_amount(trade: Trade) -> float:
    return sum(e.price * e.qty for e in trade.exits)


def avg_exit(trade: Trade) -> float | None:
    eq = exited_qty(trade)
    if eq == 0:
        return None
    return realised_amount(trade) / eq


def pnl_rs(trade: Trade) -> float:
    ae = avg_exit(trade)
    if ae is None:
        return 0.0
    eq = exited_qty(trade)
    entry = avg_entry(trade)
    if trade.side == "B":
        return (ae - entry) * eq
    return (entry - ae) * eq


def stock_move_pct(trade: Trade) -> float | None:
    """Move from avg entry to avg exit, or CMP if still open."""
    ref = avg_exit(trade)
    if ref is None:
        ref = trade.cmp
    if ref is None:
        return None
    entry = avg_entry(trade)
    if entry == 0:
        return None
    move = (ref - entry) / entry
    return move if trade.side == "B" else -move


def reward_risk(trade: Trade) -> float | None:
    ref = avg_exit(trade)
    if ref is None:
        ref = trade.cmp
    if ref is None:
        return None
    entry = avg_entry(trade)
    risk_per_share = abs(entry - trade.sl)
    # Tick size is ₹0.01 — anything below a paisa is float noise (e.g. SL
    # equal to entry) and would produce nonsense R:R in the millions.
    if risk_per_share < 0.01:
        return None
    if trade.side == "B":
        return (ref - entry) / risk_per_share
    return (entry - ref) / risk_per_share


def holding_days(trade: Trade) -> int:
    end = trade.close_date or date.today()
    return (end - trade.entry_date).days


def position_size_rs(trade: Trade) -> float:
    """Total capital ever deployed in the trade — uses ``total_qty`` so pyramids
    count. Used on the trade-detail card. Open exposure (capital still at risk
    in open qty) is `open_exposure_rs` below."""
    return avg_entry(trade) * total_qty(trade)


def open_exposure_rs(trade: Trade) -> float:
    """Capital currently tied up in the open portion of the position.

    Different from ``position_size_rs``: once you partially exit, that share of
    capital is back in your pocket and shouldn't count as exposure anymore.
    """
    return avg_entry(trade) * open_qty(trade)


def sl_pct(trade: Trade) -> float | None:
    entry = avg_entry(trade)
    if entry == 0:
        return None
    return abs(entry - trade.sl) / entry


def open_heat_rs(trade: Trade) -> float:
    """Rupees at risk if SL hits on the still-open qty. Zero once fully exited."""
    oq = open_qty(trade)
    if oq == 0:
        return 0.0
    entry = avg_entry(trade)
    if trade.side == "B":
        return max(0.0, (entry - trade.sl) * oq)
    return max(0.0, (trade.sl - entry) * oq)


@dataclass
class TradeMetrics:
    total_qty: int
    avg_entry: float
    exited_qty: int
    open_qty: int
    avg_exit: float | None
    realised_amount: float
    pnl_rs: float
    stock_move_pct: float | None
    reward_risk: float | None
    holding_days: int
    position_size_rs: float
    sl_pct: float | None
    open_heat_rs: float


def metrics(trade: Trade) -> TradeMetrics:
    # The pure-function helpers above each independently walk pyramids/exits;
    # naive calls would walk them ~20× per trade. Compute primitives once and
    # reuse — same results, one pass.
    pyramids = trade.pyramids
    exits = trade.exits
    init_qty = trade.initial_qty

    pyr_qty = sum(p.qty for p in pyramids)
    tq = init_qty + pyr_qty
    if tq:
        ae = (
            trade.initial_entry_price * init_qty
            + sum(p.price * p.qty for p in pyramids)
        ) / tq
    else:
        ae = 0.0

    eq = sum(e.qty for e in exits)
    oq = tq - eq
    real_amt = sum(e.price * e.qty for e in exits)
    avg_ex = (real_amt / eq) if eq else None

    is_buy = trade.side == "B"
    if avg_ex is not None:
        pnl = (avg_ex - ae) * eq if is_buy else (ae - avg_ex) * eq
    else:
        pnl = 0.0

    ref = avg_ex if avg_ex is not None else trade.cmp
    if ref is not None and ae != 0:
        raw_move = (ref - ae) / ae
        smp = raw_move if is_buy else -raw_move
    else:
        smp = None

    risk_per_share = abs(ae - trade.sl)
    if ref is not None and risk_per_share >= 0.01:
        rr = ((ref - ae) / risk_per_share) if is_buy else ((ae - ref) / risk_per_share)
    else:
        rr = None

    end = trade.close_date or date.today()
    hd = (end - trade.entry_date).days

    if oq == 0:
        oh = 0.0
    elif is_buy:
        oh = max(0.0, (ae - trade.sl) * oq)
    else:
        oh = max(0.0, (trade.sl - ae) * oq)

    slp = (abs(ae - trade.sl) / ae) if ae else None

    return TradeMetrics(
        total_qty=tq,
        avg_entry=ae,
        exited_qty=eq,
        open_qty=oq,
        avg_exit=avg_ex,
        realised_amount=real_amt,
        pnl_rs=pnl,
        stock_move_pct=smp,
        reward_risk=rr,
        holding_days=hd,
        position_size_rs=ae * tq,
        sl_pct=slp,
        open_heat_rs=oh,
    )


# -- Position sizing calculators -------------------------------------------


@dataclass
class SizeByRisk:
    qty: int
    risk_rs: float
    risk_per_share: float
    position_size_rs: float
    allocation_pct: float


def size_by_risk(capital: float, risk_pct: float, entry: float, sl: float) -> SizeByRisk:
    risk_rs = capital * risk_pct
    risk_per_share = abs(entry - sl)
    if risk_per_share == 0 or entry == 0:
        return SizeByRisk(0, risk_rs, risk_per_share, 0.0, 0.0)
    qty = int(risk_rs // risk_per_share)
    pos = qty * entry
    return SizeByRisk(
        qty=qty,
        risk_rs=risk_rs,
        risk_per_share=risk_per_share,
        position_size_rs=pos,
        allocation_pct=pos / capital if capital else 0.0,
    )


@dataclass
class SizeByAllocation:
    qty: int
    allocated_rs: float
    position_size_rs: float
    sl_pct: float
    risk_rs: float


def size_by_allocation(
    capital: float, allocation_pct: float, entry: float, sl: float
) -> SizeByAllocation:
    allocated = capital * allocation_pct
    if entry == 0:
        return SizeByAllocation(0, allocated, 0.0, 0.0, 0.0)
    qty = int(allocated // entry)
    pos = qty * entry
    slp = abs(entry - sl) / entry if entry else 0.0
    return SizeByAllocation(
        qty=qty,
        allocated_rs=allocated,
        position_size_rs=pos,
        sl_pct=slp,
        risk_rs=qty * abs(entry - sl),
    )
