"""Recently-listed IPO finder.

The bhavcopy doesn't tag listing events, so we infer "this symbol is a
recent IPO" from how many bars it has in the cache:

  - Established stocks (RELIANCE, TCS, …) have the **full** cache depth
    (≈ 125 trading days, capped by our 180-day calendar lookback).
  - A genuine IPO listed in the last few weeks will have **fewer bars**
    than the cache's max depth, by exactly the difference between
    listing-date and the cache's earliest day.

So we compare each symbol's bar count to the cache's max bar count and
flag the gaps. ``IPO_MAX_BARS`` is the upper bound — any stock with at
most that many bars is "younger than ~6 months in the cache." This
correctly excludes RELIANCE et al. even on a freshly-bootstrapped cache.

Caveats:
  - We cannot detect IPOs older than the cache. To surface listings from
    > 6 months ago we'd need a longer cache, or a separate listing-date
    feed from NSE corporate actions (TODO).
  - A symbol that briefly stops trading and resumes might also look new.
    Rare in practice.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..models import DailyBar
from . import universe as universe_mod

# Bar-count threshold that separates "recent IPO" from "established stock."
# Empirically: cache's max depth is ~125 trading days. Anything substantially
# below that = this symbol started trading after the cache's earliest day.
# Tune via the env if your cache is shallower or deeper.
IPO_MAX_BARS = 100

# Hide microcap noise: anything below this current price is not really
# investable for swing traders.
IPO_MIN_PRICE = 30.0


@dataclass
class IPOEntry:
    symbol: str
    listed_on: date
    days_listed: int
    ipo_open: float          # open of first bar
    last_close: float
    pct_change: float        # last_close vs ipo_open
    high_since: float        # max high since listing
    low_since: float         # min low since listing
    pct_from_high: float     # how far below the post-listing high we are


def recent_ipos(db: Session, *, max_bars: int = IPO_MAX_BARS) -> list[IPOEntry]:
    """List symbols with fewer than ``max_bars`` bars in the cache —
    those are "newer than the cache's earliest day," i.e. probable IPOs.
    Newest listings (smallest bar count) first. ETFs / funds are excluded."""
    # Per-symbol bar count — one aggregate query.
    rows = (
        db.query(
            DailyBar.symbol,
            func.count(DailyBar.id).label("bar_count"),
        )
        .group_by(DailyBar.symbol)
        .having(func.count(DailyBar.id) <= max_bars)
        .all()
    )
    if not rows:
        return []

    candidate_syms = {r.symbol for r in rows}

    # ETF / fund filter — same machinery the scanners use.
    etf_excluded = set(candidate_syms) - set(universe_mod.universe_from_cache(db))
    candidate_syms -= etf_excluded

    if not candidate_syms:
        return []

    # Pull the first bar (for IPO open) and last bar (for current close)
    # plus high/low extremes, all in one bulk query and group in Python.
    bars = (
        db.query(DailyBar)
        .filter(DailyBar.symbol.in_(candidate_syms))
        .order_by(DailyBar.symbol.asc(), DailyBar.date.asc())
        .all()
    )
    by_sym: dict[str, list[DailyBar]] = {}
    for b in bars:
        by_sym.setdefault(b.symbol, []).append(b)

    out: list[IPOEntry] = []
    for sym, sym_bars in by_sym.items():
        if not sym_bars:
            continue
        first = sym_bars[0]
        last = sym_bars[-1]
        if last.close < IPO_MIN_PRICE:
            continue
        ipo_open = first.open or first.close
        if ipo_open <= 0:
            continue
        high_since = max(b.high for b in sym_bars)
        low_since = min(b.low for b in sym_bars)
        days_listed = (last.date - first.date).days
        out.append(IPOEntry(
            symbol=sym,
            listed_on=first.date,
            days_listed=days_listed,
            ipo_open=round(ipo_open, 2),
            last_close=round(last.close, 2),
            pct_change=round((last.close - ipo_open) / ipo_open * 100, 2),
            high_since=round(high_since, 2),
            low_since=round(low_since, 2),
            pct_from_high=round((last.close - high_since) / high_since * 100, 2),
        ))
    out.sort(key=lambda e: e.listed_on, reverse=True)
    return out
