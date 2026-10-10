"""On-site site survey (S1): Grid Nova's paper rooftop checklist as survey fields on the
site assessment (proposal_inputs), the associate's on-screen signature and the customer's
declaration, the Engineer's feasibility review, and the survey report key; the
survey_correction_requests table (changes after verification/review); proposals'
needs_reverification marker; customer_onboardings.survey_grandfathered; four
TimelineEventType values.

Backfill: onboardings already submitted, verified or closed are grandfathered (not held
to the survey submit gate); drafts are not. Existing proposal_inputs get survey_variant
individual_house.

Downgrade drops the new columns, the table and the new enum types (the survey data they
hold is lost). The added timelineeventtype values stay — PostgreSQL cannot drop an enum
value.

Revision ID: 0044_site_survey
Revises: 0043_unmatched_units
Create Date: 2026-10-06
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0044_site_survey"
down_revision = "0043_unmatched_units"
branch_labels = None
depends_on = None

_TIMELINE = ("survey_signed", "survey_reviewed", "survey_correction_requested", "survey_corrected")
_ENUMS = {
    "surveyvariant": ("individual_house", "housing_society", "commercial_roof"),
    "roofmaterial": ("concrete_flat", "tin_shed", "asbestos", "other"),
    "rooforientation": ("south", "east", "west", "north", "mixed", "unknown"),
    "inverterlocation": ("terrace", "indoor_wall", "outdoor_wall", "other"),
    "distancemethod": ("tape", "laser", "estimated"),
    "societynocstatus": ("obtained", "in_progress", "not_required", "not_available"),
    "feasibilityoutcome": ("feasible", "feasible_with_conditions", "revisit_needed", "not_feasible"),
    "surveycorrectionstatus": ("pending", "applied", "rejected"),
}
_INPUT_COLUMNS = (
    "survey_report_generated_at", "survey_report_key", "survey_revised_after_review", "feasibility_reviewed_at",
    "feasibility_reviewed_by_id", "revisit_items", "feasibility_conditions", "feasibility_notes",
    "feasibility_outcome", "customer_declaration_name", "customer_declared_at", "associate_signature_ref",
    "tenant_noc_available", "contract_demand_kva", "society_noc_status", "society_flat_count", "distance_method",
    "distance_inverter_to_lt_panel_m", "distance_panel_to_inverter_m", "indoor_wall_available",
    "inverter_location", "water_available", "shadow_free_area_sqft", "roof_sections", "roof_obstacles",
    "roof_orientation", "roof_material_other", "roof_material", "roof_access_note", "roof_accessible",
    "survey_variant",
)
_PROPOSAL_COLUMNS = ("reverification_cleared_at", "reverification_cleared_by", "reverification_flagged_at",
                     "reverification_reason", "needs_reverification")


def _enum(name, pg):
    values = _ENUMS[name]
    return sa.Enum(*values, name=name).with_variant(postgresql.ENUM(*values, name=name, create_type=False),
                                                     "postgresql") if pg else sa.Enum(*values, name=name)


def upgrade() -> None:
    bind = op.get_bind()
    pg = bind.dialect.name == "postgresql"
    if pg:
        with op.get_context().autocommit_block():
            for value in _TIMELINE:
                op.execute(f"ALTER TYPE timelineeventtype ADD VALUE IF NOT EXISTS '{value}'")
        for name, values in _ENUMS.items():
            postgresql.ENUM(*values, name=name).create(bind, checkfirst=True)

    with op.batch_alter_table("proposal_inputs") as batch:
        batch.add_column(sa.Column("survey_variant", _enum("surveyvariant", pg), nullable=False,
                                   server_default="individual_house"))
        batch.add_column(sa.Column("roof_accessible", sa.Boolean(), nullable=True))
        batch.add_column(sa.Column("roof_access_note", sa.Text(), nullable=True))
        batch.add_column(sa.Column("roof_material", _enum("roofmaterial", pg), nullable=True))
        batch.add_column(sa.Column("roof_material_other", sa.String(length=200), nullable=True))
        batch.add_column(sa.Column("roof_orientation", _enum("rooforientation", pg), nullable=True))
        batch.add_column(sa.Column("roof_obstacles", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("roof_sections", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("shadow_free_area_sqft", sa.Float(), nullable=True))
        batch.add_column(sa.Column("water_available", sa.Boolean(), nullable=True))
        batch.add_column(sa.Column("inverter_location", _enum("inverterlocation", pg), nullable=True))
        batch.add_column(sa.Column("indoor_wall_available", sa.Boolean(), nullable=True))
        batch.add_column(sa.Column("distance_panel_to_inverter_m", sa.Numeric(7, 2), nullable=True))
        batch.add_column(sa.Column("distance_inverter_to_lt_panel_m", sa.Numeric(7, 2), nullable=True))
        batch.add_column(sa.Column("distance_method", _enum("distancemethod", pg), nullable=True))
        batch.add_column(sa.Column("society_flat_count", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("society_noc_status", _enum("societynocstatus", pg), nullable=True))
        batch.add_column(sa.Column("contract_demand_kva", sa.Float(), nullable=True))
        batch.add_column(sa.Column("tenant_noc_available", sa.Boolean(), nullable=True))
        batch.add_column(sa.Column("associate_signature_ref", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("customer_declared_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("customer_declaration_name", sa.String(length=200), nullable=True))
        batch.add_column(sa.Column("feasibility_outcome", _enum("feasibilityoutcome", pg), nullable=True))
        batch.add_column(sa.Column("feasibility_notes", sa.Text(), nullable=True))
        batch.add_column(sa.Column("feasibility_conditions", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("revisit_items", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("feasibility_reviewed_by_id", sa.String(), nullable=True))
        batch.add_column(sa.Column("feasibility_reviewed_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("survey_revised_after_review", sa.Boolean(), nullable=False,
                                   server_default=sa.false()))
        batch.add_column(sa.Column("survey_report_key", sa.String(), nullable=True))
        batch.add_column(sa.Column("survey_report_generated_at", sa.DateTime(timezone=True), nullable=True))
        batch.create_foreign_key("fk_proposal_inputs_feasibility_reviewed_by", "employees",
                                 ["feasibility_reviewed_by_id"], ["id"])

    with op.batch_alter_table("proposals") as batch:
        batch.add_column(sa.Column("needs_reverification", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.add_column(sa.Column("reverification_reason", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("reverification_flagged_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("reverification_cleared_by", sa.String(), nullable=True))
        batch.add_column(sa.Column("reverification_cleared_at", sa.DateTime(timezone=True), nullable=True))

    with op.batch_alter_table("customer_onboardings") as batch:
        batch.add_column(sa.Column("survey_grandfathered", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.execute("UPDATE customer_onboardings SET survey_grandfathered = true WHERE status != 'draft'")

    op.create_table(
        "survey_correction_requests",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("onboarding_id", sa.Integer(), sa.ForeignKey("customer_onboardings.id"), nullable=False),
        sa.Column("requested_by", sa.String(), nullable=False),
        sa.Column("changes", sa.JSON(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("status", _enum("surveycorrectionstatus", pg), nullable=False),
        sa.Column("reviewed_by", sa.String(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_survey_correction_requests_onboarding", "survey_correction_requests", ["onboarding_id"])
    op.create_index("ix_survey_correction_requests_status", "survey_correction_requests", ["status"])
    op.create_index("uq_survey_correction_requests_one_pending", "survey_correction_requests", ["onboarding_id"],
                    unique=True, sqlite_where=sa.text("status = 'pending'"),
                    postgresql_where=sa.text("status = 'pending'"))


def downgrade() -> None:
    op.drop_index("uq_survey_correction_requests_one_pending", table_name="survey_correction_requests")
    op.drop_index("ix_survey_correction_requests_status", table_name="survey_correction_requests")
    op.drop_index("ix_survey_correction_requests_onboarding", table_name="survey_correction_requests")
    op.drop_table("survey_correction_requests")
    with op.batch_alter_table("customer_onboardings") as batch:
        batch.drop_column("survey_grandfathered")
    with op.batch_alter_table("proposals") as batch:
        for column in _PROPOSAL_COLUMNS:
            batch.drop_column(column)
    with op.batch_alter_table("proposal_inputs") as batch:
        batch.drop_constraint("fk_proposal_inputs_feasibility_reviewed_by", type_="foreignkey")
        for column in _INPUT_COLUMNS:
            batch.drop_column(column)
    if op.get_bind().dialect.name == "postgresql":
        for name in _ENUMS:
            op.execute(f"DROP TYPE IF EXISTS {name}")
