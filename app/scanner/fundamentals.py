"""Market cap cache.

yfinance's ``fast_info.market_cap`` is one-ticker-at-a-time, so we batch via
a ThreadPoolExecutor. Results land in ``instrument_meta`` with a 7-day TTL.

The refresh is kicked off as a **background daemon thread** — the UI returns
immediately and progress is visible as rows accumulate in the meta table.
This matches the existing price-refresher pattern in ``app/prices.py``.
"""
from __future__ import annotations

import logging
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Iterable

from sqlalchemy.orm import Session

from ..db import SessionLocal
from ..models import InstrumentMeta

log = logging.getLogger("journal.scanner.fundamentals")

# Minimum market cap to include in scans. 900 Cr = ₹9,00,00,00,000.
MIN_MARKET_CAP_RS: float = 900_00_00_000.0

# How long a cached market_cap stays "fresh" before we refresh it.
MCAP_TTL_DAYS: int = 7

# Conservative — Yahoo rate-limits aggressive clients (~2000 req/hour). 4
# workers with small jitter stays well under the limit for a 2500-symbol
# refresh without taking all day.
_MAX_WORKERS = 4

# Retry behaviour for transient rate-limit errors.
_RETRY_ATTEMPTS = 4
_RETRY_BASE_DELAY = 2.0  # seconds (grows exponentially)

_RATE_LIMIT_HINTS = ("RateLimit", "Too Many Requests", "429")


def _looks_rate_limited(err_name: str, err_str: str) -> bool:
    blob = f"{err_name} {err_str}".lower()
    return any(h.lower() in blob for h in _RATE_LIMIT_HINTS)


def _fetch_mcap(symbol: str) -> tuple[float | None, str | None]:
    """Return (market_cap_rs, error_message). Tries .NS first, then .BO.

    Handles Yahoo's rate limits with exponential backoff — a rate-limit hit
    is transient (limits reset within minutes), so we retry up to 4 times
    with growing delays. Any other exception is fatal for that suffix and
    we move to the next.
    """
    try:
        import yfinance as yf
    except ImportError:
        return None, "yfinance not installed"

    last_err: str | None = None
    for suf in (".NS", ".BO"):
        for attempt in range(_RETRY_ATTEMPTS):
            try:
                ticker = yf.Ticker(f"{symbol}{suf}")
                mc = ticker.fast_info.market_cap
            except Exception as exc:  # noqa: BLE001
                err_name = type(exc).__name__
                err_str = str(exc)
                last_err = f"{suf}: {err_name}"
                if _looks_rate_limited(err_name, err_str) and attempt < _RETRY_ATTEMPTS - 1:
                    # Exponential backoff with jitter so concurrent workers
                    # don't all retry in lockstep.
                    delay = _RETRY_BASE_DELAY * (2 ** attempt) + random.uniform(0, 1)
                    time.sleep(delay)
                    continue
                break  # non-rate-limit or out of retries → try the other suffix
            try:
                mc_f = float(mc) if mc is not None else None
            except (TypeError, ValueError):
                break
            if mc_f and mc_f > 0:
                return mc_f, None
            break  # suffix responded but with no valid mcap → try the other
    return None, last_err or "no market_cap"


def _symbols_needing_refresh(db: Session, symbols: Iterable[str], force: bool) -> list[str]:
    """Return the subset of ``symbols`` whose cache is missing, stale, or
    known-failed.

    Critically, rows where a previous fetch hit a rate limit land with
    ``market_cap_rs IS NULL`` and a populated ``last_error``. Those must be
    retried on every refresh, not treated as "cached and fresh". Otherwise a
    single bad minute permanently excludes the symbol from scans.
    """
    cutoff = datetime.utcnow() - timedelta(days=MCAP_TTL_DAYS)
    sym_set = {s.upper() for s in symbols}
    existing = {
        r.symbol: r
        for r in db.query(InstrumentMeta).filter(InstrumentMeta.symbol.in_(sym_set)).all()
    }
    to_fetch: list[str] = []
    for s in sym_set:
        row = existing.get(s)
        if row is None or force:
            to_fetch.append(s)
        elif row.market_cap_rs is None:
            # Previous fetch failed (rate limit / no data) — retry on every run.
            to_fetch.append(s)
        elif row.refreshed_at is None or row.refreshed_at < cutoff:
            to_fetch.append(s)
        # else: has a valid mcap and still fresh → skip
    return to_fetch


def refresh_market_caps(
    db: Session,
    symbols: Iterable[str],
    *,
    force: bool = False,
    max_workers: int = _MAX_WORKERS,
) -> dict:
    """Synchronously refresh market caps. Call from a background thread — this
    can take several minutes for a cold cache across thousands of symbols.

    Commits every 50 rows so progress is visible mid-flight and a crashed run
    doesn't lose everything.
    """
    to_fetch = _symbols_needing_refresh(db, symbols, force)
    if not to_fetch:
        return {"requested": 0, "fetched": 0, "errors": 0, "total": 0}

    successes = 0
    failures = 0

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(_fetch_mcap, s): s for s in to_fetch}
        processed = 0
        for fut in as_completed(futures):
            sym = futures[fut]
            try:
                mc, err = fut.result()
            except Exception as exc:  # noqa: BLE001
                mc, err = None, f"exc: {type(exc).__name__}"

            row = db.get(InstrumentMeta, sym)
            if row is None:
                row = InstrumentMeta(symbol=sym)
                db.add(row)
            row.market_cap_rs = mc
            row.refreshed_at = datetime.utcnow()
            row.last_error = err
            if mc is not None:
                successes += 1
            else:
                failures += 1

            processed += 1
            if processed % 50 == 0:
                db.commit()
    db.commit()
    log.info(
        "market cap refresh: %d succeeded, %d failed, %d total",
        successes, failures, len(to_fetch),
    )
    return {
        "requested": len(to_fetch),
        "fetched": successes,
        "errors": failures,
        "total": len(to_fetch),
    }


def load_market_caps(db: Session, symbols: Iterable[str]) -> dict[str, float]:
    """Return {symbol → market_cap_rs} for whatever's cached. Missing/None values omitted."""
    sym_set = {s.upper() for s in symbols}
    if not sym_set:
        return {}
    rows = (
        db.query(InstrumentMeta.symbol, InstrumentMeta.market_cap_rs)
        .filter(InstrumentMeta.symbol.in_(sym_set), InstrumentMeta.market_cap_rs.isnot(None))
        .all()
    )
    return {s: float(mc) for s, mc in rows}


def cache_stats(db: Session) -> dict:
    """For the /scanners page: how much of our cache is populated and fresh."""
    from sqlalchemy import func

    total = db.query(func.count(InstrumentMeta.symbol)).scalar() or 0
    with_mcap = (
        db.query(func.count(InstrumentMeta.symbol))
        .filter(InstrumentMeta.market_cap_rs.isnot(None))
        .scalar()
        or 0
    )
    failed = (
        db.query(func.count(InstrumentMeta.symbol))
        .filter(InstrumentMeta.market_cap_rs.is_(None))
        .filter(InstrumentMeta.last_error.isnot(None))
        .scalar()
        or 0
    )
    above_threshold = (
        db.query(func.count(InstrumentMeta.symbol))
        .filter(InstrumentMeta.market_cap_rs >= MIN_MARKET_CAP_RS)
        .scalar()
        or 0
    )
    last = (
        db.query(func.max(InstrumentMeta.refreshed_at))
        .scalar()
    )
    return {
        "total": total,
        "with_mcap": with_mcap,
        "failed": failed,
        "above_threshold": above_threshold,
        "last_refresh": last.isoformat() if last else None,
    }


# -- Background-thread orchestration ---------------------------------------

_refresh_lock = threading.Lock()
_refresh_thread: threading.Thread | None = None
_refresh_state = {"running": False, "started_at": None, "last_result": None}


def _refresh_worker(symbols: list[str], force: bool):
    _refresh_state["running"] = True
    _refresh_state["started_at"] = datetime.utcnow().isoformat()
    try:
        with SessionLocal() as db:
            result = refresh_market_caps(db, symbols, force=force)
        _refresh_state["last_result"] = result
    except Exception:
        log.exception("market cap refresh crashed")
        _refresh_state["last_result"] = {"error": "crashed — check logs"}
    finally:
        _refresh_state["running"] = False


def start_background_refresh(symbols: Iterable[str], force: bool = False) -> bool:
    """Start a refresh on a daemon thread. Returns False if one is already running."""
    global _refresh_thread
    with _refresh_lock:
        if _refresh_thread is not None and _refresh_thread.is_alive():
            return False
        syms = [s.upper() for s in symbols]
        _refresh_thread = threading.Thread(
            target=_refresh_worker, args=(syms, force), name="mcap-refresher", daemon=True
        )
        _refresh_thread.start()
    return True


def refresh_status() -> dict:
    return dict(_refresh_state)
