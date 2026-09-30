"""Execution stages: eight new project stages, one project_executions row per
associate-sourced project from `agreement_signed` on (backfilled for projects already
past it), associate stage submissions awaiting Operations, and photo fingerprints for
duplicate-photo detection.

Revision ID: 0037_execution_stages
Revises: 0036_placeholder_registry
Create Date: 2026-09-29
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0037_execution_stages"
down_revision = "0036_placeholder_registry"
branch_labels = None
depends_on = None

_NEW_STAGES = ("feasibility_submitted", "material_dispatched", "installed", "inspection_net_meter", "commissioned",
               "subsidy_claimed", "subsidy_received", "handover_complete")
_ALL_STAGES = ("registered", "verified", "site_assessed", "proposal_requested", "proposal_prepared",
               "proposal_issued", "accepted", "agreement_signed", "portal_agreement_approved") + _NEW_STAGES
_MODES = ("associate", "grid_nova")
_STATUSES = ("pending", "verified", "rejected")


def _enum(values, name, pg):
    return sa.Enum(*values, name=name).with_variant(postgresql.ENUM(*values, name=name, create_type=False),
                                                     "postgresql") if pg else sa.Enum(*values, name=name)


def upgrade() -> None:
    bind = op.get_bind()
    pg = bind.dialect.name == "postgresql"
    if pg:
        with op.get_context().autocommit_block():
            for value in _NEW_STAGES:
                op.execute(f"ALTER TYPE projectstage ADD VALUE IF NOT EXISTS '{value}'")
            op.execute("ALTER TYPE timelineeventtype ADD VALUE IF NOT EXISTS 'execution_stage_submitted'")
            op.execute("ALTER TYPE timelineeventtype ADD VALUE IF NOT EXISTS 'execution_stage_rejected'")
        postgresql.ENUM(*_MODES, name="executionmode").create(bind, checkfirst=True)
        postgresql.ENUM(*_STATUSES, name="stagesubmissionstatus").create(bind, checkfirst=True)

    op.create_table(
        "project_executions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id"), nullable=False, unique=True),
        sa.Column("onboarding_id", sa.Integer(), sa.ForeignKey("customer_onboardings.id"), nullable=False,
                  unique=True),
        sa.Column("execution_mode", _enum(_MODES, "executionmode", pg), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("files", sa.JSON(), nullable=False),
        sa.Column("register_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("register_verified_by", sa.String(), nullable=True),
        sa.Column("stuck_alerted_stage", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "stage_submissions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("onboarding_id", sa.Integer(), sa.ForeignKey("customer_onboardings.id"), nullable=False),
        sa.Column("stage", _enum(_ALL_STAGES, "projectstage", pg), nullable=False),
        sa.Column("submitted_by", sa.String(), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("status", _enum(_STATUSES, "stagesubmissionstatus", pg), nullable=False),
        sa.Column("reviewed_by", sa.String(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_note", sa.Text(), nullable=True),
    )
    op.create_index("ix_stage_submissions_project_id", "stage_submissions", ["project_id"])
    op.create_index("ix_stage_submissions_onboarding_id", "stage_submissions", ["onboarding_id"])
    op.create_index("uq_stage_submissions_one_pending", "stage_submissions", ["project_id", "stage"], unique=True,
                    sqlite_where=sa.text("status = 'pending'"), postgresql_where=sa.text("status = 'pending'"))
    op.create_table(
        "photo_fingerprints",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("slot", sa.String(), nullable=False),
        sa.Column("storage_key", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_photo_fingerprints_sha256", "photo_fingerprints", ["sha256"])
    op.create_index("ix_photo_fingerprints_project_id", "photo_fingerprints", ["project_id"])

    # Backfill: every associate-sourced project already past agreement_signed gets its
    # execution row. Mode as app/execution/service.py decides it for new ones.
    mode = ("CASE WHEN a.associate_type = 'preferred_associate' OR a.capability = 'execution' "
            "THEN 'associate' ELSE 'grid_nova' END")
    if pg:
        mode = f"({mode})::executionmode"
    op.execute(
        "INSERT INTO project_executions (project_id, onboarding_id, execution_mode, started_at, files, created_at) "
        f"SELECT o.project_id, o.id, {mode}, e.recorded_at, '{{}}', e.recorded_at "
        "FROM customer_onboardings o "
        "JOIN project_stage_events e ON e.project_id = o.project_id AND e.stage = 'agreement_signed' "
        "JOIN associates a ON a.id = o.owner_associate_id"
    )


def downgrade() -> None:
    op.drop_index("ix_photo_fingerprints_project_id", table_name="photo_fingerprints")
    op.drop_index("ix_photo_fingerprints_sha256", table_name="photo_fingerprints")
    op.drop_table("photo_fingerprints")
    op.drop_index("uq_stage_submissions_one_pending", table_name="stage_submissions")
    op.drop_index("ix_stage_submissions_onboarding_id", table_name="stage_submissions")
    op.drop_index("ix_stage_submissions_project_id", table_name="stage_submissions")
    op.drop_table("stage_submissions")
    op.drop_table("project_executions")
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TYPE IF EXISTS stagesubmissionstatus")
        op.execute("DROP TYPE IF EXISTS executionmode")
    # The added projectstage/timelineeventtype values stay (PostgreSQL cannot drop an
    # enum value); nothing refers to them once the tables above are gone.
