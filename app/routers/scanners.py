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
    from ..scanner import index_universe as idx_uni

    last_runs = scanner_runner.last_run_summary(db)
    cache = _bars_cache_size(db)

    # Universe = NSE Total Market index ∪ liquid non-index names with bars.
    # See _load_universe_and_bars in runner.py for the rules.
    try:
        universe_breakdown = scanner_runner.gated_universe_breakdown(db)
    except Exception:  # noqa: BLE001
        universe_breakdown = {"total": 0, "in_index": 0, "soft_included": 0}
    idx_count = universe_breakdown["total"]
    nse_status = idx_uni.status()

    # Read SHARED ScanCache for unified results.
    unified = _build_unified_results(db)
    capital = dash_svc.current_capital(db) if unified else 0.0

    return templates.TemplateResponse(
        request,
        "scanners.html",
        {
            "scan_types": [{"key": k, "label": v[0]} for k, v in SCAN_TYPES.items()],
            "last_runs": last_runs,
            "cache": cache,
            "idx_count": idx_count,
            "universe_breakdown": universe_breakdown,
            "nse_status": nse_status,
            "mcap_cache": fundamentals_svc.cache_stats(db),
            "mcap_refresh_status": fundamentals_svc.refresh_status(),
            "mcap_min_rs": fundamentals_svc.MIN_MARKET_CAP_RS,
            "results": None,
            "selected_scan": None,
            "unified": unified,
            "capital": capital,
        },
    )


def _build_unified_results(db: Session) -> dict | None:
    """Read ScanCache for all 4 scans and build a unified row list keyed by
    symbol — symbols hitting multiple scans get pills for each."""
    cached = scanner_runner.latest_cached_all(db, max_age_minutes=24 * 60)
    if cached is None:
        return None
    results, rows = cached

    # Watchlist for the "already watching" badge.
    all_symbols = {c.symbol for cands in results.values() for c in cands}
    on_watchlist: set[str] = set()
    if all_symbols:
        on_watchlist = {
            s for (s,) in db.query(Watchlist.symbol)
            .filter(Watchlist.symbol.in_(all_symbols))
            .all()
        }

    # Group by symbol: dict[symbol] -> {symbol, scans:[(type,label,score)],
    # max_score, primary_candidate (highest-score), sizing, on_watchlist}.
    from ..scanner.risk import size_candidate as _size_one

    grouped: dict[str, dict] = {}
    for scan_type, candidates in results.items():
        label = SCAN_TYPES[scan_type][0]
        for c in candidates:
            slot = grouped.setdefault(c.symbol, {
                "symbol": c.symbol,
                "scans": [],
                "primary": None,
                "max_score": 0.0,
            })
            slot["scans"].append({
                "type": scan_type, "label": label, "score": c.score,
                "extras": c.extras,
            })
            if c.score > slot["max_score"]:
                slot["max_score"] = c.score
                slot["primary"] = c

    # Compute capital + risk tiers ONCE per render — current_capital walks
    # the whole trade history; risk tiers do a Settings lookup. The unified
    # table sizes 100 candidates so per-row recomputation hurts.
    from ..scanner.risk import get_user_risk_tiers
    from ..scanner.sparklines import bulk_sparklines
    capital = dash_svc.current_capital(db)
    risk_low, risk_high = get_user_risk_tiers(db)

    # Bulk-build SVG sparklines for every visible symbol in one query.
    sparklines = bulk_sparklines(db, [s for s in grouped.keys()], lookback=30)

    out_rows = []
    for sym, slot in grouped.items():
        c = slot["primary"]
        sizing = _size_one(db, c, capital=capital, risk_low=risk_low, risk_high=risk_high)
        out_rows.append({
            "symbol": sym,
            "scans": sorted(slot["scans"], key=lambda s: s["score"], reverse=True),
            "scan_count": len(slot["scans"]),
            "primary": c,
            "sizing": sizing,
            "on_watchlist": sym in on_watchlist,
            "sparkline": sparklines.get(sym, ""),
            # Conviction tier: 2+ scans = A+, single scan A if score >= 60, else B
            "tier": (
                "A+" if len(slot["scans"]) >= 2
                else ("A" if c.score >= 60 else "B")
            ),
        })
    out_rows.sort(key=lambda r: (r["scan_count"], r["primary"].score), reverse=True)

    # Meta — pick the oldest run_at across the 4 scans as the "cache age".
    oldest_run_at = min(rows[st].run_at for st in rows)
    universe_size = max((rows[st].universe_size for st in rows), default=0)
    total_elapsed = sum(rows[st].elapsed_ms for st in rows)

    return {
        "rows": out_rows,
        "scan_runs": rows,           # dict[scan_type] -> ScanCache row
        "oldest_run_at": oldest_run_at,
        "universe_size": universe_size,
        "total_elapsed_ms": total_elapsed,
        "risk_low": risk_low,
        "risk_high": risk_high,
        "capital": capital,
        "scan_summary": {
            scan_type: {
                "label": SCAN_TYPES[scan_type][0],
                "count": rows[scan_type].candidates_count,
                "elapsed_ms": rows[scan_type].elapsed_ms,
            }
            for scan_type in rows
        },
    }


@router.post("/run-all", response_class=HTMLResponse)
def run_all(request: Request, db: Session = Depends(get_db)):
    """Live re-run of all 4 scanners in parallel. Results land in ScanCache
    and are visible to every user. Use this when you want fresher data than
    the EOD pre-warm cache."""
    try:
        scanner_runner.run_all_scans(db, persist=True)
    except Exception as exc:  # noqa: BLE001
        log.exception("run-all failed")
        return RedirectResponse(url=f"/scanners?error={exc}", status_code=303)
    return RedirectResponse(url="/scanners", status_code=303)


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


@router.post("/refresh-index-universe", response_class=HTMLResponse)
def refresh_index_universe(request: Request):
    """Kick off a background refresh of the NSE Total Market constituent list.
    Returns immediately so a slow / blackholed NSE call can't pin a worker
    and take the whole site down (we got bitten by this — see jobs.py)."""
    from ..scanner import index_universe as idx_uni
    started = idx_uni.start_background_refresh()
    msg = "started" if started else "already_running"
    return RedirectResponse(url=f"/scanners?idx_refresh={msg}", status_code=303)


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
    try:
        universe_breakdown = scanner_runner.gated_universe_breakdown(db)
    except Exception:  # noqa: BLE001
        universe_breakdown = {"total": 0, "in_index": 0, "soft_included": 0}
    idx_count = universe_breakdown["total"]
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
            "idx_count": idx_count,
            "universe_breakdown": universe_breakdown,
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
