"""Record a failed formal-proposal draft on the onboarding, so it stays visible
(formal_proposal_blocked, reason "draft_failed") until a draft succeeds.

Revision ID: 0035_formal_draft_failure
Revises: 0034_estimates_and_prepared
Create Date: 2026-09-28
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0035_formal_draft_failure"
down_revision = "0034_estimates_and_prepared"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("customer_onboardings") as batch:
        batch.add_column(sa.Column("formal_draft_failed_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("formal_draft_failure", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("customer_onboardings") as batch:
        batch.drop_column("formal_draft_failure")
        batch.drop_column("formal_draft_failed_at")
