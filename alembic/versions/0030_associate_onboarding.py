"""Associate-led customer onboarding: the 'associate' and 'preferred_associate' lead
sources, three timeline event types, customer_onboardings and
onboarding_attribution_flags.

Enum values are added to the existing PostgreSQL types in an autocommit block, as
0023/0025 do. New enum types are created explicitly with checkfirst and referenced
with create_type=False, as 0028 does. On SQLite enums are plain VARCHARs.

Revision ID: 0030_associate_onboarding
Revises: 0029_kalimpong_lava
Create Date: 2026-09-27
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0030_associate_onboarding"
down_revision = "0029_kalimpong_lava"
branch_labels = None
depends_on = None

_ADDED_VALUES = {
    "leadsource": ("associate", "preferred_associate"),
    "timelineeventtype": ("onboarding_submitted", "onboarding_verified", "attribution_flagged"),
}
_ENUMS = {
    "onboardingstatus": ("draft", "submitted", "verified", "closed"),
    "customertimeline": ("within_1_month", "within_3_months", "within_6_months", "later", "unsure"),
    "attributionflagstatus": ("open", "resolved"),
    "attributionresolution": ("keep_existing", "award_new"),
}


def _enum(name: str):
    values = _ENUMS[name]
    return sa.Enum(*values, name=name).with_variant(
        postgresql.ENUM(*values, name=name, create_type=False), "postgresql",
    )


def _ts(name: str, nullable: bool = True) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            for type_name, values in _ADDED_VALUES.items():
                for value in values:
                    op.execute(f"ALTER TYPE {type_name} ADD VALUE IF NOT EXISTS '{value}'")
        for name, values in _ENUMS.items():
            postgresql.ENUM(*values, name=name).create(bind, checkfirst=True)

    op.create_table(
        "customer_onboardings",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("lead_id", sa.Integer(), sa.ForeignKey("leads.id"), nullable=False, unique=True),
        sa.Column("customer_id", sa.Integer(), sa.ForeignKey("customers.id"), nullable=False),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("proposal_inputs_id", sa.Integer(), sa.ForeignKey("proposal_inputs.id"), nullable=False, unique=True),
        sa.Column("owner_associate_id", sa.Integer(), sa.ForeignKey("associates.id"), nullable=False),
        sa.Column("status", _enum("onboardingstatus"), nullable=False),
        sa.Column("state_id", sa.Integer(), sa.ForeignKey("territory_states.id"), nullable=False),
        sa.Column("district_id", sa.Integer(), sa.ForeignKey("territory_districts.id"), nullable=False),
        sa.Column("block_id", sa.Integer(), sa.ForeignKey("territory_blocks.id"), nullable=True),
        sa.Column("site_latitude", sa.Float(), nullable=True),
        sa.Column("site_longitude", sa.Float(), nullable=True),
        sa.Column("customer_mobile", sa.String(length=10), nullable=False),
        sa.Column("consumer_number_key", sa.String(), nullable=True),
        sa.Column("connection_in_customer_name", sa.Boolean(), nullable=False),
        sa.Column("property_in_customer_name", sa.Boolean(), nullable=False),
        sa.Column("customer_timeline", _enum("customertimeline"), nullable=True),
        sa.Column("notice_version", sa.String(), nullable=False),
        _ts("notice_acknowledged_at", nullable=False),
        sa.Column("notice_recorded_by", sa.String(), nullable=False),
        _ts("submitted_at"),
        sa.Column("verification_call_completed", sa.Boolean(), nullable=False),
        sa.Column("verification_call_note", sa.Text(), nullable=True),
        sa.Column("verified_by_employee_id", sa.String(), sa.ForeignKey("employees.id"), nullable=True),
        _ts("verified_at"),
        _ts("attribution_locked_until"),
        sa.Column("closed_reason", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(), nullable=False),
        _ts("created_at", nullable=False),
        _ts("updated_at", nullable=False),
        sa.CheckConstraint(
            "status != 'verified' OR (verification_call_completed = true AND verification_call_note IS NOT NULL "
            "AND verified_by_employee_id IS NOT NULL AND attribution_locked_until IS NOT NULL)",
            name="ck_customer_onboardings_verified_after_call",
        ),
        sa.CheckConstraint(
            "(site_latitude IS NULL AND site_longitude IS NULL) OR "
            "(site_latitude BETWEEN -90 AND 90 AND site_longitude BETWEEN -180 AND 180)",
            name="ck_customer_onboardings_gps",
        ),
    )
    op.create_index("ix_customer_onboardings_owner", "customer_onboardings", ["owner_associate_id"])
    op.create_index("ix_customer_onboardings_mobile", "customer_onboardings", ["customer_mobile"])
    op.create_index("ix_customer_onboardings_consumer", "customer_onboardings", ["consumer_number_key"])
    op.create_index("ix_customer_onboardings_territory", "customer_onboardings",
                    ["state_id", "district_id", "block_id"])

    op.create_table(
        "onboarding_attribution_flags",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("onboarding_id", sa.Integer(), sa.ForeignKey("customer_onboardings.id"), nullable=False),
        sa.Column("existing_onboarding_id", sa.Integer(), sa.ForeignKey("customer_onboardings.id"), nullable=False),
        sa.Column("matched_on", sa.JSON(), nullable=False),
        sa.Column("existing_was_locked", sa.Boolean(), nullable=False),
        sa.Column("status", _enum("attributionflagstatus"), nullable=False),
        sa.Column("resolution", _enum("attributionresolution"), nullable=True),
        sa.Column("resolution_note", sa.Text(), nullable=True),
        sa.Column("resolved_by_employee_id", sa.String(), sa.ForeignKey("employees.id"), nullable=True),
        _ts("resolved_at"),
        _ts("created_at", nullable=False),
        sa.UniqueConstraint("onboarding_id", "existing_onboarding_id", name="uq_onboarding_attribution_pair"),
    )
    op.create_index("ix_onboarding_attribution_flags_status", "onboarding_attribution_flags", ["status"])


def downgrade() -> None:
    op.drop_index("ix_onboarding_attribution_flags_status", table_name="onboarding_attribution_flags")
    op.drop_table("onboarding_attribution_flags")
    for name in ("ix_customer_onboardings_territory", "ix_customer_onboardings_consumer",
                 "ix_customer_onboardings_mobile", "ix_customer_onboardings_owner"):
        op.drop_index(name, table_name="customer_onboardings")
    op.drop_table("customer_onboardings")
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for name in _ENUMS:
            op.execute(f"DROP TYPE IF EXISTS {name}")
    # The added leadsource / timelineeventtype values are left in place — PostgreSQL
    # cannot drop an enum value without rebuilding the type (as every earlier
    # enum-extending revision notes).
