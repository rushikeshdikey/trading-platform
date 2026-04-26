"""Tight stop-loss framework — psychology-first SL selection.

The user's hard constraint: cannot tolerate >2.5% SL on swing trades.
Standard "structural" stops (PDL, DL, 20-bar swing low) routinely
produce 4-8% risk per trade, which violates psychology. The fix is to
*reject* trades whose tightest viable stop is too wide, rather than
present them with a wide stop the user shouldn't take.

Algorithm:
  1. Compute every structurally meaningful stop level for a long entry:
       - last 3-bar low (intraday-equivalent on daily bars)
       - 2× ATR(7) below entry
       - last 5-bar low
       - last 10-bar low
       - last 20-bar low (the classic swing-low stop)
  2. Pad each by 0.5% (real prices undershoot intra-bar)
  3. Pick the **tightest** stop that is at least MIN_SL_PCT below entry
     (otherwise the stop is below noise — guaranteed to hit on a wick).
  4. If that stop is wider than HARD_REJECT_PCT, return None — the trade
     is rejected entirely. The detector that called us returns None too.
  5. If it's wider than SOFT_CAP_PCT, fall back to a mechanical SOFT_CAP
     stop ("if it can't hold a 2.5% pullback, the thesis is wrong").

Used by every detector in patterns.py — no detector picks its own SL
anymore. This makes the user's psychology the gate, not the detector
author's preference.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import numpy as np

from .bars_cache import Bar


# Tunable thresholds. Single source of truth.
MIN_SL_PCT = 0.005       # 0.5% — below this, the stop is just noise (wick stop)
SOFT_CAP_PCT = 0.025     # 2.5% — preferred max distance from entry
HARD_REJECT_PCT = 0.04   # 4.0% — beyond this we don't take the trade at all
PAD_PCT = 0.005          # 0.5% — pad below structural lows so wicks don't trigger


@dataclass
class TightSL:
    """Result of compute_tight_sl. ``price=None`` means rejected."""
    price: float | None
    method: str          # "3-bar low" / "ATR" / "5-bar low" / "soft cap (mechanical)" / "rejected"
    sl_pct: float        # SL distance as fraction of entry (0.018 = 1.8%)
    rejected_reason: str = ""


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
    """Pick the tightest structurally meaningful SL below ``entry``.

    Returns a TightSL where ``price=None`` means the trade should be
    rejected because no acceptable SL exists. Detectors should bail on
    None.
    """
    if not bars or entry <= 0:
        return TightSL(price=None, method="rejected", sl_pct=0.0,
                       rejected_reason="no bars or invalid entry")

    highs = np.array([b.high for b in bars], dtype=float)
    lows = np.array([b.low for b in bars], dtype=float)
    closes = np.array([b.close for b in bars], dtype=float)

    candidates: list[tuple[float, str]] = []

    # 1. Last 3-bar low (tightest structural stop on daily bars)
    if len(lows) >= 3:
        v = float(lows[-3:].min()) * (1 - PAD_PCT)
        candidates.append((v, "3-bar low"))

    # 2. 2× ATR(7) below entry — volatility-adjusted, often very tight on coiling stocks
    atr7 = _atr(highs, lows, closes, 7)
    if atr7 is not None:
        v = entry - 2 * atr7
        candidates.append((v, "2×ATR(7)"))

    # 3. Last 5-bar low
    if len(lows) >= 5:
        v = float(lows[-5:].min()) * (1 - PAD_PCT)
        candidates.append((v, "5-bar low"))

    # 4. Last 10-bar low
    if len(lows) >= 10:
        v = float(lows[-10:].min()) * (1 - PAD_PCT)
        candidates.append((v, "10-bar low"))

    # Filter to candidates that are at least MIN_SL_PCT below entry
    # (otherwise wick noise will trigger them).
    valid = [
        (price, method) for price, method in candidates
        if price > 0 and (entry - price) / entry >= MIN_SL_PCT
    ]

    if not valid:
        # Even the loosest stop is too tight — fall back to soft cap.
        return TightSL(
            price=round(entry * (1 - SOFT_CAP_PCT), 2),
            method="soft cap (mechanical)",
            sl_pct=SOFT_CAP_PCT,
        )

    # Pick the TIGHTEST valid stop (highest SL price = closest to entry)
    valid.sort(key=lambda x: x[0], reverse=True)
    best_price, best_method = valid[0]
    best_pct = (entry - best_price) / entry

    if best_pct > HARD_REJECT_PCT:
        # Even the tightest structural stop is too wide for this user's
        # psychology. Reject the trade.
        return TightSL(
            price=None, method="rejected", sl_pct=best_pct,
            rejected_reason=f"tightest SL is {best_pct*100:.1f}% — exceeds {HARD_REJECT_PCT*100:.0f}% cap",
        )

    if best_pct > SOFT_CAP_PCT:
        # Use mechanical soft-cap stop instead of the wider structural one.
        return TightSL(
            price=round(entry * (1 - SOFT_CAP_PCT), 2),
            method=f"soft cap (mechanical, {best_method} would be {best_pct*100:.1f}%)",
            sl_pct=SOFT_CAP_PCT,
        )

    return TightSL(
        price=round(best_price, 2),
        method=best_method,
        sl_pct=best_pct,
    )
