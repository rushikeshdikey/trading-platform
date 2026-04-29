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
    # Microcap 250 — covers smaller names not in Total Market. Critical for
    # user's actual tradeable universe (MACPOWER / INFOBEAN / STYLAMIND-class
    # stocks that produce real returns but are too small for Total Market).
    "microcap_250": "https://archives.nseindia.com/content/indices/ind_niftymicrocap250_list.csv",
}

DEFAULT_INDEX = "total_market"

# Indices we union together for the composite "tradeable + sector-tagged"
# universe. Keep this list short — we don't need every sectoral index, just
# the broad-base indices that have unique stocks.
COMPOSITE_INDICES: tuple[str, ...] = ("total_market", "microcap_250")

# NSE archives sometimes blackholes connections from cloud IPs unless the
# request looks like a real browser tab. Mirror the headers that bars_cache
# uses (which work in prod) — User-Agent + Accept-Language + Referer.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/csv,*/*;q=0.9",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}

# Hard-cap: NSE either responds within a few seconds or it doesn't respond
# at all. Tight timeout means we fail fast instead of pinning a worker.
_HTTP_TIMEOUT_S = 6.0

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
    resp = requests.get(url, headers=_HEADERS, timeout=_HTTP_TIMEOUT_S)
    resp.raise_for_status()
    rows = _parse_csv(resp.content)
    if not rows:
        raise RuntimeError(f"NSE returned an empty {index_name} list")
    _disk_save(index_name, resp.content)
    log.info("fetched %d rows for %s", len(rows), index_name)
    return rows


# ---------------------------------------------------------------------------
# Background refresh — never block a request thread on NSE.
# ---------------------------------------------------------------------------

_refresh_thread: threading.Thread | None = None
_refresh_status: dict = {"running": False, "last_error": None, "last_count": 0, "last_finished_at": None}


def _refresh_worker(index_name: str) -> None:
    global _refresh_status
    started = datetime.utcnow()
    try:
        rows = _fetch(index_name)
        with _lock:
            _cache[index_name] = _Cached(started, rows)
        _refresh_status = {
            "running": False, "last_error": None,
            "last_count": len(rows), "last_finished_at": datetime.utcnow().isoformat(),
        }
    except Exception as exc:  # noqa: BLE001
        log.warning("background NSE index refresh failed: %s", exc)
        _refresh_status = {
            "running": False, "last_error": f"{type(exc).__name__}: {exc}",
            "last_count": 0, "last_finished_at": datetime.utcnow().isoformat(),
        }


def start_background_refresh(index_name: str = DEFAULT_INDEX) -> bool:
    """Kick off a daemon thread to refresh the index list. Returns True if a
    new refresh started, False if one is already in flight."""
    global _refresh_thread, _refresh_status
    with _lock:
        if _refresh_thread is not None and _refresh_thread.is_alive():
            return False
        _refresh_status = {"running": True, "last_error": None, "last_count": 0, "last_finished_at": None}
        _refresh_thread = threading.Thread(
            target=_refresh_worker, args=(index_name,), daemon=True,
        )
        _refresh_thread.start()
    return True


def refresh_status() -> dict:
    return dict(_refresh_status)


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


# Path to manual sub-industry overrides — splits NSE's coarse buckets like
# "Financial Services" (121 stocks) into Banks/Capital Markets/NBFC/Insurance.
_OVERRIDES_FILE = _DATA_DIR / "industry_overrides.csv"
_overrides_cache: dict[str, str] | None = None
_overrides_mtime: float = 0.0


def _load_overrides() -> dict[str, str]:
    """Load symbol → industry overrides from the manual CSV. Re-reads when
    the file mtime changes — supports live-edit during dev."""
    global _overrides_cache, _overrides_mtime
    if not _OVERRIDES_FILE.exists():
        return {}
    mtime = _OVERRIDES_FILE.stat().st_mtime
    if _overrides_cache is not None and mtime == _overrides_mtime:
        return _overrides_cache
    out: dict[str, str] = {}
    try:
        with _OVERRIDES_FILE.open() as f:
            reader = csv.reader(f)
            for row in reader:
                if not row or row[0].strip().startswith("#"):
                    continue
                if row[0].strip().lower() == "symbol":
                    continue  # header
                if len(row) < 2:
                    continue
                sym = row[0].strip().upper()
                ind = row[1].strip()
                if sym and ind:
                    out[sym] = ind
    except Exception as exc:  # noqa: BLE001
        log.warning("industry_overrides load failed: %s", exc)
        return _overrides_cache or {}
    _overrides_cache = out
    _overrides_mtime = mtime
    log.info("loaded %d sub-industry overrides from %s", len(out), _OVERRIDES_FILE.name)
    return out


def _composite_constituents() -> list[dict]:
    """Union of all COMPOSITE_INDICES — broader coverage than just total_market.

    Microcap 250 adds smaller liquid names that aren't in Nifty Total Market.
    Symbols appearing in multiple indices: keep the first (Total Market wins
    over Microcap because Total Market's classification tends to be cleaner).
    """
    seen: dict[str, dict] = {}
    for idx_name in COMPOSITE_INDICES:
        try:
            for r in get_constituents(idx_name):
                seen.setdefault(r["symbol"], r)
        except Exception as exc:  # noqa: BLE001
            log.warning("composite_constituents: skip %s: %s", idx_name, exc)
    return list(seen.values())


def industry_map(index_name: str = DEFAULT_INDEX) -> dict[str, str]:
    """Symbol → Industry for displaying sector tags + sector-rotation lookup.

    Composes three sources, in priority order:
      1. Manual `data/industry_overrides.csv` (Screener.in-style sub-industries)
      2. Composite of NSE indices (Total Market + Microcap 250)
      3. Single-index fallback (when caller passes a specific index name)

    Override wins. Then composite. Then the requested index. The override
    file is keyed by symbol so a single line refines an existing tag.
    """
    overrides = _load_overrides()

    if index_name == DEFAULT_INDEX:
        # Use the broader composite universe for the default code path.
        rows = _composite_constituents()
    else:
        rows = get_constituents(index_name)

    out: dict[str, str] = {}
    for r in rows:
        sym = r["symbol"]
        # Override wins; otherwise NSE's Industry column.
        ind = overrides.get(sym) or r["industry"]
        if ind:
            out[sym] = ind

    # Symbols that exist ONLY in overrides (user's untagged winners) should
    # still be present even if not in any NSE index.
    for sym, ind in overrides.items():
        if sym not in out and ind:
            out[sym] = ind

    return out


# ---------------------------------------------------------------------------
# Industry → parent-sector hierarchy.
#
# Why we need this: RRG (sector_rotation.compute_rotation) needs ≥4
# constituents per group to be statistically meaningful. After splitting
# "Financial Services" into Banks / Capital Markets / NBFC / Insurance, some
# child buckets fall below 4 (e.g. Power - Distribution = 2 stocks).
#
# Solution: a stock's RRG-relevant *sector* is the PARENT of its industry.
# "Power - Distribution" rolls up to "Power", which has 30+ stocks total.
# The display tag stays fine-grained (good for the user); the RRG signal is
# coarse-grained (good for statistics).
#
# Convention: anything containing " - " is a refined child whose parent is
# the part before the dash. Other industries are their own parent.
# ---------------------------------------------------------------------------

# Explicit overrides — for cases where the parent isn't derivable from " - ".
_INDUSTRY_PARENT_OVERRIDES: dict[str, str] = {
    "Banks": "Financial Services",
    "NBFC": "Financial Services",
    "Capital Markets": "Financial Services",
    "Insurance": "Financial Services",
    "Holding Company": "Financial Services",
    "Defence": "Capital Goods",
    "Industrial Manufacturing": "Capital Goods",
    "Electrical Equipment": "Capital Goods",
    "Healthcare Services": "Healthcare",
    "Healthcare Equipment": "Healthcare",
    "Pharmaceuticals": "Healthcare",
    "Biotechnology": "Healthcare",
    "Construction Materials": "Construction",
    "Building Products": "Construction",
}


def industry_to_sector(industry: str) -> str:
    """Map fine-grained industry → coarse parent sector for RRG grouping.

    Rules (in order):
      1. Explicit lookup in _INDUSTRY_PARENT_OVERRIDES.
      2. Anything with " - " in the name: take the part before " - "
         ("Power - Distribution" → "Power"; "IT - Software" → "IT").
      3. Else the industry IS its own sector.
    """
    if not industry:
        return industry
    if industry in _INDUSTRY_PARENT_OVERRIDES:
        return _INDUSTRY_PARENT_OVERRIDES[industry]
    if " - " in industry:
        return industry.split(" - ", 1)[0].strip()
    return industry


def sector_map(index_name: str = DEFAULT_INDEX) -> dict[str, str]:
    """Symbol → coarse-grained sector for RRG grouping (NOT for display)."""
    return {sym: industry_to_sector(ind) for sym, ind in industry_map(index_name).items()}


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
