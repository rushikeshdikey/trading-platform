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

# Liquidity floor for non-index names. Calibrated so genuine smallcap setups
# like STYLAMIND (~₹14 cr/day) sail through, while penny / illiquid names get
# dropped. Stricter than NSE's own Total Market criteria on price (we won't
# scan sub-₹30 stocks) but looser on turnover.
_NON_INDEX_MIN_TURNOVER_RS = 2_00_00_000   # ₹2 crore avg daily turnover
_NON_INDEX_MIN_CLOSE_RS = 30.0
_NON_INDEX_MIN_BARS = 30


def _load_universe_and_bars(db: Session) -> tuple[list[str], dict[str, list]]:
    """Resolve the gated universe and bulk-load bars for it.

    Two-tier gate, designed so we never silently drop a real candidate just
    because NSE hasn't included it in their index family:

      1. **Always in:** every symbol in the NSE Total Market index that we
         have bars for (~750 mid+ caps). These already pass NSE's liquidity
         + free-float thresholds.
      2. **Soft-included:** any other bars-cache symbol that clears a basic
         liquidity floor — last close ≥ ₹{min_close}, 20-day avg turnover
         ≥ ₹{min_turnover_cr} cr, ≥ {min_bars} bars of history.

    If the NSE index list can't be fetched at all, we fall back to the soft
    gate alone (better to scan than not).
    """
    from . import index_universe as idx_uni

    bars_universe = universe_mod.universe_from_cache(db)
    bars_map = bars_cache.bars_by_symbol(db, bars_universe)

    try:
        index_set = idx_uni.qualified_symbols()
    except Exception as exc:  # noqa: BLE001
        log.warning("NSE index list fetch failed (%s); soft gate only", exc)
        index_set = set()

    kept: list[str] = []
    for sym in bars_universe:
        if sym in index_set:
            kept.append(sym)
            continue
        bars = bars_map.get(sym) or []
        if len(bars) < _NON_INDEX_MIN_BARS:
            continue
        last_close = bars[-1].close
        if last_close < _NON_INDEX_MIN_CLOSE_RS:
            continue
        last_20 = bars[-20:]
        avg_turnover = sum(b.close * b.volume for b in last_20) / len(last_20)
        if avg_turnover < _NON_INDEX_MIN_TURNOVER_RS:
            continue
        kept.append(sym)

    return kept, bars_map


def gated_universe_breakdown(db: Session) -> dict:
    """Used by /scanners to render an accurate universe-size headline. Same
    rules as ``_load_universe_and_bars`` — kept in sync intentionally."""
    syms, _bars = _load_universe_and_bars(db)
    from . import index_universe as idx_uni
    try:
        index_set = idx_uni.qualified_symbols()
    except Exception:  # noqa: BLE001
        index_set = set()
    in_index = sum(1 for s in syms if s in index_set)
    return {
        "total": len(syms),
        "in_index": in_index,
        "soft_included": len(syms) - in_index,
    }


def _detect_one(
    scan_type: str, symbols: list[str], bars_map: dict[str, list],
) -> list[Candidate]:
    _, detector = SCAN_TYPES[scan_type]
    out: list[Candidate] = []
    for sym in symbols:
        bars = bars_map.get(sym) or []
        if not bars:
            continue
        try:
            c = detector(sym, bars)
        except Exception as exc:  # noqa: BLE001 — never let one bad symbol kill the scan
            log.debug("detector %s failed on %s: %s", scan_type, sym, exc)
            continue
        if c is not None:
            out.append(c)
    out.sort(key=lambda c: c.score, reverse=True)
    return out[:TOP_N_RESULTS]


# -- Single-scan entry point (kept for the cockpit + per-card UI) -----------


def run_scan(db: Session, scan_type: str) -> tuple[list[Candidate], ScanRun]:
    if scan_type not in SCAN_TYPES:
        raise ValueError(f"Unknown scan_type: {scan_type}")

    started = time.perf_counter()
    symbols, bars_map = _load_universe_and_bars(db)
    top = _detect_one(scan_type, symbols, bars_map)
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
    load_ms = int((time.perf_counter() - started) * 1000)

    results: dict[str, list[Candidate]] = {}
    per_scan_ms: dict[str, int] = {}

    # 4 detectors, 4 threads. Detectors release the GIL during numpy work
    # so this gives real parallelism on multi-core machines, and at worst
    # is no slower than sequential.
    detect_started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=len(SCAN_TYPES)) as ex:
        future_to_type = {
            ex.submit(_detect_one_timed, scan_type, symbols, bars_map): scan_type
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
    scan_type: str, symbols: list[str], bars_map: dict[str, list],
) -> tuple[list[Candidate], int]:
    started = time.perf_counter()
    out = _detect_one(scan_type, symbols, bars_map)
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
