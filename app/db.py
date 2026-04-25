"""SQLAlchemy engine + session factory.

The engine URL comes from `settings.database_url`. SQLite is the dev default;
production must set `DATABASE_URL` to a Postgres URL (validated at boot).
"""
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)

# SQLite needs check_same_thread=False because FastAPI uses a thread pool
# for sync endpoints; Postgres ignores connect_args entirely.
_connect_args: dict = {"check_same_thread": False} if settings.is_sqlite else {}

engine = create_engine(
    settings.database_url,
    connect_args=_connect_args,
    future=True,
    pool_pre_ping=not settings.is_sqlite,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# -- Lightweight additive migrations ---------------------------------------
# Pre-Alembic helper: SQLAlchemy's create_all() only creates missing tables
# — it doesn't add columns. Once Alembic is wired up this will be retired.


_SCHEMA_ADDITIONS: list[tuple[str, str, str]] = [
    ("trades", "charges_rs", "FLOAT"),
    ("instrument_prices", "prev_close", "FLOAT"),
]


def apply_schema_additions() -> list[str]:
    """Add any missing columns listed in `_SCHEMA_ADDITIONS`. Returns changes."""
    applied: list[str] = []
    insp = inspect(engine)
    with engine.begin() as conn:
        for table, column, col_type in _SCHEMA_ADDITIONS:
            if table not in insp.get_table_names():
                continue
            existing = {c["name"] for c in insp.get_columns(table)}
            if column in existing:
                continue
            conn.execute(text(f'ALTER TABLE {table} ADD COLUMN {column} {col_type}'))
            applied.append(f"{table}.{column}")
    return applied
