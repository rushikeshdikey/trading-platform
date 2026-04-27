"""Live-price refresh for open trades.

Primary source is Zerodha Kite Connect when a valid access token is present —
it covers every listed NSE/BSE/SME scrip accurately. Yahoo Finance (via
yfinance) is kept as a fallback for symbols Kite can't resolve, or while the
user hasn't logged into Kite yet.

A daemon thread runs in the background during IST market hours and refreshes
all open trades every `REFRESH_INTERVAL_SECONDS` seconds. Users can also hit
POST /prices/refresh for an on-demand update.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, time as dtime, timedelta, timezone
from typing import Iterable

from sqlalchemy.orm import Session

from .db import SessionLocal
from .models import InstrumentPrice, Trade

log = logging.getLogger("journal.prices")

# Probe order. NSE SME and BSE SME listings appear under the same suffixes on
# Yahoo (there is no `.SM` suffix). Adding a stripped variant handles symbols
# like "RELIANCE-EQ" that Zerodha exports with series suffixes.
EXCHANGE_SUFFIXES: tuple[str, ...] = (".NS", ".BO")

REFRESH_INTERVAL_SECONDS = 300  # 5 minutes
IST = timezone(timedelta(hours=5, minutes=30))
MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)


def _now_ist() -> datetime:
    return datetime.now(tz=IST)


def _market_open_now() -> bool:
    now = _now_ist()
    if now.weekday() >= 5:  # Saturday / Sunday
        return False
    return MARKET_OPEN <= now.time() <= MARKET_CLOSE


def _clean_symbol(symbol: str) -> str:
    """Canonicalise the journal symbol for Yahoo lookup.

    - Strip trailing series suffixes Zerodha exports (``-EQ``, ``-BE`` …).
    - Drop trailing ``.0`` / ``.00`` — artefact of BSE numeric scrip codes
      being parsed as floats on import.
    - Keep ``-SM`` because on NSE SME the dash-SM is literally part of the
      tradingsymbol (e.g. ``SKP-SM``).
    """
    s = symbol.strip().upper()
    if "." in s:
        base, _, tail = s.rpartition(".")
        if tail in {"0", "00"} and base:
            s = base
    if "-" in s:
        base, _, tail = s.partition("-")
        if tail in {"EQ", "BE", "BZ"}:
            s = base
    return s


def _suffix_order(symbol: str, preferred: str | None) -> list[str]:
    """Pick which exchange to probe first. All-digit symbols are BSE scrip
    codes and only resolve under ``.BO``."""
    if symbol.isdigit():
        return [".BO"]
    order: list[str] = []
    if preferred:
        order.append(preferred)
    for suf in EXCHANGE_SUFFIXES:
        if suf not in order:
            order.append(suf)
    return order


def _fetch_one(yf_symbol: str) -> float | None:
    """Return the last traded price for a full Yahoo symbol (e.g. RELIANCE.NS)."""
    try:
        import yfinance as yf  # local import — keeps boot cheap when unused
    except ImportError:
        log.warning("yfinance not installed; skipping price fetch for %s", yf_symbol)
        return None
    try:
        ticker = yf.Ticker(yf_symbol)
        price = ticker.fast_info.last_price
    except Exception as exc:  # noqa: BLE001 — yfinance raises many shapes
        log.debug("fetch failed for %s: %s", yf_symbol, exc)
        return None
    if price is None:
        return None
    try:
        price = float(price)
    except (TypeError, ValueError):
        return None
    if price <= 0:
        return None
    # yfinance returns float32 values which serialise as 85.9999938964844
    # and then blow up HTML5 step="0.01" validation on the Edit form.
    return round(price, 2)


def _fetch_one_with_prev(yf_symbol: str) -> tuple[float | None, float | None]:
    """Return (last_price, prev_close). Either can be None on failure."""
    try:
        import yfinance as yf
    except ImportError:
        return None, None
    try:
        ticker = yf.Ticker(yf_symbol)
        fi = ticker.fast_info
        last = fi.last_price
        prev = getattr(fi, "previous_close", None) or getattr(fi, "regular_market_previous_close", None)
    except Exception as exc:  # noqa: BLE001
        log.debug("fetch failed for %s: %s", yf_symbol, exc)
        return None, None
    def _clean(v):
        try:
            v = float(v)
        except (TypeError, ValueError):
            return None
        return round(v, 2) if v > 0 else None
    return _clean(last), _clean(prev)


def _resolve_via_kite(db: Session, symbol: str) -> tuple[str, str] | None:
    """Use Kite's instrument master (free tier) to map a journal symbol to the
    authoritative ``(tradingsymbol, yahoo_suffix)`` pair.

    Handles NSE/BSE/SME correctly: ``ALPEXSOLAR`` → (``ALPEXSOLAR-SM``, ``.NS``),
    ``539195`` → (``POEL``, ``.BO``), ``SKP-SM`` → (``SKP-SM``, ``.NS``).
    Returns None if the symbol isn't in the master.

    The ``kite_instruments`` table is SHARED — populated whenever ANY user
    clicks "Sync instruments". No per-user auth check needed: if the row
    exists we use it. (Earlier code called ``is_authed(db)`` here, but
    ``is_authed`` takes a User — the AttributeError was being silently
    eaten by the except clause and the Kite-authoritative path NEVER ran.
    Result: every NSE-SME symbol with a ``-SM`` suffix on Kite was falling
    through to the bare-symbol heuristic and failing on Yahoo.)
    """
    try:
        from . import kite as kite_svc
        inst = kite_svc._resolve_instrument(db, symbol)
    except Exception as exc:  # noqa: BLE001
        log.debug("kite resolve failed for %s: %s", symbol, exc)
        return None
    if inst is None:
        return None
    suffix = ".NS" if inst.exchange == "NSE" else ".BO"
    return inst.tradingsymbol, suffix


def resolve_and_fetch(
    symbol: str, preferred_suffix: str | None = None, db: Session | None = None
) -> tuple[str, float, str, float | None] | None:
    """Return ``(suffix, price, resolved_tradingsymbol, prev_close)`` for a journal symbol.

    Resolution order:
      1. Kite instrument master (when authed) → Yahoo with authoritative symbol.
      2. Heuristic fallback: clean the symbol and probe .NS / .BO.
    """
    # Kite-authoritative path.
    if db is not None:
        kite_resolved = _resolve_via_kite(db, symbol)
        if kite_resolved is not None:
            tradingsymbol, suffix = kite_resolved
            price, prev = _fetch_one_with_prev(f"{tradingsymbol}{suffix}")
            if price is not None:
                return suffix, price, tradingsymbol, prev

    # Heuristic fallback — keeps the price refresh working pre-login.
    base = _clean_symbol(symbol)
    if not base:
        return None
    for suf in _suffix_order(base, preferred_suffix):
        price, prev = _fetch_one_with_prev(f"{base}{suf}")
        if price is not None:
            return suf, price, base, prev
    return None


def refresh_symbol(db: Session, symbol: str) -> InstrumentPrice:
    """Update (or create) the `InstrumentPrice` row for a symbol."""
    key = _clean_symbol(symbol)
    cache = db.get(InstrumentPrice, key)
    preferred = cache.yf_suffix if cache and cache.yf_suffix in (".NS", ".BO") else None
    result = resolve_and_fetch(key, preferred, db=db)
    if cache is None:
        cache = InstrumentPrice(symbol=key)
        db.add(cache)
    if result is None:
        cache.last_error = "no quote"
        return cache
    suf, price, _resolved, prev_close = result
    cache.yf_suffix = suf
    cache.last_price = price
    cache.prev_close = prev_close
    cache.updated_at = datetime.utcnow()
    cache.last_error = None
    return cache


def refresh_trades(db: Session, trades: Iterable[Trade]) -> dict:
    """Refresh CMP on each trade.

    `refresh_symbol` already does the right thing: it routes through the Kite
    instrument master (when authed) to find the correct exchange + tradingsymbol,
    then fetches the price from Yahoo. Kite's own LTP API is not called because
    it requires a paid subscription (₹2000/mo) and returns 403 on the free tier.
    """
    trades = list(trades)
    symbols = {_clean_symbol(t.instrument) for t in trades if t.instrument}
    if not symbols:
        return {"trades_updated": 0, "symbols_checked": 0, "failed_symbols": []}

    price_by_symbol: dict[str, float] = {}
    for sym in symbols:
        cache = refresh_symbol(db, sym)
        if cache.last_error is None and cache.last_price is not None:
            price_by_symbol[sym] = cache.last_price

    updated = 0
    for t in trades:
        p = price_by_symbol.get(_clean_symbol(t.instrument))
        if p is not None:
            t.cmp = p
            updated += 1

    db.commit()
    return {
        "trades_updated": updated,
        "symbols_checked": len(symbols),
        "failed_symbols": sorted(symbols - price_by_symbol.keys()),
    }


def refresh_all_open(db: Session) -> dict:
    trades = db.query(Trade).filter(Trade.status == "open").all()
    return refresh_trades(db, trades)


def last_refresh_at(db: Session) -> datetime | None:
    """The most recent updated_at across all cached instruments."""
    row = (
        db.query(InstrumentPrice)
        .filter(InstrumentPrice.updated_at.isnot(None))
        .order_by(InstrumentPrice.updated_at.desc())
        .first()
    )
    return row.updated_at if row else None


# -- Background refresher --------------------------------------------------

_thread_lock = threading.Lock()
_thread: threading.Thread | None = None


def _loop() -> None:
    log.info("price refresher started")
    while True:
        try:
            if _market_open_now():
                with SessionLocal() as db:
                    summary = refresh_all_open(db)
                    log.info("price refresh: %s", summary)
        except Exception:  # noqa: BLE001 — keep the loop alive
            log.exception("price refresh crashed; continuing")
        time.sleep(REFRESH_INTERVAL_SECONDS)


def start_background_refresher() -> None:
    """Idempotent — safe to call under uvicorn --reload."""
    global _thread
    with _thread_lock:
        if _thread is not None and _thread.is_alive():
            return
        _thread = threading.Thread(target=_loop, name="price-refresher", daemon=True)
        _thread.start()
