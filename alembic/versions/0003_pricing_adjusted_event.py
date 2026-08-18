"""Add the 'pricing_adjusted' value to the timelineeventtype enum (for the
PATCH /proposals/{id}/adjust-pricing endpoint).

On SQLite the enum is just a VARCHAR check so nothing is needed there; on Postgres
the native enum type must gain the new value. This migration is a no-op on SQLite.

Revision ID: 0003_pricing_adjusted_event
Revises: 0002_placeholder_customer
Create Date: 2026-08-11
"""
from __future__ import annotations

from alembic import op

revision = "0003_pricing_adjusted_event"
down_revision = "0002_placeholder_customer"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # ADD VALUE cannot run inside a transaction block on older PGs; commit first.
        op.execute("COMMIT")
        op.execute("ALTER TYPE timelineeventtype ADD VALUE IF NOT EXISTS 'pricing_adjusted'")


def downgrade() -> None:
    # Postgres does not support removing a value from an enum type without recreating
    # it; leaving the value in place is safe and non-breaking.
    pass
