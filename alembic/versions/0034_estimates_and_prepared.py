"""Onboarding-time indicative estimates, and the 'proposal_prepared' progress stage.

Appends values to existing PostgreSQL enum types in an autocommit block (as 0023/0025/
0030 do) — ProjectStage gains 'proposal_prepared', TimelineEventType gains
'indicative_estimate_created' — and creates indicative_estimates. Stage ORDER lives in
app/onboarding/progress.py, not in the enum's sort order.

Revision ID: 0034_estimates_and_prepared
Revises: 0033_drafter_and_agreements
Create Date: 2026-09-28
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0034_estimates_and_prepared"
down_revision = "0033_drafter_and_agreements"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute("ALTER TYPE projectstage ADD VALUE IF NOT EXISTS 'proposal_prepared'")
            op.execute("ALTER TYPE timelineeventtype ADD VALUE IF NOT EXISTS 'indicative_estimate_created'")
    op.create_table(
        "indicative_estimates",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("onboarding_id", sa.Integer(), sa.ForeignKey("customer_onboardings.id"), nullable=False),
        sa.Column("inputs", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_indicative_estimates_onboarding_id", "indicative_estimates", ["onboarding_id"])


def downgrade() -> None:
    op.drop_index("ix_indicative_estimates_onboarding_id", table_name="indicative_estimates")
    op.drop_table("indicative_estimates")
    # The added enum values stay (PostgreSQL cannot drop an enum value).
