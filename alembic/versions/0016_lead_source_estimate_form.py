"""Add the 'website_estimate_form' value to the leadsource enum, so leads created
by POST /api/v1/public/estimate-request are distinguishable in reporting from
chat-sourced leads.

On SQLite the enum is just a VARCHAR check so nothing is needed there; on
Postgres the native enum type must gain the new value. No-op on SQLite.

Revision ID: 0016_lead_source_estimate_form
Revises: 0015_project_required
Create Date: 2026-09-03
"""
from __future__ import annotations

from alembic import op

revision = "0016_lead_source_estimate_form"
down_revision = "0015_project_required"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("COMMIT")
        op.execute("ALTER TYPE leadsource ADD VALUE IF NOT EXISTS 'website_estimate_form'")


def downgrade() -> None:
    # Postgres does not support removing a value from an enum type without
    # recreating it; leaving the value in place is safe and non-breaking.
    pass
