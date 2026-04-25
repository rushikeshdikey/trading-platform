"""add scan_run.payload — JSON-serialized candidate cache

Revision ID: 6aa891b8b79d
Revises: 39531fc3641a
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "6aa891b8b79d"
down_revision: Union[str, Sequence[str], None] = "39531fc3641a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("scan_run") as batch_op:
        batch_op.add_column(sa.Column("payload", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("scan_run") as batch_op:
        batch_op.drop_column("payload")
