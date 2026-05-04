"""Intraday OHLC fetcher for Auto-Pilot reality-check.

The bhavcopy publishes after market close, so during market hours our
``daily_bars`` table has stale data — yesterday's close instead of
today's prices. This makes Auto-Pilot picks misleading: a stock that
gapped past the planned entry shows as actionable when it actually
requires chasing 5% above plan, and a stock whose intraday low has
already breached the SL shows as actionable when the setup is dead.

This module bridges that gap with **yfinance** — free, no Quote-
subscription required, ~15-min delayed (good enough for a swing
trader checking pre-noon). One-shot lookup per symbol with a 60-second
TTL cache so multiple cockpit refreshes within a minute don't pound
yfinance's servers.

We fetch INTRADAY (interval='1d' returns today's running bar during
market hours), not daily, so today's running OHLC is captured with
the latest LTP as the close.

Failure modes are silent — if yfinance returns empty/errors, the caller
falls back to the cached close (status = 'unknown' in the DailyPick).
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

log = logging.getLogger("journal.scanner.intraday_ltp")

CACHE_TTL_S = 60.0


@dataclass
class TodayOHLC:
    symbol: str
    open: float
    high: float
    low: float
    ltp: float          # last traded price (today's running close)
    fetched_at: float   # epoch seconds


_cache: dict[str, TodayOHLC] = {}
_cache_lock = threading.Lock()


def _yf_candidates(symbol: str) -> list[str]:
    """Order in which to try yfinance suffixes for an Indian symbol.

    NSE first because most of our scanner universe is NSE-listed; BSE
    fallback catches the small-/micro-caps yfinance hasn't ingested
    on the NSE feed yet (PAISALO is a recent example).
    """
    s = (symbol or "").strip().upper()
    if not s:
        return []
    if s.endswith(".NS") or s.endswith(".BO"):
        return [s]
    return [f"{s}.NS", f"{s}.BO"]


def _fetch_one_yf(yf_sym: str, period: str = "2d", interval: str = "1d"):
    """Single yfinance call. Returns the dataframe or None on empty/error."""
    try:
        import yfinance as yf
        ticker = yf.Ticker(yf_sym)
        df = ticker.history(period=period, interval=interval, auto_adjust=False)
        if df is None or df.empty:
            return None
        return df
    except Exception as exc:  # noqa: BLE001
        log.debug("yfinance fetch failed for %s: %s", yf_sym, exc)
        return None


def fetch_today_ohlc(symbol: str) -> TodayOHLC | None:
    """Return today's running OHLC for ``symbol`` or None on any failure.

    Tries NSE first (.NS), falls back to BSE (.BO) if NSE has no data.
    Cached for ``CACHE_TTL_S`` seconds.
    """
    sym = (symbol or "").strip().upper()
    if not sym:
        return None

    now = time.time()
    with _cache_lock:
        cached = _cache.get(sym)
        if cached and (now - cached.fetched_at) < CACHE_TTL_S:
            return cached

    from datetime import date, timedelta
    today = date.today()

    df = None
    used_sym = None
    for candidate in _yf_candidates(sym):
        df = _fetch_one_yf(candidate, period="2d", interval="1d")
        if df is not None and not df.empty:
            used_sym = candidate
            break

    if df is None:
        log.debug("yfinance empty for %s on all suffixes", sym)
        return None

    last = df.iloc[-1]
    idx_date = last.name.date() if hasattr(last.name, "date") else None
    if idx_date and idx_date < today - timedelta(days=4):
        log.debug("yfinance returned stale row for %s: %s", used_sym, idx_date)
        return None

    try:
        ohlc = TodayOHLC(
            symbol=sym,
            open=float(last["Open"]),
            high=float(last["High"]),
            low=float(last["Low"]),
            ltp=float(last["Close"]),
            fetched_at=now,
        )
    except (KeyError, ValueError, TypeError) as exc:
        log.debug("yfinance row parse failed for %s: %s", sym, exc)
        return None

    with _cache_lock:
        _cache[sym] = ohlc
    return ohlc


def fetch_first_15m_high(symbol: str) -> float | None:
    """First-15-min high of today's session — the "Strong Start" trigger
    reference. Scans the 15m bar at 09:15-09:30 IST (first 15 minutes
    of NSE cash session).

    Returns None if 15-min data unavailable (yfinance is unreliable on
    intraday for some Indian small-caps) or if today's first bar hasn't
    formed yet (called before 09:30 IST).
    """
    sym = (symbol or "").strip().upper()
    if not sym:
        return None

    cache_key = f"{sym}::first15m"
    now = time.time()
    with _cache_lock:
        cached = _cache.get(cache_key)
        if cached and (now - cached.fetched_at) < CACHE_TTL_S * 5:
            # Once today's first bar is in, it doesn't change — cache
            # for 5 minutes (instead of 1) to save yfinance calls.
            return cached.high

    df = None
    for candidate in _yf_candidates(sym):
        # 5d window ensures we see today's bars even if today is the
        # first session post-weekend.
        df = _fetch_one_yf(candidate, period="5d", interval="15m")
        if df is not None and not df.empty:
            break

    if df is None or df.empty:
        return None

    from datetime import date as _date
    today = _date.today()
    # yfinance 15m index is timezone-aware UTC; first bar of NSE day is
    # 09:15 IST = 03:45 UTC. Filter to today's bars.
    try:
        df_today = df[df.index.date == today]
    except Exception:  # noqa: BLE001
        return None
    if df_today.empty:
        return None

    first_bar = df_today.iloc[0]
    high = float(first_bar["High"])

    with _cache_lock:
        _cache[cache_key] = TodayOHLC(
            symbol=sym, open=0, high=high, low=0, ltp=0, fetched_at=now,
        )
    return high


def fetch_many(symbols: list[str]) -> dict[str, TodayOHLC]:
    """Best-effort batch fetch. Sequential — yfinance's batch endpoint
    is finicky for Indian tickers; per-symbol with caching is more
    reliable. ~150ms per symbol uncached, instant cached.
    """
    out: dict[str, TodayOHLC] = {}
    for s in symbols:
        ohlc = fetch_today_ohlc(s)
        if ohlc is not None:
            out[s] = ohlc
    return out
