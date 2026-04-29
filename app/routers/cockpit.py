"""HTTP surface for the Daily Trading Cockpit.

- GET /cockpit  → main page (instant — reads scanner cache, never re-runs)

The old /cockpit/signals HTMX partial was removed. Auto-Pilot at the top
of /cockpit replaces it: same scanner output, ranked via composite
scoring with sector + regime context. /scanners is the place to browse
the full ranked list.
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
    state = cockpit_svc.build_cockpit(db)
    return templates.TemplateResponse(
        request,
        "cockpit.html",
        {"state": state},
    )
