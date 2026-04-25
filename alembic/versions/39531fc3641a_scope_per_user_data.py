"""scope per-user data

Adds user_id to per-user tables and backfills every existing row with the
bootstrap admin user. After this migration:

- Per-user: trades, masterlist_items, capital_events, settings (composite PK),
  imported_executions (composite PK), watchlist, scan_run.
- Shared (no user_id): daily_bars, kite_instruments, instrument_meta,
  market_breadth, instrument_prices.

Backfill strategy: every existing row belongs to the FIRST user in the
`users` table (which on a fresh deploy is the bootstrap admin created via
/setup). If no user exists yet AND data exists, refuses to run — set
BOOTSTRAP_ADMIN_EMAIL/PASSWORD env vars to create one inline.

Revision ID: 39531fc3641a
Revises: efe95376eebf
"""
from __future__ import annotations

import os
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "39531fc3641a"
down_revision: Union[str, Sequence[str], None] = "efe95376eebf"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Tables that just need a `user_id INTEGER NOT NULL` FK column added.
_SIMPLE_TABLES = ["trades", "masterlist_items", "capital_events", "watchlist", "scan_run"]


def _resolve_bootstrap_user_id(conn) -> int:
    row = conn.execute(sa.text("SELECT id FROM users ORDER BY id LIMIT 1")).first()
    if row is not None:
        return int(row[0])

    email = os.environ.get("BOOTSTRAP_ADMIN_EMAIL")
    password = os.environ.get("BOOTSTRAP_ADMIN_PASSWORD")
    if email and password:
        from argon2 import PasswordHasher
        h = PasswordHasher().hash(password)
        result = conn.execute(
            sa.text(
                "INSERT INTO users (email, password_hash, is_admin, is_active, "
                "must_change_password, created_at) "
                "VALUES (:e, :p, 1, 1, 0, CURRENT_TIMESTAMP) RETURNING id"
            ),
            {"e": email.lower(), "p": h},
        )
        return int(result.scalar_one())

    counts = []
    for table in ["trades", "masterlist_items", "capital_events", "settings",
                  "imported_executions", "watchlist", "scan_run"]:
        c = conn.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar() or 0
        counts.append((table, c))
    if all(c == 0 for _, c in counts):
        return 0  # nothing to backfill
    raise RuntimeError(
        f"Per-user data exists but no user account does. Run /setup first, "
        f"or set BOOTSTRAP_ADMIN_EMAIL + BOOTSTRAP_ADMIN_PASSWORD env vars "
        f"before running migrations. Row counts: {counts}"
    )


def upgrade() -> None:
    bind = op.get_bind()
    bootstrap_id = _resolve_bootstrap_user_id(bind)

    # ---- Phase 1: simple tables — add nullable col, backfill, lock ------
    for table in _SIMPLE_TABLES:
        with op.batch_alter_table(table) as batch:
            batch.add_column(sa.Column("user_id", sa.Integer(), nullable=True))
    if bootstrap_id:
        for table in _SIMPLE_TABLES:
            op.execute(
                sa.text(f"UPDATE {table} SET user_id = :uid WHERE user_id IS NULL")
                .bindparams(uid=bootstrap_id)
            )
    for table in _SIMPLE_TABLES:
        with op.batch_alter_table(table) as batch:
            batch.alter_column("user_id", existing_type=sa.Integer(), nullable=False)
            batch.create_foreign_key(
                f"fk_{table}_user_id", "users", ["user_id"], ["id"], ondelete="CASCADE"
            )
            batch.create_index(f"ix_{table}_user_id", ["user_id"])

    # ---- Phase 2: settings — composite PK (user_id, key) ----------------
    # Approach: create a new table with the new schema, copy data, drop old.
    # batch_alter_table can't easily change a PK; explicit rebuild is safest.
    op.execute("ALTER TABLE settings RENAME TO _settings_old")
    op.create_table(
        "settings",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("value", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("user_id", "key", name="pk_settings"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_settings_user_id", ondelete="CASCADE"
        ),
    )
    if bootstrap_id:
        op.execute(
            sa.text(
                "INSERT INTO settings (user_id, key, value) "
                "SELECT :uid, key, value FROM _settings_old"
            ).bindparams(uid=bootstrap_id)
        )
    op.execute("DROP TABLE _settings_old")

    # ---- Phase 3: imported_executions — composite PK (user_id, trade_id) -
    # SQLite preserves named indexes during RENAME — they keep their name and
    # point at the renamed table. We drop the old indexes BEFORE rename to
    # avoid name collisions when we recreate them on the new table.
    op.execute("DROP INDEX IF EXISTS ix_imported_executions_symbol")
    op.execute("ALTER TABLE imported_executions RENAME TO _imported_executions_old")
    op.create_table(
        "imported_executions",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("trade_id", sa.String(), nullable=False),
        sa.Column("symbol", sa.String(), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("side", sa.String(), nullable=False),
        sa.Column("qty", sa.Integer(), nullable=False),
        sa.Column("price", sa.Float(), nullable=False),
        sa.Column("order_id", sa.String(), nullable=True),
        sa.Column("exchange", sa.String(), nullable=True),
        sa.Column("source", sa.String(), nullable=False, server_default="zerodha"),
        sa.Column("applied_to_trade_id", sa.Integer(), nullable=True),
        sa.Column("imported_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("user_id", "trade_id", name="pk_imported_executions"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"],
            name="fk_imported_executions_user_id", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["applied_to_trade_id"], ["trades.id"],
            name="fk_imported_executions_trade_id", ondelete="SET NULL",
        ),
    )
    op.create_index("ix_imported_executions_symbol", "imported_executions", ["symbol"])
    if bootstrap_id:
        op.execute(
            sa.text(
                "INSERT INTO imported_executions "
                "(user_id, trade_id, symbol, trade_date, side, qty, price, "
                " order_id, exchange, source, applied_to_trade_id, imported_at) "
                "SELECT :uid, trade_id, symbol, trade_date, side, qty, price, "
                "       order_id, exchange, source, applied_to_trade_id, imported_at "
                "FROM _imported_executions_old"
            ).bindparams(uid=bootstrap_id)
        )
    op.execute("DROP TABLE _imported_executions_old")

    # ---- Phase 4: replace old uniques with user-scoped variants ---------
    with op.batch_alter_table("masterlist_items") as batch:
        batch.drop_constraint("uq_category_value", type_="unique")
        batch.create_unique_constraint(
            "uq_user_category_value", ["user_id", "category", "value"]
        )

    # Old watchlist had `unique=True` on symbol → ix_watchlist_symbol unique
    # index. We need to drop+recreate it as non-unique, then add the
    # composite (user_id, symbol) uniqueness.
    with op.batch_alter_table("watchlist") as batch:
        batch.drop_index("ix_watchlist_symbol")
        batch.create_index("ix_watchlist_symbol", ["symbol"])  # non-unique
        batch.create_unique_constraint(
            "uq_watchlist_user_symbol", ["user_id", "symbol"]
        )


def downgrade() -> None:
    raise NotImplementedError(
        "Downgrade is unsupported. Restore from a snapshot if you need to roll back."
    )
