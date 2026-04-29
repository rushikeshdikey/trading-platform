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

import json
import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from datetime import datetime, timedelta

from fastapi import Form, HTTPException
from fastapi.responses import RedirectResponse

from .. import auth as auth_mod
from .. import dashboard as dash_svc
from .. import kite as kite_mod
from ..db import get_db
from ..deps import templates
from ..models import BrokerAudit, Trade, User
from ..trading_engine import kite_audited

log = logging.getLogger("journal.trading")

router = APIRouter(prefix="/trading")

# Phase E1 hard daily cap on cumulative new risk. Refuses GTT submissions
# once the sum of (entry - stop) × qty for *today's* placed GTTs exceeds
# this fraction of total capital. Server-side enforcement so the user
# can't override even by forging the form.
DAILY_RISK_CAP_PCT = 0.025   # 2.5% of capital — see user_north_star memory


# Symbol-alias loader — maps journal-symbol → broker-symbol so SSEGL in
# the journal reconciles against SSEGL-SM at the broker. File is hand-
# curated (data/symbol_aliases.csv); reload on mtime change.

from pathlib import Path as _Path
import csv as _csv

_ALIASES_FILE = _Path(__file__).resolve().parent.parent.parent / "data" / "symbol_aliases.csv"
_alias_cache: dict[str, str] | None = None
_alias_mtime: float = 0.0


def _load_aliases() -> dict[str, str]:
    """Return {journal_symbol → broker_symbol} dict. Empty if file missing."""
    global _alias_cache, _alias_mtime
    if not _ALIASES_FILE.exists():
        return {}
    mtime = _ALIASES_FILE.stat().st_mtime
    if _alias_cache is not None and mtime == _alias_mtime:
        return _alias_cache
    out: dict[str, str] = {}
    try:
        with _ALIASES_FILE.open() as f:
            for row in _csv.reader(f):
                if not row or len(row) < 2:
                    continue
                first = row[0].strip()
                if not first or first.startswith("#") or first.lower() == "journal_symbol":
                    continue
                journal_sym = first.upper()
                broker_sym = row[1].strip().upper()
                if journal_sym and broker_sym:
                    out[journal_sym] = broker_sym
    except Exception as exc:  # noqa: BLE001
        log.warning("symbol_aliases load failed: %s", exc)
        return _alias_cache or {}
    _alias_cache = out
    _alias_mtime = mtime
    return out


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
    # Symbol aliases: journal-symbol → broker-symbol. SSEGL in journal but
    # SSEGL-SM at broker is the same stock. Apply forward (journal → broker)
    # AND reverse (broker → journal) so either-direction lookups merge cleanly.
    aliases = _load_aliases()
    reverse_aliases = {v: k for k, v in aliases.items()}

    def _canonical(sym: str) -> str:
        """Normalise a symbol to its journal-side canonical form, so two
        rows that are actually the same stock collapse into one bucket."""
        s = (sym or "").upper()
        return reverse_aliases.get(s, s)

    # Build broker-side qty per symbol. Holdings = settled, positions.net = today.
    broker_qty: dict[str, int] = {}
    for h in holdings:
        sym = _canonical(h.get("tradingsymbol") or "")
        broker_qty[sym] = broker_qty.get(sym, 0) + int(h.get("quantity") or 0)
    for p in net_positions:
        sym = _canonical(p.get("tradingsymbol") or "")
        broker_qty[sym] = broker_qty.get(sym, 0) + int(p.get("quantity") or 0)

    # Journal-side qty per symbol = sum(initial_qty + pyramids) - sum(exits).
    journal_qty: dict[str, int] = {}
    for t in open_trades:
        held = t.initial_qty + sum(p.qty for p in t.pyramids)
        held -= sum(e.qty for e in t.exits)
        sym = _canonical(t.instrument)
        journal_qty[sym] = journal_qty.get(sym, 0) + held

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


# ---------------------------------------------------------------------------
# Phase E1 — one-click GTT submit from Auto-Pilot picks
# ---------------------------------------------------------------------------


def _today_placed_risk(db: Session, user: User) -> float:
    """Sum of (entry - stop) × qty across all GTT placements made today
    by this user. Read from broker_audit — that's the canonical record
    of every Kite call we made.
    """
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    rows = (
        db.query(BrokerAudit)
        .filter(
            BrokerAudit.user_id == user.id,
            BrokerAudit.endpoint == "place_gtt",
            BrokerAudit.created_at >= today_start,
            BrokerAudit.status == 200,
        )
        .all()
    )
    total_risk = 0.0
    for r in rows:
        try:
            payload = json.loads(r.request_json or "{}")
            tv = payload.get("trigger_values") or []
            orders = payload.get("orders") or []
            if not tv or not orders:
                continue
            qty = orders[0].get("quantity") or 0
            last_price = payload.get("last_price") or 0
            stop = min(tv) if tv else 0
            # For BUY: risk = (last_price - stop) × qty; entry ≈ last_price.
            risk = max(0.0, (last_price - stop) * qty)
            total_risk += risk
        except Exception:  # noqa: BLE001
            continue
    return total_risk


@router.post("/gtt/submit")
def submit_gtt(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(auth_mod.require_user),
    symbol: str = Form(...),
    qty: int = Form(...),
    entry_price: float = Form(...),
    stop_price: float = Form(...),
    target_price: float = Form(...),
    transaction_type: str = Form("BUY"),
    auto_pilot_rank: str = Form(""),  # cosmetic, for the audit comment
):
    """Phase E1: place a GTT-OCO bracket from a Cockpit Auto-Pilot pick.

    Server-side guards (cannot be bypassed by tampering with the form):
      1. User must be Kite-authed
      2. qty > 0, prices in correct order
      3. Cumulative day risk + this trade's risk ≤ DAILY_RISK_CAP_PCT × capital
      4. Reconciliation matched-or-extra count (we refuse to place new
         orders if the prior state has unresolved discrepancies > N)
    """
    auth = kite_mod.auth_status(user)
    if not auth["authed"]:
        return RedirectResponse(
            url="/trading/kite?gtt_err=not_authed", status_code=303,
        )

    # Daily-cap check (server-side, hard).
    capital = dash_svc.current_capital(db)
    proposed_risk = max(0.0, (entry_price - stop_price) * qty) if transaction_type == "BUY" \
        else max(0.0, (stop_price - entry_price) * qty)
    today_risk = _today_placed_risk(db, user)
    cap_rs = capital * DAILY_RISK_CAP_PCT
    if today_risk + proposed_risk > cap_rs:
        return RedirectResponse(
            url=(
                f"/cockpit?gtt_err=daily_cap_exceeded"
                f"&cap=₹{int(cap_rs)}"
                f"&already=₹{int(today_risk)}"
                f"&proposed=₹{int(proposed_risk)}"
            ),
            status_code=303,
        )

    try:
        resp = kite_audited.place_gtt_oco(
            db, user,
            symbol=symbol, qty=qty,
            entry_price=entry_price,
            stop_price=stop_price,
            target_price=target_price,
            transaction_type=transaction_type,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("place_gtt failed")
        from urllib.parse import quote
        return RedirectResponse(
            url=f"/cockpit?gtt_err={quote(str(exc))[:200]}",
            status_code=303,
        )

    trigger_id = resp.get("trigger_id") if isinstance(resp, dict) else None
    return RedirectResponse(
        url=f"/cockpit?gtt_ok=1&symbol={symbol}&trigger_id={trigger_id or 'unknown'}",
        status_code=303,
    )


@router.post("/gtt/{trigger_id}/cancel")
def cancel_gtt(
    request: Request,
    trigger_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(auth_mod.require_user),
):
    """Cancel a GTT trigger by ID. Refuses if not Kite-authed."""
    auth = kite_mod.auth_status(user)
    if not auth["authed"]:
        return RedirectResponse(url="/trading/kite", status_code=303)
    try:
        kite_audited.cancel_gtt(db, user, trigger_id)
    except Exception as exc:  # noqa: BLE001
        log.exception("cancel_gtt failed")
        from urllib.parse import quote
        return RedirectResponse(
            url=f"/trading/kite?gtt_err={quote(str(exc))[:200]}",
            status_code=303,
        )
    return RedirectResponse(url="/trading/kite?gtt_cancelled=1", status_code=303)
