"""Public /status page — uptime/downtime timeline.

No auth required (intentional). Even if login is broken, you can hit
this URL from outside and see whether the app is up.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from .. import health_monitor as hm
from ..db import get_db
from ..deps import templates

router = APIRouter(prefix="/status")


@router.get("", response_class=HTMLResponse)
def status_page(request: Request, db: Session = Depends(get_db)):
    summary = hm.build_summary(db)
    return templates.TemplateResponse(
        request, "status.html", {"summary": summary},
    )
