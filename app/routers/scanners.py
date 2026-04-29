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
from ..scanner.risk import get_user_risk_tiers, size_candidate

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

    from ..scanner import bars_cache as bc

    last_runs = scanner_runner.last_run_summary(db)
    cache = _bars_cache_size(db)
    bars_refresh_state = bc.refresh_status()
    scan_run_state = scanner_runner.scan_status()

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
            "bars_refresh_state": bars_refresh_state,
            "scan_run_state": scan_run_state,
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


# Tier algorithm has moved to ``app.scanner.scoring`` — composite-score
# based, replacing the rigid "≥3 scanners = A+" rule. Kept this file's
# router code thin: gather inputs, call composite_score, render.
from ..scanner import scoring as scoring_mod
from .. import breadth as breadth_mod
from .. import sector_rotation as sector_rotation_mod


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

    # Composite-scoring inputs — computed ONCE for all rows.
    # 1. Regime context from the latest "all"-universe breadth row.
    breadth_row = breadth_mod.latest(db, universe="all")
    mood = breadth_mod.mood_score(breadth_row) if breadth_row is not None else {"score": None}
    regime = scoring_mod.regime_multiplier_from_breadth(
        mood_score=mood.get("score") if mood else None,
        pct_above_50ema=float(breadth_row.pct_above_50ema) if breadth_row else None,
        pct_above_200ema=float(breadth_row.pct_above_200ema) if breadth_row else None,
    )
    # 2. Symbol → sector quadrant (Leading/Improving/Weakening/Lagging).
    quadrant_map = sector_rotation_mod.symbol_quadrant_map(db)

    out_rows = []
    for sym, slot in grouped.items():
        c = slot["primary"]
        sizing = _size_one(db, c, capital=capital, risk_low=risk_low, risk_high=risk_high)
        rs_rating = (c.extras or {}).get("rs_rating") if c.extras else None
        breakdown = scoring_mod.composite_score(
            scans=slot["scans"],
            rs_rating=rs_rating,
            sector_quadrant=quadrant_map.get(sym),
            regime=regime,
        )
        out_rows.append({
            "symbol": sym,
            "scans": sorted(slot["scans"], key=lambda s: s["score"], reverse=True),
            "scan_count": len(slot["scans"]),
            "primary": c,
            "sizing": sizing,
            "on_watchlist": sym in on_watchlist,
            "sparkline": sparklines.get(sym, ""),
            "tier": breakdown.tier,
            "tier_reason": breakdown.reason,
            "composite": round(breakdown.composite, 1),
            "composite_breakdown": breakdown,
            "sector_quadrant": quadrant_map.get(sym),
        })
    # Sort by composite score desc — the new canonical ranking.
    out_rows.sort(key=lambda r: r["composite"], reverse=True)

    # Meta — pick the oldest run_at across the 4 scans as the "cache age".
    oldest_run_at = min(rows[st].run_at for st in rows)
    universe_size = max((rows[st].universe_size for st in rows), default=0)
    total_elapsed = sum(rows[st].elapsed_ms for st in rows)

    # Cache age in hours — used by the template to flag staleness loudly.
    # The page text on the home banner promises "auto-refreshed at 15:35
    # IST every weekday", which is a lie when the scheduler missed (laptop
    # asleep, deploy gap, uvicorn reload kill). Surface the truth.
    from datetime import datetime as _dt
    cache_age_hours = (_dt.utcnow() - oldest_run_at).total_seconds() / 3600.0

    return {
        "rows": out_rows,
        "scan_runs": rows,           # dict[scan_type] -> ScanCache row
        "oldest_run_at": oldest_run_at,
        "universe_size": universe_size,
        "total_elapsed_ms": total_elapsed,
        "risk_low": risk_low,
        "risk_high": risk_high,
        "capital": capital,
        "regime": regime,
        "sector_tagged_count": len(quadrant_map),
        "cache_age_hours": round(cache_age_hours, 1),
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
    """Kick off all 7 scanners in a daemon thread and return immediately.

    Synchronous run on the deep bars cache (4500 symbols × 252 bars × 7
    detectors) routinely exceeded the gunicorn 180s worker timeout —
    same shape as the bars-refresh issue. Background-thread it; user
    sees a "scan in progress" banner and reloads to see fresh results.
    """
    user = getattr(request.state, "user", None)
    user_id = user.id if user else None
    started = scanner_runner.start_background_scan(user_id=user_id)
    msg = "started" if started else "already_running"
    return RedirectResponse(url=f"/scanners?scan={msg}", status_code=303)


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


@router.get("/ipos", response_class=HTMLResponse)
def ipos_page(request: Request, db: Session = Depends(get_db)):
    """Stocks listed in the last 4 quarters — surfaces fresh IPOs the
    scanners can't see yet (their detectors need 120 bars of history)."""
    from ..scanner import ipos as ipos_svc

    entries = ipos_svc.recent_ipos(db)
    cache = _bars_cache_size(db)
    return templates.TemplateResponse(
        request,
        "ipos.html",
        {
            "entries": entries,
            "cache": cache,
            "max_bars": ipos_svc.IPO_MAX_BARS,
        },
    )


@router.post("/refresh-bars", response_class=HTMLResponse)
def refresh_bars(request: Request, db: Session = Depends(get_db)):
    """Kick off a bhavcopy refresh in a daemon thread and return immediately.

    The refresh hits NSE + BSE archives day-by-day for up to 180 days — a
    cold start can take 5-15 minutes and routinely exceeds gunicorn's
    request timeout. Pinning the worker = the whole site goes down. So we
    background-thread it and let progress show up via the cache stats on
    page reload (Bars count + latest_date will tick up).
    """
    from ..scanner import bars_cache as bc

    # 380 calendar days = ~252 trading bars — needed for Minervini Trend
    # Template's 52-week measures and the RS Rating's 12-month return window.
    # First-time refresh against an empty/shallow cache takes ~20-40 minutes.
    started = bc.start_background_refresh(lookback_days=380)
    msg = "started" if started else "already_running"
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
    # Pre-fetch capital + risk tiers ONCE — size_candidate's defaults would
    # otherwise call current_capital(db) (full build_year walk) per candidate.
    capital = dash_svc.current_capital(db)
    risk_low, risk_high = get_user_risk_tiers(db)
    rows = []
    for c in candidates:
        sizing = size_candidate(
            db, c, capital=capital, risk_low=risk_low, risk_high=risk_high,
        )
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
