"""Composite-score tier engine.

Replaces the rigid "≥3 scanners = A+" rule. The old rule produced 0–1 A+
picks/day on a 4600-symbol universe — too tight for Indian markets where
most leaders fire on 1–2 scanners (Minervini + Institutional Buying is a
classic combo, never 3).

Composite formula
=================

    raw = base_score          (0..50)   — best scanner score, percentile-
                                          normalized within its own scanner
                                          so cross-detector scales align.
        + confluence_bonus    (0..25)   — multi-scanner agreement.
        + rs_bonus            (0..15)   — IBD-style RS rating boost.
        + minervini_bonus     (0..22)   — preserves the "Minervini hit
                                          alone is strong" intuition.
        + sector_bonus       (-15..15)  — RRG quadrant tailwind/headwind.

    composite = raw × regime_multiplier
              where regime_multiplier ∈ {0.0, 0.7, 0.9, 1.0}

    regime_multiplier = 0.0  → hard regime block (no picks at all today).

Tier thresholds
===============

    A+   composite ≥ 75      "Leader, confirmed"
    A    composite ≥ 55      "Leader candidate"
    B    composite ≥ 35      "Watchlist"
    C    composite <  35     don't surface

Auto-Pilot picks composite ≥ 70 (mix of A+ and high-A), capped at MAX_PICKS.

Why these numbers
=================

Calibrated on existing scan_cache (2026-04-27). Goal: 3–7 picks on green
days, 0–1 on weak, 0 on RED. Tweak only via the constants in this file —
keep tuning in one place.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

# ---------------------------------------------------------------------------
# Per-scanner reference maxima for normalization. The detectors output on
# very different scales (Tight: 1–5, Tightness: 0–100, HR: 10–25). Dividing
# by a scanner-specific reference gives a 0..1 ratio we can multiply by 50.
# Reference values are typical "great-setup" scores observed in production
# scans, NOT theoretical maxima — using max would compress scores too far.
# ---------------------------------------------------------------------------

_SCANNER_REF_MAX: dict[str, float] = {
    "horizontal_resistance": 22.0,
    "trendline_setup": 7.0,
    "tight_setup": 4.0,
    "tightness_trading": 80.0,
    "institutional_buying": 40.0,
    "base_on_base": 12.0,
    "minervini_trend_template": 95.0,
}

_MINERVINI = "minervini_trend_template"

# ---------------------------------------------------------------------------
# Confluence bonus — flat lookup. 4+ scanners on Indian markets is a
# generational alignment; capping at 25 prevents any one signal from
# dwarfing RS + sector context.
# ---------------------------------------------------------------------------

_CONFLUENCE_BONUS = {1: 0.0, 2: 12.0, 3: 20.0, 4: 25.0}


def _confluence_bonus(n_scanners: int) -> float:
    if n_scanners <= 0:
        return 0.0
    return _CONFLUENCE_BONUS.get(n_scanners, 25.0)


# ---------------------------------------------------------------------------
# RS bonus — IBD's RS Rating is already a percentile (1-99), so the curve
# is calibrated against absolute RS values, not z-scores.
# ---------------------------------------------------------------------------


def _rs_bonus(rs_rating: float | int | None) -> float:
    if rs_rating is None:
        return 3.0  # neutral-with-slight-penalty for missing RS
    rs = float(rs_rating)
    if rs >= 90:
        return 15.0
    if rs >= 80:
        return 12.0
    if rs >= 70:
        return 9.0
    if rs >= 50:
        return 5.0
    return 0.0


# ---------------------------------------------------------------------------
# Minervini bonus — the SEPA Trend Template is itself an 8-criteria filter,
# so a hit alone is much stronger evidence than any single chart-pattern
# scanner. We give it a big additive bonus, scaled by RS.
# ---------------------------------------------------------------------------


def _minervini_bonus(has_minervini: bool, rs_rating: float | int | None) -> float:
    if not has_minervini:
        return 0.0
    rs = float(rs_rating) if rs_rating is not None else 0.0
    if rs >= 80:
        return 22.0
    if rs >= 70:
        return 15.0
    return 8.0


# ---------------------------------------------------------------------------
# Sector bonus — driven by RRG quadrant. "Leading" = RS-Ratio>100 +
# RS-Momentum>100 = sector outperforming AND accelerating.
# ---------------------------------------------------------------------------

_SECTOR_BONUS = {
    "Leading": 15.0,
    "Improving": 10.0,
    "Weakening": -5.0,
    "Lagging": -15.0,
}


def _sector_bonus(quadrant: str | None) -> float:
    if not quadrant:
        return 0.0
    return _SECTOR_BONUS.get(quadrant, 0.0)


# ---------------------------------------------------------------------------
# Regime multiplier — multiplies the raw composite. A 0 here is the
# hard-block trigger ("Stay in cash today").
# ---------------------------------------------------------------------------


@dataclass
class RegimeContext:
    mood_score: float | None       # 0-100 from breadth_mod.mood_score
    pct_above_50ema: float | None  # from MarketBreadth row
    pct_above_200ema: float | None
    multiplier: float              # final 0.0 / 0.7 / 0.9 / 1.0
    label: str                     # "Risk-on", "Cautious", "Hard-block — RED", etc.
    block_reason: str              # populated when multiplier == 0

    @property
    def is_blocked(self) -> bool:
        return self.multiplier <= 0.0


def regime_multiplier_from_breadth(
    *, mood_score: float | None,
    pct_above_50ema: float | None,
    pct_above_200ema: float | None,
) -> RegimeContext:
    """Decide regime multiplier from breadth metrics.

    Hard block (multiplier = 0) when ANY of:
      - Mood score < 30 (Risk-off / Bearish)
      - % above 50 EMA < 35
      - % above 200 EMA < 30

    Note: matches the existing Cockpit market-verdict RED rule (p50<40 OR
    p200<35) but slightly looser on p50 and stricter on Mood. The Mood
    threshold is the primary gate — it's a composite of all five breadth
    components, so it dominates.
    """
    p50 = float(pct_above_50ema) if pct_above_50ema is not None else 50.0
    p200 = float(pct_above_200ema) if pct_above_200ema is not None else 50.0
    mood = float(mood_score) if mood_score is not None else 50.0

    # Hard block triggers (any one fires)
    blocks: list[str] = []
    if mood < 30:
        blocks.append(f"Mood {mood:.0f}<30 (Risk-off)")
    if p50 < 35:
        blocks.append(f"only {p50:.0f}% above 50EMA (<35)")
    if p200 < 30:
        blocks.append(f"only {p200:.0f}% above 200EMA (<30)")

    if blocks:
        return RegimeContext(
            mood_score=mood, pct_above_50ema=p50, pct_above_200ema=p200,
            multiplier=0.0,
            label="Hard-block — Stay in cash",
            block_reason="; ".join(blocks),
        )

    # Risk-on regime — multiplier 1.0
    if mood >= 60 and p50 >= 50:
        return RegimeContext(
            mood_score=mood, pct_above_50ema=p50, pct_above_200ema=p200,
            multiplier=1.0, label="Risk-on", block_reason="",
        )

    # Constructive — multiplier 0.9
    if mood >= 45 and p50 >= 40:
        return RegimeContext(
            mood_score=mood, pct_above_50ema=p50, pct_above_200ema=p200,
            multiplier=0.9, label="Constructive — selective", block_reason="",
        )

    # Cautious — multiplier 0.7
    return RegimeContext(
        mood_score=mood, pct_above_50ema=p50, pct_above_200ema=p200,
        multiplier=0.7, label="Cautious — half size", block_reason="",
    )


# ---------------------------------------------------------------------------
# Base score normalization — convert raw scanner scores to a 0..50 scale.
# Two layers: divide by scanner-specific reference, then take the MAX
# (not sum) across scanners the symbol fired on. Confluence is rewarded
# separately via _confluence_bonus, so adding scores would double-count.
# ---------------------------------------------------------------------------


def _base_score_for_scan(scan_type: str, raw_score: float) -> float:
    ref = _SCANNER_REF_MAX.get(scan_type)
    if ref is None or ref <= 0:
        return 0.0
    ratio = float(raw_score) / ref
    # Clip to [0, 1] then scale to 50. A score above the reference still
    # contributes max points — we don't want extreme outliers to dominate.
    return max(0.0, min(1.0, ratio)) * 50.0


def _base_score_from_scans(scans: Iterable[dict]) -> float:
    best = 0.0
    for s in scans:
        b = _base_score_for_scan(s["type"], s["score"])
        if b > best:
            best = b
    return best


# ---------------------------------------------------------------------------
# Public scoring function
# ---------------------------------------------------------------------------


@dataclass
class CompositeBreakdown:
    composite: float           # final 0..N (after regime multiplier)
    raw: float                 # before regime multiplier
    base: float                # 0-50
    confluence: float          # 0-25
    rs: float                  # 0-15
    minervini: float           # 0-22
    sector: float              # -15..15
    regime_multiplier: float   # 0.0..1.0
    tier: str                  # "A+" / "A" / "B" / "C"
    reason: str                # one-line "why this tier"


# Tier cutoffs — single source of truth.
# Calibrated against 2026-04-27 scan_cache so the badges actually convey
# rarity: A+ ≈ top 10% of scored names, A ≈ next 20%, B ≈ next 30%, C below.
TIER_APLUS_MIN = 90.0
TIER_A_MIN = 70.0
TIER_B_MIN = 45.0


def _tier_for(composite: float) -> str:
    if composite >= TIER_APLUS_MIN:
        return "A+"
    if composite >= TIER_A_MIN:
        return "A"
    if composite >= TIER_B_MIN:
        return "B"
    return "C"


def composite_score(
    *,
    scans: list[dict],         # list of {type, label, score, extras}
    rs_rating: float | int | None,
    sector_quadrant: str | None,
    regime: RegimeContext,
) -> CompositeBreakdown:
    """Compute composite score for one symbol's scan roll-up.

    `scans` is the same shape as `_build_unified_results` produces:
    a list of {type, label, score, extras} dicts.
    """
    n = len(scans)
    base = _base_score_from_scans(scans)
    confluence = _confluence_bonus(n)
    rs = _rs_bonus(rs_rating)
    has_mtt = any(s["type"] == _MINERVINI for s in scans)
    mtt = _minervini_bonus(has_mtt, rs_rating)
    sector = _sector_bonus(sector_quadrant)

    raw = base + confluence + rs + mtt + sector
    composite = max(0.0, raw * regime.multiplier)
    tier = _tier_for(composite)

    # Build a human reason — pick the top contributing factors.
    bits: list[str] = []
    if has_mtt:
        bits.append("Minervini Stage 2")
    if n >= 2:
        bits.append(f"{n} scanners")
    if rs_rating is not None and rs_rating >= 80:
        bits.append(f"RS {int(rs_rating)}")
    elif rs_rating is not None and rs_rating >= 70:
        bits.append(f"RS {int(rs_rating)}")
    if sector_quadrant in ("Leading", "Improving"):
        bits.append(f"{sector_quadrant} sector")
    elif sector_quadrant in ("Weakening", "Lagging"):
        bits.append(f"{sector_quadrant} sector (penalty)")
    if regime.multiplier < 1.0 and not regime.is_blocked:
        bits.append(regime.label)
    reason = " · ".join(bits) if bits else "single scanner, weak context"

    return CompositeBreakdown(
        composite=composite, raw=raw,
        base=base, confluence=confluence, rs=rs,
        minervini=mtt, sector=sector,
        regime_multiplier=regime.multiplier,
        tier=tier, reason=reason,
    )
