"""HTTP surface for the scanner.

- GET  /scanners              → landing page: pick a scanner, see last-run metadata.
- POST /scanners/refresh-bars → seed/update the bhavcopy cache. Synchronous; first
                                run is slow (~30 min cold), subsequent incremental
                                refreshes are seconds.
- POST /scanners/run          → run one of the three scans synchronously; shows
                                results inline.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from .. import dashboard as dash_svc
from .. import settings as app_settings
from ..db import get_db
from ..deps import templates
from ..models import DailyBar, Watchlist
from ..scanner import fundamentals as fundamentals_svc
from ..scanner import runner as scanner_runner
from ..scanner import universe as universe_mod
from ..scanner.patterns import SCAN_TYPES
from ..scanner.risk import size_candidate

log = logging.getLogger("journal.scanners")

router = APIRouter(prefix="/scanners")


def _bars_cache_size(db: Session) -> dict:
    from sqlalchemy import func

    total = db.query(func.count(DailyBar.id)).scalar() or 0
    symbols = db.query(func.count(func.distinct(DailyBar.symbol))).scalar() or 0
    latest = db.query(func.max(DailyBar.date)).scalar()
    return {
        "rows": total,
        "symbols": symbols,
        "latest_date": latest.isoformat() if latest else None,
    }


@router.get("", response_class=HTMLResponse)
def scanners_home(request: Request, db: Session = Depends(get_db)):
    last_runs = scanner_runner.last_run_summary(db)
    cache = _bars_cache_size(db)
    mcap_cache = fundamentals_svc.cache_stats(db)
    return templates.TemplateResponse(
        request,
        "scanners.html",
        {
            "scan_types": [
                {"key": k, "label": v[0]} for k, v in SCAN_TYPES.items()
            ],
            "last_runs": last_runs,
            "cache": cache,
            "mcap_cache": mcap_cache,
            "mcap_refresh_status": fundamentals_svc.refresh_status(),
            "mcap_min_rs": fundamentals_svc.MIN_MARKET_CAP_RS,
            "results": None,
            "selected_scan": None,
        },
    )


@router.post("/refresh-fundamentals", response_class=HTMLResponse)
def refresh_fundamentals(
    request: Request,
    db: Session = Depends(get_db),
    force: bool = False,
):
    """Kick off a background thread to refresh market caps for the current
    universe. Returns immediately; progress is visible via the cache counts."""
    symbols = universe_mod.universe_from_cache(db)
    if not symbols:
        return RedirectResponse(
            url="/scanners?refresh_fund_error=No+symbols+in+bars+cache+yet",
            status_code=303,
        )
    started = fundamentals_svc.start_background_refresh(symbols, force=force)
    msg = "started" if started else "already_running"
    return RedirectResponse(url=f"/scanners?refresh_fund={msg}", status_code=303)


@router.post("/refresh-bars", response_class=HTMLResponse)
def refresh_bars(request: Request, db: Session = Depends(get_db)):
    """Synchronous bhavcopy refresh. First run pulls 180 calendar days."""
    summary = scanner_runner.refresh_bars_cache(db, lookback_days=180)
    msg = (
        f"downloaded={summary.days_downloaded} "
        f"skipped_existing={summary.days_skipped_existing} "
        f"failed={summary.days_failed} "
        f"rows_added={summary.rows_upserted}"
    )
    return RedirectResponse(url=f"/scanners?refresh={msg}", status_code=303)


@router.post("/run", response_class=HTMLResponse)
def run_scan(
    request: Request,
    db: Session = Depends(get_db),
    scan_type: str = Form(...),
):
    if scan_type not in SCAN_TYPES:
        return RedirectResponse(url="/scanners?error=unknown_scan", status_code=303)
    try:
        candidates, run = scanner_runner.run_scan(db, scan_type)
    except Exception as exc:  # noqa: BLE001
        log.exception("scan failed")
        return RedirectResponse(url=f"/scanners?error={exc}", status_code=303)

    # Enrich each candidate with risk-sized qty; also mark whether it's already
    # on the watchlist so the UI can show "Already watching".
    existing = {
        s
        for (s,) in db.query(Watchlist.symbol)
        .filter(Watchlist.symbol.in_([c.symbol for c in candidates]))
        .all()
    }
    rows = []
    for c in candidates:
        sizing = size_candidate(db, c)
        rows.append(
            {
                "candidate": c,
                "sizing": sizing,
                "on_watchlist": c.symbol in existing,
            }
        )

    last_runs = scanner_runner.last_run_summary(db)
    cache = _bars_cache_size(db)
    mcap_cache = fundamentals_svc.cache_stats(db)
    scan_label = SCAN_TYPES[scan_type][0]
    capital = dash_svc.current_capital(db)
    default_risk = app_settings.get_float(db, "default_risk_pct", 0.005)

    return templates.TemplateResponse(
        request,
        "scanners.html",
        {
            "scan_types": [
                {"key": k, "label": v[0]} for k, v in SCAN_TYPES.items()
            ],
            "last_runs": last_runs,
            "cache": cache,
            "mcap_cache": mcap_cache,
            "mcap_refresh_status": fundamentals_svc.refresh_status(),
            "mcap_min_rs": fundamentals_svc.MIN_MARKET_CAP_RS,
            "results": rows,
            "selected_scan": {"key": scan_type, "label": scan_label},
            "run_meta": {
                "universe_size": run.universe_size,
                "candidates_count": run.candidates_count,
                "elapsed_ms": run.elapsed_ms,
            },
            "capital": capital,
            "default_risk": default_risk,
        },
    )
