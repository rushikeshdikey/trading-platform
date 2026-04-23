"""Historical OHLC fetcher for the trade-detail chart.

Uses Kite's instrument master (when authed) to resolve the journal symbol to
the correct Yahoo ticker + exchange, then pulls daily OHLC via yfinance.
Kite's own `historical_data` API is on the ₹2000/mo tier, so this stays on
the free stack.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from sqlalchemy.orm import Session

log = logging.getLogger("journal.market_data")


def _yahoo_symbol(db: Session, journal_symbol: str) -> str | None:
    """Mirror of the logic used for price refreshes — Kite master first, then heuristic."""
    try:
        from . import kite as kite_svc
        if kite_svc.is_authed(db):
            inst = kite_svc._resolve_instrument(db, journal_symbol)
            if inst is not None:
                suffix = ".NS" if inst.exchange == "NSE" else ".BO"
                return f"{inst.tradingsymbol}{suffix}"
    except Exception as exc:  # noqa: BLE001
        log.debug("kite resolve failed: %s", exc)

    # Heuristic fallback — uses the same cleaner as prices.py
    from . import prices

    base = prices._clean_symbol(journal_symbol)
    if not base:
        return None
    if base.isdigit():
        return f"{base}.BO"
    return f"{base}.NS"


def fetch_ohlc(
    db: Session,
    journal_symbol: str,
    start: date,
    end: date,
) -> list[dict[str, Any]]:
    """Return daily OHLC bars as a list of dicts; empty on any failure."""
    yahoo_sym = _yahoo_symbol(db, journal_symbol)
    if not yahoo_sym:
        return []
    try:
        import yfinance as yf
    except ImportError:
        return []

    try:
        ticker = yf.Ticker(yahoo_sym)
        # yfinance's end is exclusive — extend by one day so `end` is included.
        df = ticker.history(
            start=start.isoformat(),
            end=(end + timedelta(days=1)).isoformat(),
            interval="1d",
            auto_adjust=False,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("yfinance history failed for %s: %s", yahoo_sym, exc)
        return []

    if df is None or df.empty:
        return []

    import math

    bars: list[dict[str, Any]] = []
    for idx, row in df.iterrows():
        try:
            d = idx.date() if hasattr(idx, "date") else idx
            vals = [float(row["Open"]), float(row["High"]), float(row["Low"]), float(row["Close"])]
            if any(math.isnan(v) or math.isinf(v) or v <= 0 for v in vals):
                continue  # holidays / missing rows come through as NaN — skip them
            bars.append(
                {
                    "date": d.isoformat(),
                    "open": round(vals[0], 2),
                    "high": round(vals[1], 2),
                    "low": round(vals[2], 2),
                    "close": round(vals[3], 2),
                }
            )
        except (ValueError, KeyError, TypeError):
            continue
    return bars
