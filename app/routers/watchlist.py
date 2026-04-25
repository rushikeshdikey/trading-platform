"""Watchlist CRUD.

- GET  /watchlist                    → list, grouped by setup_label.
- POST /watchlist/add                → add one (from scanner results or manual).
- POST /watchlist/{id}/delete        → remove.
- POST /watchlist/{id}/convert-to-trade → 303 → /trades/new?… prefilled.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session
from urllib.parse import urlencode

from ..db import get_db
from ..deps import templates
from ..models import Watchlist

router = APIRouter(prefix="/watchlist")


@router.get("", response_class=HTMLResponse)
def watchlist_home(request: Request, db: Session = Depends(get_db)):
    rows = db.query(Watchlist).order_by(Watchlist.added_at.desc()).all()
    # Group for the template
    groups: dict[str, list[Watchlist]] = {}
    for r in rows:
        groups.setdefault(r.setup_label or "Manual", []).append(r)
    return templates.TemplateResponse(
        request,
        "watchlist.html",
        {"groups": groups, "total": len(rows)},
    )


@router.post("/add")
def add(
    db: Session = Depends(get_db),
    symbol: str = Form(...),
    setup_label: str | None = Form(None),
    alert_price: float | None = Form(None),
    suggested_sl: float | None = Form(None),
    notes: str | None = Form(None),
    return_to: str | None = Form(None),
):
    sym = symbol.strip().upper()
    existing = db.query(Watchlist).filter(Watchlist.symbol == sym).first()
    if existing is None:
        db.add(
            Watchlist(
                symbol=sym,
                setup_label=setup_label or None,
                alert_price=alert_price,
                suggested_sl=suggested_sl,
                notes=notes or None,
            )
        )
    else:
        # Update with fresher signal.
        if setup_label:
            existing.setup_label = setup_label
        if alert_price is not None:
            existing.alert_price = alert_price
        if suggested_sl is not None:
            existing.suggested_sl = suggested_sl
        if notes:
            existing.notes = notes
    db.commit()
    return RedirectResponse(url=return_to or "/watchlist", status_code=303)


@router.post("/bulk-add")
def bulk_add(
    request: Request,
    db: Session = Depends(get_db),
    symbols: str = Form(...),
    setup_label: str | None = Form(None),
    return_to: str | None = Form(None),
):
    """Add many symbols at once. ``symbols`` is comma-separated, may contain
    whitespace and `NSE:`/`BSE:` prefixes which we strip. Skips duplicates."""
    raw = [s.strip() for s in symbols.split(",") if s.strip()]
    cleaned = []
    for s in raw:
        # Strip exchange prefix if present (NSE:RELIANCE → RELIANCE).
        if ":" in s:
            s = s.split(":", 1)[1]
        s = s.upper()
        if s and s not in cleaned:
            cleaned.append(s)

    existing = {
        s for (s,) in db.query(Watchlist.symbol).filter(Watchlist.symbol.in_(cleaned)).all()
    }
    added = 0
    for sym in cleaned:
        if sym in existing:
            continue
        db.add(Watchlist(symbol=sym, setup_label=setup_label or None))
        added += 1
    db.commit()

    target = return_to or "/watchlist"
    sep = "&" if "?" in target else "?"
    return RedirectResponse(
        url=f"{target}{sep}added={added}&skipped={len(cleaned) - added}",
        status_code=303,
    )


@router.post("/{wid}/delete")
def delete(wid: int, db: Session = Depends(get_db)):
    row = db.get(Watchlist, wid)
    if row is not None:
        db.delete(row)
        db.commit()
    return RedirectResponse(url="/watchlist", status_code=303)


@router.post("/{wid}/convert-to-trade")
def convert_to_trade(wid: int, db: Session = Depends(get_db)):
    row = db.get(Watchlist, wid)
    if row is None:
        raise HTTPException(404)
    params: dict[str, str] = {"instrument": row.symbol}
    if row.alert_price is not None:
        params["entry_price"] = str(row.alert_price)
    if row.suggested_sl is not None:
        params["sl"] = str(row.suggested_sl)
    if row.setup_label:
        params["setup"] = row.setup_label
    return RedirectResponse(url=f"/trades/new?{urlencode(params)}", status_code=303)
