"""HTTP surface for the Daily Trading Cockpit.

- GET /cockpit          → main page (instant — no scanner runs)
- GET /cockpit/signals  → HTMX partial; runs all 4 scanners + conviction scoring
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from .. import cockpit as cockpit_svc
from ..db import get_db
from ..deps import templates

router = APIRouter(prefix="/cockpit")


@router.get("", response_class=HTMLResponse)
def cockpit_home(request: Request, db: Session = Depends(get_db)):
    state = cockpit_svc.build_cockpit(db, include_signals=False)
    return templates.TemplateResponse(
        request,
        "cockpit.html",
        {"state": state},
    )


@router.get("/signals", response_class=HTMLResponse)
def cockpit_signals(request: Request, db: Session = Depends(get_db)):
    """HTMX partial — runs scanners, conviction-scores, returns the signals
    panel HTML. The main page shows a spinner until this responds."""
    signals, meta = cockpit_svc.build_signals(db)
    return templates.TemplateResponse(
        request,
        "partials/cockpit_signals.html",
        {"signals": signals, "meta": meta},
    )
