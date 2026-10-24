"""prevent duplicate endpoint registrations

Adds a unique constraint on endpoints(url, event_type).

Why this exists: registering the same subscriber URL twice created two rows,
and every event then fanned out to both — so the subscriber silently received
each event twice. The uq_event_endpoint constraint on deliveries only stopped
one event hitting one endpoint ROW twice; nothing stopped two rows existing
for the same URL.

Revision ID: bc5a376aa80e
Revises: a31acc6e753e
Create Date: 2026-09-27 18:14:35.688126

"""
from alembic import op
import sqlalchemy as sa


revision = 'bc5a376aa80e'
down_revision = 'a31acc6e753e'
branch_labels = None
depends_on = None


def upgrade():
    # The constraint cannot be created while duplicate rows exist, so they
    # are collapsed first. Within each (url, event_type) group the OLDEST
    # row survives, keeping the longest delivery history.
    op.execute("""
        CREATE TEMPORARY TABLE _endpoint_dupes ON COMMIT DROP AS
        WITH ranked AS (
            SELECT id, url, event_type,
                   row_number() OVER (
                       PARTITION BY url, event_type
                       ORDER BY created_at, id
                   ) AS rn
            FROM endpoints
        )
        SELECT r.id AS dup_id, k.id AS keep_id
        FROM ranked r
        JOIN ranked k
          ON k.url = r.url
         AND k.event_type = r.event_type
         AND k.rn = 1
        WHERE r.rn > 1
    """)

    # Deliveries are moved onto the surviving endpoint rather than deleted,
    # so history is preserved — except where a move would break the
    # deliveries table's own uq_event_endpoint constraint, i.e. the same
    # event already has a row for the survivor. Those rows ARE the
    # accidental double-sends this migration exists to remove, so they go.
    op.execute("""
        DELETE FROM deliveries d
        USING _endpoint_dupes m
        WHERE d.endpoint_id = m.dup_id
          AND EXISTS (
              SELECT 1 FROM deliveries k
              WHERE k.event_id = d.event_id
                AND k.endpoint_id = m.keep_id
          )
    """)

    op.execute("""
        UPDATE deliveries d
        SET endpoint_id = m.keep_id
        FROM _endpoint_dupes m
        WHERE d.endpoint_id = m.dup_id
    """)

    op.execute("DELETE FROM endpoints WHERE id IN (SELECT dup_id FROM _endpoint_dupes)")

    op.create_unique_constraint('uq_endpoint_url_event_type', 'endpoints', ['url', 'event_type'])


def downgrade():
    # Drops the constraint only. The collapsed duplicate rows are not
    # recreated — restoring them would re-introduce the double-delivery
    # bug, so the duplicate endpoint has to be re-registered deliberately.
    op.drop_constraint('uq_endpoint_url_event_type', 'endpoints', type_='unique')
