"""Add the 'proposal_auto_drafted_from_inputs' value to the timelineeventtype
enum — a distinct marker from the generic 'proposal_draft_created' event so a
lead's history shows WHY a draft appeared: triggered automatically when a
reviewer approved its ProposalInputs, not clicked manually in the dashboard.

On SQLite the enum is just a VARCHAR check so nothing is needed there; on
Postgres the native enum type must gain the new value. No-op on SQLite.

Revision ID: 0017_proposal_auto_drafted_event
Revises: 0016_lead_source_estimate_form
Create Date: 2026-09-03
"""
from __future__ import annotations

from alembic import op

revision = "0017_proposal_auto_drafted_event"
down_revision = "0016_lead_source_estimate_form"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("COMMIT")
        op.execute(
            "ALTER TYPE timelineeventtype ADD VALUE IF NOT EXISTS 'proposal_auto_drafted_from_inputs'"
        )


def downgrade() -> None:
    # Postgres does not support removing a value from an enum type without
    # recreating it; leaving the value in place is safe and non-breaking.
    pass
