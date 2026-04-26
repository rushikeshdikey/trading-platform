"""Stop-loss picker — chooses one consistent SL per candidate.

Default rule for Indian swing trading: **SL = Previous Day Low (PDL)**
(the last completed daily bar's low, padded slightly so wicks don't
trigger on the gap-down open). PDL is the canonical Indian intraday/
swing SL — when the trader enters at next-session open, yesterday's
low is "PDL" and that's the natural stop.

For very-tight setups (coiling stocks), we may pick a TIGHTER stop:
last 3-bar low or 2× ATR(7), whichever is closer to entry — gives
the trader a smaller R per trade when the chart genuinely allows it.

Wider-than-PDL stops (5-bar / 10-bar / swing low) are NOT used as
defaults — the trader doesn't want to widen beyond PDL.

We never *reject* candidates based on SL distance. The trader sees
the SL% prominently in the row and decides themselves whether the
risk fits — that's a per-trade decision, not a system-wide gate.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import numpy as np

from .bars_cache import Bar


# Tunable thresholds. Single source of truth.
MIN_SL_PCT = 0.005       # 0.5% — below this, the stop is just noise (wick stop)
PAD_PCT = 0.005          # 0.5% — pad below structural lows so wicks don't trigger


@dataclass
class TightSL:
    """Result of compute_tight_sl. Always returns a valid SL (no rejection)."""
    price: float
    method: str          # "PDL" / "3-bar low" / "2×ATR(7)" — explains where SL came from
    sl_pct: float        # SL distance as fraction of entry (0.018 = 1.8%)


def _atr(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int) -> float | None:
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


def compute_tight_sl(bars: Sequence[Bar], entry: float) -> TightSL:
    """Pick a stop-loss for a long entry.

    Default = **PDL (Previous Day Low)** — the last completed daily bar's
    low, padded by 0.5% so a small wick on the gap-down open doesn't
    trigger. PDL is the canonical Indian swing SL and is what the trader
    would set on the broker side anyway.

    If a tighter, structurally meaningful stop exists (last-3-bar low or
    2×ATR(7) — both still ≥ 0.5% below entry, never a wick stop), and
    is closer to entry than PDL, use that instead. This gives a smaller
    R per trade on legitimately coiling setups.

    Always returns a valid SL — never rejects the trade.
    """
    if not bars or entry <= 0:
        # Pathological — fall back to a 2.5% mechanical stop just so
        # callers always get a usable number.
        return TightSL(price=round(entry * 0.975, 2), method="fallback (no bars)", sl_pct=0.025)

    highs = np.array([b.high for b in bars], dtype=float)
    lows = np.array([b.low for b in bars], dtype=float)
    closes = np.array([b.close for b in bars], dtype=float)

    # Default: PDL = last completed daily bar's low, padded.
    pdl = float(lows[-1]) * (1 - PAD_PCT)

    # Candidates that may be TIGHTER than PDL (closer to entry).
    tighter_options: list[tuple[float, str]] = []

    # Last 3-bar low — only counts if it's actually higher (tighter) than PDL.
    if len(lows) >= 3:
        three_bar = float(lows[-3:].min()) * (1 - PAD_PCT)
        if three_bar > pdl:
            tighter_options.append((three_bar, "3-bar low"))

    # 2× ATR(7) below entry — volatility-adjusted.
    atr7 = _atr(highs, lows, closes, 7)
    if atr7 is not None:
        atr_stop = entry - 2 * atr7
        if atr_stop > pdl:
            tighter_options.append((atr_stop, "2×ATR(7)"))

    # Pick PDL by default; replace if a TIGHTER option is meaningful
    # (still ≥ MIN_SL_PCT below entry, otherwise it's wick noise).
    chosen_price, chosen_method = pdl, "PDL"
    for price, method in tighter_options:
        if (entry - price) / entry >= MIN_SL_PCT and price > chosen_price:
            chosen_price, chosen_method = price, method

    sl_pct = max(0.0, (entry - chosen_price) / entry)
    return TightSL(
        price=round(chosen_price, 2),
        method=chosen_method,
        sl_pct=sl_pct,
    )
