"""Add proposal fallback flag, employee Google Calendar fields, lead pending slots,
and the meetings table.

Note: this repository has historically relied on Base.metadata.create_all for schema
creation (dev/test), so there is no baseline migration. This is the first committed
migration (down_revision = None) and applies only the incremental changes listed above;
it assumes the pre-existing tables (employees, leads, proposals, ...) already exist.
Batch mode is used so the ALTERs also work on SQLite.

Revision ID: 0001_scheduling_and_fallback
Revises:
Create Date: 2026-08-11
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001_scheduling_and_fallback"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Part A — proposals.used_fallback_template
    with op.batch_alter_table("proposals") as batch:
        batch.add_column(
            sa.Column("used_fallback_template", sa.Boolean(), nullable=False, server_default=sa.false())
        )

    # Part B — employees Google Calendar connection fields
    with op.batch_alter_table("employees") as batch:
        batch.add_column(
            sa.Column("google_calendar_connected", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch.add_column(sa.Column("google_refresh_token", sa.Text(), nullable=True))
        batch.add_column(sa.Column("calendar_id", sa.String(), nullable=True))

    # Part B — leads.pending_meeting_slots
    with op.batch_alter_table("leads") as batch:
        batch.add_column(sa.Column("pending_meeting_slots", sa.JSON(), nullable=True))

    # Part B — meetings table
    op.create_table(
        "meetings",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("lead_id", sa.Integer(), sa.ForeignKey("leads.id"), nullable=False),
        sa.Column("employee_id", sa.String(), sa.ForeignKey("employees.id"), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_minutes", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("google_event_id", sa.String(), nullable=True),
        sa.Column("meet_link", sa.String(), nullable=True),
        sa.Column(
            "status",
            sa.Enum("scheduled", "cancelled", "completed", name="meetingstatus"),
            nullable=False,
            server_default="scheduled",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("meetings")
    with op.batch_alter_table("leads") as batch:
        batch.drop_column("pending_meeting_slots")
    with op.batch_alter_table("employees") as batch:
        batch.drop_column("calendar_id")
        batch.drop_column("google_refresh_token")
        batch.drop_column("google_calendar_connected")
    with op.batch_alter_table("proposals") as batch:
        batch.drop_column("used_fallback_template")
    # Drop the enum type on backends that create one (e.g. Postgres).
    sa.Enum(name="meetingstatus").drop(op.get_bind(), checkfirst=True)
