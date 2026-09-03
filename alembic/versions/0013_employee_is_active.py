"""Add employees.is_active for deactivation (never a hard delete). Every historical
FK to employees (leads.assigned_employee_id, proposals.approved_by_employee_id,
proposal_inputs.reviewed_by_id, meetings.employee_id) keeps pointing at a real row
regardless of this flag — deactivation is purely additive.

Revision ID: 0013_employee_is_active
Revises: 0012_memory_kind_outcome
Create Date: 2026-09-03
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0013_employee_is_active"
down_revision = "0012_memory_kind_outcome"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("employees") as batch:
        batch.add_column(
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true")
        )


def downgrade() -> None:
    with op.batch_alter_table("employees") as batch:
        batch.drop_column("is_active")
