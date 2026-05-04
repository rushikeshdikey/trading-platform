"""Entry-type recommender for Auto-Pilot picks.

Maps each scanner setup to its **natural entry type** and computes the
specific trigger price that should fire the BUY order. This converts the
cockpit from "buy at the close price we cached yesterday" to "use a
``Pivot Break`` entry above ₹X with this trigger logic, because that's
how a Tightness Trading B-point breakout actually works in practice."

The 6 entry types we support (Strong Start is beta — needs 15-min
intraday data which yfinance is unreliable on for Indian tickers):

  1. PDH Entry            — break above Previous Day's High (continuation)
  2. Strong Start Entry   — break above first 5/15/30-min high (BETA)
  3. Pivot Break Entry    — break above multi-tested resistance level
  4. Anticipation Entry   — buy NOW inside a tight consolidation
  5. Pullback Entry       — bounce off rising 10-EMA (Minervini-style)
  6. Inside Bar Entry     — break above mother-bar high after inside day

Scanner → entry-type natural mapping (defaults; trader can override
per-pick on the cockpit form):

  horizontal_resistance   → Pivot Break    (cluster of prior highs IS the pivot)
  trendline_setup         → Pullback       (rising trendline = up-MA-equivalent)
  tight_setup             → Anticipation   (tight consolidation, EOD anticipation)
  tightness_trading       → Pivot Break (B) or Pullback (A) — scanner-extras-driven
  institutional_buying    → PDH Entry       (momentum continuation)
  base_on_base            → Pivot Break    (Stage-2 base high)
  minervini_trend_template → Pullback      (10/20-EMA test on Stage-2 leader)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Sequence

log = logging.getLogger("journal.scanner.entry_types")


# Entry-type identifiers — referenced from the template + form override.
PDH = "PDH"
STRONG_START = "StrongStart"
PIVOT_BREAK = "PivotBreak"
ANTICIPATION = "Anticipation"
PULLBACK = "Pullback"
INSIDE_BAR = "InsideBar"


SCANNER_NATURAL_ENTRY: dict[str, str] = {
    "horizontal_resistance": PIVOT_BREAK,
    "trendline_setup":       PULLBACK,
    "tight_setup":           ANTICIPATION,
    "tightness_trading":     PIVOT_BREAK,    # default to B (breakout); A computed too
    "institutional_buying":  PDH,
    "base_on_base":          PIVOT_BREAK,
    "minervini_trend_template": PULLBACK,
}


# Display labels for the cockpit pill.
ENTRY_TYPE_LABELS: dict[str, str] = {
    PDH:           "PDH",
    STRONG_START:  "Strong Start",
    PIVOT_BREAK:   "Pivot Break",
    ANTICIPATION:  "Anticipation",
    PULLBACK:      "Pullback",
    INSIDE_BAR:    "Inside Bar",
}


@dataclass
class EntryRecommendation:
    """One specific entry suggestion — type + trigger + rationale."""
    entry_type: str               # one of the constants above
    trigger_price: float          # the BUY level
    rationale: str                # short human note for the badge tooltip
    sl_override: float | None = None  # if entry type implies a different SL


# ---------------------------------------------------------------------------
# Per-entry-type trigger calculators. Each takes whatever data it needs
# (prior bars, today's OHLC, scanner extras) and returns (trigger_price,
# rationale) or None if uncomputable.
# ---------------------------------------------------------------------------


def _pad(price: float, bps: int = 10) -> float:
    """Add a small slip pad above a level so the buy stop fires once
    price *clears* the level (not just touches it). Default 10 bps."""
    return round(price * (1 + bps / 10000), 2)


def pdh_trigger(prev_high: float) -> EntryRecommendation:
    """Buy stop just above yesterday's high. Confirms continuation when
    today's price clears prior peak with momentum. Best on stocks already
    trending and showing institutional accumulation."""
    return EntryRecommendation(
        entry_type=PDH,
        trigger_price=_pad(prev_high, 10),
        rationale=f"Buy when price clears yest high ₹{prev_high:.2f}",
    )


def pivot_break_trigger(
    pivot_level: float, *, source: str = "pivot",
) -> EntryRecommendation:
    """Buy stop just above the pivot level — cluster of prior highs that
    has rejected price multiple times. ``source`` describes the level
    origin for the rationale."""
    return EntryRecommendation(
        entry_type=PIVOT_BREAK,
        trigger_price=_pad(pivot_level, 10),
        rationale=f"Break above {source} ₹{pivot_level:.2f}",
    )


def anticipation_trigger(today_ltp: float) -> EntryRecommendation:
    """Buy NOW at LTP — used when the consolidation has gone tight enough
    that anticipating the breakout pays better R:R than waiting for the
    pivot to clear. SL stays at the planned PDL.
    """
    return EntryRecommendation(
        entry_type=ANTICIPATION,
        trigger_price=round(today_ltp, 2),
        rationale=f"Anticipate breakout — buy at LTP ₹{today_ltp:.2f}",
    )


def pullback_trigger(
    ema10: float | None, ema20: float | None,
    today_ltp: float | None = None,
) -> EntryRecommendation | None:
    """Buy at the nearest rising EMA (prefer 10-EMA per Minervini).
    Returns None if neither EMA is available.

    The trigger is the EMA value itself — a touch-and-bounce confirms
    the entry. Trader can place a buy stop at EMA + small pad if they
    want price to reclaim the average before entering.
    """
    chosen: float | None = None
    src = ""
    if ema10 is not None and ema10 > 0:
        chosen = ema10
        src = "10-EMA"
    elif ema20 is not None and ema20 > 0:
        chosen = ema20
        src = "20-EMA"
    if chosen is None:
        return None
    # If price is above the EMA, the pullback hasn't happened yet —
    # rationale acknowledges that. If price is below, this is the test.
    if today_ltp is not None and today_ltp < chosen * 0.998:
        rationale = f"Pullback to {src} ₹{chosen:.2f} — wait for bounce confirmation"
    else:
        rationale = f"Buy on touch of {src} ₹{chosen:.2f}"
    return EntryRecommendation(
        entry_type=PULLBACK,
        trigger_price=round(chosen, 2),
        rationale=rationale,
    )


def inside_bar_trigger(
    today_high: float, today_low: float,
    prev_high: float, prev_low: float,
) -> EntryRecommendation | None:
    """If today's bar is inside yesterday's, set buy stop above the
    *mother* bar's high. Returns None if today is NOT an inside bar."""
    is_inside = today_high < prev_high and today_low > prev_low
    if not is_inside:
        return None
    return EntryRecommendation(
        entry_type=INSIDE_BAR,
        trigger_price=_pad(prev_high, 10),
        rationale=f"Inside-day — buy above mother high ₹{prev_high:.2f}",
    )


# ---------------------------------------------------------------------------
# EMA helper — small, dependency-free; mirrors tsl_runner._ema.
# ---------------------------------------------------------------------------


def _ema(values: Sequence[float], period: int) -> float | None:
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    ema = sum(values[:period]) / period
    for v in values[period:]:
        ema = v * k + ema * (1 - k)
    return ema


# ---------------------------------------------------------------------------
# Master recommender — single entry point used by auto_pilot.
# ---------------------------------------------------------------------------


def recommend_entry_for_pick(
    *,
    scan_types_fired: list[str],     # all scan types that fired for this symbol
    primary_scan_type: str,           # the highest-score one (controls the default)
    candidate_extras: dict | None,    # scanner-attached extras (buy_point, pivot, etc.)
    daily_closes: list[float],        # ascending-date closes for EMA calc
    prev_high: float,
    prev_low: float,
    today_open: float | None,
    today_high: float | None,
    today_low: float | None,
    today_ltp: float | None,
    fallback_entry: float,            # scanner's suggested_entry (used if recommender returns None)
) -> EntryRecommendation:
    """Pick the natural entry type for the scanner that fired and
    compute the trigger. Falls back to PDH if the natural type isn't
    computable (e.g. Pullback needs EMA but bars are too short).

    Always returns a recommendation — never None — so the cockpit row
    always has SOMETHING actionable.
    """
    natural = SCANNER_NATURAL_ENTRY.get(primary_scan_type, PIVOT_BREAK)
    extras = candidate_extras or {}

    # Tightness Trading carries an explicit buy_point ('A' or 'B') in
    # extras — A = pullback to base support, B = breakout. Honour it.
    if primary_scan_type == "tightness_trading":
        bp = extras.get("buy_point")
        if bp == "A":
            # A = pullback at base support; suggested_entry already there.
            return EntryRecommendation(
                entry_type=PULLBACK,
                trigger_price=round(fallback_entry, 2),
                rationale="Tightness Trading buy point A — pullback to base support",
            )
        if bp == "B":
            return EntryRecommendation(
                entry_type=PIVOT_BREAK,
                trigger_price=round(fallback_entry, 2),
                rationale="Tightness Trading buy point B — breakout from tight high",
            )

    if natural == PDH:
        return pdh_trigger(prev_high)

    if natural == PIVOT_BREAK:
        # Prefer scanner's pivot extras if available (cluster top, base
        # high, etc.); otherwise fall back to PDH-style.
        pivot = (
            extras.get("cluster_high")
            or extras.get("base_high")
            or extras.get("resistance")
            or extras.get("resistance_level")
            or fallback_entry
        )
        return pivot_break_trigger(pivot, source="pivot/base high")

    if natural == ANTICIPATION:
        if today_ltp:
            return anticipation_trigger(today_ltp)
        # Fall back to PDH if no LTP — anticipation NEEDS today's price.
        return pdh_trigger(prev_high)

    if natural == PULLBACK:
        ema10 = _ema(daily_closes, 10)
        ema20 = _ema(daily_closes, 20)
        rec = pullback_trigger(ema10, ema20, today_ltp)
        if rec is not None:
            return rec
        # Bars too short → fall back to PDH.
        return pdh_trigger(prev_high)

    if natural == INSIDE_BAR:
        if today_high and today_low:
            rec = inside_bar_trigger(today_high, today_low, prev_high, prev_low)
            if rec is not None:
                return rec
        # Not inside bar today → fall back to PDH.
        return pdh_trigger(prev_high)

    # Strong Start — beta. Without 15-min intraday bars we can't compute
    # the first-15m high, so degrade to PDH.
    return pdh_trigger(prev_high)
