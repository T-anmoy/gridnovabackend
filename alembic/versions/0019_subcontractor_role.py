"""Add the 'subcontractor' value to the employeerole enum — scaffolding for a
future Subcontractor role. Deliberately maximally restricted from day one: see
AuthenticatedEmployee.can_view_assigned_work() in app/auth/dependencies.py,
which returns False unconditionally until Work Orders actually exist.

On SQLite the enum is just a VARCHAR check so nothing is needed there; on
Postgres the native enum type must gain the new value. No-op on SQLite.

Revision ID: 0019_subcontractor_role
Revises: 0018_project_reference_id
Create Date: 2026-09-04
"""
from __future__ import annotations

from alembic import op

revision = "0019_subcontractor_role"
down_revision = "0018_project_reference_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("COMMIT")
        op.execute("ALTER TYPE employeerole ADD VALUE IF NOT EXISTS 'subcontractor'")


def downgrade() -> None:
    # Postgres does not support removing a value from an enum type without
    # recreating it; leaving the value in place is safe and non-breaking.
    pass
