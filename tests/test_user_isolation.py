"""Smoke test for the auto-filter — the load-bearing security guarantee.

If these break, data leaks between users. They're worth their weight in gold.
"""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Import once — events register on first import.
from app import auth as auth_mod
from app import models as m
from app import orm_events  # noqa: F401  -- registers events on Session class
from app.db import Base


@pytest.fixture
def two_users_with_data(tmp_path):
    """Build a fresh DB with two users, each owning some trades."""
    db_path = tmp_path / "iso.db"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False)
    db = Session()

    # Make sure the contextvar starts clean (no leak from another test).
    auth_mod.current_user_id_var.set(None)

    alice = m.User(
        email="alice@example.com",
        password_hash=auth_mod.hash_password("alicepass1234"),
        is_admin=False, is_active=True, must_change_password=False,
    )
    bob = m.User(
        email="bob@example.com",
        password_hash=auth_mod.hash_password("bobpass1234"),
        is_admin=False, is_active=True, must_change_password=False,
    )
    db.add_all([alice, bob])
    db.commit()
    db.refresh(alice)
    db.refresh(bob)

    db.add_all([
        m.Trade(user_id=alice.id, instrument="ALICE_A", side="B",
                entry_date=date(2026, 1, 1), initial_entry_price=100,
                initial_qty=10, sl=95),
        m.Trade(user_id=alice.id, instrument="ALICE_B", side="B",
                entry_date=date(2026, 1, 2), initial_entry_price=200,
                initial_qty=5, sl=190),
        m.Trade(user_id=bob.id, instrument="BOB_A", side="B",
                entry_date=date(2026, 1, 3), initial_entry_price=300,
                initial_qty=8, sl=285),
    ])
    db.commit()
    db.expire_all()  # subsequent reads go through the filter

    def activate(user):
        auth_mod.current_user_id_var.set(user.id)
        db.expire_all()
        return user

    yield db, alice, bob, activate
    auth_mod.current_user_id_var.set(None)
    db.close()
    engine.dispose()


def test_select_only_returns_current_users_rows(two_users_with_data):
    db, alice, bob, activate = two_users_with_data

    activate(alice)
    rows = db.query(m.Trade).all()
    assert {t.instrument for t in rows} == {"ALICE_A", "ALICE_B"}

    activate(bob)
    rows = db.query(m.Trade).all()
    assert [t.instrument for t in rows] == ["BOB_A"]


def test_count_is_filtered(two_users_with_data):
    db, alice, _bob, activate = two_users_with_data
    activate(alice)
    assert db.query(m.Trade).count() == 2


def test_insert_auto_stamps_user_id(two_users_with_data):
    db, alice, _bob, activate = two_users_with_data
    activate(alice)
    t = m.Trade(
        instrument="AUTO_STAMPED", side="B", entry_date=date(2026, 1, 4),
        initial_entry_price=50, initial_qty=10, sl=45,
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    assert t.user_id == alice.id


def test_skip_user_filter_escape_hatch_returns_everything(two_users_with_data):
    """Admin code can opt out for cross-user views (e.g. /admin/users)."""
    db, alice, _bob, activate = two_users_with_data
    activate(alice)
    rows = (
        db.query(m.Trade)
        .execution_options(skip_user_filter=True)
        .all()
    )
    assert len(rows) == 3


def test_no_contextvar_means_no_filter(two_users_with_data):
    """Background tasks (no request) leave the contextvar None and see
    every row — documented behavior."""
    db, _alice, _bob, _activate = two_users_with_data
    auth_mod.current_user_id_var.set(None)
    rows = db.query(m.Trade).all()
    assert len(rows) == 3
