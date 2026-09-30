"""Installed-equipment serial register: installed_units (serial_normalized unique across
the whole system), and the DCR-mismatch alert/override columns on project_executions.

Revision ID: 0038_installed_units
Revises: 0037_execution_stages
Create Date: 2026-09-29
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0038_installed_units"
down_revision = "0037_execution_stages"
branch_labels = None
depends_on = None

_CATEGORIES = ("module", "inverter", "other")
_STATUSES = ("entered", "verified", "voided")


def _enum(values, name, pg):
    return sa.Enum(*values, name=name).with_variant(postgresql.ENUM(*values, name=name, create_type=False),
                                                     "postgresql") if pg else sa.Enum(*values, name=name)


def upgrade() -> None:
    bind = op.get_bind()
    pg = bind.dialect.name == "postgresql"
    if pg:
        postgresql.ENUM(*_CATEGORIES, name="installedunitcategory").create(bind, checkfirst=True)
        postgresql.ENUM(*_STATUSES, name="installedunitstatus").create(bind, checkfirst=True)
    op.create_table(
        "installed_units",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("onboarding_id", sa.Integer(), sa.ForeignKey("customer_onboardings.id"), nullable=False),
        sa.Column("category", _enum(_CATEGORIES, "installedunitcategory", pg), nullable=False),
        sa.Column("equipment_id", sa.Integer(), sa.ForeignKey("equipment.id"), nullable=True),
        sa.Column("serial_number", sa.String(length=80), nullable=False),
        sa.Column("serial_normalized", sa.String(length=80), nullable=True, unique=True),
        sa.Column("rating_wp", sa.Numeric(10, 2), nullable=True),
        sa.Column("rating_kw", sa.Numeric(10, 3), nullable=True),
        sa.Column("is_dcr", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("warranty_start", sa.Date(), nullable=True),
        sa.Column("warranty_end", sa.Date(), nullable=True),
        sa.Column("status", _enum(_STATUSES, "installedunitstatus", pg), nullable=False),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verified_by", sa.String(), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("void_reason", sa.Text(), nullable=True),
        sa.CheckConstraint("category = 'other' OR equipment_id IS NOT NULL",
                           name="ck_installed_units_equipment_required"),
        sa.CheckConstraint("status = 'voided' OR serial_normalized IS NOT NULL",
                           name="ck_installed_units_serial_held"),
        sa.CheckConstraint("warranty_start IS NULL OR warranty_end IS NULL OR warranty_end > warranty_start",
                           name="ck_installed_units_warranty_order"),
    )
    op.create_index("ix_installed_units_project_id", "installed_units", ["project_id"])
    op.create_index("ix_installed_units_onboarding_id", "installed_units", ["onboarding_id"])
    with op.batch_alter_table("project_executions") as batch:
        batch.add_column(sa.Column("dcr_alerted_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("dcr_override_reason", sa.Text(), nullable=True))
        batch.add_column(sa.Column("dcr_override_by", sa.String(), nullable=True))
        batch.add_column(sa.Column("dcr_override_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("project_executions") as batch:
        batch.drop_column("dcr_override_at")
        batch.drop_column("dcr_override_by")
        batch.drop_column("dcr_override_reason")
        batch.drop_column("dcr_alerted_at")
    op.drop_index("ix_installed_units_onboarding_id", table_name="installed_units")
    op.drop_index("ix_installed_units_project_id", table_name="installed_units")
    op.drop_table("installed_units")
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TYPE IF EXISTS installedunitstatus")
        op.execute("DROP TYPE IF EXISTS installedunitcategory")
