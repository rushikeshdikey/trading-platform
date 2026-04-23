from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "journal.db"

engine = create_engine(
    f"sqlite:///{DB_PATH}",
    connect_args={"check_same_thread": False},
    future=True,
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
# SQLAlchemy's create_all() only creates missing tables — it doesn't add
# columns to existing ones. For a single-user SQLite app we don't need a full
# migration tool; this helper adds nullable columns when they're absent.


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
