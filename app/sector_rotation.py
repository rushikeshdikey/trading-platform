"""Sector Rotation (RRG) — relative-strength quadrant chart.

Inspired by algoxr's Sector Rotation panel, but built from data we
already have (no new external data fetchers, no NSE blackhole risk).

Pipeline:

  1. Group symbols by Industry (NSE Total Market CSV's "Industry" column,
     surfaced via index_universe.industry_map()).
  2. For each industry, build a daily synthetic sector index by
     equal-weight-averaging constituent log-returns and chaining them into
     a price series. Equal-weight is fine for a relative-strength view —
     we're comparing sectors, not pricing them in rupees.
  3. Build a market baseline = equal-weight average of every constituent
     in every sector. This is our "market" for RS purposes.
  4. JdK RS-Ratio = rolling Z-score of (sector / market) ratio, scaled to
     centre at 100. Reading > 100 = sector outperforming.
  5. JdK RS-Momentum = rate-of-change of RS-Ratio over a shorter window.
     Reading > 100 = momentum still building.
  6. Quadrant assignment from the (RS-Ratio, RS-Momentum) point.

The output is a list of `SectorPoint` rows the template renders as a
scatter chart with a short trail per sector so the user can see the
sweep through quadrants.
"""
from __future__ import annotations

import logging
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Iterable

from sqlalchemy.orm import Session

from .models import DailyBar
from .scanner import bars_cache

log = logging.getLogger("journal.sector_rotation")

# Tuning — feel free to adjust. Defaults chosen so a fast-rotating sector
# completes one quadrant sweep in ~3-6 weeks of trading sessions.
LOOKBACK_DAYS = 120                 # how much history we need (~6 trading mo)
RS_RATIO_WINDOW = 30                # Z-score window for RS-Ratio
RS_MOMENTUM_WINDOW = 5              # RoC window for RS-Momentum
TRAIL_POINTS = 5                    # # of (RS-R, RS-M) points per sector
MIN_CONSTITUENTS_PER_SECTOR = 4     # ignore tiny sectors with too few names
MIN_BARS_PER_SYMBOL = 60            # skip recently-listed symbols

QUADRANT_LEADING   = "Leading"      # RS-R > 100, RS-M > 100
QUADRANT_WEAKENING = "Weakening"    # RS-R > 100, RS-M < 100
QUADRANT_LAGGING   = "Lagging"      # RS-R < 100, RS-M < 100
QUADRANT_IMPROVING = "Improving"    # RS-R < 100, RS-M > 100


def _classify(rs_ratio: float, rs_momentum: float) -> str:
    if rs_ratio >= 100 and rs_momentum >= 100:
        return QUADRANT_LEADING
    if rs_ratio >= 100 and rs_momentum < 100:
        return QUADRANT_WEAKENING
    if rs_ratio < 100 and rs_momentum < 100:
        return QUADRANT_LAGGING
    return QUADRANT_IMPROVING


@dataclass
class SectorPoint:
    """One sector's current position + a short trail of recent positions."""
    name: str
    constituents: int
    quadrant: str
    rs_ratio: float
    rs_momentum: float
    # trail[0] = oldest, trail[-1] = newest (== (rs_ratio, rs_momentum))
    trail: list[tuple[float, float]] = field(default_factory=list)
    # Latest cumulative return vs market over RS_RATIO_WINDOW
    relative_return_pct: float = 0.0
    # Multi-timeframe price return on the synthetic sector index. Computed
    # from the equal-weight constituent series (the same chain of returns
    # we use for RS); always vs the broad market, not absolute. NaN when
    # the lookback window doesn't have enough bars.
    return_1d_pct: float = 0.0
    return_1w_pct: float = 0.0
    return_1m_pct: float = 0.0
    return_3m_pct: float = 0.0
    # % of constituents trading above their 50-DMA. Read in `compute_sector_strength`.
    pct_above_50dma: float = 0.0
    # Number of scanner candidates currently firing in this sector (filled
    # in by `compute_sector_strength` from ScanCache).
    scanner_hits: int = 0


def _build_sector_series(
    sector_to_symbols: dict[str, list[str]],
    bars_map: dict[str, list],
    *, anchor_date: date,
) -> tuple[dict[str, list[float]], list[date]]:
    """Build {sector → daily price series} aligned to a common date grid.

    Algorithm: for each trading date present in the universe, compute the
    mean log-return across the constituents that have a bar for both that
    date and the prior date. Chain those into a starting-at-1.0 price.
    Sectors with fewer than MIN_CONSTITUENTS_PER_SECTOR usable names are
    dropped — the noise floor is too high otherwise.
    """
    # Union of all dates seen in any bars_map entry, capped at anchor.
    all_dates: set[date] = set()
    for syms in sector_to_symbols.values():
        for s in syms:
            for b in bars_map.get(s, ()):
                if b.date <= anchor_date:
                    all_dates.add(b.date)
    sorted_dates = sorted(all_dates)
    if len(sorted_dates) < 2:
        return {}, []

    # For each symbol, build close-by-date dict for fast lookup.
    sym_close: dict[str, dict[date, float]] = {}
    for syms in sector_to_symbols.values():
        for s in syms:
            bars = bars_map.get(s, ())
            if len(bars) < MIN_BARS_PER_SYMBOL:
                continue
            sym_close[s] = {b.date: b.close for b in bars if b.date <= anchor_date}

    sector_series: dict[str, list[float]] = {}
    for sector, syms in sector_to_symbols.items():
        usable = [s for s in syms if s in sym_close]
        if len(usable) < MIN_CONSTITUENTS_PER_SECTOR:
            continue
        prices = [1.0]
        for i in range(1, len(sorted_dates)):
            d_prev, d_cur = sorted_dates[i - 1], sorted_dates[i]
            log_returns = []
            for s in usable:
                p_prev = sym_close[s].get(d_prev)
                p_cur = sym_close[s].get(d_cur)
                if p_prev and p_cur and p_prev > 0:
                    log_returns.append(math.log(p_cur / p_prev))
            if log_returns:
                avg = sum(log_returns) / len(log_returns)
                prices.append(prices[-1] * math.exp(avg))
            else:
                prices.append(prices[-1])
        sector_series[sector] = prices
    return sector_series, sorted_dates


def _rs_series(sector_prices: list[float], market_prices: list[float]) -> list[float]:
    """Raw relative-strength ratio (sector / market), aligned indices."""
    n = min(len(sector_prices), len(market_prices))
    if n == 0:
        return []
    # Normalize both to start at 1.0 so RS starts at 1.0 — same time origin.
    s0 = sector_prices[0] or 1.0
    m0 = market_prices[0] or 1.0
    return [
        (sector_prices[i] / s0) / (market_prices[i] / m0)
        for i in range(n)
    ]


def _zscore_rebased(series: list[float], window: int) -> list[float]:
    """Rolling Z-score, recentred at 100 (each std-dev = +/- 5 points).
    Output length = len(series); first ``window`` entries are filled with
    100 (neutral) until enough data exists."""
    out: list[float] = []
    for i, x in enumerate(series):
        if i < window:
            out.append(100.0)
            continue
        win = series[i - window:i]
        mu = sum(win) / window
        var = sum((v - mu) ** 2 for v in win) / window
        sigma = math.sqrt(var) if var > 0 else 1e-9
        out.append(100.0 + ((x - mu) / sigma) * 5.0)
    return out


def _roc_rebased(series: list[float], window: int) -> list[float]:
    """Rate-of-change of an RS-Ratio series, rebased to 100.

    Definition: RS-Mom[t] = 100 + (RS-Ratio[t] - RS-Ratio[t-window]).
    Above 100 means RS-Ratio is rising (momentum building); below means
    rolling over.
    """
    out: list[float] = []
    for i, x in enumerate(series):
        if i < window:
            out.append(100.0)
            continue
        out.append(100.0 + (x - series[i - window]))
    return out


def compute_rotation(db: Session, *, lookback_days: int = LOOKBACK_DAYS) -> list[SectorPoint]:
    """End-to-end: build sector series → RS-Ratio → RS-Momentum → points.

    Groups by COARSE-GRAINED parent sector, not fine-grained industry. So
    "Power - Distribution" (2 stocks) rolls up to "Power" (30+ stocks)
    instead of being dropped for too-few constituents.

    The fine-grained tag is preserved for display — the symbol_quadrant_map
    helper exposes both. Always returns a list (possibly empty if data is
    too sparse). Never raises; logs and skips on any per-symbol issue.
    """
    from .scanner import index_universe as idx_uni

    # Symbol → industry map. Skips symbols with no industry tagged.
    try:
        ind_map = idx_uni.industry_map()
    except Exception as exc:  # noqa: BLE001
        log.warning("industry map unavailable: %s", exc)
        ind_map = {}

    if not ind_map:
        return []

    # Hierarchical grouping:
    #   1. First try grouping at the FINE industry level (Capital Markets,
    #      Power - Generation, etc.) — preserves rotation signal for splits.
    #   2. Industries with <MIN_CONSTITUENTS_PER_SECTOR fall back to PARENT
    #      ("Power - Distribution" with 2 stocks → "Power" with 30+).
    #   3. The parent fallback bucket aggregates ALL the parent's stocks
    #      (not just the orphaned ones), so the inherited quadrant reflects
    #      the broad sector trend, not a small fragment of it.
    industry_constituents: dict[str, list[str]] = defaultdict(list)
    for sym, industry in ind_map.items():
        if industry:
            industry_constituents[industry].append(sym)

    # Decide which industries are large enough for their own RRG point.
    kept_industries = {
        ind for ind, syms in industry_constituents.items()
        if len(syms) >= MIN_CONSTITUENTS_PER_SECTOR
    }

    sector_to_symbols: dict[str, list[str]] = defaultdict(list)
    for sym, industry in ind_map.items():
        if not industry:
            continue
        if industry in kept_industries:
            sector_to_symbols[industry].append(sym)
            continue
        # Industry too small to RRG on its own — fall back to parent.
        parent = idx_uni.industry_to_sector(industry)
        # Parent bucket pools EVERY parent-sector symbol so the signal is
        # broad. We rebuild parent membership from scratch here.
        # (Done in second pass below — for now, mark with parent placeholder.)
        sector_to_symbols[f"__parent__{parent}"].append(sym)

    # Second pass: for each parent placeholder, replace with the FULL list
    # of stocks under that parent (across all child industries, kept or not).
    parent_to_all_syms: dict[str, list[str]] = defaultdict(list)
    for sym, industry in ind_map.items():
        if industry:
            parent_to_all_syms[idx_uni.industry_to_sector(industry)].append(sym)

    rebuilt: dict[str, list[str]] = {}
    for key, syms in sector_to_symbols.items():
        if key.startswith("__parent__"):
            parent = key[len("__parent__"):]
            # Use the full parent-sector pool. The orphaned stocks will
            # inherit this parent's quadrant via symbol_quadrant_map.
            rebuilt[parent] = parent_to_all_syms[parent]
        else:
            rebuilt[key] = syms
    sector_to_symbols = rebuilt

    # Pull bars only for the symbols we care about — keeps memory reasonable.
    all_syms = [s for syms in sector_to_symbols.values() for s in syms]
    bars_map = bars_cache.bars_by_symbol(db, all_syms, lookback_days=lookback_days)

    anchor = date.today()
    # Walk back to the latest day with bars (handles weekends + early calls).
    for s in all_syms:
        bars = bars_map.get(s) or []
        if bars:
            anchor = max(anchor, bars[-1].date)
            break

    sector_series, dates = _build_sector_series(
        sector_to_symbols, bars_map, anchor_date=anchor,
    )
    if not sector_series:
        return []

    # Market baseline = equal-weight average of every kept sector's series.
    n_dates = min(len(s) for s in sector_series.values())
    market = [
        sum(sector_series[k][i] for k in sector_series) / len(sector_series)
        for i in range(n_dates)
    ]

    out: list[SectorPoint] = []
    for sector, prices in sector_series.items():
        rs = _rs_series(prices[:n_dates], market)
        rs_ratio_series = _zscore_rebased(rs, RS_RATIO_WINDOW)
        rs_mom_series = _roc_rebased(rs_ratio_series, RS_MOMENTUM_WINDOW)

        if len(rs_ratio_series) < TRAIL_POINTS:
            continue

        trail = list(zip(
            rs_ratio_series[-TRAIL_POINTS:],
            rs_mom_series[-TRAIL_POINTS:],
        ))
        rs_r = trail[-1][0]
        rs_m = trail[-1][1]
        # Sector cumulative return vs market over the RS-Ratio window.
        if len(rs) >= RS_RATIO_WINDOW + 1 and rs[-RS_RATIO_WINDOW - 1] > 0:
            relative_return_pct = (rs[-1] / rs[-RS_RATIO_WINDOW - 1] - 1) * 100
        else:
            relative_return_pct = 0.0

        # Multi-timeframe sector returns (absolute, not vs market).
        s_prices = sector_series[sector][:n_dates]
        ret = lambda lookback: (
            (s_prices[-1] / s_prices[-1 - lookback] - 1) * 100
            if len(s_prices) > lookback and s_prices[-1 - lookback] > 0
            else 0.0
        )
        return_1d = ret(1)
        return_1w = ret(5)
        return_1m = ret(21)
        return_3m = ret(63)

        out.append(SectorPoint(
            name=sector,
            constituents=len(sector_to_symbols[sector]),
            quadrant=_classify(rs_r, rs_m),
            rs_ratio=round(rs_r, 2),
            rs_momentum=round(rs_m, 2),
            trail=[(round(a, 2), round(b, 2)) for a, b in trail],
            relative_return_pct=round(relative_return_pct, 2),
            return_1d_pct=round(return_1d, 2),
            return_1w_pct=round(return_1w, 2),
            return_1m_pct=round(return_1m, 2),
            return_3m_pct=round(return_3m, 2),
        ))

    # Order: Leading first (most actionable), then Improving, Weakening, Lagging.
    quadrant_order = {
        QUADRANT_LEADING: 0, QUADRANT_IMPROVING: 1,
        QUADRANT_WEAKENING: 2, QUADRANT_LAGGING: 3,
    }
    out.sort(key=lambda p: (quadrant_order[p.quadrant], -p.rs_ratio))
    return out


def latest_anchor_date(db: Session) -> date | None:
    """Last bar date in the cache — used for the page's 'as-of' headline."""
    from sqlalchemy import func
    row = db.query(func.max(DailyBar.date)).scalar()
    return row


def compute_sector_strength(db: Session, *, lookback_days: int = LOOKBACK_DAYS) -> list[SectorPoint]:
    """Like compute_rotation, but also fills pct_above_50dma + scanner_hits.

    Used by the /sectors heatmap page. compute_rotation alone is enough for
    the RRG chart; this wrapper adds the breadth + scanner-firing signals
    that turn the heatmap into a "what's actionable" view.
    """
    from .scanner import index_universe as idx_uni
    from .scanner import bars_cache, runner as scanner_runner

    points = compute_rotation(db, lookback_days=lookback_days)
    if not points:
        return []

    # 1. % above 50-DMA per sector. Reload bars (compute_rotation already did,
    # but it dropped them); re-using would need a refactor. Cheap enough.
    ind_map = idx_uni.industry_map()
    parent_map = {sym: idx_uni.industry_to_sector(ind) for sym, ind in ind_map.items()}
    # For each SectorPoint name, build the symbol set:
    sector_symbols: dict[str, list[str]] = defaultdict(list)
    for sym, ind in ind_map.items():
        # Match against both the fine industry AND the parent — SectorPoint
        # names can be either ("Capital Markets" or "Power").
        sector_symbols[ind].append(sym)
        sector_symbols[parent_map[sym]].append(sym)

    all_syms = list({s for syms in sector_symbols.values() for s in syms})
    bars_map = bars_cache.bars_by_symbol(db, all_syms, lookback_days=80)

    def _pct_above_50dma(syms: list[str]) -> float:
        n_above, n_total = 0, 0
        for s in syms:
            bars = bars_map.get(s) or []
            if len(bars) < 50:
                continue
            closes = [b.close for b in bars[-50:]]
            sma50 = sum(closes) / 50
            if bars[-1].close > sma50:
                n_above += 1
            n_total += 1
        return (100.0 * n_above / n_total) if n_total > 0 else 0.0

    # 2. Scanner hits per sector — count distinct symbols across all scans.
    cached = scanner_runner.latest_cached_all(db, max_age_minutes=72 * 60)
    sector_hits: dict[str, set[str]] = defaultdict(set)
    if cached is not None:
        results, _rows = cached
        for _scan_type, candidates in results.items():
            for c in candidates:
                ind = ind_map.get(c.symbol)
                if not ind:
                    continue
                # Add to BOTH the fine industry bucket AND the parent — each
                # SectorPoint should see its own hits.
                sector_hits[ind].add(c.symbol)
                sector_hits[parent_map[c.symbol]].add(c.symbol)

    # 3. Stamp the enrichments onto each SectorPoint.
    for p in points:
        syms = sector_symbols.get(p.name, [])
        p.pct_above_50dma = round(_pct_above_50dma(syms), 1)
        p.scanner_hits = len(sector_hits.get(p.name, set()))

    return points


# ---------------------------------------------------------------------------
# Symbol → quadrant lookup (used by composite scoring).
#
# `compute_rotation` is expensive (loads bars for ~750 symbols, runs Z-score
# windows). We cache by anchor_date so the lookup is free on subsequent
# scoring calls within the same trading day.
# ---------------------------------------------------------------------------

_quadrant_cache: dict | None = None  # {"as_of": date, "map": {symbol: quadrant}}


def symbol_quadrant_map(db: Session) -> dict[str, str]:
    """Return a {symbol → quadrant} dict for use in composite scoring.

    Untagged symbols (not in NSE Total Market) get no entry — caller treats
    that as "unknown sector, neutral multiplier". Cached for the trading day.
    """
    global _quadrant_cache
    from .scanner import index_universe as idx_uni

    anchor = latest_anchor_date(db)
    if anchor is None:
        return {}

    if _quadrant_cache is not None and _quadrant_cache.get("as_of") == anchor:
        return _quadrant_cache["map"]

    # Recompute. compute_rotation groups at the parent-sector level, so
    # we look up via industry_to_sector() to find each symbol's coarse
    # bucket and read its quadrant from there.
    try:
        rotation = compute_rotation(db)
        ind_map = idx_uni.industry_map()
    except Exception as exc:  # noqa: BLE001
        log.warning("symbol_quadrant_map: rotation failed: %s", exc)
        return {}

    sector_to_quadrant = {p.name: p.quadrant for p in rotation}
    out: dict[str, str] = {}
    for sym, industry in ind_map.items():
        # Hierarchical lookup: try the fine industry first (e.g.
        # "Capital Markets"). If not classified — usually because it had
        # <MIN_CONSTITUENTS — fall back to the parent ("Financial Services").
        q = sector_to_quadrant.get(industry)
        if not q:
            parent = idx_uni.industry_to_sector(industry)
            q = sector_to_quadrant.get(parent)
        if q:
            out[sym] = q

    _quadrant_cache = {"as_of": anchor, "map": out}
    log.info("symbol_quadrant_map: %d tagged symbols across %d sector buckets",
             len(out), len(sector_to_quadrant))
    return out


def invalidate_quadrant_cache() -> None:
    """Drop the cached quadrant lookup (after sector recompute or daily roll)."""
    global _quadrant_cache
    _quadrant_cache = None
