"""broker_audit: append-only log of every Kite API call

Revision ID: d5e6f7a8b9c0
Revises: c4d5e6f7a8b9
Create Date: 2026-04-29 17:00:00

Foundation for the trading engine. Every read or write call we make to
Kite Connect lands here with request/response/latency/status. Forever-keep.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd5e6f7a8b9c0'
down_revision: Union[str, Sequence[str], None] = 'c4d5e6f7a8b9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'broker_audit',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('endpoint', sa.String(), nullable=False),
        sa.Column('request_json', sa.Text(), nullable=True),
        sa.Column('response_json', sa.Text(), nullable=True),
        sa.Column('status', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('latency_ms', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('error', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('broker_audit', schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f('ix_broker_audit_user_id'), ['user_id'], unique=False
        )
        batch_op.create_index(
            batch_op.f('ix_broker_audit_created_at'), ['created_at'], unique=False
        )
        batch_op.create_index(
            batch_op.f('ix_broker_audit_endpoint'), ['endpoint'], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table('broker_audit', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_broker_audit_endpoint'))
        batch_op.drop_index(batch_op.f('ix_broker_audit_created_at'))
        batch_op.drop_index(batch_op.f('ix_broker_audit_user_id'))
    op.drop_table('broker_audit')
