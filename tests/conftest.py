"""Pytest fixtures.

Tests use a fresh in-memory-ish SQLite per test (file-backed in tmp dir to
preserve cross-connection state — ``:memory:`` doesn't survive multiple
connections in SQLAlchemy without StaticPool gymnastics).
"""
from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.fixture
def db_session(tmp_path):
    """Fresh SQLite DB with all app tables; yields a Session."""
    db_path = tmp_path / "test.db"
    # IMPORTANT: set DATABASE_URL before importing app modules so any
    # module-level engine creation honors the test DB.
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"

    # Import inside the fixture to pick up env override.
    from importlib import reload
    import app.config as cfg
    reload(cfg)
    import app.db as db_mod
    reload(db_mod)
    import app.models  # noqa: F401  -- register tables on Base

    db_mod.Base.metadata.create_all(bind=db_mod.engine)
    Session = sessionmaker(bind=db_mod.engine, autoflush=False, autocommit=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        db_mod.engine.dispose()


@pytest.fixture
def make_trade():
    """Factory that builds a Trade ORM instance (not persisted) with sane
    defaults. Override via kwargs. Pyramids/exits passed as lists of dicts.
    """
    from app.models import Trade, Pyramid, Exit

    def _make(**kwargs):
        defaults = dict(
            instrument="TEST",
            side="B",
            entry_date=date(2026, 1, 1),
            initial_entry_price=100.0,
            initial_qty=100,
            sl=95.0,
            status="open",
        )
        pyramids = kwargs.pop("pyramids", [])
        exits = kwargs.pop("exits", [])
        defaults.update(kwargs)
        t = Trade(**defaults)
        for i, p in enumerate(pyramids, start=1):
            t.pyramids.append(Pyramid(
                sequence=i,
                price=p["price"], qty=p["qty"],
                date=p.get("date", t.entry_date),
            ))
        for i, e in enumerate(exits, start=1):
            t.exits.append(Exit(
                sequence=i,
                price=e["price"], qty=e["qty"],
                date=e.get("date", t.entry_date),
            ))
        return t

    return _make
