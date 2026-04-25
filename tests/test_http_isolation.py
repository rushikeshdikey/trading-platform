"""End-to-end test that two real users hitting real HTTP routes can't see
each other's data. Goes through the full middleware stack (SessionMiddleware
→ _attach_user_to_request → require_user → SQLAlchemy events).

This is the test that catches the contextvar-doesn't-propagate-from-threadpool
class of bugs that unit tests miss.
"""
from __future__ import annotations

import os

import pytest


@pytest.fixture
def app_with_two_users(tmp_path, monkeypatch):
    db_path = tmp_path / "http_iso.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("SECRET_KEY", "x" * 64)

    # Re-import app modules so they pick up the new DATABASE_URL.
    # Order matters: config → db → main (which runs alembic upgrade).
    from importlib import reload
    import app.config as cfg
    reload(cfg)
    import app.db as db_mod
    reload(db_mod)
    import app.main as main_mod
    reload(main_mod)

    from app import models as m
    from app import auth as auth_mod
    from app import orm_events  # noqa: F401

    # main_mod's reload ran `upgrade_to_head` so all tables exist.
    db = db_mod.SessionLocal()
    alice = m.User(
        email="alice@x.com",
        password_hash=auth_mod.hash_password("alicepass1234"),
        is_admin=False, is_active=True, must_change_password=False,
    )
    bob = m.User(
        email="bob@x.com",
        password_hash=auth_mod.hash_password("bobpass1234"),
        is_admin=False, is_active=True, must_change_password=False,
    )
    db.add_all([alice, bob])
    db.commit()
    db.refresh(alice)
    db.refresh(bob)

    db.add(m.Watchlist(user_id=alice.id, symbol="ALICE_ONLY"))
    db.add(m.Watchlist(user_id=bob.id, symbol="BOB_ONLY"))
    db.commit()
    alice_email = alice.email
    bob_email = bob.email
    db.close()

    from fastapi.testclient import TestClient
    return TestClient(main_mod.app), alice_email, bob_email


def _login(client, email, password):
    return client.post(
        "/login",
        data={"email": email, "password": password, "next": "/watchlist"},
        follow_redirects=False,
    )


def test_alice_sees_only_alices_watchlist(app_with_two_users):
    client, alice_email, _bob = app_with_two_users
    r = _login(client, alice_email, "alicepass1234")
    assert r.status_code == 303
    page = client.get("/watchlist")
    assert page.status_code == 200
    assert "ALICE_ONLY" in page.text
    assert "BOB_ONLY" not in page.text


def test_bob_sees_only_bobs_watchlist(app_with_two_users):
    client, _alice, bob_email = app_with_two_users
    _login(client, bob_email, "bobpass1234")
    page = client.get("/watchlist")
    assert page.status_code == 200
    assert "BOB_ONLY" in page.text
    assert "ALICE_ONLY" not in page.text


def test_bulk_add_writes_under_correct_user(app_with_two_users):
    """A bulk-add as alice must NOT make bob see those symbols."""
    client, alice_email, bob_email = app_with_two_users
    _login(client, alice_email, "alicepass1234")
    r = client.post(
        "/watchlist/bulk-add",
        data={"symbols": "NSE:ALICEBULK1,NSE:ALICEBULK2", "return_to": "/watchlist"},
        follow_redirects=False,
    )
    assert r.status_code == 303

    # Alice sees them
    page = client.get("/watchlist")
    assert "ALICEBULK1" in page.text

    # Switch to bob — he shouldn't
    client.post("/logout", follow_redirects=False)
    _login(client, bob_email, "bobpass1234")
    page = client.get("/watchlist")
    assert "ALICEBULK1" not in page.text
    assert "ALICEBULK2" not in page.text
    assert "BOB_ONLY" in page.text


def test_unauthed_request_redirects_to_login(app_with_two_users):
    client, _, _ = app_with_two_users
    r = client.get("/watchlist", follow_redirects=False)
    assert r.status_code == 303
    assert "/login" in r.headers["location"]
