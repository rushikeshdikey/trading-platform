"""Sector Rotation (RRG) HTTP surface."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from dataclasses import asdict

from .. import sector_rotation as sr
from ..db import get_db
from ..deps import templates

log = logging.getLogger("journal.sector_rotation")

router = APIRouter(prefix="/sector-rotation")


@router.get("", response_class=HTMLResponse)
def sector_rotation_page(request: Request, db: Session = Depends(get_db)):
    try:
        points = sr.compute_rotation(db)
    except Exception as exc:  # noqa: BLE001
        log.exception("sector rotation failed")
        points = []
    anchor = sr.latest_anchor_date(db)
    quadrant_counts = {q: 0 for q in (sr.QUADRANT_LEADING, sr.QUADRANT_IMPROVING, sr.QUADRANT_WEAKENING, sr.QUADRANT_LAGGING)}
    for p in points:
        quadrant_counts[p.quadrant] = quadrant_counts.get(p.quadrant, 0) + 1
    # Pre-serialize for the template's JSON-embedded chart payload (dataclasses
    # don't go through Jinja's tojson filter cleanly).
    points_json = [asdict(p) for p in points]
    return templates.TemplateResponse(
        request,
        "sector_rotation.html",
        {
            "points": points,
            "points_json": points_json,
            "anchor_date": anchor,
            "quadrant_counts": quadrant_counts,
            "rs_ratio_window": sr.RS_RATIO_WINDOW,
            "rs_momentum_window": sr.RS_MOMENTUM_WINDOW,
        },
    )
