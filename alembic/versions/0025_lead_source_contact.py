"""Add the 'website_contact' value to the leadsource enum, so enquiries from the
website's contact form (POST /api/v1/public/contact) are distinguishable from the
estimate form, bookings and chat.

On SQLite the enum is a plain VARCHAR with no CHECK constraint, so nothing is needed
there. On PostgreSQL the native enum type gains the value in an autocommit block
(ADD VALUE cannot be used inside the transaction that adds it).

Revision ID: 0025_lead_source_contact
Revises: 0024_proposal_pdf_storage_key
Create Date: 2026-09-25
"""
from __future__ import annotations

from alembic import op

revision = "0025_lead_source_contact"
down_revision = "0024_proposal_pdf_storage_key"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute("ALTER TYPE leadsource ADD VALUE IF NOT EXISTS 'website_contact'")


def downgrade() -> None:
    # PostgreSQL cannot drop a value from an enum type without rebuilding it; the
    # value is left in place, as every earlier enum-extending revision does.
    pass
