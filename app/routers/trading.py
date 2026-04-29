"""Trading engine HTTP surface.

Phase E0 (read-only): /trading/kite — broker session health + holdings +
positions + GTT list + reconciliation against journal Trade rows. ZERO
write actions. Every Kite call goes through ``trading_engine.kite_audited``
so each one writes a ``broker_audit`` row.

Future phases:
  /trading/orders   (E1+: list, place, modify, cancel)
  /trading/gtts     (E1+: per-pick GTT submit, modification log)
  /trading/audit    (any phase: paginated broker_audit viewer)
  /trading/halt     (kill switch toggle)
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from .. import auth as auth_mod
from .. import kite as kite_mod
from ..db import get_db
from ..deps import templates
from ..models import Trade, User
from ..trading_engine import kite_audited

log = logging.getLogger("journal.trading")

router = APIRouter(prefix="/trading")


def _reconcile(holdings: list[dict], net_positions: list[dict],
               open_trades: list[Trade]) -> dict:
    """Cross-check journal open trades against broker state.

    Three buckets returned:
      - matched:    trade exists in broker (qty agrees within tolerance)
      - mismatched: trade exists in broker but qty differs (suspicious)
      - missing:    trade exists in journal but NOT in broker (orphan)
      - extra:      broker has a position with no journal Trade (manual?)

    Reconciliation uses tradingsymbol as the join key. Holdings (T1+) and
    net positions (intraday) are unioned — a stock you bought today shows
    up in net positions only until settlement.
    """
    # Build broker-side qty per symbol. Holdings = settled, positions.net = today.
    broker_qty: dict[str, int] = {}
    for h in holdings:
        sym = (h.get("tradingsymbol") or "").upper()
        broker_qty[sym] = broker_qty.get(sym, 0) + int(h.get("quantity") or 0)
    for p in net_positions:
        sym = (p.get("tradingsymbol") or "").upper()
        broker_qty[sym] = broker_qty.get(sym, 0) + int(p.get("quantity") or 0)

    # Journal-side qty per symbol = sum(initial_qty + pyramids) - sum(exits).
    journal_qty: dict[str, int] = {}
    for t in open_trades:
        held = t.initial_qty + sum(p.qty for p in t.pyramids)
        held -= sum(e.qty for e in t.exits)
        journal_qty[t.instrument.upper()] = journal_qty.get(t.instrument.upper(), 0) + held

    matched, mismatched, missing, extra = [], [], [], []
    all_syms = set(broker_qty) | set(journal_qty)
    for sym in sorted(all_syms):
        b = broker_qty.get(sym, 0)
        j = journal_qty.get(sym, 0)
        if b == j and b > 0:
            matched.append({"symbol": sym, "qty": b})
        elif b > 0 and j > 0:
            mismatched.append({"symbol": sym, "broker_qty": b, "journal_qty": j})
        elif j > 0:
            missing.append({"symbol": sym, "journal_qty": j})
        elif b > 0:
            extra.append({"symbol": sym, "broker_qty": b})

    return {
        "matched": matched,
        "mismatched": mismatched,
        "missing": missing,
        "extra": extra,
        "total_broker_symbols": len([s for s in broker_qty if broker_qty[s] > 0]),
        "total_journal_symbols": len([s for s in journal_qty if journal_qty[s] > 0]),
    }


@router.get("/kite", response_class=HTMLResponse)
def kite_dashboard(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(auth_mod.require_user),
):
    """Phase E0 dashboard. Read-only broker mirror."""
    auth = kite_mod.auth_status(user)
    profile, margins, holdings, positions, gtts = {}, {}, [], {"net": [], "day": []}, []
    fetch_error: str | None = None

    if auth["authed"]:
        try:
            profile = kite_audited.fetch_profile(db, user)
            margins = kite_audited.fetch_margins(db, user)
            holdings = kite_audited.fetch_holdings(db, user)
            positions = kite_audited.fetch_positions(db, user)
            gtts = kite_audited.fetch_gtts(db, user)
        except Exception as exc:  # noqa: BLE001
            log.exception("kite read-only fetch failed")
            fetch_error = f"{type(exc).__name__}: {exc}"

    # Reconcile against journal open trades.
    open_trades = (
        db.query(Trade)
        .filter(Trade.status == "open")
        .all()
    )
    # Eager-load pyramids/exits so .qty access doesn't N+1.
    for t in open_trades:
        _ = t.pyramids
        _ = t.exits
    reconciliation = _reconcile(holdings, positions["net"], open_trades)

    # Recent audit log entries for this user (last 20).
    from ..models import BrokerAudit
    recent_audit = (
        db.query(BrokerAudit)
        .filter(BrokerAudit.user_id == user.id)
        .order_by(BrokerAudit.created_at.desc())
        .limit(20)
        .all()
    )

    return templates.TemplateResponse(
        request,
        "trading/kite.html",
        {
            "auth": auth,
            "profile": profile,
            "margins": margins,
            "holdings": holdings,
            "positions": positions,
            "gtts": gtts,
            "reconciliation": reconciliation,
            "fetch_error": fetch_error,
            "recent_audit": recent_audit,
        },
    )
