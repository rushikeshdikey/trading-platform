"""Daily Trading Cockpit — single-page decision support.

Five panels, each a pure function on the DB:

1. **Market verdict** — GREEN/YELLOW/RED based on breadth (% above 50 EMA,
   % above 200 EMA). Drives whether new longs are encouraged.
2. **Open-position actions** — for each open trade, a HOLD / TIGHTEN SL TO
   ENTRY / TRIM HALF / EXIT / REVIEW verdict using rule-based logic.
3. **Risk budget** — open heat vs ceiling, plus the loss-streak cooldown alert.
4. **Conviction signals** — top scanner candidates, conviction-scored using
   pattern strength × market regime × user's personal edge on that setup.
5. **Edge sidebar** — the user's expectancy by setup, so they know which
   patterns they actually make money on.

Scanner runs are NOT triggered here (they take seconds). The signals panel
loads via HTMX from /cockpit/signals so the page itself is instant.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from . import analytics
from . import breadth as breadth_mod
from . import portfolio as portfolio_mod
from . import dashboard as dash_svc
from . import settings as app_settings
from . import calculations as calc
from .models import Trade
from .scanner import fundamentals as scanner_fund
from .scanner import runner as scanner_runner
from .scanner import universe as scanner_universe
from .scanner.patterns import SCAN_TYPES
from .scanner.risk import size_candidate


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
    nav_rs: float                   # mark-to-market: capital + unrealised P&L on opens
    unrealized_pnl_rs: float        # nav_rs - capital, for the "+₹X (today's marks)" line
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

    # Unrealised P&L on open positions, summed mark-to-market. NAV = capital
    # + unrealised. Skips trades where CMP isn't available (those just don't
    # contribute, same shape as portfolio.build()).
    unrealized = 0.0
    for t in opens:
        if t.cmp is None:
            continue
        m = calc.metrics(t)
        sign = 1 if t.side == "B" else -1
        unrealized += (t.cmp - m.avg_entry) * sign * m.open_qty

    return RiskBudgetPanel(
        capital=round(capital, 2),
        nav_rs=round(capital + unrealized, 2),
        unrealized_pnl_rs=round(unrealized, 2),
        open_heat_rs=round(open_heat, 2),
        open_heat_pct=(open_heat / capital) if capital else 0.0,
        max_heat_rs=round(max_rs, 2),
        max_heat_pct=max_pct,
        remaining_heat_rs=round(max(0.0, max_rs - open_heat), 2),
        open_positions=len(opens),
        over_budget=open_heat > max_rs + 0.01,
    )


# -- Conviction-scored signals ------------------------------------------------


@dataclass
class ConvictionSignal:
    scan_type: str
    scan_label: str
    candidate: object       # patterns.Candidate
    sizing: dict
    conviction: int         # 0-100
    tier: str               # "A+" / "A" / "B"
    edge_note: str
    on_watchlist: bool


_SETUP_LABEL_TO_KEY = {v[0]: k for k, v in SCAN_TYPES.items()}


def _conviction_score(
    candidate, max_score_today: float, market: MarketVerdict,
    edge_expectancy_r: float | None, edge_trades: int,
) -> int:
    """0-100 composite. Pattern × market × user's edge on this setup."""
    # Pattern strength (40 pts) — score relative to today's best.
    score_pct = (candidate.score / max_score_today) if max_score_today > 0 else 0.5
    pattern = 40 * max(0.0, min(1.0, score_pct))

    # Market regime (25 pts).
    market_bonus = {"GREEN": 25, "YELLOW": 12, "RED": 0, "UNKNOWN": 12}[market.level]

    # Personal edge (-20 to +25). Insufficient sample → small neutral bonus.
    if edge_expectancy_r is None or edge_trades < 5:
        edge_bonus = 5
    else:
        e = max(-0.5, min(1.0, edge_expectancy_r))
        # Map [-0.5, 1.0] → [-20, +25]
        edge_bonus = ((e + 0.5) / 1.5) * 45 - 20

    return int(max(0, min(100, pattern + market_bonus + edge_bonus)))


def _tier_for(conviction: int) -> str:
    if conviction >= 80:
        return "A+"
    if conviction >= 65:
        return "A"
    if conviction >= 50:
        return "B"
    return "C"


def build_signals(
    db: Session, top_n: int = 12,
) -> tuple[list[ConvictionSignal], dict]:
    """Run all 4 scanners, conviction-score, return top N.

    Also returns a meta dict with cache health flags (bars cache warm? mcap
    cache warm?) so the UI can explain emptiness.
    """
    from .models import Watchlist

    # Cache health gates — same conditions the scanner page uses.
    universe = scanner_universe.universe_from_cache(db)
    mcap_count = scanner_fund.cache_stats(db).get("above_threshold", 0)
    meta = {
        "universe_in_cache": len(universe),
        "mcap_above_threshold": mcap_count,
        "ready": len(universe) > 0 and mcap_count > 0,
    }
    if not meta["ready"]:
        return [], meta

    market = build_market_verdict(db)

    # Edge per setup (label → SetupStats).
    edge_rows = analytics.setup_edge(db)
    edge_by_label = {row.setup: row for row in edge_rows}

    # Watchlist symbols for the "already watching" badge.
    on_watch = {s for (s,) in db.query(Watchlist.symbol).all()}

    all_signals: list[ConvictionSignal] = []
    for scan_type, (label, _detector) in SCAN_TYPES.items():
        try:
            candidates, _run = scanner_runner.run_scan(db, scan_type)
        except Exception:  # noqa: BLE001
            continue
        if not candidates:
            continue
        max_score = max(c.score for c in candidates)
        edge = edge_by_label.get(label)
        edge_exp = edge.expectancy_r if edge else None
        edge_trades = edge.trades if edge else 0
        edge_note = (
            f"Your edge on this setup: {edge_exp:+.2f}R over {edge_trades} closed trades"
            if edge and edge_trades >= 5
            else "Insufficient personal history on this setup"
        )
        for c in candidates:
            conv = _conviction_score(c, max_score, market, edge_exp, edge_trades)
            tier = _tier_for(conv)
            if tier == "C":
                continue  # don't show low-conviction noise
            sizing = size_candidate(db, c)
            all_signals.append(ConvictionSignal(
                scan_type=scan_type,
                scan_label=label,
                candidate=c,
                sizing=sizing,
                conviction=conv,
                tier=tier,
                edge_note=edge_note,
                on_watchlist=c.symbol in on_watch,
            ))

    all_signals.sort(key=lambda s: s.conviction, reverse=True)
    return all_signals[:top_n], meta


# -- Edge sidebar -------------------------------------------------------------


def build_edge_panel(db: Session, min_trades: int = 3) -> list[analytics.SetupStats]:
    """Setups with ≥ min_trades closed history, sorted by expectancy desc."""
    rows = analytics.setup_edge(db)
    rows = [r for r in rows if r.trades >= min_trades]
    rows.sort(key=lambda r: (r.expectancy_r if r.expectancy_r is not None else -99), reverse=True)
    return rows


# -- Top-level builder --------------------------------------------------------


@dataclass
class CockpitState:
    market: MarketVerdict
    positions: list[PositionAction]
    risk_budget: RiskBudgetPanel
    cooldown: analytics.StreakAlert
    edge: list[analytics.SetupStats]
    # Auto-Pilot — top 1-3 prescriptive picks for today. The headline panel.
    auto_pilot: object = None  # AutoPilotState; loose-typed to avoid circular import
    # Signals are loaded lazily via /cockpit/signals (HTMX); included here
    # only when explicitly requested.
    signals: list[ConvictionSignal] = field(default_factory=list)
    signals_meta: dict = field(default_factory=dict)


def build_cockpit(db: Session, *, include_signals: bool = False) -> CockpitState:
    market = build_market_verdict(db)
    positions = build_position_actions(db)
    risk = build_risk_budget(db)
    cooldown = analytics.consecutive_loss_alert(db, streak_threshold=3)
    edge = build_edge_panel(db)
    from . import auto_pilot as ap_mod
    auto_pilot = ap_mod.build_daily_picks(db)
    # Stamp the market verdict so the panel can render "Stay in cash" loud
    # when the macro is RED regardless of A+ picks existing.
    auto_pilot.market_verdict_level = market.level
    signals: list[ConvictionSignal] = []
    signals_meta: dict = {}
    if include_signals:
        signals, signals_meta = build_signals(db)
    return CockpitState(
        market=market,
        positions=positions,
        risk_budget=risk,
        cooldown=cooldown,
        edge=edge,
        auto_pilot=auto_pilot,
        signals=signals,
        signals_meta=signals_meta,
    )
