"""hybrid entry mode: pending-fill BUY trigger + filled-now market BUY

Revision ID: a8b9c0d1e2f3
Revises: f7a8b9c0d1e2
Create Date: 2026-04-30 15:00:00

Phase E1.1 — splits the GTT-OCO submission into two paths:

  - entry_mode='now'     → MARKET BUY now + GTT-OCO bracket
                            (entry_status='filled', kite_buy_order_id set)
  - entry_mode='trigger' → GTT-single BUY at entry_price, no OCO yet
                            (entry_status='pending', kite_buy_trigger_id set;
                             OCO is placed by the TSL daemon once the
                             single trigger fires and the order fills)

entry_status default is 'filled' so legacy Trade rows (manual journal +
prior E1 OCO-only submissions) round-trip cleanly without backfill.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a8b9c0d1e2f3'
down_revision: Union[str, Sequence[str], None] = 'f7a8b9c0d1e2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('trades') as batch:
        batch.add_column(sa.Column(
            'entry_status', sa.String(),
            nullable=False, server_default='filled',
        ))
        batch.add_column(sa.Column(
            'kite_buy_trigger_id', sa.Integer(), nullable=True,
        ))
        batch.add_column(sa.Column(
            'kite_buy_order_id', sa.String(), nullable=True,
        ))
    op.create_index(
        'ix_trades_entry_status', 'trades', ['entry_status'], unique=False,
    )
    op.create_index(
        'ix_trades_kite_buy_trigger_id', 'trades',
        ['kite_buy_trigger_id'], unique=False,
    )


def downgrade() -> None:
    op.drop_index('ix_trades_kite_buy_trigger_id', table_name='trades')
    op.drop_index('ix_trades_entry_status', table_name='trades')
    with op.batch_alter_table('trades') as batch:
        batch.drop_column('kite_buy_order_id')
        batch.drop_column('kite_buy_trigger_id')
        batch.drop_column('entry_status')
