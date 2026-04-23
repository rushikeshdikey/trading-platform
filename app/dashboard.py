"""Monthly / yearly rollups used by the dashboard view."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from . import calculations as calc
from . import charges as charges_svc
from . import settings as app_settings
from .models import CapitalEvent, Trade


def _prior_realised_pnl(db: Session, before: date) -> float:
    """Net realised P&L from trades closed strictly before the given date."""
    trs = (
        db.query(Trade)
        .filter(Trade.status == "closed")
        .filter(Trade.close_date < before)
        .all()
    )
    return sum(charges_svc.net_pnl(t) for t in trs)


MONTHS = [
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
]

# Indian financial year runs April to March. Months in FY order:
FY_MONTH_ORDER = [4, 5, 6, 7, 8, 9, 10, 11, 12, 1, 2, 3]


def fy_start(d: date) -> int:
    """Return the FY's starting calendar year for a date.
    Jan-Mar belong to the PREVIOUS FY's final quarter. E.g. 2026-03-15 → FY
    2025-26 → returns 2025. 2026-04-01 → FY 2026-27 → returns 2026.
    """
    return d.year if d.month >= 4 else d.year - 1


def fy_label(fy: int) -> str:
    """`2025` → `'FY 2025-26'`."""
    return f"FY {fy}-{str(fy + 1)[-2:]}"


@dataclass
class MonthRow:
    month: str
    starting_capital: float
    added_withdrawn: float
    pnl_rs: float
    pnl_pct: float
    final_capital: float
    num_trades: int
    win_pct: float | None
    avg_gain: float | None
    avg_loss: float | None
    avg_rr: float | None
    best_impact: float
    worst_impact: float
    avg_holding_days: float | None
    fo_count: int


@dataclass
class YearAggregates:
    total_trades: int
    win_pct: float | None
    avg_positive_move: float | None
    avg_negative_move: float | None
    avg_position_size: float | None
    avg_holding_days: float | None
    plan_followed_pct: float | None
    total_pnl: float
    current_capital: float


def _month_key(d: date) -> tuple[int, int]:
    return (d.year, d.month)


def years_with_activity(db: Session) -> list[int]:
    """FY start years with any trade or capital-event activity — sorted asc.
    Always includes the current FY so the view works on a fresh DB.
    """
    years: set[int] = {fy_start(date.today())}
    for (d,) in db.query(Trade.entry_date).all():
        if d:
            years.add(fy_start(d))
    for (d,) in db.query(Trade.close_date).filter(Trade.close_date.isnot(None)).all():
        if d:
            years.add(fy_start(d))
    for (d,) in db.query(CapitalEvent.date).all():
        if d:
            years.add(fy_start(d))
    return sorted(years)


def current_capital(db: Session) -> float:
    """Capital today. For a fresh FY with no closed trades yet it's the prior
    FY's end capital plus any new events; for the current FY it's computed
    via ``build_year``."""
    _, yagg, _ = build_year(db, fy_start(date.today()))
    return yagg.current_capital


def build_year(db: Session, year: int) -> tuple[list[MonthRow], YearAggregates, list[dict]]:
    """`year` is interpreted as the FY start year. FY 2025-26 = year 2025."""
    starting_capital = app_settings.get_float(db, "starting_capital", 0.0)
    events = db.query(CapitalEvent).all()
    year_start = date(year, 4, 1)
    year_end = date(year + 1, 3, 31)
    # Include trades that either opened or closed in this FY.
    trades = (
        db.query(Trade)
        .filter(
            or_(
                and_(Trade.entry_date >= year_start, Trade.entry_date <= year_end),
                and_(Trade.close_date >= year_start, Trade.close_date <= year_end),
            )
        )
        .all()
    )

    # Capital baseline at start of this FY =
    #   starting_capital                          (absolute seed)
    #   + all capital events before FY start       (deposits/withdrawals)
    #   + realised P&L from trades closed pre-FY   (rolls prior FY trading forward)
    baseline = starting_capital
    for ev in events:
        if ev.date < year_start:
            baseline += ev.amount
    baseline += _prior_realised_pnl(db, year_start)

    # Group trades by CLOSE month within this FY. Key is calendar month (1–12).
    trades_by_month: dict[int, list[Trade]] = defaultdict(list)
    for t in trades:
        anchor = t.close_date if t.status == "closed" and t.close_date else t.entry_date
        if year_start <= anchor <= year_end:
            trades_by_month[anchor.month].append(t)

    events_by_month: dict[int, float] = defaultdict(float)
    for ev in events:
        if year_start <= ev.date <= year_end:
            events_by_month[ev.date.month] += ev.amount

    rows: list[MonthRow] = []
    equity_curve: list[dict] = []
    running_capital = baseline
    total_pnl = 0.0
    all_pnl_pcts: list[float] = []
    all_positive_moves: list[float] = []
    all_negative_moves: list[float] = []
    all_position_sizes: list[float] = []
    all_holding_days: list[int] = []
    total_trades_count = 0
    wins = 0
    plan_followed_trades: list[bool] = []

    # Walk months in FY order (Apr→Mar). For the current FY stop at today's
    # month so we don't print empty rows for months that haven't happened.
    today = date.today()
    is_current_fy = fy_start(today) == year
    months_to_walk = list(FY_MONTH_ORDER)
    if is_current_fy:
        if today.month >= 4:
            cutoff_idx = today.month - 4  # index in FY order
        else:
            cutoff_idx = (today.month - 4) % 12  # Jan/Feb/Mar → index 9/10/11
        months_to_walk = months_to_walk[: cutoff_idx + 1]

    for m in months_to_walk:
        trs = trades_by_month.get(m, [])
        added = events_by_month.get(m, 0.0)
        starting = running_capital
        month_pnl = 0.0
        gains: list[float] = []
        losses: list[float] = []
        rrs: list[float] = []
        impacts: list[float] = []
        fo_count = 0
        holding_days: list[int] = []

        for t in trs:
            m_data = calc.metrics(t)
            if t.status == "closed":
                # Net P&L = gross - charges (STT + brokerage + exchange + GST + DP)
                pnl = m_data.pnl_rs - charges_svc.charges_for(t)
                month_pnl += pnl
                impact = pnl / starting if starting else 0.0
                impacts.append(impact)
                if pnl > 0:
                    wins += 1
                    gains.append(pnl / starting if starting else 0.0)
                elif pnl < 0:
                    losses.append(pnl / starting if starting else 0.0)
                if m_data.reward_risk is not None:
                    rrs.append(m_data.reward_risk)
                if m_data.stock_move_pct is not None:
                    if m_data.stock_move_pct >= 0:
                        all_positive_moves.append(m_data.stock_move_pct)
                    else:
                        all_negative_moves.append(m_data.stock_move_pct)
                all_position_sizes.append(
                    m_data.position_size_rs / starting if starting else 0.0
                )
                holding_days.append(m_data.holding_days)
                all_holding_days.append(m_data.holding_days)
                if t.plan_followed is not None:
                    plan_followed_trades.append(bool(t.plan_followed))
                total_trades_count += 1
            if t.option_type:
                fo_count += 1

        final_capital = starting + added + month_pnl
        pnl_pct = month_pnl / starting if starting else 0.0
        all_pnl_pcts.append(pnl_pct)
        total_pnl += month_pnl

        row = MonthRow(
            month=MONTHS[m - 1],
            starting_capital=starting,
            added_withdrawn=added,
            pnl_rs=month_pnl,
            pnl_pct=pnl_pct,
            final_capital=final_capital,
            num_trades=len(trs),
            win_pct=(
                sum(1 for t in trs if t.status == "closed" and calc.pnl_rs(t) > 0)
                / sum(1 for t in trs if t.status == "closed")
                if any(t.status == "closed" for t in trs)
                else None
            ),
            avg_gain=(sum(gains) / len(gains)) if gains else None,
            avg_loss=(sum(losses) / len(losses)) if losses else None,
            avg_rr=(sum(rrs) / len(rrs)) if rrs else None,
            best_impact=max(impacts) if impacts else 0.0,
            worst_impact=min(impacts) if impacts else 0.0,
            avg_holding_days=(
                sum(holding_days) / len(holding_days) if holding_days else None
            ),
            fo_count=fo_count,
        )
        rows.append(row)
        equity_curve.append({"month": MONTHS[m - 1], "capital": round(final_capital, 2)})
        running_capital = final_capital

    closed_trades = [t for t in trades if t.status == "closed"]
    yagg = YearAggregates(
        total_trades=len(closed_trades),
        win_pct=(wins / len(closed_trades)) if closed_trades else None,
        avg_positive_move=(
            sum(all_positive_moves) / len(all_positive_moves) if all_positive_moves else None
        ),
        avg_negative_move=(
            sum(all_negative_moves) / len(all_negative_moves) if all_negative_moves else None
        ),
        avg_position_size=(
            sum(all_position_sizes) / len(all_position_sizes) if all_position_sizes else None
        ),
        avg_holding_days=(
            sum(all_holding_days) / len(all_holding_days) if all_holding_days else None
        ),
        plan_followed_pct=(
            sum(1 for v in plan_followed_trades if v) / len(plan_followed_trades)
            if plan_followed_trades
            else None
        ),
        total_pnl=total_pnl,
        current_capital=running_capital,
    )
    return rows, yagg, equity_curve


def setup_performance(db: Session, year: int) -> list[dict]:
    """Aggregate closed trades by Setup for the given FY (`year` is FY start)."""
    trades = (
        db.query(Trade)
        .filter(Trade.status == "closed")
        .filter(Trade.close_date >= date(year, 4, 1))
        .filter(Trade.close_date <= date(year + 1, 3, 31))
        .all()
    )
    grouped: dict[str, list[Trade]] = defaultdict(list)
    for t in trades:
        grouped[t.setup or "(none)"].append(t)

    out = []
    for setup, trs in sorted(grouped.items(), key=lambda kv: -len(kv[1])):
        pnls = [charges_svc.net_pnl(t) for t in trs]
        wins = sum(1 for p in pnls if p > 0)
        out.append(
            {
                "setup": setup,
                "trades": len(trs),
                "wins": wins,
                "win_pct": (wins / len(trs)) if trs else 0,
                "total_pnl": sum(pnls),
                "avg_rr": (
                    sum(rr for t in trs if (rr := calc.reward_risk(t)) is not None)
                    / max(1, sum(1 for t in trs if calc.reward_risk(t) is not None))
                ),
            }
        )
    return out
