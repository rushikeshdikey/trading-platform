"""Phase E2: trade Kite-management fields + tsl_decision table

Revision ID: e6f7a8b9c0d1
Revises: d5e6f7a8b9c0
Create Date: 2026-04-30 19:00:00

Adds three nullable columns to ``trades`` for trades placed via E1 GTT
submit + the Kite trigger id, target leg, and TSL anchor preference.
Adds a new ``tsl_decision`` table — append-only log of every daemon run.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e6f7a8b9c0d1'
down_revision: Union[str, Sequence[str], None] = 'd5e6f7a8b9c0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Trade columns (additive, all nullable, no backfill needed)
    with op.batch_alter_table('trades', schema=None) as batch_op:
        batch_op.add_column(sa.Column('kite_trigger_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('kite_target_price', sa.Float(), nullable=True))
        batch_op.add_column(sa.Column('tsl_anchor', sa.String(), nullable=True))
        batch_op.create_index(
            batch_op.f('ix_trades_kite_trigger_id'),
            ['kite_trigger_id'], unique=False,
        )

    # 2. tsl_decision table
    op.create_table(
        'tsl_decision',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('trade_id', sa.Integer(), nullable=False),
        sa.Column('decision_date', sa.Date(), nullable=False),
        sa.Column('decided_at', sa.DateTime(), nullable=False),
        sa.Column('cmp', sa.Float(), nullable=False),
        sa.Column('current_r', sa.Float(), nullable=True),
        sa.Column('anchor', sa.String(), nullable=True),
        sa.Column('anchor_value', sa.Float(), nullable=True),
        sa.Column('current_stop', sa.Float(), nullable=False),
        sa.Column('proposed_stop', sa.Float(), nullable=True),
        sa.Column('action', sa.String(), nullable=False),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('modify_response', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['trade_id'], ['trades.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('tsl_decision', schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f('ix_tsl_decision_user_id'), ['user_id'], unique=False,
        )
        batch_op.create_index(
            batch_op.f('ix_tsl_decision_trade_id'), ['trade_id'], unique=False,
        )
        batch_op.create_index(
            batch_op.f('ix_tsl_decision_decision_date'), ['decision_date'], unique=False,
        )
        batch_op.create_index(
            batch_op.f('ix_tsl_decision_action'), ['action'], unique=False,
        )
        batch_op.create_index(
            'ix_tsl_decision_trade_day',
            ['trade_id', 'decision_date'], unique=True,
        )


def downgrade() -> None:
    with op.batch_alter_table('tsl_decision', schema=None) as batch_op:
        batch_op.drop_index('ix_tsl_decision_trade_day')
        batch_op.drop_index(batch_op.f('ix_tsl_decision_action'))
        batch_op.drop_index(batch_op.f('ix_tsl_decision_decision_date'))
        batch_op.drop_index(batch_op.f('ix_tsl_decision_trade_id'))
        batch_op.drop_index(batch_op.f('ix_tsl_decision_user_id'))
    op.drop_table('tsl_decision')

    with op.batch_alter_table('trades', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_trades_kite_trigger_id'))
        batch_op.drop_column('tsl_anchor')
        batch_op.drop_column('kite_target_price')
        batch_op.drop_column('kite_trigger_id')
