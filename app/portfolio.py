"""Live portfolio summary & per-position cards for the dashboard.

Concepts:
  - **Invested** = avg_entry × open_qty, summed across open trades. What's
    currently deployed.
  - **Total P&L** = unrealised P&L on open positions at CMP.
  - **Day's P&L** = (CMP − prev_close) × open_qty × side_sign, summed. Needs
    prev_close populated (done by the price refresher).
  - **Open risk** = sum of (CMP − effective_stop) × open_qty × sign, clamped
    to non-negative. Effective stop = the more favorable of SL / TSL.
  - **Locked profit** = sum of (effective_stop − entry) × open_qty × sign,
    across trades whose stop has trailed past breakeven. Money you keep even
    if every position stops right now.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from sqlalchemy.orm import Session

from . import calculations as calc
from . import dashboard as dash
from .models import InstrumentPrice, Trade


@dataclass
class PositionCard:
    trade_id: int
    instrument: str
    setup: str | None
    side: str              # 'B' or 'S'
    holding_days: int
    avg_entry: float
    cmp: float | None
    effective_stop: float  # better of SL/TSL
    sl: float
    tsl: float | None
    open_qty: int
    total_qty: int
    # Unrealised move / P&L
    move_pct: float | None     # (CMP - entry) / entry × sign
    pnl_rs: float | None       # unrealised ₹
    r_multiple: float | None   # unrealised R from initial risk
    allocation_pct: float      # invested / capital
    invested_rs: float         # avg_entry × open_qty
    risk_rs: float             # ₹ at risk from CMP to effective_stop (≥ 0)
    locked_profit_rs: float    # guaranteed ₹ if effective_stop hits (≥ 0, else 0)
    status_tag: str            # "Safe" / "Break-even" / "At risk" / "No CMP"


@dataclass
class Summary:
    positions: int
    invested_rs: float
    total_pnl_rs: float
    total_pnl_pct: float | None      # vs invested
    day_pnl_rs: float | None
    day_pnl_pct: float | None
    open_risk_rs: float
    open_risk_pct_of_capital: float | None
    locked_profit_rs: float
    capital: float                   # book value: starting + capital events + REALISED P&L
    cards: list[PositionCard] = field(default_factory=list)


def _effective_stop(t: Trade) -> float:
    if t.tsl is None:
        return t.sl
    if t.side == "B":
        return max(t.sl, t.tsl)
    return min(t.sl, t.tsl)


def _prev_close(db: Session, t: Trade) -> float | None:
    from . import prices
    row = db.get(InstrumentPrice, prices._clean_symbol(t.instrument))
    return row.prev_close if row and row.prev_close else None


def _status_tag(t: Trade, cmp: float | None, stop: float) -> str:
    if cmp is None:
        return "No CMP"
    entry = t.initial_entry_price
    sign = 1 if t.side == "B" else -1
    # Price cushion = how far CMP is above stop (for long) as fraction of price.
    cushion = (cmp - stop) * sign / cmp if cmp else 0
    # Is the stop already past entry (i.e. profit locked)?
    locked = (stop - entry) * sign > 0
    if locked:
        # Trailing stop has moved into profit territory.
        if cushion > 0.05:
            return "Safe"
        return "Marginal"
    # Stop still below/at entry — pure risk.
    if cushion <= 0:
        return "At risk"
    if cushion < 0.02:
        return "Tight"
    return "Watch"


def build(db: Session) -> Summary:
    capital = dash.current_capital(db)
    opens = (
        db.query(Trade)
        .filter(Trade.status == "open")
        .order_by(Trade.entry_date.desc())
        .all()
    )

    cards: list[PositionCard] = []
    invested = 0.0
    total_pnl = 0.0
    day_pnl = 0.0
    day_pnl_seen = False
    open_risk = 0.0
    locked_profit = 0.0
    today = date.today()

    for t in opens:
        m = calc.metrics(t)
        stop = _effective_stop(t)
        sign = 1 if t.side == "B" else -1
        oq = m.open_qty
        entry = m.avg_entry
        cmp = t.cmp

        inv = entry * oq
        invested += inv

        # Unrealised P&L
        if cmp is not None:
            pnl = (cmp - entry) * sign * oq
            total_pnl += pnl
        else:
            pnl = None

        # Day's P&L — from prev close
        prev = _prev_close(db, t)
        if cmp is not None and prev is not None:
            day_pnl += (cmp - prev) * sign * oq
            day_pnl_seen = True

        # Open risk (CMP → effective_stop)
        if cmp is not None:
            at_risk = max(0.0, (cmp - stop) * sign * oq)
        else:
            at_risk = max(0.0, (entry - stop) * sign * oq)
        open_risk += at_risk

        # Locked profit (effective_stop past entry)
        lp = max(0.0, (stop - entry) * sign * oq)
        locked_profit += lp

        # R-multiple from initial risk (used for the +X.XR badge)
        ips = abs(t.initial_entry_price - t.sl) * t.initial_qty
        r_mult = (pnl / ips) if (pnl is not None and ips >= 0.01) else None

        cards.append(PositionCard(
            trade_id=t.id,
            instrument=t.instrument,
            setup=t.setup,
            side=t.side,
            holding_days=m.holding_days,
            avg_entry=entry,
            cmp=cmp,
            effective_stop=stop,
            sl=t.sl,
            tsl=t.tsl,
            open_qty=oq,
            total_qty=m.total_qty,
            move_pct=m.stock_move_pct,
            pnl_rs=pnl,
            r_multiple=r_mult,
            allocation_pct=(inv / capital) if capital else 0,
            invested_rs=inv,
            risk_rs=at_risk,
            locked_profit_rs=lp,
            status_tag=_status_tag(t, cmp, stop),
        ))

    return Summary(
        positions=len(opens),
        invested_rs=invested,
        total_pnl_rs=total_pnl,
        total_pnl_pct=(total_pnl / invested) if invested else None,
        day_pnl_rs=day_pnl if day_pnl_seen else None,
        day_pnl_pct=(day_pnl / invested) if (day_pnl_seen and invested) else None,
        open_risk_rs=open_risk,
        open_risk_pct_of_capital=(open_risk / capital) if capital else None,
        locked_profit_rs=locked_profit,
        capital=capital,
        cards=cards,
    )
