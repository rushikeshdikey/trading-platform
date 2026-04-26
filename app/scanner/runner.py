"""Scanner orchestrator.

Three public entry points:

- ``run_scan(db, scan_type)`` — runs ONE detector. Used by the existing
  per-scanner card flow and by the cockpit.
- ``run_all_scans(db)`` — loads bars once, runs all 4 detectors in parallel,
  returns a flat list of candidates plus per-scan timing. The fast path.
- ``latest_cached_run(db, scan_type, max_age_minutes)`` — reads the most
  recent ``ScanRun.payload`` JSON if it's fresh, so /scanners can render
  in 50ms instead of re-scanning.

The single-source-of-truth for "what does a scan produce" is the
``Candidate`` dataclass in ``patterns``. Both code paths (live + cache)
return ``Candidate`` so the UI doesn't have to care which produced it.
"""
from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import date as _date, datetime, timedelta
from typing import Iterable

from sqlalchemy.orm import Session

from ..models import ScanCache, ScanRun
from . import bars_cache
from . import fundamentals
from . import universe as universe_mod
from .patterns import Candidate, SCAN_TYPES

log = logging.getLogger("journal.scanner.runner")

TOP_N_RESULTS = 100


# -- Shared loader ----------------------------------------------------------


def _load_universe_and_bars(db: Session) -> tuple[list[str], dict[str, list]]:
    """Resolve the scan universe and bulk-load bars for it.

    Universe = every symbol in the bars cache, minus ETFs / debt funds.
    No market-cap gate, no NSE-index-list gate. We trust:

      1. The bhavcopy itself — only NSE-listed EQ + BE securities.
      2. ``universe_mod.universe_from_cache`` — strips ETFs/BEES/liquid funds
         by symbol regex + Kite instrument-name tokens.
      3. The detectors — each has its own MIN_BARS / MIN_PRICE / MIN_ADV20_RS
         quality gate (see ``patterns.py``), so a stock with 30 bars or
         ₹50 lakh/day turnover gets filtered there, not here.

    Removing the universe-level gate exposed real candidates that NSE's
    Total Market index didn't include (e.g. STYLAMIND). The price is a
    bigger inner loop, but at ~4500 symbols × 4 detectors the run still
    completes in single-digit seconds.
    """
    symbols = universe_mod.universe_from_cache(db)
    # 380 calendar days ≈ 252 trading days — enough for the Minervini Trend
    # Template's 52-week measures. Cheaper detectors slice down to their own
    # smaller windows internally.
    bars_map = bars_cache.bars_by_symbol(db, symbols, lookback_days=380)
    return symbols, bars_map


_breakdown_cache: dict = {"at": 0.0, "value": None}
_BREAKDOWN_TTL_S = 300  # 5 minutes — funnel doesn't change minute-to-minute


def gated_universe_breakdown(db: Session) -> dict:
    """Funnel breakdown for the /scanners status panel — makes the
    "0 candidates" failure mode visible instead of mysterious.

    Cached for 5 minutes because the underlying work (load 4500 symbols'
    bars, count gates) is multi-second on a small VM and was pinning
    gunicorn workers when called per-request. The funnel changes only
    when the bars cache changes (refresh runs in background), so a stale
    read for a few minutes is harmless.
    """
    import time
    now = time.time()
    cached = _breakdown_cache
    if cached["value"] is not None and (now - cached["at"]) < _BREAKDOWN_TTL_S:
        return cached["value"]

    from .patterns import MIN_ADV20_RS, MIN_BARS, MIN_PRICE

    symbols = universe_mod.universe_from_cache(db)
    bars_map = bars_cache.bars_by_symbol(db, symbols)

    n_total = len(symbols)
    n_have_bars = sum(1 for s in symbols if bars_map.get(s))
    n_min_bars = 0
    n_min_price = 0
    n_min_adv20 = 0
    for s in symbols:
        bars = bars_map.get(s) or []
        if len(bars) < MIN_BARS:
            continue
        n_min_bars += 1
        if bars[-1].close < MIN_PRICE:
            continue
        n_min_price += 1
        last20 = bars[-20:]
        adv20 = sum(b.close * b.volume for b in last20) / len(last20)
        if adv20 >= MIN_ADV20_RS:
            n_min_adv20 += 1

    value = {
        "total": n_total,
        "have_bars": n_have_bars,
        "min_bars": n_min_bars,
        "min_price": n_min_price,
        "min_adv20": n_min_adv20,
        "min_bars_threshold": MIN_BARS,
        "min_price_threshold": MIN_PRICE,
        "min_adv20_threshold_cr": MIN_ADV20_RS / 1e7,
    }
    _breakdown_cache["at"] = now
    _breakdown_cache["value"] = value
    return value


def _detect_one(
    scan_type: str,
    symbols: list[str],
    bars_map: dict[str, list],
    rs_ratings: dict[str, int] | None = None,
) -> list[Candidate]:
    _, detector = SCAN_TYPES[scan_type]
    rs_ratings = rs_ratings or {}
    from . import tight_sl as tsl
    out: list[Candidate] = []
    for sym in symbols:
        bars = bars_map.get(sym) or []
        if not bars:
            continue
        try:
            c = detector(sym, bars, rs_rating=rs_ratings.get(sym))
        except Exception as exc:  # noqa: BLE001 — never let one bad symbol kill the scan
            log.debug("detector %s failed on %s: %s", scan_type, sym, exc)
            continue
        if c is None:
            continue

        # Replace each detector's home-grown SL with a unified picker —
        # PDL by default (the canonical Indian swing SL), or a tighter
        # 3-bar-low / 2×ATR(7) stop when the chart genuinely allows it.
        # Never rejects the candidate — the trader sees the SL% and
        # decides per-trade whether to take it.
        sl = tsl.compute_tight_sl(bars, c.suggested_entry or c.close)
        c.suggested_sl = sl.price
        c.extras["sl_method"] = sl.method
        c.extras["sl_pct"] = round(sl.sl_pct * 100, 2)

        # Attach RS rating to every candidate's extras for display,
        # whether the detector used it as a gate or not.
        rating = rs_ratings.get(sym)
        if rating is not None and "rs_rating" not in c.extras:
            c.extras["rs_rating"] = rating
        out.append(c)
    out.sort(key=lambda c: c.score, reverse=True)
    return out[:TOP_N_RESULTS]


# -- Single-scan entry point (kept for the cockpit + per-card UI) -----------


def run_scan(db: Session, scan_type: str) -> tuple[list[Candidate], ScanRun]:
    if scan_type not in SCAN_TYPES:
        raise ValueError(f"Unknown scan_type: {scan_type}")

    started = time.perf_counter()
    symbols, bars_map = _load_universe_and_bars(db)
    from . import rs_rating as rs
    rs_ratings = rs.compute_ratings(db, symbols)
    top = _detect_one(scan_type, symbols, bars_map, rs_ratings=rs_ratings)
    elapsed_ms = int((time.perf_counter() - started) * 1000)

    now = datetime.utcnow()
    run = ScanRun(
        run_at=now, scan_type=scan_type,
        universe_size=len(symbols), candidates_count=len(top),
        elapsed_ms=elapsed_ms, bars_refreshed=0,
    )
    db.add(run)
    _upsert_scan_cache(db, scan_type, len(symbols), len(top), elapsed_ms, top, now)
    db.commit()
    return top, run


# -- All-scans-in-one entry point (the fast path) ---------------------------


def run_all_scans(
    db: Session, persist: bool = True,
) -> tuple[dict[str, list[Candidate]], dict[str, int], int, int]:
    """Run all 4 detectors against the same loaded bars in parallel.

    Returns:
        results        — {scan_type: [Candidate]}
        per_scan_ms    — {scan_type: elapsed_ms}
        total_ms       — wall-clock for the whole batch (load + parallel detect)
        universe_size  — symbols that passed the mcap gate

    When ``persist`` is True (default), the shared ``ScanCache`` rows are
    upserted AND a per-user ``ScanRun`` history row is added (only if the
    contextvar is set; pre-warm calls with persist=False).
    """
    started = time.perf_counter()
    symbols, bars_map = _load_universe_and_bars(db)
    from . import rs_rating as rs
    rs_ratings = rs.compute_ratings(db, symbols)
    load_ms = int((time.perf_counter() - started) * 1000)

    results: dict[str, list[Candidate]] = {}
    per_scan_ms: dict[str, int] = {}

    # All detectors, parallel threads. Detectors release the GIL during numpy
    # work so this gives real parallelism on multi-core machines, and at worst
    # is no slower than sequential. RS ratings are precomputed once and shared.
    detect_started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=len(SCAN_TYPES)) as ex:
        future_to_type = {
            ex.submit(_detect_one_timed, scan_type, symbols, bars_map, rs_ratings): scan_type
            for scan_type in SCAN_TYPES
        }
        for future in as_completed(future_to_type):
            scan_type = future_to_type[future]
            top, ms = future.result()
            results[scan_type] = top
            per_scan_ms[scan_type] = ms
    detect_ms = int((time.perf_counter() - detect_started) * 1000)

    total_ms = int((time.perf_counter() - started) * 1000)
    log.info(
        "run_all_scans: load=%dms detect=%dms total=%dms",
        load_ms, detect_ms, total_ms,
    )

    if persist:
        now = datetime.utcnow()
        for scan_type, top in results.items():
            db.add(ScanRun(
                run_at=now, scan_type=scan_type,
                universe_size=len(symbols), candidates_count=len(top),
                elapsed_ms=per_scan_ms[scan_type], bars_refreshed=0,
            ))
            _upsert_scan_cache(
                db, scan_type, len(symbols), len(top),
                per_scan_ms[scan_type], top, now,
            )
        db.commit()

    return results, per_scan_ms, total_ms, len(symbols)


def _upsert_scan_cache(
    db: Session, scan_type: str, universe_size: int, candidates_count: int,
    elapsed_ms: int, top: list[Candidate], now: datetime,
) -> None:
    """Upsert one row in the shared ScanCache table.

    Why not ``ON CONFLICT``: SQLite + Postgres differ on the conflict syntax;
    using a get-then-mutate keeps the migration of dialects portable. Race
    conditions don't matter — this table only ever has one writer at a time
    (UI button or scheduler).
    """
    existing = db.get(ScanCache, scan_type)
    payload = _serialize_candidates(top)
    if existing is None:
        db.add(ScanCache(
            scan_type=scan_type, run_at=now,
            universe_size=universe_size, candidates_count=candidates_count,
            elapsed_ms=elapsed_ms, payload=payload,
        ))
    else:
        existing.run_at = now
        existing.universe_size = universe_size
        existing.candidates_count = candidates_count
        existing.elapsed_ms = elapsed_ms
        existing.payload = payload


def _detect_one_timed(
    scan_type: str,
    symbols: list[str],
    bars_map: dict[str, list],
    rs_ratings: dict[str, int] | None = None,
) -> tuple[list[Candidate], int]:
    started = time.perf_counter()
    out = _detect_one(scan_type, symbols, bars_map, rs_ratings=rs_ratings)
    return out, int((time.perf_counter() - started) * 1000)


# -- Cache reader -----------------------------------------------------------


def latest_cached_run(
    db: Session, scan_type: str, max_age_minutes: int = 24 * 60,
) -> tuple[list[Candidate], ScanCache] | None:
    """Return the cached candidate list for ``scan_type`` if it's within
    ``max_age_minutes``. ``None`` when the cache is cold or stale.

    Default 24-hour TTL is generous because EOD bhavcopy data only changes
    once a day. The pre-warm scheduler refreshes every weekday at 15:35 IST.
    """
    cutoff = datetime.utcnow() - timedelta(minutes=max_age_minutes)
    row = (
        db.query(ScanCache)
        .filter(ScanCache.scan_type == scan_type, ScanCache.run_at >= cutoff)
        .first()
    )
    if row is None:
        return None
    return _deserialize_candidates(row.payload), row


def latest_cached_all(
    db: Session, max_age_minutes: int = 24 * 60,
) -> tuple[dict[str, list[Candidate]], dict[str, ScanCache]] | None:
    """Cached read for all 4 scans. Returns None if any scan is missing
    a fresh cached row — caller falls back to a live run."""
    results: dict[str, list[Candidate]] = {}
    rows: dict[str, ScanCache] = {}
    for scan_type in SCAN_TYPES:
        hit = latest_cached_run(db, scan_type, max_age_minutes)
        if hit is None:
            return None
        candidates, row = hit
        results[scan_type] = candidates
        rows[scan_type] = row
    return results, rows


# -- (de)serialization for ScanRun.payload ----------------------------------


def _serialize_candidates(candidates: Iterable[Candidate]) -> str:
    """Candidate → JSON. ``extras`` values are already JSON-safe primitives
    (floats, ints, isoformat date strings)."""
    return json.dumps([asdict(c) for c in candidates])


def _deserialize_candidates(payload: str | None) -> list[Candidate]:
    if not payload:
        return []
    raw = json.loads(payload)
    return [Candidate(**row) for row in raw]


# -- Other utilities (passthroughs) -----------------------------------------


def refresh_bars_cache(db: Session, lookback_days: int = 180):
    return bars_cache.refresh_bars(db, lookback_days=lookback_days)


def last_run_summary(db: Session) -> dict[str, dict | None]:
    out: dict[str, dict | None] = {}
    for scan_type in SCAN_TYPES:
        row = (
            db.query(ScanRun)
            .filter(ScanRun.scan_type == scan_type)
            .order_by(ScanRun.run_at.desc())
            .first()
        )
        out[scan_type] = (
            None
            if row is None
            else {
                "run_at": row.run_at.isoformat(),
                "universe_size": row.universe_size,
                "candidates_count": row.candidates_count,
                "elapsed_ms": row.elapsed_ms,
            }
        )
    return out
