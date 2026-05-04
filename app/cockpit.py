"""Daily Trading Cockpit — single-page decision support.

Five panels, each a pure function on the DB:

1. **Auto-Pilot picks** — top composite-scored daily trades from the
   shared scanner cache, pre-sized + pre-stopped. Headline panel.
   See ``app.auto_pilot``.
2. **Market verdict** — GREEN/YELLOW/RED based on breadth (% above 50 EMA,
   % above 200 EMA). Drives whether new longs are encouraged.
3. **Open-position actions** — for each open trade, a HOLD / TIGHTEN SL TO
   ENTRY / TRIM HALF / EXIT / REVIEW verdict using rule-based logic.
4. **Risk budget** — open heat vs ceiling, plus the loss-streak cooldown alert.
5. **Edge sidebar** — the user's expectancy by setup, so they know which
   patterns they actually make money on.

Scanner runs are NOT triggered here. Auto-Pilot reads the shared scanner
cache (refreshed by the EOD pre-warm at 15:35 IST and the boot-time
catch-up); /scanners is where you re-run live.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from . import analytics
from . import breadth as breadth_mod
from . import portfolio as portfolio_mod
from . import dashboard as dash_svc
from . import settings as app_settings
from . import calculations as calc
from .models import Trade


# -- Market verdict -----------------------------------------------------------


@dataclass
class MarketVerdict:
    level: str            # "GREEN" / "YELLOW" / "RED" / "UNKNOWN"
    headline: str
    color_class: str      # tailwind classes for the banner
    pct_above_50ema: float | None
    pct_above_200ema: float | None
    advances: int | None
    declines: int | None
    label: str            # breadth_mod.sentiment_label result
    detail: str
    allow_new_longs: bool
    # Composite Market Mood score (0-100) + per-component breakdown.
    # Computed via breadth_mod.mood_score from the same row above.
    mood: dict | None = None


def build_market_verdict(db: Session) -> MarketVerdict:
    row = breadth_mod.latest(db, universe="all")
    if row is None:
        return MarketVerdict(
            level="UNKNOWN",
            headline="No breadth data yet",
            color_class="bg-zinc-100 text-zinc-700 border-zinc-200",
            pct_above_50ema=None, pct_above_200ema=None,
            advances=None, declines=None,
            label="—",
            detail="Visit /breadth and click Refresh to compute today's breadth.",
            allow_new_longs=True,  # no gating without data
        )

    p50 = float(row.pct_above_50ema or 0)
    p200 = float(row.pct_above_200ema or 0)
    label, _ = breadth_mod.sentiment_label(p200, p50)

    if p50 >= 60 and p200 >= 55:
        level = "GREEN"
        headline = "Clear sky — bias toward new longs"
        color = "bg-emerald-50 border-emerald-300 text-emerald-900"
        detail = f"{p50:.0f}% of stocks above 50 EMA, {p200:.0f}% above 200 EMA. Trend-following bias works here."
        allow = True
    elif p50 < 40 or p200 < 35:
        level = "RED"
        headline = "Risk-off — avoid new longs"
        color = "bg-rose-50 border-rose-300 text-rose-900"
        detail = f"Only {p50:.0f}% above 50 EMA, {p200:.0f}% above 200 EMA. Most longs fail in this regime."
        allow = False
    else:
        level = "YELLOW"
        headline = "Mixed — only A+ setups"
        color = "bg-amber-50 border-amber-300 text-amber-900"
        detail = f"{p50:.0f}% above 50 EMA, {p200:.0f}% above 200 EMA. Selectivity matters more than usual."
        allow = True

    return MarketVerdict(
        level=level,
        headline=headline,
        color_class=color,
        pct_above_50ema=p50,
        pct_above_200ema=p200,
        advances=int(row.advances or 0),
        declines=int(row.declines or 0),
        label=label,
        detail=detail,
        allow_new_longs=allow,
        mood=breadth_mod.mood_score(row),
    )


# -- Position actions ---------------------------------------------------------


@dataclass
class PositionAction:
    card: portfolio_mod.PositionCard
    action: str         # "HOLD" / "TIGHTEN SL TO ENTRY" / "TRIM HALF" / "EXIT" / "REVIEW"
    reason: str
    urgency: str        # "low" / "medium" / "high"
    color_class: str    # tailwind for the action pill


def _action_for(card: portfolio_mod.PositionCard) -> tuple[str, str, str, str]:
    """Rule-based exit framework. Order matters — first matching rule wins.

    Returns (action, reason, urgency, tailwind classes).
    """
    r = card.r_multiple
    cmp = card.cmp
    sign = 1 if card.side == "B" else -1

    # No CMP — can't decide.
    if cmp is None:
        return ("REFRESH", "No live price — refresh prices.", "low",
                "bg-zinc-100 text-zinc-700 border-zinc-200")

    # Stop already hit territory (price below stop for long).
    if (cmp - card.effective_stop) * sign <= 0:
        return ("EXIT", "Price has reached your stop — exit at market.", "high",
                "bg-rose-100 text-rose-800 border-rose-300")

    # Big winner — book partial.
    if r is not None and r >= 4:
        return ("TRIM HALF", f"+{r:.1f}R — book half, ride the rest with trailing SL.", "medium",
                "bg-sky-50 text-sky-800 border-sky-200")

    # Decent winner with no trailing stop yet → tighten to entry.
    if r is not None and r >= 2 and card.tsl is None:
        return ("TIGHTEN SL → ENTRY", f"+{r:.1f}R reached. Move SL to entry; lock in 0R floor.", "medium",
                "bg-sky-50 text-sky-800 border-sky-200")

    # Stale loser — trade has been on for a while and is still red.
    if r is not None and r < 0 and card.holding_days >= 30:
        return ("REVIEW", f"{card.holding_days}d in trade, still {r:.1f}R. Review thesis or exit.", "medium",
                "bg-amber-50 text-amber-800 border-amber-200")

    # Cushion is tight — stop within 2% of CMP, no profit cushion yet.
    if card.status_tag in ("Tight", "At risk"):
        return ("TIGHTEN STOP", "Price compressed near SL — consider exiting on weakness.", "medium",
                "bg-amber-50 text-amber-800 border-amber-200")

    # Default — hold.
    return ("HOLD", "Trade is working as planned.", "low",
            "bg-emerald-50 text-emerald-800 border-emerald-200")


def build_position_actions(db: Session) -> list[PositionAction]:
    summary = portfolio_mod.build(db)
    out: list[PositionAction] = []
    for card in summary.cards:
        action, reason, urgency, color = _action_for(card)
        out.append(PositionAction(card, action, reason, urgency, color))
    # Sort: high urgency first, then by R-multiple desc (biggest winners visible)
    urgency_rank = {"high": 0, "medium": 1, "low": 2}
    out.sort(key=lambda p: (urgency_rank[p.urgency], -(p.card.r_multiple or 0)))
    return out


# -- Risk budget + cooldown ---------------------------------------------------


@dataclass
class RiskBudgetPanel:
    capital: float                  # book value (realised P&L only)
    open_heat_rs: float
    open_heat_pct: float
    max_heat_rs: float
    max_heat_pct: float
    remaining_heat_rs: float
    open_positions: int
    over_budget: bool


def build_risk_budget(db: Session) -> RiskBudgetPanel:
    opens = db.query(Trade).filter(Trade.status == "open").all()
    open_heat = sum(calc.open_heat_rs(t) for t in opens)
    capital = dash_svc.current_capital(db)
    max_pct = app_settings.get_float(db, "max_open_heat_pct", 0.06)
    max_rs = capital * max_pct

    return RiskBudgetPanel(
        capital=round(capital, 2),
        open_heat_rs=round(open_heat, 2),
        open_heat_pct=(open_heat / capital) if capital else 0.0,
        max_heat_rs=round(max_rs, 2),
        max_heat_pct=max_pct,
        remaining_heat_rs=round(max(0.0, max_rs - open_heat), 2),
        open_positions=len(opens),
        over_budget=open_heat > max_rs + 0.01,
    )


# -- Edge sidebar -------------------------------------------------------------


def build_edge_panel(db: Session, min_trades: int = 3) -> list[analytics.SetupStats]:
    """Setups with ≥ min_trades closed history, sorted by expectancy desc."""
    rows = analytics.setup_edge(db)
    rows = [r for r in rows if r.trades >= min_trades]
    rows.sort(key=lambda r: (r.expectancy_r if r.expectancy_r is not None else -99), reverse=True)
    return rows


# -- Top-level builder --------------------------------------------------------


@dataclass
class PendingExit:
    """One trade with EXIT_RECOMMENDED in the most-recent TSL decision."""
    trade_id: int
    instrument: str
    decided_at: object  # datetime
    cmp: float
    current_r: float | None
    anchor: str | None
    anchor_value: float | None
    reason: str


@dataclass
class CockpitState:
    market: MarketVerdict
    positions: list[PositionAction]
    risk_budget: RiskBudgetPanel
    cooldown: analytics.StreakAlert
    edge: list[analytics.SetupStats]
    # Auto-Pilot — top prescriptive picks for today. The headline panel.
    auto_pilot: object = None  # AutoPilotState; loose-typed to avoid circular import
    # Pending exit recommendations (EXIT_RECOMMENDED rows from TSL daemon
    # that the trader hasn't acted on yet). Drives a red banner.
    pending_exits: list[PendingExit] = None  # type: ignore


def _pending_exits(db: Session) -> list[PendingExit]:
    """Find every Kite-managed open trade whose MOST RECENT TSL decision
    is EXIT_RECOMMENDED. We compare the latest decision per trade — if the
    trader closed the position OR the next day's run produced a HOLD/MOVED,
    the recommendation no longer pends.
    """
    from sqlalchemy import func
    from .models import Trade, TslDecision

    open_trades = (
        db.query(Trade)
        .filter(Trade.status == "open")
        .filter(Trade.kite_trigger_id.isnot(None))
        .all()
    )
    if not open_trades:
        return []
    trade_ids = [t.id for t in open_trades]

    # For each trade, fetch its most-recent TslDecision.
    subq = (
        db.query(
            TslDecision.trade_id,
            func.max(TslDecision.decided_at).label("max_at"),
        )
        .filter(TslDecision.trade_id.in_(trade_ids))
        .group_by(TslDecision.trade_id)
        .subquery()
    )
    latest = (
        db.query(TslDecision)
        .join(
            subq,
            (TslDecision.trade_id == subq.c.trade_id)
            & (TslDecision.decided_at == subq.c.max_at),
        )
        .all()
    )
    by_id = {t.id: t for t in open_trades}
    out: list[PendingExit] = []
    for d in latest:
        if d.action != "EXIT_RECOMMENDED":
            continue
        t = by_id.get(d.trade_id)
        if t is None:
            continue
        out.append(PendingExit(
            trade_id=t.id, instrument=t.instrument,
            decided_at=d.decided_at, cmp=d.cmp,
            current_r=d.current_r, anchor=d.anchor,
            anchor_value=d.anchor_value, reason=d.reason or "",
        ))
    return out


def build_cockpit(
    db: Session, *, entry_overrides: dict[str, str] | None = None,
) -> CockpitState:
    """Build the cockpit state. ``entry_overrides`` is a per-symbol map
    of forced entry types (Phase C — URL-driven overrides like
    ``?override=BHARATFORG:Pullback&override=PAISALO:StrongStart``)."""
    market = build_market_verdict(db)
    positions = build_position_actions(db)
    risk = build_risk_budget(db)
    cooldown = analytics.consecutive_loss_alert(db, streak_threshold=3)
    edge = build_edge_panel(db)
    pending_exits = _pending_exits(db)
    from . import auto_pilot as ap_mod
    auto_pilot = ap_mod.build_daily_picks(db, overrides=entry_overrides or {})
    # Stamp the market verdict so the panel can render "Stay in cash" loud
    # when the macro is RED regardless of A+ picks existing.
    auto_pilot.market_verdict_level = market.level
    return CockpitState(
        market=market,
        positions=positions,
        risk_budget=risk,
        cooldown=cooldown,
        edge=edge,
        auto_pilot=auto_pilot,
        pending_exits=pending_exits,
    )
