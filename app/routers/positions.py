"""/positions — visual card-grid view of open + closed trades.

Distinct from /trades (table-only). This page is the "what's on my desk
right now" view: top metrics strip, per-position card with mini OHLC
candlestick chart, action verdict, color-coded border by P&L.

Tabs: Active / Closed / All.

The cards reuse:
  - portfolio.build() for the metrics (already power /cockpit)
  - cockpit._action_for() for the HOLD/EXIT/TRIM verdict
  - daily_bars for 60-day mini OHLC charts (Chart.js financial plugin)
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import date, timedelta

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from .. import auth as auth_mod
from .. import calculations as calc
from .. import cockpit as cockpit_svc
from .. import portfolio as portfolio_mod
from ..db import get_db
from ..deps import templates
from ..models import DailyBar, Trade, User

log = logging.getLogger("journal.positions")

router = APIRouter(prefix="/positions")


CHART_LOOKBACK_DAYS = 75   # ~60 trading bars rendered in the mini chart


def _bars_for_symbols(db: Session, symbols: list[str]) -> dict[str, list[dict]]:
    """Bulk-load OHLC for every position, last CHART_LOOKBACK_DAYS days.
    Returns {symbol → list of {t, o, h, l, c}} where t is millis-since-epoch
    (Chart.js wants numeric x by default for time scales)."""
    if not symbols:
        return {}
    cutoff = date.today() - timedelta(days=CHART_LOOKBACK_DAYS)
    rows = (
        db.query(DailyBar)
        .filter(DailyBar.symbol.in_(symbols), DailyBar.date >= cutoff)
        .order_by(DailyBar.symbol, DailyBar.date)
        .all()
    )
    out: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        out[r.symbol].append({
            "t": int(date(r.date.year, r.date.month, r.date.day).strftime("%s")) * 1000,
            "o": round(float(r.open), 2),
            "h": round(float(r.high), 2),
            "l": round(float(r.low), 2),
            "c": round(float(r.close), 2),
        })
    return dict(out)


def _closed_summary(db: Session, lookback_days: int = 90) -> tuple[list[dict], dict]:
    """Closed trades, last ``lookback_days``. Each card lighter than open
    cards — no live CMP, just final result."""
    cutoff = date.today() - timedelta(days=lookback_days)
    closed_trades = (
        db.query(Trade)
        .filter(Trade.status == "closed")
        .filter(Trade.close_date >= cutoff)
        .order_by(Trade.close_date.desc())
        .all()
    )
    cards: list[dict] = []
    realized_total = 0.0
    wins = losses = 0
    for t in closed_trades:
        m = calc.metrics(t)
        pnl = m.pnl_rs or 0.0
        realized_total += pnl
        if pnl > 0:
            wins += 1
        elif pnl < 0:
            losses += 1
        cards.append({
            "trade_id": t.id,
            "instrument": t.instrument,
            "setup": t.setup or "—",
            "side": t.side,
            "entry_date": t.entry_date,
            "close_date": t.close_date,
            "holding_days": m.holding_days,
            "avg_entry": m.avg_entry,
            "avg_exit": m.avg_exit,
            "qty": m.total_qty,
            "pnl_rs": pnl,
            "pnl_pct": m.stock_move_pct,
            "r_multiple": m.reward_risk,
        })
    stats = {
        "count": len(cards),
        "realized_total": realized_total,
        "wins": wins,
        "losses": losses,
        "winrate": (wins / (wins + losses)) if (wins + losses) > 0 else None,
    }
    return cards, stats


@router.get("", response_class=HTMLResponse)
def positions_home(
    request: Request,
    tab: str = Query("active"),
    db: Session = Depends(get_db),
    user: User = Depends(auth_mod.require_user),
):
    """Card-grid Position Manager."""
    summary = portfolio_mod.build(db)

    # Action verdicts — same logic as cockpit's open-position table.
    verdicts: dict[int, dict] = {}
    for card in summary.cards:
        action, reason, urgency, color = cockpit_svc._action_for(card)
        verdicts[card.trade_id] = {
            "action": action,
            "reason": reason,
            "urgency": urgency,
            "color_class": color,
        }

    # OHLC for active position cards.
    active_symbols = [c.instrument for c in summary.cards]
    chart_data = _bars_for_symbols(db, active_symbols)

    closed_cards, closed_stats = ([], {"count": 0})
    closed_chart_data: dict[str, list[dict]] = {}
    if tab in ("closed", "all"):
        closed_cards, closed_stats = _closed_summary(db)
        closed_symbols = [c["instrument"] for c in closed_cards]
        closed_chart_data = _bars_for_symbols(db, closed_symbols)

    # Pct-invested headline metric — invested vs total capital ceiling.
    pct_invested = (summary.invested_rs / summary.capital) if summary.capital else 0.0

    # Pending entries (entry_mode='trigger' BUYs that haven't fired yet).
    # Surfaced as a separate strip — they have no broker position so
    # they shouldn't pollute the active grid or risk metrics.
    pending_trades = (
        db.query(Trade)
        .filter(Trade.user_id == user.id)
        .filter(Trade.status == "open")
        .filter(Trade.entry_status == "pending")
        .order_by(Trade.entry_date.desc())
        .all()
    )

    return templates.TemplateResponse(
        request,
        "positions.html",
        {
            "summary": summary,
            "verdicts": verdicts,
            "tab": tab,
            "pct_invested": pct_invested,
            "active_chart_json": json.dumps(chart_data),
            "closed_cards": closed_cards,
            "closed_stats": closed_stats,
            "closed_chart_json": json.dumps(closed_chart_data),
            "pending_trades": pending_trades,
        },
    )
