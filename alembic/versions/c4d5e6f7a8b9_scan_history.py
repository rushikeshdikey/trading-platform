"""scan_history: append-only daily snapshots of scanner output

Revision ID: c4d5e6f7a8b9
Revises: 3bcc233e81c3
Create Date: 2026-04-29 12:00:00

ScanCache (existing) is keyed only by scan_type, overwritten daily — no
replay capability. ScanHistory adds (scan_date, scan_type) rows so we can
audit "did POWER stocks fire on day X" and tune the composite scorer
against a real backtest set.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c4d5e6f7a8b9'
down_revision: Union[str, Sequence[str], None] = 'b2c3d4e5f6a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'scan_history',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('scan_date', sa.Date(), nullable=False),
        sa.Column('scan_type', sa.String(), nullable=False),
        sa.Column('run_at', sa.DateTime(), nullable=False),
        sa.Column('universe_size', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('candidates_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('elapsed_ms', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('payload', sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('scan_history', schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f('ix_scan_history_scan_date'), ['scan_date'], unique=False
        )
        batch_op.create_index(
            batch_op.f('ix_scan_history_scan_type'), ['scan_type'], unique=False
        )
        # Composite index for the common forensic query
        # (e.g., "what fired on this date for this scan_type?").
        batch_op.create_index(
            'ix_scan_history_date_type',
            ['scan_date', 'scan_type'],
            unique=True,
        )


def downgrade() -> None:
    with op.batch_alter_table('scan_history', schema=None) as batch_op:
        batch_op.drop_index('ix_scan_history_date_type')
        batch_op.drop_index(batch_op.f('ix_scan_history_scan_type'))
        batch_op.drop_index(batch_op.f('ix_scan_history_scan_date'))
    op.drop_table('scan_history')
