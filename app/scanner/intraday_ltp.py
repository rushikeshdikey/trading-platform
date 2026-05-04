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


def _resolve_yf_symbol(symbol: str) -> str:
    """NSE-listed Indian stocks need the .NS suffix on yfinance."""
    s = (symbol or "").strip().upper()
    if not s:
        return s
    if s.endswith(".NS") or s.endswith(".BO"):
        return s
    return f"{s}.NS"


def fetch_today_ohlc(symbol: str) -> TodayOHLC | None:
    """Return today's running OHLC for ``symbol`` or None on any failure.

    Cached for ``CACHE_TTL_S`` seconds. Two requests for the same symbol
    inside the window share the same fetch — important when /cockpit
    renders 5 picks and the user hits refresh.
    """
    sym = (symbol or "").strip().upper()
    if not sym:
        return None

    now = time.time()
    with _cache_lock:
        cached = _cache.get(sym)
        if cached and (now - cached.fetched_at) < CACHE_TTL_S:
            return cached

    try:
        import yfinance as yf
        from datetime import date, timedelta

        yf_sym = _resolve_yf_symbol(sym)
        ticker = yf.Ticker(yf_sym)
        # period='2d' covers today + yesterday; we want today's row.
        # Using 1d interval; during market hours today's row updates with
        # running OHLC, after close it's the final settled bar.
        df = ticker.history(period="2d", interval="1d", auto_adjust=False)
        if df is None or df.empty:
            log.debug("yfinance empty for %s", yf_sym)
            return None

        last = df.iloc[-1]
        # Sanity: ensure last row corresponds to today (yfinance occasionally
        # lags by a session over weekends/holidays).
        idx_date = last.name.date() if hasattr(last.name, "date") else None
        today = date.today()
        if idx_date and idx_date < today - timedelta(days=4):
            # More than 4 days old — definitely not "today" — bail.
            log.debug("yfinance returned stale row for %s: %s", yf_sym, idx_date)
            return None

        ohlc = TodayOHLC(
            symbol=sym,
            open=float(last["Open"]),
            high=float(last["High"]),
            low=float(last["Low"]),
            ltp=float(last["Close"]),
            fetched_at=now,
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("yfinance fetch failed for %s: %s", sym, exc)
        return None

    with _cache_lock:
        _cache[sym] = ohlc
    return ohlc


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
