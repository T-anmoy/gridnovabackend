"""BE-8: the Project Archive.

  * archive_import_batches — one row per committed CSV import (file hash, counts, the
    validation report; voiding is a status, never a delete).
  * archived_projects — Grid Nova's completed projects that never went through this
    system. Voiding a batch stamps voided_at on its rows; the unique indexes on
    grid_nova_ref and project_id apply to live (non-voided) rows only.

Downgrade drops both tables and the three enum types (imported archive data is lost —
a development/staging downgrade).

Revision ID: 0050_project_archive
Revises: 0049_handover_warranty
Create Date: 2026-10-16
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0050_project_archive"
down_revision = "0049_handover_warranty"
branch_labels = None
depends_on = None

_ENUMS = {
    "archivescheme": ("pm_surya_ghar_residential", "housing_society", "commercial", "industrial",
                      "institutional_govt", "wbreda_school", "pm_kusum_pump", "street_lighting",
                      "ground_mounted", "other"),
    "archivesystemtype": ("on_grid", "off_grid", "hybrid", "pump", "street_light", "other"),
    "archivebatchstatus": ("validated", "imported", "voided"),
}


def _enum(name: str, pg: bool):
    values = _ENUMS[name]
    if pg:
        return sa.Enum(*values, name=name).with_variant(postgresql.ENUM(*values, name=name, create_type=False),
                                                         "postgresql")
    return sa.Enum(*values, name=name)


def _dt():
    return sa.DateTime(timezone=True)


def upgrade() -> None:
    bind = op.get_bind()
    pg = bind.dialect.name == "postgresql"
    if pg:
        for name, values in _ENUMS.items():
            postgresql.ENUM(*values, name=name).create(bind, checkfirst=True)

    op.create_table(
        "archive_import_batches",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("uploaded_by", sa.String(), nullable=False),
        sa.Column("uploaded_at", _dt(), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("accepted_count", sa.Integer(), nullable=False),
        sa.Column("rejected_count", sa.Integer(), nullable=False),
        sa.Column("status", _enum("archivebatchstatus", pg), nullable=False),
        sa.Column("report", sa.JSON(), nullable=False),
        sa.Column("voided_by", sa.String(), nullable=True),
        sa.Column("voided_at", _dt(), nullable=True),
        sa.Column("void_reason", sa.Text(), nullable=True),
    )
    op.create_index("ix_archive_import_batches_sha256", "archive_import_batches", ["sha256"])

    op.create_table(
        "archived_projects",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("grid_nova_ref", sa.String(length=100), nullable=True),
        sa.Column("import_batch_id", sa.Integer(), sa.ForeignKey("archive_import_batches.id"), nullable=False),
        sa.Column("source_row", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id"), nullable=True),
        sa.Column("voided_at", _dt(), nullable=True),
        sa.Column("created_at", _dt(), nullable=False),
        sa.Column("scheme_or_segment", _enum("archivescheme", pg), nullable=False),
        sa.Column("system_type", _enum("archivesystemtype", pg), nullable=True),
        sa.Column("capacity_kw", sa.Numeric(12, 3), nullable=True),
        sa.Column("pump_hp", sa.Numeric(8, 2), nullable=True),
        sa.Column("street_lamp_count", sa.Integer(), nullable=True),
        sa.Column("state", sa.String(length=120), nullable=False),
        sa.Column("district", sa.String(length=120), nullable=False),
        sa.Column("block", sa.String(length=120), nullable=True),
        sa.Column("state_id", sa.Integer(), sa.ForeignKey("territory_states.id"), nullable=True),
        sa.Column("district_id", sa.Integer(), sa.ForeignKey("territory_districts.id"), nullable=True),
        sa.Column("block_id", sa.Integer(), sa.ForeignKey("territory_blocks.id"), nullable=True),
        sa.Column("address_text", sa.Text(), nullable=True),
        sa.Column("pincode", sa.String(length=6), nullable=True),
        sa.Column("latitude", sa.Float(), nullable=True),
        sa.Column("longitude", sa.Float(), nullable=True),
        sa.Column("customer_name", sa.String(length=200), nullable=True),
        sa.Column("customer_phone", sa.String(length=32), nullable=True),
        sa.Column("customer_email", sa.String(length=200), nullable=True),
        sa.Column("discom", sa.String(length=120), nullable=True),
        sa.Column("consumer_number", sa.String(length=60), nullable=True),
        sa.Column("contract_value_inr", sa.Numeric(14, 2), nullable=True),
        sa.Column("subsidy_amount_inr", sa.Numeric(14, 2), nullable=True),
        sa.Column("module_make", sa.String(length=120), nullable=True),
        sa.Column("module_model", sa.String(length=120), nullable=True),
        sa.Column("module_wattage_wp", sa.Numeric(8, 2), nullable=True),
        sa.Column("module_count", sa.Integer(), nullable=True),
        sa.Column("module_dcr", sa.String(length=8), nullable=True),
        sa.Column("inverter_make", sa.String(length=120), nullable=True),
        sa.Column("inverter_model", sa.String(length=120), nullable=True),
        sa.Column("inverter_capacity_kw", sa.Numeric(10, 3), nullable=True),
        sa.Column("inverter_count", sa.Integer(), nullable=True),
        sa.Column("structure_type", sa.String(length=120), nullable=True),
        sa.Column("battery_details", sa.Text(), nullable=True),
        sa.Column("other_equipment", sa.Text(), nullable=True),
        sa.Column("order_date", sa.Date(), nullable=True),
        sa.Column("installation_date", sa.Date(), nullable=True),
        sa.Column("commissioning_date", sa.Date(), nullable=False),
        sa.Column("net_meter_date", sa.Date(), nullable=True),
        sa.Column("warranty_end_modules", sa.Date(), nullable=True),
        sa.Column("warranty_end_inverter", sa.Date(), nullable=True),
        sa.Column("om_status", sa.String(length=200), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("documents", sa.JSON(), nullable=False),
        sa.Column("data_quality_flags", sa.JSON(), nullable=False),
    )
    op.create_index("uq_archived_projects_ref", "archived_projects", ["grid_nova_ref"], unique=True,
                    sqlite_where=sa.text("grid_nova_ref IS NOT NULL AND voided_at IS NULL"),
                    postgresql_where=sa.text("grid_nova_ref IS NOT NULL AND voided_at IS NULL"))
    op.create_index("uq_archived_projects_project", "archived_projects", ["project_id"], unique=True,
                    sqlite_where=sa.text("project_id IS NOT NULL AND voided_at IS NULL"),
                    postgresql_where=sa.text("project_id IS NOT NULL AND voided_at IS NULL"))
    op.create_index("ix_archived_projects_state", "archived_projects", ["state"])
    op.create_index("ix_archived_projects_district", "archived_projects", ["district"])
    op.create_index("ix_archived_projects_scheme", "archived_projects", ["scheme_or_segment"])
    op.create_index("ix_archived_projects_commissioning", "archived_projects", ["commissioning_date"])
    op.create_index("ix_archived_projects_capacity", "archived_projects", ["capacity_kw"])
    op.create_index("ix_archived_projects_batch", "archived_projects", ["import_batch_id"])


def downgrade() -> None:
    bind = op.get_bind()
    pg = bind.dialect.name == "postgresql"
    for name in ("ix_archived_projects_batch", "ix_archived_projects_capacity", "ix_archived_projects_commissioning",
                 "ix_archived_projects_scheme", "ix_archived_projects_district", "ix_archived_projects_state",
                 "uq_archived_projects_project", "uq_archived_projects_ref"):
        op.drop_index(name, table_name="archived_projects")
    op.drop_table("archived_projects")
    op.drop_index("ix_archive_import_batches_sha256", table_name="archive_import_batches")
    op.drop_table("archive_import_batches")
    if pg:
        for name in _ENUMS:
            op.execute(f"DROP TYPE IF EXISTS {name}")
