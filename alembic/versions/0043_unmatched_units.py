"""Unmatched installed units: a module/inverter may be registered before the catalogue
has its model — with the brand and model exactly as printed on the nameplate
(brand_as_printed, model_as_printed) instead of equipment_id. The check constraint
ck_installed_units_equipment_required becomes ck_installed_units_make_known.

Downgrade note: restoring the old constraint fails while any unmatched unit exists — by
design; nothing is deleted or guessed to make a downgrade succeed. Match the units first.

Revision ID: 0043_unmatched_units
Revises: 0042_site_location
Create Date: 2026-10-05
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0043_unmatched_units"
down_revision = "0042_site_location"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("installed_units") as batch:
        batch.add_column(sa.Column("brand_as_printed", sa.String(length=100), nullable=True))
        batch.add_column(sa.Column("model_as_printed", sa.String(length=100), nullable=True))
        batch.drop_constraint("ck_installed_units_equipment_required", type_="check")
        batch.create_check_constraint(
            "ck_installed_units_make_known",
            "category = 'other' OR equipment_id IS NOT NULL OR "
            "(brand_as_printed IS NOT NULL AND model_as_printed IS NOT NULL)")


def downgrade() -> None:
    with op.batch_alter_table("installed_units") as batch:
        batch.drop_constraint("ck_installed_units_make_known", type_="check")
        batch.create_check_constraint("ck_installed_units_equipment_required",
                                      "category = 'other' OR equipment_id IS NOT NULL")
        batch.drop_column("model_as_printed")
        batch.drop_column("brand_as_printed")
