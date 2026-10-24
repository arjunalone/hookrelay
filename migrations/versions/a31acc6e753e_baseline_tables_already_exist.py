"""baseline - create endpoints, events, deliveries

Revision ID: a31acc6e753e
Revises:
Create Date: 2026-09-27 14:20:47.268019

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = 'a31acc6e753e'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'endpoints',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('url', sa.String(), nullable=False),
        sa.Column('secret', sa.String(), nullable=False),
        sa.Column('event_type', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )

    op.create_table(
        'events',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('event_type', sa.String(), nullable=False),
        sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('idempotency_key', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('idempotency_key'),
    )

    op.create_table(
        'deliveries',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('event_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('endpoint_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('attempt_count', sa.Integer(), nullable=False),
        sa.Column('next_attempt_at', sa.DateTime(), nullable=True),
        sa.Column('locked_until', sa.DateTime(), nullable=True),
        sa.Column('locked_by', sa.String(), nullable=True),
        sa.Column('last_status_code', sa.Integer(), nullable=True),
        sa.Column('last_error', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['event_id'], ['events.id']),
        sa.ForeignKeyConstraint(['endpoint_id'], ['endpoints.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('event_id', 'endpoint_id', name='uq_event_endpoint'),
    )


def downgrade():
    op.drop_table('deliveries')
    op.drop_table('events')
    op.drop_table('endpoints')
