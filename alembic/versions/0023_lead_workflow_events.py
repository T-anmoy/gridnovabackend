"""Add 'stage_changed', 'lead_assigned' and 'fields_filled_from_inputs' to the
timelineeventtype enum — the timeline entries for manual/automatic pipeline-stage
changes, lead reassignment, and filling a lead's required fields from an approved
site assessment.

On SQLite the enum is a plain VARCHAR with no CHECK constraint, so nothing is
needed there. On PostgreSQL the native enum type must gain the values; ADD VALUE
runs in an autocommit block because it cannot be used inside the transaction that
adds it (and could not run in a transaction at all before PostgreSQL 12).

Revision ID: 0023_lead_workflow_events
Revises: 0022_site_assessment_fields
Create Date: 2026-09-25
"""
from __future__ import annotations

from alembic import op

revision = "0023_lead_workflow_events"
down_revision = "0022_site_assessment_fields"
branch_labels = None
depends_on = None

_NEW_VALUES = ("stage_changed", "lead_assigned", "fields_filled_from_inputs")


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            for value in _NEW_VALUES:
                op.execute(f"ALTER TYPE timelineeventtype ADD VALUE IF NOT EXISTS '{value}'")


def downgrade() -> None:
    # PostgreSQL cannot drop a value from an enum type without rebuilding it and
    # rewriting every timeline_events row; the values are left in place, as every
    # earlier enum-extending revision in this chain does.
    pass
