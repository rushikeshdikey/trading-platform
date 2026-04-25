"""Scanner orchestrator.

One function — ``run_scan`` — ties the pieces together:

1. Load the universe of symbols that have bars cached.
2. Bulk-read bars for those symbols.
3. Run the chosen pattern detector over each symbol's bar sequence.
4. Sort by score, keep the top N.
5. Record a ``ScanRun`` row so the UI can show "last run at".

Refreshing the bars cache is a separate explicit operation (expensive first
time) exposed via ``refresh_bars_cache``.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime

from sqlalchemy.orm import Session

from ..models import ScanRun
from . import bars_cache
from . import fundamentals
from . import universe as universe_mod
from .patterns import Candidate, SCAN_TYPES

log = logging.getLogger("journal.scanner.runner")

TOP_N_RESULTS = 100


def run_scan(db: Session, scan_type: str) -> tuple[list[Candidate], ScanRun]:
    """Execute one of ``SCAN_TYPES`` across the cached universe.

    The universe goes through two gates before reaching the pattern detector:
    1. ETF filter (in ``universe.universe_from_cache``).
    2. Market-cap gate — only symbols with cached ``market_cap >= MIN_MARKET_CAP_RS``
       survive. Symbols without cached mcap are **excluded** (safer than assuming
       they pass). Run "Refresh fundamentals" from /scanners to populate the
       cache; until then, scans will return empty.
    """
    if scan_type not in SCAN_TYPES:
        raise ValueError(f"Unknown scan_type: {scan_type}")

    _, detector = SCAN_TYPES[scan_type]
    started = time.perf_counter()

    symbols = universe_mod.universe_from_cache(db)
    mcaps = fundamentals.load_market_caps(db, symbols)
    symbols = [
        s for s in symbols if mcaps.get(s, 0.0) >= fundamentals.MIN_MARKET_CAP_RS
    ]
    bars_map = bars_cache.bars_by_symbol(db, symbols)

    candidates: list[Candidate] = []
    for sym in symbols:
        bars = bars_map.get(sym) or []
        if not bars:
            continue
        try:
            c = detector(sym, bars)
        except Exception as exc:  # noqa: BLE001 — never let one bad symbol kill the scan
            log.debug("detector failed on %s: %s", sym, exc)
            continue
        if c is not None:
            candidates.append(c)

    candidates.sort(key=lambda c: c.score, reverse=True)
    top = candidates[:TOP_N_RESULTS]

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    run = ScanRun(
        run_at=datetime.utcnow(),
        scan_type=scan_type,
        universe_size=len(symbols),
        candidates_count=len(top),
        elapsed_ms=elapsed_ms,
        bars_refreshed=0,
    )
    db.add(run)
    db.commit()
    return top, run


def refresh_bars_cache(db: Session, lookback_days: int = 180):
    """Proxy to bars_cache.refresh_bars — wrapping here so routers only import
    runner, not three submodules."""
    return bars_cache.refresh_bars(db, lookback_days=lookback_days)


def last_run_summary(db: Session) -> dict[str, dict | None]:
    """One dict per scan_type: latest ScanRun or None."""
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
