"""IBD-style Relative Strength Rating (1-99 percentile).

Cross-sectional rank of every stock in the scan universe by a weighted
trailing-return composite, normalised to the 1-99 IBD scale. Front-loads
the most recent quarter so the rating is responsive to fresh trends but
not noisy on a single bad week.

Used by:
  - Minervini Trend Template (gate: RS ≥ 70)
  - Cockpit conviction scoring (boost candidates with RS ≥ 80)
  - Display column on every scanner

Caching:
  - Computed once per scan run (single bulk pull of bars_cache).
  - 5-minute TTL in-memory cache so back-to-back page loads don't
    recompute. Same TTL pattern as gated_universe_breakdown.
"""
from __future__ import annotations

import logging
import time
from typing import Iterable

from sqlalchemy.orm import Session

from . import bars_cache
from . import universe as universe_mod

log = logging.getLogger("journal.scanner.rs_rating")

# Lookback windows in TRADING bars (≈ market days). 252 covers a full
# year at standard NSE/BSE schedules.
LOOKBACK_BARS = 252
WINDOWS = {
    "r3m":  63,    # 3 months
    "r6m":  126,   # 6 months
    "r9m":  189,   # 9 months
    "r12m": 252,   # 12 months
}

# IBD's classic weighting — most recent quarter dominates so the rating
# turns over fast enough to catch new leaders, but the 12-month tail keeps
# established trends.
WEIGHTS = {"r3m": 0.40, "r6m": 0.20, "r9m": 0.20, "r12m": 0.20}

# Minimum bars a symbol needs to receive a rating. We require AT LEAST the
# 3-month window — without it the front-loaded weight collapses to zero
# and we'd be ranking on noise.
MIN_BARS_FOR_RATING = 63

# Cache: symbol → rating int 1..99
_cache: dict = {"at": 0.0, "value": None}
_TTL_S = 300


def _weighted_return(closes: list[float]) -> float | None:
    """Compute the weighted-return composite for one symbol.
    Returns None if not enough history for the 3-month window."""
    n = len(closes)
    if n < MIN_BARS_FOR_RATING:
        return None
    last = closes[-1]
    if last <= 0:
        return None
    total = 0.0
    weight_used = 0.0
    for window_name, bars_back in WINDOWS.items():
        if n < bars_back + 1:
            continue
        ref = closes[-bars_back - 1]
        if ref <= 0:
            continue
        ret = (last / ref) - 1.0
        w = WEIGHTS[window_name]
        total += ret * w
        weight_used += w
    if weight_used == 0:
        return None
    # Re-normalise so a symbol with only 3-month history isn't penalised
    # against one with full 12-month history.
    return total / weight_used


def compute_ratings(db: Session, symbols: Iterable[str] | None = None) -> dict[str, int]:
    """Return {symbol → 1..99 RS rating} for the universe.

    Uses an in-memory 5-min cache. Pass an explicit ``symbols`` list to
    bypass cache (useful for tests).
    """
    cached = _cache
    now = time.time()
    if symbols is None and cached["value"] is not None and (now - cached["at"]) < _TTL_S:
        return cached["value"]

    if symbols is None:
        symbols = universe_mod.universe_from_cache(db)
    symbols = list(symbols)

    bars_map = bars_cache.bars_by_symbol(db, symbols, lookback_days=380)

    # Pull weighted returns per symbol.
    pairs: list[tuple[str, float]] = []
    for sym in symbols:
        bars = bars_map.get(sym) or []
        if len(bars) < MIN_BARS_FOR_RATING:
            continue
        closes = [b.close for b in bars]
        wr = _weighted_return(closes)
        if wr is None:
            continue
        pairs.append((sym, wr))

    if not pairs:
        out: dict[str, int] = {}
    else:
        # Percentile-rank into 1..99. Sort ascending → index gives the rank.
        pairs.sort(key=lambda p: p[1])
        n = len(pairs)
        out = {}
        for i, (sym, _) in enumerate(pairs):
            # Map rank 0..n-1 onto 1..99 inclusive.
            rating = 1 + int(round((i / max(1, n - 1)) * 98))
            out[sym] = rating

    if symbols is None or len(out) > 100:
        # Only cache "full universe" computations — partial calls bypass.
        _cache["at"] = now
        _cache["value"] = out
    return out


def latest_for(db: Session, symbol: str) -> int | None:
    """Convenience: rating for a single symbol if available."""
    ratings = compute_ratings(db)
    return ratings.get(symbol.upper())
