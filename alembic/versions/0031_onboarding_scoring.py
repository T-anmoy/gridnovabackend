"""Lead scoring v2 on customer_onboardings: score, band, factor breakdown, response
deadline and escalation tracking.

Revision ID: 0031_onboarding_scoring
Revises: 0030_associate_onboarding
Create Date: 2026-09-27
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0031_onboarding_scoring"
down_revision = "0030_associate_onboarding"
branch_labels = None
depends_on = None

_BAND = ("hot", "warm", "cold")


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        postgresql.ENUM(*_BAND, name="scoreband").create(bind, checkfirst=True)
    band = sa.Enum(*_BAND, name="scoreband").with_variant(
        postgresql.ENUM(*_BAND, name="scoreband", create_type=False), "postgresql")
    with op.batch_alter_table("customer_onboardings") as batch:
        batch.add_column(sa.Column("score", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("score_band", band, nullable=True))
        batch.add_column(sa.Column("score_factors", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("scored_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("response_due_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("escalation_level", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("last_escalated_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("customer_onboardings") as batch:
        for column in ("last_escalated_at", "escalation_level", "response_due_at", "scored_at",
                       "score_factors", "score_band", "score"):
            batch.drop_column(column)
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TYPE IF EXISTS scoreband")
