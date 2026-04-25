"""NSE-published index constituent universe.

Replaces the per-symbol yfinance market-cap pull as the scanner's
"investable universe" gate. NSE publishes free CSVs of every index's
constituents — one HTTP call gives us ~750 highly liquid mid+ cap stocks
in well under a second.

CSV format::

    Company Name,Industry,Symbol,Series,ISIN Code
    360 ONE WAM Ltd.,Financial Services,360ONE,EQ,INE466L01038
    ...

Sources used:
- ``ind_niftytotalmarket_list.csv`` — Nifty 500 + Nifty Microcap 250 ≈ 750
  stocks. This is our default — the broad investable universe.
- Optional: ``ind_nifty500list.csv`` for a tighter ≈ 500-stock cut.

The list updates quarterly when NSE rebalances; we refresh daily.

Why we keep the bars cache separate: bhavcopy gives us OHLCV for ~4700
stocks (everything trading on NSE), but only ~750 of those clear the
liquidity / cap bar to be worth scanning. The bars cache is the data
source; this module is the *gate*.
"""
from __future__ import annotations

import csv
import io
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

import requests

log = logging.getLogger("journal.scanner.index_universe")

# Repo-root/data — same dir we use for SQLite + DB backups. Created by
# app/db.py at boot.
_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
_CACHE_FILE = _DATA_DIR / "nse_index_universe.csv"
_CACHE_TTL = timedelta(hours=24)

NSE_INDEX_URLS: dict[str, str] = {
    "total_market": "https://archives.nseindia.com/content/indices/ind_niftytotalmarket_list.csv",
    "nifty_500":    "https://archives.nseindia.com/content/indices/ind_nifty500list.csv",
}

DEFAULT_INDEX = "total_market"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/csv,*/*;q=0.9",
}

# In-memory cache keyed by index name. Persisted to disk so a restart
# doesn't trigger another fetch within the TTL.
@dataclass
class _Cached:
    refreshed_at: datetime
    rows: list[dict]   # [{symbol, name, industry}]


_lock = threading.Lock()
_cache: dict[str, _Cached] = {}


def _disk_load(index_name: str) -> _Cached | None:
    path = _CACHE_FILE.with_name(f"nse_{index_name}.csv")
    if not path.exists():
        return None
    age = datetime.utcnow() - datetime.utcfromtimestamp(path.stat().st_mtime)
    if age > _CACHE_TTL:
        return None
    return _Cached(
        refreshed_at=datetime.utcnow() - age,
        rows=_parse_csv(path.read_bytes()),
    )


def _disk_save(index_name: str, raw: bytes) -> None:
    path = _CACHE_FILE.with_name(f"nse_{index_name}.csv")
    path.write_bytes(raw)


def _parse_csv(raw: bytes) -> list[dict]:
    text = raw.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    out: list[dict] = []
    for row in reader:
        sym = (row.get("Symbol") or "").strip().upper()
        if not sym:
            continue
        out.append({
            "symbol": sym,
            "name": (row.get("Company Name") or "").strip(),
            "industry": (row.get("Industry") or "").strip(),
        })
    return out


def _fetch(index_name: str) -> list[dict]:
    """Download from NSE archives. Caller holds the lock."""
    url = NSE_INDEX_URLS[index_name]
    log.info("fetching %s from %s", index_name, url)
    resp = requests.get(url, headers=_HEADERS, timeout=15)
    resp.raise_for_status()
    rows = _parse_csv(resp.content)
    if not rows:
        raise RuntimeError(f"NSE returned an empty {index_name} list")
    _disk_save(index_name, resp.content)
    log.info("fetched %d rows for %s", len(rows), index_name)
    return rows


def get_constituents(
    index_name: str = DEFAULT_INDEX, force_refresh: bool = False,
) -> list[dict]:
    """Return the constituent rows for ``index_name``. Memoized + disk-cached
    for 24h. Concurrent callers serialize on a lock so we never duplicate
    HTTP fetches.
    """
    if index_name not in NSE_INDEX_URLS:
        raise ValueError(f"unknown index: {index_name}")
    with _lock:
        cached = _cache.get(index_name)
        if not force_refresh and cached and (datetime.utcnow() - cached.refreshed_at) < _CACHE_TTL:
            return cached.rows
        if not force_refresh:
            disk = _disk_load(index_name)
            if disk is not None:
                _cache[index_name] = disk
                return disk.rows
        rows = _fetch(index_name)
        _cache[index_name] = _Cached(datetime.utcnow(), rows)
        return rows


def qualified_symbols(index_name: str = DEFAULT_INDEX) -> set[str]:
    """Set of symbols that qualify as 'tradeable swing universe'. Stocks in
    the Nifty Total Market index are large + mid + small caps that already
    pass NSE's liquidity / free-float filter."""
    return {r["symbol"] for r in get_constituents(index_name)}


def industry_map(index_name: str = DEFAULT_INDEX) -> dict[str, str]:
    """Symbol → Industry for displaying sector tags in scanner results."""
    return {r["symbol"]: r["industry"] for r in get_constituents(index_name) if r["industry"]}


def status() -> dict:
    """For the /scanners status strip."""
    out: dict = {}
    for name in NSE_INDEX_URLS:
        cached = _cache.get(name)
        if cached is None:
            cached = _disk_load(name)
            if cached:
                _cache[name] = cached
        out[name] = {
            "count": len(cached.rows) if cached else 0,
            "refreshed_at": cached.refreshed_at.isoformat() if cached else None,
        }
    return out
