"""Add proposal.approved_at (timestamp of approval) — drives time-to-close and
pipeline-trend reporting.

Revision ID: 0007_proposal_approved_at
Revises: 0006_stale_lead_tracking
Create Date: 2026-08-11
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007_proposal_approved_at"
down_revision = "0006_stale_lead_tracking"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("proposals") as batch:
        batch.add_column(sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("proposals") as batch:
        batch.drop_column("approved_at")
