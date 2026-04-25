"""Tests for app/calculations.py — pure functions over Trade ORM.

Critical because trade P/L, R-multiples, and open-heat math drive every
panel in the cockpit. A regression here silently corrupts every screen.
"""
from __future__ import annotations

from datetime import date

from app.calculations import (
    avg_entry,
    metrics,
    open_heat_rs,
    pnl_rs,
    reward_risk,
    size_by_allocation,
    size_by_risk,
    stock_move_pct,
)


# -- Long-side ---------------------------------------------------------------


def test_long_simple_winner(make_trade):
    t = make_trade(
        side="B", initial_entry_price=100, initial_qty=100, sl=95,
        cmp=110, status="closed", close_date=date(2026, 1, 31),
        exits=[{"price": 110, "qty": 100, "date": date(2026, 1, 31)}],
    )
    m = metrics(t)
    assert m.total_qty == 100
    assert m.avg_entry == 100
    assert m.exited_qty == 100
    assert m.pnl_rs == 1000  # (110-100)*100
    assert m.reward_risk == 2.0  # (110-100)/(100-95)
    assert abs(m.stock_move_pct - 0.10) < 1e-9
    assert m.open_heat_rs == 0  # fully exited


def test_long_with_pyramid_avg_entry(make_trade):
    t = make_trade(
        side="B", initial_entry_price=100, initial_qty=100, sl=95,
        pyramids=[{"price": 110, "qty": 100}],
    )
    # weighted avg: (100*100 + 110*100) / 200 = 105
    assert avg_entry(t) == 105


def test_long_open_heat_rupees(make_trade):
    t = make_trade(
        side="B", initial_entry_price=100, initial_qty=100, sl=95, cmp=120,
    )
    # Risk if SL hits on 100 open shares = (100-95)*100
    assert open_heat_rs(t) == 500


def test_long_partial_exit_open_heat_drops(make_trade):
    t = make_trade(
        side="B", initial_entry_price=100, initial_qty=100, sl=95,
        exits=[{"price": 110, "qty": 60}],
    )
    # 40 open at SL distance of ₹5 -> ₹200 at risk
    assert open_heat_rs(t) == 200


# -- Short-side (signs flip) -------------------------------------------------


def test_short_pnl_signed_correctly(make_trade):
    """For a short, price goes DOWN = profit."""
    t = make_trade(
        side="S", initial_entry_price=100, initial_qty=100, sl=105,
        exits=[{"price": 90, "qty": 100}],
        status="closed", close_date=date(2026, 1, 31),
    )
    assert pnl_rs(t) == 1000  # (100-90)*100


def test_short_reward_risk_signed_correctly(make_trade):
    t = make_trade(
        side="S", initial_entry_price=100, initial_qty=100, sl=105, cmp=90,
    )
    # Risk per share = ₹5 (entry to SL). Move per share = ₹10 in our favor.
    assert reward_risk(t) == 2.0


def test_short_stock_move_signed_correctly(make_trade):
    t = make_trade(side="S", initial_entry_price=100, initial_qty=10, sl=105, cmp=90)
    # Price dropped 10% — for short that's +10% move.
    assert abs(stock_move_pct(t) - 0.10) < 1e-9


def test_short_open_heat_uses_distance_above_entry(make_trade):
    t = make_trade(side="S", initial_entry_price=100, initial_qty=100, sl=105)
    # Loss if SL hits = (105-100)*100
    assert open_heat_rs(t) == 500


# -- Edge cases the calculations module guards explicitly -------------------


def test_reward_risk_handles_zero_distance(make_trade):
    """SL == entry → return None instead of dividing by ~0 noise."""
    t = make_trade(initial_entry_price=100, sl=100, cmp=110)
    assert reward_risk(t) is None


def test_no_exit_no_cmp_returns_none(make_trade):
    """Without a close ref or CMP, R:R can't be computed."""
    t = make_trade(initial_entry_price=100, sl=95, cmp=None)
    assert reward_risk(t) is None
    assert stock_move_pct(t) is None


# -- Position sizing ---------------------------------------------------------


def test_size_by_risk_basic():
    # 5L capital, 0.5% risk = ₹2500. Risk per share = ₹5. -> 500 shares.
    s = size_by_risk(capital=500_000, risk_pct=0.005, entry=100, sl=95)
    assert s.qty == 500
    assert s.risk_rs == 2500
    assert s.position_size_rs == 50_000
    assert abs(s.allocation_pct - 0.10) < 1e-9


def test_size_by_risk_zero_distance_safely_returns_zero():
    s = size_by_risk(capital=500_000, risk_pct=0.005, entry=100, sl=100)
    assert s.qty == 0


def test_size_by_allocation_basic():
    # 5L capital, 10% alloc = 50K. Entry 100. -> 500 shares.
    s = size_by_allocation(capital=500_000, allocation_pct=0.10, entry=100, sl=95)
    assert s.qty == 500
    assert s.allocated_rs == 50_000
    assert abs(s.sl_pct - 0.05) < 1e-9
    assert s.risk_rs == 2500
