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
    # Phase C — parse ?override=SYMBOL:EntryType params (multi-valued).
    # E.g. /cockpit?override=BHARATFORG:Pullback&override=PAISALO:PDH
    overrides: dict[str, str] = {}
    for raw in request.query_params.getlist("override"):
        if ":" in raw:
            sym, _, etype = raw.partition(":")
            sym = sym.strip().upper()
            etype = etype.strip()
            if sym and etype:
                overrides[sym] = etype
    state = cockpit_svc.build_cockpit(db, entry_overrides=overrides)
    return templates.TemplateResponse(
        request,
        "cockpit.html",
        {"state": state, "entry_overrides": overrides},
    )
