"""Add the 'website_booking' value to the leadsource enum, so leads created by
POST /api/v1/public/booking are distinguishable in reporting from chat-sourced
and estimate-form-sourced leads.

On SQLite the enum is just a VARCHAR check so nothing is needed there; on
Postgres the native enum type must gain the new value. No-op on SQLite.

Revision ID: 0021_lead_source_booking
Revises: 0020_equipment_master
Create Date: 2026-09-06
"""
from __future__ import annotations

from alembic import op

revision = "0021_lead_source_booking"
down_revision = "0020_equipment_master"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("COMMIT")
        op.execute("ALTER TYPE leadsource ADD VALUE IF NOT EXISTS 'website_booking'")


def downgrade() -> None:
    # Postgres does not support removing a value from an enum type without
    # recreating it; leaving the value in place is safe and non-breaking.
    pass
