"""Add lead.source (outreach channel attribution), server_default 'website_chat'
so existing rows backfill safely.

Revision ID: 0005_lead_source
Revises: 0004_proposal_approved_requires_approver
Create Date: 2026-08-11
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_lead_source"
down_revision = "0004_proposal_approved_requires_approver"
branch_labels = None
depends_on = None

_ENUM = sa.Enum(
    "website_chat", "whatsapp", "instagram", "cold_call", "referral", "other",
    name="leadsource",
)


def upgrade() -> None:
    bind = op.get_bind()
    _ENUM.create(bind, checkfirst=True)  # no-op on SQLite; creates the type on Postgres
    with op.batch_alter_table("leads") as batch:
        batch.add_column(
            sa.Column("source", _ENUM, nullable=False, server_default="website_chat")
        )


def downgrade() -> None:
    with op.batch_alter_table("leads") as batch:
        batch.drop_column("source")
    _ENUM.drop(op.get_bind(), checkfirst=True)
