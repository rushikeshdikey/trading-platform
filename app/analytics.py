"""Edge-analytics rollups for the dashboard.

Separate from `app/dashboard.py` (which is monthly P&L + equity curve) because
these are cross-cutting: we slice by setup, plan-followed, hold time, weekday —
questions like "which of my setups actually make money?" and "how much does
plan discipline pay?"

All numbers are derived from closed trades only; open positions have no final
P&L. "R" throughout means R-multiples — the trade's P&L divided by its initial
risk (|entry − SL| × initial qty). R-multiples are the universal unit for
comparing setups of different sizes and price points.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from statistics import mean
from typing import Iterable

from sqlalchemy.orm import Session

from . import calculations as calc
from . import charges as charges_svc
from .models import Trade


WEEKDAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MONTH_LABELS = [
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
]

# Hold-time buckets in days — swing-trading-friendly cutoffs.
HOLD_BUCKETS: list[tuple[str, int, int]] = [
    ("0-1d", 0, 1),
    ("2-5d", 2, 5),
    ("6-15d", 6, 15),
    ("16-30d", 16, 30),
    ("31-60d", 31, 60),
    ("61+d", 61, 10_000),
]


@dataclass
class SetupStats:
    setup: str
    trades: int
    wins: int
    losses: int
    scratches: int
    win_pct: float
    avg_win_r: float | None  # mean R for winners only
    avg_loss_r: float | None  # mean R for losers only (negative)
    expectancy_r: float | None  # win_rate * avg_win_r + loss_rate * avg_loss_r
    total_pnl: float
    best_r: float | None
    worst_r: float | None
    verdict: str  # 🟢 / 🟡 / 🔴 / 📉 / ℹ️
    verdict_reason: str


@dataclass
class PlanFollowedBreakdown:
    label: str  # "Plan followed" / "Plan not followed" / "Not marked"
    trades: int
    win_pct: float | None
    avg_r: float | None
    total_pnl: float


@dataclass
class HoldBucketStats:
    label: str
    trades: int
    wins: int
    win_pct: float
    avg_r: float | None
    total_pnl: float


def _initial_risk_per_share(t: Trade) -> float:
    """|initial entry price − SL|.

    Deliberately uses the **opening leg's** entry price rather than avg_entry:
    at the moment you plan a trade you only know the initial entry and the
    SL, so that's the honest risk unit. Trades imported from a broker with no
    user-set SL have ``sl == initial_entry_price`` and this returns 0 —
    intentionally excluding them from R analytics.
    """
    return abs(t.initial_entry_price - t.sl)


def _initial_risk_rs(t: Trade) -> float:
    return _initial_risk_per_share(t) * t.initial_qty


def r_multiple(t: Trade) -> float | None:
    """Trade **net** P&L expressed as multiples of initial planned risk.

    None when the trade has no valid SL (SL == entry, common for broker-
    imported trades that haven't been edited to add an SL). Uses net P&L so
    R-multiples already bake in STT / brokerage / DP / GST — matches the
    post-charges outcome the trader actually lives with.
    """
    risk = _initial_risk_rs(t)
    if risk < 0.01:
        return None
    return charges_svc.net_pnl(t) / risk


def _closed(db: Session, year: int | None = None) -> list[Trade]:
    """When `year` is given, it's the FY start year (FY 2025-26 = 2025)."""
    q = db.query(Trade).filter(Trade.status == "closed")
    if year is not None:
        from datetime import date as _date
        q = q.filter(Trade.close_date >= _date(year, 4, 1))
        q = q.filter(Trade.close_date <= _date(year + 1, 3, 31))
    return q.all()


def _verdict(stats_args: dict) -> tuple[str, str]:
    """Assign a traffic-light verdict based on expectancy and sample size."""
    n = stats_args["trades"]
    exp = stats_args["expectancy_r"]
    if n < 5:
        return "ℹ️ Too new", f"Only {n} trade{'s' if n != 1 else ''} — not enough to judge."
    if exp is None:
        return "ℹ️ No R data", "Trades don't have a usable SL to compute R."
    if n < 10:
        base = "🟡 Watch"
        reason = f"Promising" if exp > 0.3 else "Weak" if exp < -0.3 else "Flat"
        return base, f"{reason} over {n} trades — need {10 - n} more to be sure."
    # ≥ 10 trades: judge
    if exp >= 0.5:
        return "🟢 Trade more", f"Expectancy +{exp:.2f}R over {n} trades."
    if exp >= 0.0:
        return "🟡 Marginal", f"Expectancy {exp:+.2f}R — barely positive; refine or drop."
    return "🔴 Drop", f"Expectancy {exp:+.2f}R — consistently losing over {n} trades."


def _summarise_setup(setup: str, trades: list[Trade]) -> SetupStats:
    n = len(trades)
    pnls = [charges_svc.net_pnl(t) for t in trades]
    # Win% is P&L-based (always computable, easy to read). R-multiples are a
    # separate filtered view — only trades with a valid SL contribute.
    wins_by_pnl = sum(1 for p in pnls if p > 0)
    losses_by_pnl = sum(1 for p in pnls if p < 0)

    rs: list[float] = [r for t in trades if (r := r_multiple(t)) is not None]
    r_wins = [r for r in rs if r > 0]
    r_losses = [r for r in rs if r < 0]

    avg_win_r = mean(r_wins) if r_wins else None
    avg_loss_r = mean(r_losses) if r_losses else None
    if r_wins and r_losses:
        expectancy_r = (len(r_wins) / len(rs)) * avg_win_r + (len(r_losses) / len(rs)) * avg_loss_r  # type: ignore[operator]
    elif rs:
        expectancy_r = mean(rs)
    else:
        expectancy_r = None

    out = {
        "setup": setup,
        "trades": n,
        "wins": wins_by_pnl,
        "losses": losses_by_pnl,
        "scratches": n - wins_by_pnl - losses_by_pnl,
        "win_pct": (wins_by_pnl / n) if n else 0.0,
        "avg_win_r": avg_win_r,
        "avg_loss_r": avg_loss_r,
        "expectancy_r": expectancy_r,
        "total_pnl": sum(pnls),
        "best_r": max(rs) if rs else None,
        "worst_r": min(rs) if rs else None,
    }
    verdict, reason = _verdict(out)
    return SetupStats(**out, verdict=verdict, verdict_reason=reason)


def setup_edge(db: Session, year: int | None = None) -> list[SetupStats]:
    trades = _closed(db, year)
    grouped: dict[str, list[Trade]] = defaultdict(list)
    for t in trades:
        grouped[t.setup or "(unlabelled)"].append(t)

    out = [_summarise_setup(setup, trs) for setup, trs in grouped.items()]
    # Most-traded setups first, then best expectancy.
    out.sort(key=lambda s: (-s.trades, -(s.expectancy_r or -1e9)))
    return out


def plan_followed_breakdown(
    db: Session, year: int | None = None
) -> list[PlanFollowedBreakdown]:
    trades = _closed(db, year)
    buckets = {True: [], False: [], None: []}  # type: dict[object, list[Trade]]
    for t in trades:
        buckets[t.plan_followed].append(t)

    labels = {True: "Plan followed", False: "Plan NOT followed", None: "Not marked"}
    out: list[PlanFollowedBreakdown] = []
    for key, label in labels.items():
        trs = buckets[key]
        if not trs:
            continue
        pnls = [charges_svc.net_pnl(t) for t in trs]
        rs = [r for t in trs if (r := r_multiple(t)) is not None]
        wins = sum(1 for p in pnls if p > 0)
        out.append(
            PlanFollowedBreakdown(
                label=label,
                trades=len(trs),
                win_pct=(wins / len(trs)) if trs else None,
                avg_r=mean(rs) if rs else None,
                total_pnl=sum(pnls),
            )
        )
    # Show "Plan followed" first so the contrast is obvious.
    order = {"Plan followed": 0, "Plan NOT followed": 1, "Not marked": 2}
    out.sort(key=lambda b: order.get(b.label, 99))
    return out


def hold_time_buckets(
    db: Session, year: int | None = None
) -> list[HoldBucketStats]:
    trades = _closed(db, year)
    grouped: dict[str, list[Trade]] = defaultdict(list)
    for t in trades:
        h = calc.holding_days(t)
        for label, lo, hi in HOLD_BUCKETS:
            if lo <= h <= hi:
                grouped[label].append(t)
                break

    out: list[HoldBucketStats] = []
    for label, _, _ in HOLD_BUCKETS:
        trs = grouped.get(label, [])
        pnls = [charges_svc.net_pnl(t) for t in trs]
        rs = [r for t in trs if (r := r_multiple(t)) is not None]
        wins = sum(1 for p in pnls if p > 0)
        out.append(
            HoldBucketStats(
                label=label,
                trades=len(trs),
                wins=wins,
                win_pct=(wins / len(trs)) if trs else 0.0,
                avg_r=mean(rs) if rs else None,
                total_pnl=sum(pnls),
            )
        )
    return out


@dataclass
class WeekdayStats:
    label: str
    trades: int
    wins: int
    win_pct: float
    avg_r: float | None
    total_pnl: float


@dataclass
class WeeklySummary:
    trades: int
    wins: int
    losses: int
    win_pct: float
    total_pnl: float
    best_r: float | None
    worst_r: float | None
    avg_r: float | None
    plan_followed_pct: float | None
    top_mistakes: list[tuple[str, int]]


def weekly_summary(db: Session, days: int = 7) -> WeeklySummary:
    """Rollup of trades closed in the last `days` days — shown on dashboard."""
    from datetime import date as _date, timedelta

    cutoff = _date.today() - timedelta(days=days)
    trades = (
        db.query(Trade)
        .filter(Trade.status == "closed")
        .filter(Trade.close_date >= cutoff)
        .all()
    )
    pnls = [charges_svc.net_pnl(t) for t in trades]
    wins = sum(1 for p in pnls if p > 0)
    losses = sum(1 for p in pnls if p < 0)
    rs = [r for t in trades if (r := r_multiple(t)) is not None]
    plans = [t.plan_followed for t in trades if t.plan_followed is not None]
    plan_pct = (sum(1 for v in plans if v) / len(plans)) if plans else None

    mistake_counts: dict[str, int] = defaultdict(int)
    for t in trades:
        if not t.growth_areas:
            continue
        for tag in (x.strip() for x in t.growth_areas.split(",")):
            if tag:
                mistake_counts[tag] += 1
    top_mistakes = sorted(mistake_counts.items(), key=lambda kv: -kv[1])[:3]

    return WeeklySummary(
        trades=len(trades),
        wins=wins,
        losses=losses,
        win_pct=(wins / len(trades)) if trades else 0.0,
        total_pnl=sum(pnls),
        best_r=max(rs) if rs else None,
        worst_r=min(rs) if rs else None,
        avg_r=(sum(rs) / len(rs)) if rs else None,
        plan_followed_pct=plan_pct,
        top_mistakes=top_mistakes,
    )


@dataclass
class StreakAlert:
    """Signal for the dashboard red-banner: 'you're on tilt, consider pausing'."""
    losing_streak: int
    historical_win_pct_overall: float | None
    historical_win_pct_after_streak: float | None
    warn: bool


def consecutive_loss_alert(db: Session, streak_threshold: int = 3) -> StreakAlert:
    """Look at the most recent closed trades in reverse-chronological order.

    Count how many at the head are losses. If ≥ threshold, compute the
    baseline win rate vs the win rate *immediately after* any past streak of
    the same length — gives the user a data-backed reason to pause.
    """
    trades = (
        db.query(Trade)
        .filter(Trade.status == "closed")
        .order_by(Trade.close_date.desc(), Trade.id.desc())
        .all()
    )
    pnls = [charges_svc.net_pnl(t) for t in trades]

    streak = 0
    for p in pnls:
        if p < 0:
            streak += 1
        else:
            break

    if not trades:
        return StreakAlert(0, None, None, False)

    overall_wins = sum(1 for p in pnls if p > 0)
    overall_win_pct = overall_wins / len(trades)

    # For "after a streak" stats, walk the history chronologically.
    pnls_chrono = list(reversed(pnls))
    post_streak_wins = post_streak_total = 0
    run = 0
    for i, p in enumerate(pnls_chrono):
        if p < 0:
            run += 1
        else:
            # Once the loss streak breaks, count the NEXT trade (this one) as post-streak.
            if run >= streak_threshold:
                post_streak_total += 1
                if p > 0:
                    post_streak_wins += 1
            run = 0
    # Also count immediate-next trades after each streak length crossing.
    post_streak_win_pct = (
        post_streak_wins / post_streak_total if post_streak_total else None
    )

    return StreakAlert(
        losing_streak=streak,
        historical_win_pct_overall=overall_win_pct,
        historical_win_pct_after_streak=post_streak_win_pct,
        warn=streak >= streak_threshold,
    )


def trades_needing_review(db: Session, limit: int = 8):
    """Recently-closed trades where plan_followed is still unset — gentle nudge."""
    return (
        db.query(Trade)
        .filter(Trade.status == "closed")
        .filter(Trade.plan_followed.is_(None))
        .order_by(Trade.close_date.desc(), Trade.id.desc())
        .limit(limit)
        .all()
    )


def weekday_breakdown(
    db: Session, year: int | None = None
) -> list[WeekdayStats]:
    """Break down by ENTRY weekday. Indian markets trade Mon–Fri."""
    trades = _closed(db, year)
    grouped: dict[int, list[Trade]] = defaultdict(list)
    for t in trades:
        grouped[t.entry_date.weekday()].append(t)

    out: list[WeekdayStats] = []
    for idx in range(5):  # Mon–Fri only
        trs = grouped.get(idx, [])
        pnls = [charges_svc.net_pnl(t) for t in trs]
        rs = [r for t in trs if (r := r_multiple(t)) is not None]
        wins = sum(1 for p in pnls if p > 0)
        out.append(
            WeekdayStats(
                label=WEEKDAY_LABELS[idx],
                trades=len(trs),
                wins=wins,
                win_pct=(wins / len(trs)) if trs else 0.0,
                avg_r=mean(rs) if rs else None,
                total_pnl=sum(pnls),
            )
        )
    return out
