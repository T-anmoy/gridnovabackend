"""Per-project sales progress: project_stage_events (append-only, one row per stage)
and the 'project_stage_recorded' timeline event type.

Revision ID: 0032_project_stages
Revises: 0031_onboarding_scoring
Create Date: 2026-09-27
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0032_project_stages"
down_revision = "0031_onboarding_scoring"
branch_labels = None
depends_on = None

_STAGES = ("registered", "verified", "site_assessed", "proposal_requested", "proposal_issued", "accepted",
           "agreement_signed", "portal_agreement_approved")


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute("ALTER TYPE timelineeventtype ADD VALUE IF NOT EXISTS 'project_stage_recorded'")
        postgresql.ENUM(*_STAGES, name="projectstage").create(bind, checkfirst=True)
    stage = sa.Enum(*_STAGES, name="projectstage").with_variant(
        postgresql.ENUM(*_STAGES, name="projectstage", create_type=False), "postgresql")
    op.create_table(
        "project_stage_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("stage", stage, nullable=False),
        sa.Column("recorded_by", sa.String(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.UniqueConstraint("project_id", "stage", name="uq_project_stage_once"),
    )
    op.create_index("ix_project_stage_events_project_id", "project_stage_events", ["project_id"])


def downgrade() -> None:
    op.drop_index("ix_project_stage_events_project_id", table_name="project_stage_events")
    op.drop_table("project_stage_events")
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TYPE IF EXISTS projectstage")
    # 'project_stage_recorded' stays in timelineeventtype (PostgreSQL cannot drop an enum value).
