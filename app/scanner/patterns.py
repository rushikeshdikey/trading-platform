"""Pattern detectors for the three chartsmaze-style setups.

Pure functions. Each takes ``list[Bar]`` → ``Candidate | None``. All thresholds
are hardcoded at the top of this module — keep them in one place so tuning is
a single-file edit.

Setups:
- ``horizontal_resistance`` — stock approaching a clustered prior-high zone.
- ``trendline_setup`` — stock near a fitted rising trendline of swing lows.
- ``tight_setup`` — volatility contracting inside a compressed base.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Sequence

import numpy as np

from .bars_cache import Bar


# -- Hardcoded thresholds (single source of truth) ----------------------------

LOOKBACK_BARS = 180
PIVOT_STRENGTH = 5
# 60 trading bars = ~3 months. Lowered from 120 because prod's bars cache
# only goes ~70 days deep until you've run multiple refresh cycles, and the
# old 120 gate locked the user out of EVERY signal (funnel showed 0/4397).
# Detectors that genuinely need more history (Tightness Trading, Base on Base)
# do their own len(bars) check inside and silently skip shallow symbols —
# that's the correct behaviour for those particular patterns.
MIN_BARS = 60
MIN_ADV20_RS = 2_00_00_000.0  # ₹2 Cr average daily traded value
MIN_PRICE = 20.0

# Horizontal Resistance (tuned against chartsmaze-style base breakouts)
HR_TOLERANCE_PCT = 0.02                  # pivots within 2% band cluster
HR_MIN_TOUCHES = 2                        # minimum pivots in the cluster
HR_MIN_PIVOT_SPACING = 10                 # bars between counted touches
HR_PROXIMITY_BELOW_MAX = 0.04             # close within 4% below level (approaching)
HR_PROXIMITY_ABOVE_MAX = 0.015            # close up to 1.5% above (fresh breakout)
HR_LAST_TOUCH_MAX_BARS_AGO = 60           # most recent touch in last 60 bars
HR_FIRST_TOUCH_MIN_BARS_AGO = 20          # oldest touch ≥ 20 bars back (real base)
HR_BASE_LOW_MAX_DRAWDOWN = 0.35           # base low no more than 35% below level
HR_RECENT_TIGHT_BARS = 15                 # tightness window just before today
HR_RECENT_TIGHT_RANGE_MAX = 0.10          # last 15 bars' (hi-lo)/close ≤ 10%
HR_NOT_EXTENDED_SMA = 20                  # 20-SMA still below the level

# Trendline (rising, from swing lows)
TL_MIN_R_SQUARED = 0.92
TL_PROXIMITY_MAX = 0.03        # close within 3% of projected trendline today
TL_MIN_PIVOT_SPAN = 20         # first→last pivot span in bars
TL_MIN_TOUCHES = 3

# Tight Setup
TIGHT_ATR14_PCT_MAX = 0.025
TIGHT_RANGE20_PCT_MAX = 0.10
TIGHT_ATR_CONTRACTION = 0.8    # atr14 < atr50 * 0.8
TIGHT_SL_PAD_BELOW_LOW = 0.01  # SL 1% below base low

# Tightness Trading (Ankur Patel "Focus on one setup" — 5 phases)
#   Phase windows (bars back from today):
#     tight  = last 10 bars
#     base   = 35 bars before that  (bars -45..-10)
#     upmove = 75 bars before that  (bars -120..-45)
TT_TIGHT_BARS = 10
TT_BASE_BARS = 35
TT_UPMOVE_BARS = 75
TT_LOOKBACK = TT_TIGHT_BARS + TT_BASE_BARS + TT_UPMOVE_BARS  # 120

TT_UPMOVE_MIN_GAIN = 0.30            # ≥ 30% low→high across upmove window
TT_BASE_MAX_DRAWDOWN = 0.20          # base low ≤ 20% below upmove high
TT_BASE_MAX_RANGE_PCT = 0.20         # base range ≤ 20% of close
TT_BASE_VOL_RATIO = 0.80             # base avg vol < 80% of upmove avg vol
TT_TIGHT_VOL_RATIO = 0.75            # tight avg vol < 75% of base avg vol
TT_TIGHT_RANGE_PCT_MAX = 0.06        # last 10-bar range ≤ 6% of close
TT_TIGHT_ATR5_PCT_MAX = 0.022        # ATR(5)/close ≤ 2.2%
TT_BREAKOUT_TOLERANCE = 1.01         # close may be up to 1% above base high


@dataclass
class Candidate:
    symbol: str
    scan_type: str
    score: float
    close: float
    suggested_entry: float
    suggested_sl: float
    extras: dict = field(default_factory=dict)


# -- Helpers ------------------------------------------------------------------


def _closes(bars: Sequence[Bar]) -> np.ndarray:
    return np.array([b.close for b in bars], dtype=float)


def _highs(bars: Sequence[Bar]) -> np.ndarray:
    return np.array([b.high for b in bars], dtype=float)


def _lows(bars: Sequence[Bar]) -> np.ndarray:
    return np.array([b.low for b in bars], dtype=float)


def _volumes(bars: Sequence[Bar]) -> np.ndarray:
    return np.array([b.volume for b in bars], dtype=float)


def _atr(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int) -> float | None:
    """Wilder's ATR over the most recent ``period`` bars. None if not enough data."""
    if len(highs) < period + 1:
        return None
    tr = np.maximum(
        highs[1:] - lows[1:],
        np.maximum(
            np.abs(highs[1:] - closes[:-1]),
            np.abs(lows[1:] - closes[:-1]),
        ),
    )
    if len(tr) < period:
        return None
    return float(tr[-period:].mean())


def _swing_high_pivots(highs: np.ndarray, strength: int) -> list[int]:
    """Indices where high[i] > all highs in ±strength window (strict)."""
    n = len(highs)
    pivots: list[int] = []
    for i in range(strength, n - strength):
        window = highs[i - strength : i + strength + 1]
        if highs[i] == window.max() and (window == highs[i]).sum() == 1:
            pivots.append(i)
    return pivots


def _swing_low_pivots(lows: np.ndarray, strength: int) -> list[int]:
    n = len(lows)
    pivots: list[int] = []
    for i in range(strength, n - strength):
        window = lows[i - strength : i + strength + 1]
        if lows[i] == window.min() and (window == lows[i]).sum() == 1:
            pivots.append(i)
    return pivots


def _linreg(xs: np.ndarray, ys: np.ndarray) -> tuple[float, float, float]:
    """Return (slope, intercept, r²). xs/ys must be >= 2 points."""
    if len(xs) < 2:
        return 0.0, 0.0, 0.0
    slope, intercept = np.polyfit(xs, ys, 1)
    y_pred = slope * xs + intercept
    ss_res = float(((ys - y_pred) ** 2).sum())
    ss_tot = float(((ys - ys.mean()) ** 2).sum())
    r2 = 1.0 - (ss_res / ss_tot) if ss_tot > 0 else 0.0
    return float(slope), float(intercept), r2


def _passes_liquidity(bars: Sequence[Bar]) -> bool:
    if len(bars) < MIN_BARS:
        return False
    last_close = bars[-1].close
    if last_close < MIN_PRICE:
        return False
    closes = _closes(bars[-20:])
    vols = _volumes(bars[-20:])
    adv20 = float((closes * vols).mean())
    return adv20 >= MIN_ADV20_RS


# -- Horizontal Resistance ----------------------------------------------------


def horizontal_resistance(symbol: str, bars: Sequence[Bar]) -> Candidate | None:
    """Chartsmaze-style horizontal resistance.

    Quality gates in order (any failure → return None):

    1. Liquidity + price floor (``_passes_liquidity``).
    2. Pivot cluster: ≥ 2 swing-high pivots within a 2% band, spaced ≥ 10 bars.
    3. **Base age**: oldest touch ≥ 20 bars back (not a fly-by double-top).
    4. **Base freshness**: newest touch ≤ 60 bars back (not ancient history).
    5. **Base depth**: base low ≤ 35% below the level (not free-falling).
    6. **Proximity**: close within 4% below or 1.5% above the level (covers
       both "approaching" and "fresh breakout" setups).
    7. **Recent tightness**: last 15 bars' (high-low)/close ≤ 8% — the stock
       is actually coiling under the line, not volatile-flying-by.
    8. Not extended: 20-SMA still below the level.
    """
    if not _passes_liquidity(bars):
        return None
    bars = list(bars[-LOOKBACK_BARS:])
    highs = _highs(bars)
    lows = _lows(bars)
    closes = _closes(bars)
    if len(bars) < MIN_BARS:
        return None
    last_close = float(closes[-1])
    today_idx = len(bars) - 1

    pivot_idx = _swing_high_pivots(highs, PIVOT_STRENGTH)
    if len(pivot_idx) < HR_MIN_TOUCHES:
        return None

    # Cluster pivots within 2% band.
    best: tuple[float, list[int]] | None = None
    for anchor_i in pivot_idx:
        anchor_price = highs[anchor_i]
        band_hi = anchor_price * (1 + HR_TOLERANCE_PCT)
        band_lo = anchor_price * (1 - HR_TOLERANCE_PCT)
        members = [i for i in pivot_idx if band_lo <= highs[i] <= band_hi]
        if len(members) < HR_MIN_TOUCHES:
            continue
        filtered: list[int] = []
        for i in sorted(members):
            if not filtered or i - filtered[-1] >= HR_MIN_PIVOT_SPACING:
                filtered.append(i)
        if len(filtered) < HR_MIN_TOUCHES:
            continue
        level = float(np.mean([highs[i] for i in filtered]))
        if best is None or len(filtered) > len(best[1]):
            best = (level, filtered)

    if best is None:
        return None
    level, members = best

    # Base age: oldest touch reasonably old, newest touch reasonably recent.
    bars_since_first = today_idx - members[0]
    bars_since_last = today_idx - members[-1]
    if bars_since_first < HR_FIRST_TOUCH_MIN_BARS_AGO:
        return None
    if bars_since_last > HR_LAST_TOUCH_MAX_BARS_AGO:
        return None

    # Proximity: close within tolerance band around the level.
    if last_close > level * (1 + HR_PROXIMITY_ABOVE_MAX):
        return None  # too extended above the line
    if last_close < level:
        distance_pct = (level - last_close) / level
        if distance_pct > HR_PROXIMITY_BELOW_MAX:
            return None
    else:
        distance_pct = -((last_close - level) / level)  # negative = above line

    # Base depth: how far has price pulled back from the level during the base?
    base_window_start = members[0]
    base_low = float(lows[base_window_start:].min())
    base_drawdown = (level - base_low) / level if level > 0 else 1.0
    if base_drawdown > HR_BASE_LOW_MAX_DRAWDOWN:
        return None

    # Recent tightness: coil just under the line, not wild oscillation.
    recent_high = float(highs[-HR_RECENT_TIGHT_BARS:].max())
    recent_low_tight = float(lows[-HR_RECENT_TIGHT_BARS:].min())
    recent_range_pct = (recent_high - recent_low_tight) / last_close
    if recent_range_pct > HR_RECENT_TIGHT_RANGE_MAX:
        return None

    # Not extended — 20-SMA still below the level.
    sma20 = float(closes[-HR_NOT_EXTENDED_SMA:].mean())
    if sma20 >= level:
        return None

    # Entries.
    suggested_entry = round(level * 1.005, 2)                 # 0.5% above line
    suggested_sl = round(recent_low_tight * 0.99, 2)          # 1% below recent tight low
    if suggested_sl >= suggested_entry:
        return None

    # Score: more touches, tighter coil, closer to the line, more-recent touch.
    proximity_bonus = (1 - abs(distance_pct)) * 10
    tightness_bonus = (1 - recent_range_pct / HR_RECENT_TIGHT_RANGE_MAX) * 3
    recency_bonus = (1 - bars_since_last / HR_LAST_TOUCH_MAX_BARS_AGO) * 2
    score = len(members) * 2 + proximity_bonus + tightness_bonus + recency_bonus

    return Candidate(
        symbol=symbol,
        scan_type="horizontal_resistance",
        score=round(float(score), 3),
        close=round(last_close, 2),
        suggested_entry=suggested_entry,
        suggested_sl=suggested_sl,
        extras={
            "resistance_level": round(level, 2),
            "touches": len(members),
            "last_touch_date": bars[members[-1]].date.isoformat(),
            "distance_pct": round(distance_pct * 100, 2),
            "recent_tight_pct": round(recent_range_pct * 100, 2),
            "base_drawdown_pct": round(base_drawdown * 100, 1),
            "bars_since_last_touch": bars_since_last,
            "base_span_bars": bars_since_first,
            "sma20": round(sma20, 2),
        },
    )


# -- Trendline Setup ----------------------------------------------------------


def trendline_setup(symbol: str, bars: Sequence[Bar]) -> Candidate | None:
    """Rising trendline drawn through swing lows.

    Only rising trendlines are emitted (long setup). For falling trendlines
    (short setups) a separate scan type would be cleaner — out of scope here.
    """
    if not _passes_liquidity(bars):
        return None
    bars = list(bars[-LOOKBACK_BARS:])
    lows = _lows(bars)
    closes = _closes(bars)
    if len(bars) < MIN_BARS:
        return None
    last_close = float(closes[-1])

    pivots = _swing_low_pivots(lows, PIVOT_STRENGTH)
    if len(pivots) < TL_MIN_TOUCHES:
        return None

    # Use all swing lows — let linreg accept only well-aligned ones. Require
    # positive slope, high R², and enough time span.
    xs = np.array(pivots, dtype=float)
    ys = np.array([lows[i] for i in pivots], dtype=float)
    slope, intercept, r2 = _linreg(xs, ys)

    if slope <= 0 or r2 < TL_MIN_R_SQUARED:
        return None
    if pivots[-1] - pivots[0] < TL_MIN_PIVOT_SPAN:
        return None

    today_idx = len(bars) - 1
    trendline_today = float(slope * today_idx + intercept)
    if trendline_today <= 0:
        return None
    proximity = abs(last_close - trendline_today) / last_close
    if proximity > TL_PROXIMITY_MAX:
        return None

    # Only emit if price is at or above the line (not already broken below).
    if last_close < trendline_today * 0.98:
        return None

    suggested_entry = round(last_close * 1.005, 2)
    suggested_sl = round(trendline_today * (1 - 0.015), 2)  # 1.5% below the line

    if suggested_sl >= suggested_entry:
        return None

    score = r2 * len(pivots) + max(0.0, 1 - proximity) * 2
    return Candidate(
        symbol=symbol,
        scan_type="trendline_setup",
        score=round(float(score), 3),
        close=round(last_close, 2),
        suggested_entry=suggested_entry,
        suggested_sl=suggested_sl,
        extras={
            "trendline_today": round(trendline_today, 2),
            "slope_per_day": round(slope, 4),
            "r_squared": round(r2, 3),
            "touches": len(pivots),
            "proximity_pct": round(proximity * 100, 2),
        },
    )


# -- Tight Setup --------------------------------------------------------------


def tight_setup(symbol: str, bars: Sequence[Bar]) -> Candidate | None:
    if not _passes_liquidity(bars):
        return None
    bars = list(bars[-LOOKBACK_BARS:])
    highs = _highs(bars)
    lows = _lows(bars)
    closes = _closes(bars)
    if len(bars) < MIN_BARS:
        return None
    last_close = float(closes[-1])

    atr14 = _atr(highs, lows, closes, 14)
    atr50 = _atr(highs, lows, closes, 50)
    if atr14 is None or atr50 is None or atr50 == 0:
        return None

    atr14_pct = atr14 / last_close
    if atr14_pct > TIGHT_ATR14_PCT_MAX:
        return None

    base_high = float(highs[-20:].max())
    base_low = float(lows[-20:].min())
    range20_pct = (base_high - base_low) / last_close
    if range20_pct > TIGHT_RANGE20_PCT_MAX:
        return None

    if atr14 >= atr50 * TIGHT_ATR_CONTRACTION:
        return None  # not contracting

    # 50-SMA flat-or-rising over the last 20 bars.
    sma50_now = float(closes[-50:].mean())
    sma50_prev = float(closes[-70:-20].mean()) if len(closes) >= 70 else sma50_now
    if sma50_now < sma50_prev:
        return None

    # Close must be inside the base, not already breaking out.
    if last_close > base_high or last_close < base_low:
        return None

    suggested_entry = round(base_high * 1.005, 2)
    suggested_sl = round(base_low * (1 - TIGHT_SL_PAD_BELOW_LOW), 2)

    if suggested_sl >= suggested_entry:
        return None

    score = (atr50 / atr14) / max(range20_pct, 1e-4)
    return Candidate(
        symbol=symbol,
        scan_type="tight_setup",
        score=round(float(score), 3),
        close=round(last_close, 2),
        suggested_entry=suggested_entry,
        suggested_sl=suggested_sl,
        extras={
            "base_high": round(base_high, 2),
            "base_low": round(base_low, 2),
            "atr14": round(atr14, 3),
            "atr14_pct": round(atr14_pct * 100, 2),
            "range20_pct": round(range20_pct * 100, 2),
            "atr_contraction": round(atr14 / atr50, 3),
        },
    )


# -- Tightness Trading (Ankur Patel) ------------------------------------------


def tightness_trading(symbol: str, bars: Sequence[Bar]) -> Candidate | None:
    """Focus-on-one-setup: prior strong upmove → basing phase → low-volume dry-up
    → tight-range consolidation → buy points A (cheat) and B (breakout).

    Each of the 5 phases must pass independently. Volume dry-up is the hardest
    gate — it's why this produces way fewer candidates than plain Tight Setup
    but with higher follow-through potential.
    """
    if not _passes_liquidity(bars):
        return None
    bars = list(bars[-TT_LOOKBACK:])
    if len(bars) < TT_LOOKBACK:
        return None

    highs = _highs(bars)
    lows = _lows(bars)
    closes = _closes(bars)
    vols = _volumes(bars)
    last_close = float(closes[-1])

    # Phase slices. Newest-last indexing, negative offsets.
    tight_slice = slice(-TT_TIGHT_BARS, None)
    base_slice = slice(-(TT_TIGHT_BARS + TT_BASE_BARS), -TT_TIGHT_BARS)
    upmove_slice = slice(
        -(TT_TIGHT_BARS + TT_BASE_BARS + TT_UPMOVE_BARS),
        -(TT_TIGHT_BARS + TT_BASE_BARS),
    )

    tight_hi = float(highs[tight_slice].max())
    tight_lo = float(lows[tight_slice].min())
    base_hi = float(highs[base_slice].max())
    base_lo = float(lows[base_slice].min())
    upmove_hi = float(highs[upmove_slice].max())
    upmove_lo = float(lows[upmove_slice].min())

    # 1. Strong upmove (≥ 30% gain within the upmove window).
    if upmove_lo <= 0:
        return None
    upmove_gain = (upmove_hi - upmove_lo) / upmove_lo
    if upmove_gain < TT_UPMOVE_MIN_GAIN:
        return None

    # 2. Basing phase: not a deep pullback, moderate range.
    base_drawdown = (upmove_hi - base_lo) / upmove_hi if upmove_hi > 0 else 1.0
    if base_drawdown > TT_BASE_MAX_DRAWDOWN:
        return None
    base_range_pct = (base_hi - base_lo) / last_close
    if base_range_pct > TT_BASE_MAX_RANGE_PCT:
        return None

    # 3. Volume dry-up: base < upmove, tight < base.
    upmove_vol = float(vols[upmove_slice].mean())
    base_vol = float(vols[base_slice].mean())
    tight_vol = float(vols[tight_slice].mean())
    if upmove_vol <= 0 or base_vol <= 0:
        return None
    if base_vol / upmove_vol > TT_BASE_VOL_RATIO:
        return None
    if tight_vol / base_vol > TT_TIGHT_VOL_RATIO:
        return None

    # 4. Tight range consolidation on the right side of the base.
    tight_range_pct = (tight_hi - tight_lo) / last_close
    if tight_range_pct > TT_TIGHT_RANGE_PCT_MAX:
        return None
    atr5 = _atr(highs, lows, closes, 5)
    if atr5 is None or atr5 / last_close > TT_TIGHT_ATR5_PCT_MAX:
        return None

    # Price must be inside the tight range (not already broken out too far).
    if last_close < tight_lo * 0.99 or last_close > base_hi * TT_BREAKOUT_TOLERANCE:
        return None

    # 5. Buy points.
    buy_point_a = round(tight_hi * 1.005, 2)   # cheat — break of tight high
    buy_point_b = round(base_hi * 1.005, 2)    # confirmation — break of base high

    suggested_entry = buy_point_a              # prefer earlier entry (better R:R)
    suggested_sl = round(tight_lo * 0.99, 2)   # 1% below tight low

    if suggested_sl >= suggested_entry:
        return None

    vol_dryup = 1 - (tight_vol / upmove_vol)
    score = (upmove_gain * vol_dryup) / max(tight_range_pct, 1e-4)

    return Candidate(
        symbol=symbol,
        scan_type="tightness_trading",
        score=round(float(score), 3),
        close=round(last_close, 2),
        suggested_entry=suggested_entry,
        suggested_sl=suggested_sl,
        extras={
            "upmove_gain_pct": round(upmove_gain * 100, 1),
            "base_drawdown_pct": round(base_drawdown * 100, 1),
            "base_high": round(base_hi, 2),
            "tight_high": round(tight_hi, 2),
            "tight_low": round(tight_lo, 2),
            "tight_range_pct": round(tight_range_pct * 100, 2),
            "vol_dryup_pct": round(vol_dryup * 100, 0),
            "buy_point_a": buy_point_a,
            "buy_point_b": buy_point_b,
        },
    )


# -- Institutional Buying (accumulation) --------------------------------------

# Window over which we count accumulation vs distribution days.
INST_LOOKBACK_BARS = 25
INST_VOL_AVG_BARS = 50              # baseline for "above-average volume"
INST_VOL_SPIKE_RATIO = 1.25         # volume must be ≥ 1.25× 50-day avg
INST_RANGE_UPPER_PCT = 0.5          # close in upper 50% of day range = accum
INST_MIN_NET_DAYS = 5               # net (accum - distrib) ≥ 5 over 25 bars
INST_MIN_ACCUM_DAYS = 6             # absolute accum days ≥ 6 (not just netting)
INST_MIN_GAIN_PCT = 0.05            # underlying uptrend bias: 25-bar return ≥ 5%
INST_MIN_VS_50DMA = 1.0             # must be above 50-DMA — not buying weakness


def institutional_buying(symbol: str, bars: Sequence[Bar]) -> Candidate | None:
    """Smart-money accumulation detector — William O'Neil style.

    An "accumulation day" is: close in the upper 50% of day range AND
    close > prior close AND volume > 1.25× 50-day avg. A "distribution
    day" is the mirror image (lower half, close < prior, same volume
    spike). We score the net count over the last 25 bars.

    Quality gates: standard liquidity, plus the symbol must be in a mild
    uptrend (25-bar return ≥ 2%) — otherwise a stock falling in a
    waterfall produces fake accumulation days off oversold bounces.
    """
    if not _passes_liquidity(bars):
        return None
    bars = list(bars[-LOOKBACK_BARS:])
    if len(bars) < INST_VOL_AVG_BARS + INST_LOOKBACK_BARS:
        return None

    closes = _closes(bars)
    highs = _highs(bars)
    lows = _lows(bars)
    vols = _volumes(bars)
    last_close = float(closes[-1])

    # Underlying uptrend bias — the stock must be in a confirmed uptrend,
    # otherwise volume spikes off oversold bounces look like accumulation.
    ref_close = float(closes[-INST_LOOKBACK_BARS])
    gain_pct = (last_close - ref_close) / ref_close
    if gain_pct < INST_MIN_GAIN_PCT:
        return None
    sma50 = float(closes[-INST_VOL_AVG_BARS:].mean())
    if last_close < sma50 * INST_MIN_VS_50DMA:
        return None

    # 50-bar baseline volume, computed at each window position so a
    # gradual volume rise doesn't read as a constant spike.
    accum = 0
    distrib = 0
    accum_days_dates: list = []
    for i in range(len(bars) - INST_LOOKBACK_BARS, len(bars)):
        if i < INST_VOL_AVG_BARS:
            continue
        baseline = vols[i - INST_VOL_AVG_BARS:i].mean()
        if baseline <= 0:
            continue
        if vols[i] < baseline * INST_VOL_SPIKE_RATIO:
            continue
        rng = highs[i] - lows[i]
        if rng <= 0:
            continue
        upper_close = (closes[i] - lows[i]) / rng
        prev_close = closes[i - 1]
        if upper_close >= INST_RANGE_UPPER_PCT and closes[i] > prev_close:
            accum += 1
            accum_days_dates.append(bars[i].date)
        elif upper_close < (1 - INST_RANGE_UPPER_PCT) and closes[i] < prev_close:
            distrib += 1

    net = accum - distrib
    if accum < INST_MIN_ACCUM_DAYS or net < INST_MIN_NET_DAYS:
        return None

    # Sizing scaffolding: enter at last close, SL at 25-bar low.
    base_low = float(lows[-INST_LOOKBACK_BARS:].min())
    suggested_sl = round(base_low * 0.98, 2)
    suggested_entry = round(last_close, 2)

    # Score: heavier accumulation + cleaner net = higher score.
    score = round(accum * 4.0 + net * 3.0 + min(gain_pct * 100, 15), 2)

    return Candidate(
        symbol=symbol,
        scan_type="institutional_buying",
        score=score,
        close=last_close,
        suggested_entry=suggested_entry,
        suggested_sl=suggested_sl,
        extras={
            "accum_days": accum,
            "distrib_days": distrib,
            "net_days": net,
            "gain_25b_pct": round(gain_pct * 100, 2),
            "last_accum_date": accum_days_dates[-1].isoformat() if accum_days_dates else None,
        },
    )


# -- Base on Base (continuation) ----------------------------------------------

# Two-stage horizontal-resistance pattern. Windows sized to fit inside
# the 120-bar minimum guaranteed by MIN_BARS / bars cache lookback.
#   prior base    : bars [-120:-60]  (60 bars, oldest)
#   breakout zone : bars  [-60:-40]  (20 bars, transition)
#   current base  : bars  [-40:  0]  (40 bars, today's pivot)
BOB_PRIOR_START = -120
BOB_PRIOR_END = -60
BOB_BREAKOUT_END = -40
BOB_CURRENT_LEN = 40
BOB_MIN_STEP_UP_PCT = 0.03          # second base level ≥ 3% above first
BOB_PROXIMITY_MAX = 0.07            # close within 7% below current pivot
BOB_NO_FAIL_TOLERANCE = 0.05        # didn't close > 5% below R1 after breakout
BOB_BREAKOUT_MARGIN = 0.02          # close > R1 * 1.02 = real breakout


def _cluster_pivot(highs: np.ndarray, start_idx: int, end_idx: int,
                   strength: int = PIVOT_STRENGTH,
                   tolerance_pct: float = HR_TOLERANCE_PCT,
                   min_touches: int = 2) -> float | None:
    """Find the dominant resistance cluster in highs[start:end]. Returns the
    cluster's mean level or None if no qualifying cluster exists."""
    if end_idx - start_idx < strength * 2 + 5:
        return None
    section = highs[start_idx:end_idx]
    pivots = _swing_high_pivots(section, strength)
    if len(pivots) < min_touches:
        return None
    pivot_vals = [section[p] for p in pivots]
    # Greedy clustering: pick highest pivot as anchor, gather neighbours.
    pivot_vals.sort(reverse=True)
    anchor = pivot_vals[0]
    cluster = [v for v in pivot_vals if abs(v - anchor) / anchor <= tolerance_pct]
    if len(cluster) < min_touches:
        return None
    return float(sum(cluster) / len(cluster))


def base_on_base(symbol: str, bars: Sequence[Bar]) -> Candidate | None:
    """Detect a base-on-base continuation pattern.

    Pipeline:
      1. Find R1 — swing-high cluster in bars[-180:-90].
      2. Confirm breakout — some bar between -90 and -50 closes ≥ R1×1.02.
      3. Confirm price held — no close < R1×0.97 in last 90 bars.
      4. Find R2 — swing-high cluster in last 90 bars.
      5. R2 ≥ R1 × 1.05 (real step up).
      6. Today's close within 5% below R2 (approaching the new pivot).
    """
    if not _passes_liquidity(bars):
        return None
    bars = list(bars[-LOOKBACK_BARS:])
    if len(bars) < abs(BOB_PRIOR_START):
        return None

    highs = _highs(bars)
    closes = _closes(bars)
    lows = _lows(bars)
    n = len(bars)

    # Step 1: R1 from the prior base window.
    r1 = _cluster_pivot(highs, n + BOB_PRIOR_START, n + BOB_PRIOR_END)
    if r1 is None:
        return None

    # Step 2: at least one close in the breakout zone cleared R1.
    breakout_slice = closes[n + BOB_PRIOR_END:n + BOB_BREAKOUT_END]
    if len(breakout_slice) == 0 or float(breakout_slice.max()) < r1 * (1 + BOB_BREAKOUT_MARGIN):
        return None

    # Step 3: price never collapsed back below R1 by more than tolerance.
    post_breakout_closes = closes[n + BOB_PRIOR_END:]
    if float(post_breakout_closes.min()) < r1 * (1 - BOB_NO_FAIL_TOLERANCE):
        return None

    # Step 4: R2 from the current base window.
    r2 = _cluster_pivot(highs, n - BOB_CURRENT_LEN, n)
    if r2 is None:
        return None

    # Step 5: meaningful step up.
    if r2 < r1 * (1 + BOB_MIN_STEP_UP_PCT):
        return None

    # Step 6: proximity to the new pivot.
    last_close = float(closes[-1])
    distance_pct = (r2 - last_close) / r2
    if distance_pct < 0 or distance_pct > BOB_PROXIMITY_MAX:
        return None

    base_low = float(lows[-BOB_CURRENT_LEN:].min())
    suggested_entry = round(r2 * 1.005, 2)
    suggested_sl = round(base_low * 0.99, 2)

    # Score: bigger step up + closer proximity = higher score.
    step_up_pct = (r2 - r1) / r1 * 100
    proximity_bonus = (1 - distance_pct / BOB_PROXIMITY_MAX) * 5
    score = round(step_up_pct + proximity_bonus, 2)

    return Candidate(
        symbol=symbol,
        scan_type="base_on_base",
        score=score,
        close=last_close,
        suggested_entry=suggested_entry,
        suggested_sl=suggested_sl,
        extras={
            "prior_base_level": round(r1, 2),
            "current_base_level": round(r2, 2),
            "step_up_pct": round(step_up_pct, 2),
            "distance_pct": round(distance_pct * 100, 2),
        },
    )


# -- Dispatch -----------------------------------------------------------------

SCAN_TYPES = {
    "horizontal_resistance": ("Horizontal Resistance", horizontal_resistance),
    "trendline_setup": ("Trendline Setup", trendline_setup),
    "tight_setup": ("Tight Setup", tight_setup),
    "tightness_trading": ("Tightness Trading", tightness_trading),
    "institutional_buying": ("Institutional Buying", institutional_buying),
    "base_on_base": ("Base on Base", base_on_base),
}
