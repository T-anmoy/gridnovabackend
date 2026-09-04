"""Add the equipment table (Equipment/SKU Master) and equipmentcategory enum.
Empty, additive-only — no existing table is touched.

Revision ID: 0020_equipment_master
Revises: 0019_subcontractor_role
Create Date: 2026-09-04
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0020_equipment_master"
down_revision = "0019_subcontractor_role"
branch_labels = None
depends_on = None

_EQUIPMENT_CATEGORY = sa.Enum(
    "module", "inverter", "structure", "bos_component",
    name="equipmentcategory",
)


def upgrade() -> None:
    bind = op.get_bind()
    _EQUIPMENT_CATEGORY.create(bind, checkfirst=True)

    op.create_table(
        "equipment",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("category", _EQUIPMENT_CATEGORY, nullable=False),
        sa.Column("brand", sa.String(), nullable=False),
        sa.Column("model_name", sa.String(), nullable=False),
        sa.Column("specification", sa.Text(), nullable=True),
        sa.Column("is_dcr_compliant", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("procurement_price_inr", sa.Float(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_equipment_category", "equipment", ["category"])
    op.create_index("ix_equipment_is_active", "equipment", ["is_active"])


def downgrade() -> None:
    op.drop_index("ix_equipment_is_active", table_name="equipment")
    op.drop_index("ix_equipment_category", table_name="equipment")
    op.drop_table("equipment")

    bind = op.get_bind()
    _EQUIPMENT_CATEGORY.drop(bind, checkfirst=True)
