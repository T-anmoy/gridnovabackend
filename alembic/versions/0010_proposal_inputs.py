"""Add the proposal_inputs table — raw customer-provided site assessment data
submitted via the /proposal/new form, kept separate from the AI-generated
Proposal table. Additive only: no existing table is touched.

Revision ID: 0010_proposal_inputs
Revises: 0009_meeting_slot_unique_index
Create Date: 2026-08-29
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010_proposal_inputs"
down_revision = "0009_meeting_slot_unique_index"
branch_labels = None
depends_on = None

_INPUT_STATUS = sa.Enum(
    "submitted", "under_review", "approved_for_proposal", "rejected",
    name="inputstatus",
)
_CUSTOMER_TYPE = sa.Enum("individual", "organisation", name="customertype")
_PROJECT_FAMILY = sa.Enum(
    "rooftop_solar", "ground_mounted", "government_institutional",
    "street_lighting", "water_pump", "om_service", "consulting",
    name="projectfamily",
)
_GOVERNMENT_SCHEME = sa.Enum(
    "pm_surya_ghar", "pm_kusum_a", "pm_kusum_b", "pm_kusum_c",
    "wbreda", "other_government", "none",
    name="governmentscheme",
)
_CONNECTION_TYPE = sa.Enum("on_grid", "off_grid", "hybrid", name="connectiontype")
_CONNECTION_PHASE = sa.Enum("single_phase", "three_phase", name="connectionphase")
_SITE_TYPE = sa.Enum(
    "rooftop_residential", "rooftop_commercial", "rooftop_institutional",
    "ground_mounted", "carport", "other",
    name="sitetype",
)
_SHADING_CONDITION = sa.Enum(
    "none", "partial_morning", "partial_evening", "heavy",
    name="shadingcondition",
)
_EQUIPMENT_TIER = sa.Enum("standard", "premium", "as_recommended", name="equipmenttier")

_ENUMS = [
    _INPUT_STATUS, _CUSTOMER_TYPE, _PROJECT_FAMILY, _GOVERNMENT_SCHEME,
    _CONNECTION_TYPE, _CONNECTION_PHASE, _SITE_TYPE, _SHADING_CONDITION,
    _EQUIPMENT_TIER,
]


def upgrade() -> None:
    bind = op.get_bind()
    for enum_type in _ENUMS:
        enum_type.create(bind, checkfirst=True)  # no-op on SQLite; creates the type on Postgres

    op.create_table(
        "proposal_inputs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("lead_id", sa.Integer(), sa.ForeignKey("leads.id"), nullable=False),
        sa.Column("status", _INPUT_STATUS, nullable=False, server_default="submitted"),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_by_id", sa.String(), sa.ForeignKey("employees.id"), nullable=True),
        sa.Column("rejection_reason", sa.Text(), nullable=True),

        # Customer & Project
        sa.Column("customer_type", _CUSTOMER_TYPE, nullable=False),
        sa.Column("organisation_name", sa.String(), nullable=True),
        sa.Column("gst_number", sa.String(), nullable=True),
        sa.Column("project_family", _PROJECT_FAMILY, nullable=False),
        sa.Column("scheme_linked", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("scheme_name", _GOVERNMENT_SCHEME, nullable=True, server_default="none"),

        # Site Location
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("district", sa.String(), nullable=False),
        sa.Column("pin_code", sa.String(), nullable=False),
        sa.Column("full_address", sa.Text(), nullable=True),
        sa.Column("discom", sa.String(), nullable=True),
        sa.Column("consumer_number", sa.String(), nullable=True),
        sa.Column("site_type", _SITE_TYPE, nullable=False),
        sa.Column("roof_area_sqft", sa.Float(), nullable=True),
        sa.Column("floors", sa.Integer(), nullable=True),
        sa.Column("shading_condition", _SHADING_CONDITION, nullable=True),
        sa.Column("structural_notes", sa.Text(), nullable=True),

        # Load & Output
        sa.Column("avg_monthly_bill_inr", sa.Float(), nullable=True),
        sa.Column("avg_monthly_units_kwh", sa.Float(), nullable=True),
        sa.Column("peak_load_kw", sa.Float(), nullable=True),
        sa.Column("connection_phase", _CONNECTION_PHASE, nullable=True),
        sa.Column("desired_output_kw", sa.Float(), nullable=True),
        sa.Column("load_notes", sa.Text(), nullable=True),

        # System Configuration
        sa.Column("connection_type", _CONNECTION_TYPE, nullable=False),
        sa.Column("net_metering_required", sa.Boolean(), nullable=True),
        sa.Column("dcr_panels_required", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("ev_charging", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("battery_storage", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("equipment_tier", _EQUIPMENT_TIER, nullable=True),
        sa.Column("additional_requirements", sa.Text(), nullable=True),

        # Documents
        sa.Column("document_refs", sa.JSON(), nullable=True),

        # Internal notes
        sa.Column("internal_notes", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("proposal_inputs")
    for enum_type in reversed(_ENUMS):
        enum_type.drop(op.get_bind(), checkfirst=True)
