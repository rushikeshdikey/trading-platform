"""widen daily_bars.volume INTEGER → BIGINT

Some Indian symbols (high-volume ETFs, micro-cap movers) post daily
volumes above the 2,147,483,647 INT32 ceiling, which Postgres rejects
with NumericValueOutOfRange. SQLite happily stores them as int64 so
local never saw the issue; prod (Postgres) refuses the insert and the
backfill aborts mid-day. BIGINT (int64) max is 9.2 * 10^18 — plenty.

Revision ID: a1b2c3d4e5f6
Revises: 3bcc233e81c3
Create Date: 2026-04-26 19:30:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "3bcc233e81c3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # SQLite stores ints as 64-bit by default and ignores the typedef
    # change; Postgres needs the explicit ALTER. The conditional keeps
    # both paths happy and avoids tripping on bind detection.
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.alter_column(
            "daily_bars", "volume",
            existing_type=sa.Integer(),
            type_=sa.BigInteger(),
            existing_nullable=True,
            existing_server_default=sa.text("0"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.alter_column(
            "daily_bars", "volume",
            existing_type=sa.BigInteger(),
            type_=sa.Integer(),
            existing_nullable=True,
            existing_server_default=sa.text("0"),
        )
