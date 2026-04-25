"""NSE bhavcopy → DailyBar cache.

One HTTP download per trading day returns OHLCV for every NSE equity — vastly
more efficient than 2000 yfinance calls. We iterate the last N calendar days,
skip days we already have coverage for, download missing days, upsert rows
into ``daily_bars``.

Endpoint: ``https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_DDMMYYYY.csv``
Requires a browser-like User-Agent. Weekends/holidays return 404 — skipped silently.
"""
from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterable

import requests
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..models import DailyBar

log = logging.getLogger("journal.scanner.bars_cache")

NSE_BHAV_URL = "https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{ddmmyyyy}.csv"
NSE_BHAV_URL_FALLBACK = "https://archives.nseindia.com/products/content/sec_bhavdata_full_{ddmmyyyy}.csv"
BSE_BHAV_URL = "https://www.bseindia.com/download/BhavCopy/Equity/BhavCopy_BSE_CM_0_0_0_{yyyymmdd}_F_0000.CSV"

HEADERS_NSE = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}

HEADERS_BSE = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.bseindia.com/",
}

# NSE: only keep "EQ" and "BE" series rows (regular equity + book-entry). Skip ETFs
# ("IV"), SME ("ST"), government securities, etc.
NSE_ALLOWED_SERIES = {"EQ", "BE", "BZ"}

# BSE series (SctySrs): A/B are liquid equity, T/Z/X/XT are stressed-tier equity,
# all tradeable. Skip F (fixed income), G (gov sec), M/MS (SME — our mcap filter
# will drop these anyway if we ever include them).
BSE_ALLOWED_SERIES = {"A", "B", "T", "Z", "X", "XT"}
# FinInstrmTp=STK means stock; other values (BND, MUT, OPT, FUT) we don't want.
BSE_ALLOWED_INSTR_TYPES = {"STK"}


@dataclass
class Bar:
    date: date
    open: float
    high: float
    low: float
    close: float
    volume: int


@dataclass
class RefreshSummary:
    days_checked: int = 0
    days_downloaded: int = 0
    days_skipped_existing: int = 0
    days_failed: int = 0
    rows_upserted: int = 0
    errors: list[str] = None

    def __post_init__(self):
        if self.errors is None:
            self.errors = []


def _download_bhavcopy_nse(d: date) -> bytes | None:
    """NSE bhavcopy for date ``d``. None on 404/network error."""
    ddmmyyyy = d.strftime("%d%m%Y")
    for url_tpl in (NSE_BHAV_URL, NSE_BHAV_URL_FALLBACK):
        url = url_tpl.format(ddmmyyyy=ddmmyyyy)
        try:
            resp = requests.get(url, headers=HEADERS_NSE, timeout=20)
        except requests.RequestException as exc:
            log.debug("nse bhavcopy GET failed %s: %s", url, exc)
            continue
        if resp.status_code == 200 and len(resp.content) > 200:
            return resp.content
        if resp.status_code == 404:
            return None
    return None


def _download_bhavcopy_bse(d: date) -> bytes | None:
    """BSE bhavcopy for date ``d``. None on 404/network error."""
    url = BSE_BHAV_URL.format(yyyymmdd=d.strftime("%Y%m%d"))
    try:
        resp = requests.get(url, headers=HEADERS_BSE, timeout=20)
    except requests.RequestException as exc:
        log.debug("bse bhavcopy GET failed %s: %s", url, exc)
        return None
    if resp.status_code == 200 and len(resp.content) > 200:
        return resp.content
    return None


# Back-compat alias so tests / old call sites keep working.
_download_bhavcopy = _download_bhavcopy_nse


def _parse_bhavcopy_nse(content: bytes) -> list[dict]:
    """Return list of OHLCV dicts from an NSE sec_bhavdata_full CSV (EQ/BE/BZ)."""
    text = content.decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text))
    header_raw = next(reader, None)
    if not header_raw:
        return []
    header = [h.strip().upper() for h in header_raw]

    def idx(name: str) -> int | None:
        return header.index(name) if name in header else None

    i_sym = idx("SYMBOL")
    i_ser = idx("SERIES")
    i_open = idx("OPEN_PRICE")
    i_high = idx("HIGH_PRICE")
    i_low = idx("LOW_PRICE")
    i_close = idx("CLOSE_PRICE")
    i_vol = idx("TTL_TRD_QNTY")
    if None in (i_sym, i_ser, i_open, i_high, i_low, i_close, i_vol):
        return []

    out: list[dict] = []
    for row in reader:
        if not row or len(row) <= max(i_sym, i_ser, i_open, i_high, i_low, i_close, i_vol):
            continue
        series = row[i_ser].strip().upper()
        if series not in NSE_ALLOWED_SERIES:
            continue
        try:
            out.append(
                {
                    "symbol": row[i_sym].strip().upper(),
                    "open": float(row[i_open].strip()),
                    "high": float(row[i_high].strip()),
                    "low": float(row[i_low].strip()),
                    "close": float(row[i_close].strip()),
                    "volume": int(float(row[i_vol].strip() or 0)),
                }
            )
        except (ValueError, IndexError):
            continue
    return out


def _parse_bhavcopy_bse(content: bytes) -> list[dict]:
    """Return list of OHLCV dicts from a BSE BhavCopy_BSE_CM CSV (STK + liquid series)."""
    text = content.decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text))
    header_raw = next(reader, None)
    if not header_raw:
        return []
    header = [h.strip() for h in header_raw]
    try:
        i_sym = header.index("TckrSymb")
        i_ser = header.index("SctySrs")
        i_typ = header.index("FinInstrmTp")
        i_open = header.index("OpnPric")
        i_high = header.index("HghPric")
        i_low = header.index("LwPric")
        i_close = header.index("ClsPric")
        i_vol = header.index("TtlTradgVol")
    except ValueError:
        return []

    out: list[dict] = []
    max_i = max(i_sym, i_ser, i_typ, i_open, i_high, i_low, i_close, i_vol)
    for row in reader:
        if not row or len(row) <= max_i:
            continue
        if row[i_typ].strip().upper() not in BSE_ALLOWED_INSTR_TYPES:
            continue
        if row[i_ser].strip().upper() not in BSE_ALLOWED_SERIES:
            continue
        try:
            out.append(
                {
                    "symbol": row[i_sym].strip().upper(),
                    "open": float(row[i_open].strip()),
                    "high": float(row[i_high].strip()),
                    "low": float(row[i_low].strip()),
                    "close": float(row[i_close].strip()),
                    "volume": int(float(row[i_vol].strip() or 0)),
                }
            )
        except (ValueError, IndexError):
            continue
    return out


# Back-compat alias
_parse_bhavcopy = _parse_bhavcopy_nse


# A fully-populated day after NSE + BSE merge sits around ~4300 rows (NSE
# ~2700 + BSE-exclusive ~1600). If a date has substantially fewer, one of
# the two exchanges hasn't been fetched yet and we should re-run both. The
# per-row dedup in _insert_rows ensures re-running is cheap.
_FULL_DAY_ROW_THRESHOLD = 3500


def _dates_with_data(db: Session, start: date, end: date) -> set[date]:
    """Which dates in [start, end] already have full NSE + BSE coverage?"""
    rows = (
        db.query(DailyBar.date, func.count(DailyBar.id))
        .filter(DailyBar.date >= start, DailyBar.date <= end)
        .group_by(DailyBar.date)
        .all()
    )
    return {d for d, c in rows if c >= _FULL_DAY_ROW_THRESHOLD}


def _insert_rows(db: Session, rows: list[dict], d: date, existing_for_day: set[str]) -> int:
    """Insert new (symbol, date) rows from ``rows`` if not already present.
    ``existing_for_day`` is mutated — callers share it across NSE+BSE for the
    same day so NSE always wins over BSE on dual listings.
    """
    added = 0
    for r in rows:
        sym = r["symbol"]
        if sym in existing_for_day:
            continue
        db.add(
            DailyBar(
                symbol=sym,
                date=d,
                open=r["open"],
                high=r["high"],
                low=r["low"],
                close=r["close"],
                volume=r["volume"],
            )
        )
        existing_for_day.add(sym)
        added += 1
    return added


def refresh_bars(db: Session, lookback_days: int = 180) -> RefreshSummary:
    """Fill ``daily_bars`` for the last ``lookback_days`` calendar days from
    **NSE and BSE** bhavcopies. NSE is inserted first per day so dual-listed
    names (the vast majority) keep the NSE row; BSE adds anything that's
    BSE-exclusive.

    Skips days we already have coverage for, skips weekends, swallows holiday
    404s. Commits after each successful day so a long-running refresh is
    incrementally durable (kill it and resume).
    """
    summary = RefreshSummary()
    today = date.today()
    start = today - timedelta(days=lookback_days)
    already = _dates_with_data(db, start, today)

    d = today
    while d >= start:
        summary.days_checked += 1
        if d.weekday() >= 5:
            d -= timedelta(days=1)
            continue
        if d in already:
            summary.days_skipped_existing += 1
            d -= timedelta(days=1)
            continue

        # Pre-load what's already in this day (for mid-day re-runs that hit a
        # partially-populated date).
        existing_for_day = {
            s for (s,) in db.query(DailyBar.symbol).filter(DailyBar.date == d).all()
        }
        day_added = 0
        any_success = False

        nse_raw = _download_bhavcopy_nse(d)
        if nse_raw is not None:
            nse_rows = _parse_bhavcopy_nse(nse_raw)
            if nse_rows:
                day_added += _insert_rows(db, nse_rows, d, existing_for_day)
                any_success = True
                log.info("nse bhavcopy %s: %d rows", d.isoformat(), len(nse_rows))

        bse_raw = _download_bhavcopy_bse(d)
        if bse_raw is not None:
            bse_rows = _parse_bhavcopy_bse(bse_raw)
            if bse_rows:
                day_added += _insert_rows(db, bse_rows, d, existing_for_day)
                any_success = True
                log.info("bse bhavcopy %s: %d rows", d.isoformat(), len(bse_rows))

        if any_success:
            db.commit()
            summary.days_downloaded += 1
            summary.rows_upserted += day_added
        else:
            summary.days_failed += 1

        d -= timedelta(days=1)

    return summary


def get_bars(db: Session, symbol: str, lookback_days: int = 180) -> list[Bar]:
    """Read cached bars for ``symbol``, newest-last, limited to lookback."""
    start = date.today() - timedelta(days=lookback_days)
    rows = (
        db.query(DailyBar)
        .filter(DailyBar.symbol == symbol, DailyBar.date >= start)
        .order_by(DailyBar.date.asc())
        .all()
    )
    return [
        Bar(
            date=r.date,
            open=r.open,
            high=r.high,
            low=r.low,
            close=r.close,
            volume=r.volume or 0,
        )
        for r in rows
    ]


def bars_by_symbol(db: Session, symbols: Iterable[str], lookback_days: int = 180) -> dict[str, list[Bar]]:
    """Bulk fetch: {symbol → list[Bar]} for the given symbol universe.

    One DB query, grouped in Python. Faster than ``get_bars`` in a loop when
    the universe is large.
    """
    start = date.today() - timedelta(days=lookback_days)
    sym_set = {s.upper() for s in symbols}
    if not sym_set:
        return {}
    rows = (
        db.query(DailyBar)
        .filter(DailyBar.symbol.in_(sym_set), DailyBar.date >= start)
        .order_by(DailyBar.symbol.asc(), DailyBar.date.asc())
        .all()
    )
    out: dict[str, list[Bar]] = {s: [] for s in sym_set}
    for r in rows:
        out[r.symbol].append(
            Bar(
                date=r.date,
                open=r.open,
                high=r.high,
                low=r.low,
                close=r.close,
                volume=r.volume or 0,
            )
        )
    return out


def latest_bar_date(db: Session) -> date | None:
    row = db.query(func.max(DailyBar.date)).scalar()
    return row


# ---------------------------------------------------------------------------
# Background refresh — moves the multi-minute bhavcopy pull off the request
# path so it can't pin a gunicorn worker (the same pattern we use for the
# NSE index-universe refresh).
# ---------------------------------------------------------------------------

import threading  # noqa: E402

_refresh_lock = threading.Lock()
_refresh_thread: threading.Thread | None = None
_refresh_state: dict = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "last_summary": None,
    "last_error": None,
}


def _refresh_worker(lookback_days: int) -> None:
    """Worker entrypoint. Owns its own DB session — the request thread that
    spawned us has long since returned."""
    from ..db import SessionLocal

    global _refresh_state
    with SessionLocal() as db:
        try:
            summary = refresh_bars(db, lookback_days=lookback_days)
            _refresh_state = {
                "running": False,
                "started_at": _refresh_state.get("started_at"),
                "finished_at": datetime.utcnow().isoformat(),
                "last_summary": {
                    "days_downloaded": summary.days_downloaded,
                    "days_skipped_existing": summary.days_skipped_existing,
                    "days_failed": summary.days_failed,
                    "rows_upserted": summary.rows_upserted,
                },
                "last_error": None,
            }
        except Exception as exc:  # noqa: BLE001
            log.exception("background bars refresh failed")
            _refresh_state = {
                "running": False,
                "started_at": _refresh_state.get("started_at"),
                "finished_at": datetime.utcnow().isoformat(),
                "last_summary": None,
                "last_error": f"{type(exc).__name__}: {exc}",
            }


def start_background_refresh(lookback_days: int = 180) -> bool:
    """Kick off a daemon thread to pull bhavcopies. Returns True if a new
    refresh started, False if one was already in flight (idempotent — safe
    to spam-click the button)."""
    global _refresh_thread, _refresh_state
    with _refresh_lock:
        if _refresh_thread is not None and _refresh_thread.is_alive():
            return False
        _refresh_state = {
            "running": True,
            "started_at": datetime.utcnow().isoformat(),
            "finished_at": None,
            "last_summary": None,
            "last_error": None,
        }
        _refresh_thread = threading.Thread(
            target=_refresh_worker, args=(lookback_days,), daemon=True,
        )
        _refresh_thread.start()
    return True


def refresh_status() -> dict:
    return dict(_refresh_state)
