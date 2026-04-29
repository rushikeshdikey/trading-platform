"""/sectors — industry-level strength heatmap.

A complement to /sector-rotation (RRG quadrant chart). This page is the
"glance check" — at-a-glance view of which industries are strongest by
multi-timeframe returns, breadth, and how many scanner candidates are
firing in each.

Layered design:
- /sectors                → heatmap table, all sectors sorted by RS-Ratio
- /sectors/<name>         → drilldown: constituents + scanner hits in that sector
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from .. import sector_rotation as sr
from ..db import get_db
from ..deps import templates
from ..scanner import index_universe as idx_uni
from ..scanner import runner as scanner_runner
from ..scanner.patterns import SCAN_TYPES

log = logging.getLogger("journal.sectors")

router = APIRouter(prefix="/sectors")


@router.get("", response_class=HTMLResponse)
def sectors_home(request: Request, db: Session = Depends(get_db)):
    try:
        points = sr.compute_sector_strength(db)
    except Exception as exc:  # noqa: BLE001
        log.exception("sector strength failed")
        points = []

    anchor = sr.latest_anchor_date(db)
    # Sort by RS-Ratio desc — Leading sectors at the top.
    points.sort(key=lambda p: (-p.rs_ratio, p.name))

    quadrant_counts = {
        sr.QUADRANT_LEADING: 0,
        sr.QUADRANT_IMPROVING: 0,
        sr.QUADRANT_WEAKENING: 0,
        sr.QUADRANT_LAGGING: 0,
    }
    for p in points:
        quadrant_counts[p.quadrant] = quadrant_counts.get(p.quadrant, 0) + 1

    return templates.TemplateResponse(
        request,
        "sectors.html",
        {
            "points": points,
            "anchor_date": anchor,
            "quadrant_counts": quadrant_counts,
        },
    )


@router.get("/{sector_name}", response_class=HTMLResponse)
def sector_detail(request: Request, sector_name: str, db: Session = Depends(get_db)):
    """Drilldown — constituents of one sector + scanner hits, ranked by score."""
    # Decode any URL-escaped name (we use the raw sector name as the slug).
    from urllib.parse import unquote
    sector_name = unquote(sector_name)

    try:
        points = sr.compute_sector_strength(db)
    except Exception as exc:  # noqa: BLE001
        log.exception("sector strength failed in detail")
        points = []

    point = next((p for p in points if p.name == sector_name), None)
    if point is None:
        return RedirectResponse(url="/sectors?err=unknown_sector", status_code=303)

    # Constituent symbols of this sector. Match either fine industry == name
    # OR parent sector == name.
    ind_map = idx_uni.industry_map()
    constituents: list[str] = []
    for sym, ind in ind_map.items():
        if ind == sector_name or idx_uni.industry_to_sector(ind) == sector_name:
            constituents.append(sym)
    constituents = sorted(set(constituents))

    # Scanner hits for this sector. Pull from cache; map symbol → list of hits.
    cached = scanner_runner.latest_cached_all(db, max_age_minutes=72 * 60)
    sym_scans: dict[str, list[dict]] = {}
    if cached:
        results, _rows = cached
        constituent_set = set(constituents)
        for scan_type, candidates in results.items():
            label = SCAN_TYPES[scan_type][0]
            for c in candidates:
                if c.symbol in constituent_set:
                    sym_scans.setdefault(c.symbol, []).append({
                        "type": scan_type, "label": label, "score": c.score,
                        "extras": c.extras,
                    })

    # Top hits by max-scanner-score, with confluence count.
    hits = []
    for sym in constituents:
        scans = sym_scans.get(sym)
        if scans:
            hits.append({
                "symbol": sym,
                "industry": ind_map.get(sym, "—"),
                "scans": sorted(scans, key=lambda s: s["score"], reverse=True),
                "scan_count": len(scans),
                "max_score": max(s["score"] for s in scans),
                "rs_rating": (scans[0].get("extras") or {}).get("rs_rating"),
            })
    hits.sort(key=lambda h: (h["scan_count"], h["max_score"]), reverse=True)

    return templates.TemplateResponse(
        request,
        "sector_detail.html",
        {
            "point": point,
            "constituents": constituents,
            "hits": hits,
            "anchor_date": sr.latest_anchor_date(db),
        },
    )
