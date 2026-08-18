"""Add a CheckConstraint enforcing that an 'approved' proposal always has a
recorded approver — a DB-level safety net for the approval gate.

Revision ID: 0004_proposal_approved_requires_approver
Revises: 0003_pricing_adjusted_event
Create Date: 2026-08-11
"""
from __future__ import annotations

from alembic import op

revision = "0004_proposal_approved_requires_approver"
down_revision = "0003_pricing_adjusted_event"
branch_labels = None
depends_on = None

_CONSTRAINT = "ck_proposal_approved_requires_approver"
_CONDITION = "status != 'approved' OR approved_by_employee_id IS NOT NULL"


def upgrade() -> None:
    # Batch mode so the ADD CONSTRAINT also works on SQLite (table recreate).
    with op.batch_alter_table("proposals") as batch:
        batch.create_check_constraint(_CONSTRAINT, _CONDITION)


def downgrade() -> None:
    with op.batch_alter_table("proposals") as batch:
        batch.drop_constraint(_CONSTRAINT, type_="check")
