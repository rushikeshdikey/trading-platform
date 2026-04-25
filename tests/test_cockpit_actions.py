"""Tests for app/cockpit.py::_action_for — the rule chain that decides
HOLD / EXIT / TRIM HALF / TIGHTEN SL / REVIEW for each open position.

Order of rules matters (first match wins). These tests lock the order in.
"""
from __future__ import annotations

from app.cockpit import _action_for
from app.portfolio import PositionCard


def _card(**overrides):
    """Build a PositionCard with sane defaults — only specify what matters per test."""
    base = dict(
        trade_id=1, instrument="X", setup="HR", side="B", holding_days=5,
        avg_entry=100.0, cmp=110.0, effective_stop=98.0, sl=95.0, tsl=None,
        open_qty=100, total_qty=100,
        move_pct=0.10, pnl_rs=1000.0, r_multiple=2.0,
        allocation_pct=0.10, invested_rs=10_000, risk_rs=200, locked_profit_rs=0,
        status_tag="Safe",
    )
    base.update(overrides)
    return PositionCard(**base)


def test_no_cmp_says_refresh():
    a, *_ = _action_for(_card(cmp=None))
    assert a == "REFRESH"


def test_long_below_stop_says_exit():
    """Stop hit territory: CMP <= stop for a long."""
    a, *_ = _action_for(_card(cmp=97.0, effective_stop=98.0, side="B"))
    assert a == "EXIT"


def test_short_above_stop_says_exit():
    """Stop hit territory: CMP >= stop for a short."""
    a, *_ = _action_for(_card(cmp=106.0, effective_stop=105.0, side="S"))
    assert a == "EXIT"


def test_big_winner_says_trim_half():
    a, *_ = _action_for(_card(r_multiple=4.5))
    assert a == "TRIM HALF"


def test_two_r_no_tsl_says_tighten_to_entry():
    a, *_ = _action_for(_card(r_multiple=2.5, tsl=None))
    assert a == "TIGHTEN SL → ENTRY"


def test_two_r_with_tsl_does_not_say_tighten():
    """If TSL is already set, don't nag user to tighten — they've trailed."""
    a, *_ = _action_for(_card(r_multiple=2.5, tsl=100.0))
    # Could fall through to HOLD or another rule, just not TIGHTEN→ENTRY.
    assert a != "TIGHTEN SL → ENTRY"


def test_stale_loser_says_review():
    a, *_ = _action_for(_card(r_multiple=-0.5, holding_days=35))
    assert a == "REVIEW"


def test_tight_status_says_tighten_stop():
    a, *_ = _action_for(_card(r_multiple=0.3, status_tag="Tight"))
    assert a == "TIGHTEN STOP"


def test_default_says_hold():
    a, *_ = _action_for(_card(r_multiple=0.5, status_tag="Safe"))
    assert a == "HOLD"


# -- Rule ordering: ensure earlier rules pre-empt later ones ----------------


def test_exit_pre_empts_trim_when_stop_already_hit():
    """Even if also a big winner on paper, if price has reached stop, EXIT wins."""
    a, *_ = _action_for(_card(cmp=98.0, effective_stop=98.0, r_multiple=5.0, side="B"))
    assert a == "EXIT"


def test_trim_half_pre_empts_tighten_to_entry():
    """+4R should TRIM HALF, not TIGHTEN→ENTRY (which is for +2R no TSL)."""
    a, *_ = _action_for(_card(r_multiple=4.2, tsl=None))
    assert a == "TRIM HALF"
