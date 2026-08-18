"""Add a partial unique index on meetings(employee_id, scheduled_at), scoped to
status = 'scheduled', as a hard database-level backstop against double-booking.

Live concurrency testing showed the application-level re-verification
(is_employee_free -> Google Calendar free/busy) is a check-then-act race: two
simultaneous booking requests for the same employee/slot can both observe "free"
before either has committed, and both succeed. This index makes that impossible
at the database layer regardless of application-level races. Scoped to
status = 'scheduled' (not a blanket constraint) so a cancelled meeting doesn't
permanently block ever re-booking that slot.

Revision ID: 0009_meeting_slot_unique_index
Revises: 0008_proposal_accept_link
Create Date: 2026-08-18
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009_meeting_slot_unique_index"
down_revision = "0008_proposal_accept_link"
branch_labels = None
depends_on = None

INDEX_NAME = "uq_meetings_employee_slot_active"


def upgrade() -> None:
    op.create_index(
        INDEX_NAME,
        "meetings",
        ["employee_id", "scheduled_at"],
        unique=True,
        sqlite_where=sa.text("status = 'scheduled'"),
        postgresql_where=sa.text("status = 'scheduled'"),
    )


def downgrade() -> None:
    op.drop_index(INDEX_NAME, table_name="meetings")
