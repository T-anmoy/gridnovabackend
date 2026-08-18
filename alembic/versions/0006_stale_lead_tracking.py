"""Add lead.last_customer_message_at and lead.stale_notified_at for stale-lead
follow-up detection.

Revision ID: 0006_stale_lead_tracking
Revises: 0005_lead_source
Create Date: 2026-08-11
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006_stale_lead_tracking"
down_revision = "0005_lead_source"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("leads") as batch:
        batch.add_column(sa.Column("last_customer_message_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("stale_notified_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("leads") as batch:
        batch.drop_column("stale_notified_at")
        batch.drop_column("last_customer_message_at")
