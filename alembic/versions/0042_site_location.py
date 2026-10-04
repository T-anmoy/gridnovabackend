"""Site location: how an onboarding's site coordinates were obtained (device GPS, a camera
photo's fix, a Google Maps link, pasted coordinates, staff, or a legacy raw pair), how
precise they are, and whether the associate confirmed them as the customer's house —
which submitting now requires. Adds TimelineEventType.site_location_updated.

Backfill: rows that already carry coordinates become `unknown_legacy`, unverified and
unconfirmed (so a draft among them must be confirmed before its next submit; submitted,
verified and closed customers are grandfathered by the submit gate, not by data).

Downgrade drops the new columns (the provenance they hold is lost; the coordinates
themselves stay in site_latitude/site_longitude). The added timelineeventtype value
stays — PostgreSQL cannot drop an enum value.

Revision ID: 0042_site_location
Revises: 0041_phase4a_followups
Create Date: 2026-10-04
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0042_site_location"
down_revision = "0041_phase4a_followups"
branch_labels = None
depends_on = None

_SOURCES = ("device_gps", "photo_gps", "google_maps_link", "pasted_coordinates", "staff_entered", "unknown_legacy")
_PRECISIONS = ("exact", "viewport_center")


def _enum(values, name, pg):
    return sa.Enum(*values, name=name).with_variant(postgresql.ENUM(*values, name=name, create_type=False),
                                                     "postgresql") if pg else sa.Enum(*values, name=name)


def upgrade() -> None:
    bind = op.get_bind()
    pg = bind.dialect.name == "postgresql"
    if pg:
        with op.get_context().autocommit_block():
            op.execute("ALTER TYPE timelineeventtype ADD VALUE IF NOT EXISTS 'site_location_updated'")
        postgresql.ENUM(*_SOURCES, name="sitelocationsource").create(bind, checkfirst=True)
        postgresql.ENUM(*_PRECISIONS, name="sitelocationprecision").create(bind, checkfirst=True)

    with op.batch_alter_table("customer_onboardings") as batch:
        batch.add_column(sa.Column("site_location_source", _enum(_SOURCES, "sitelocationsource", pg), nullable=True))
        batch.add_column(sa.Column("site_location_accuracy_m", sa.Float(), nullable=True))
        batch.add_column(sa.Column("site_location_captured_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("site_location_verified", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.add_column(sa.Column("site_location_confirmed_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("site_location_confirmed_by", sa.String(), nullable=True))
        batch.add_column(sa.Column("site_location_raw_input", sa.String(length=500), nullable=True))
        batch.add_column(sa.Column("site_location_precision", _enum(_PRECISIONS, "sitelocationprecision", pg),
                                   nullable=True))

    op.execute("UPDATE customer_onboardings SET site_location_source = 'unknown_legacy' "
               "WHERE site_latitude IS NOT NULL")

    with op.batch_alter_table("customer_onboardings") as batch:
        batch.create_check_constraint("ck_customer_onboardings_location_source",
                                      "site_latitude IS NULL OR site_location_source IS NOT NULL")


def downgrade() -> None:
    with op.batch_alter_table("customer_onboardings") as batch:
        batch.drop_constraint("ck_customer_onboardings_location_source", type_="check")
        for column in ("site_location_precision", "site_location_raw_input", "site_location_confirmed_by",
                       "site_location_confirmed_at", "site_location_verified", "site_location_captured_at",
                       "site_location_accuracy_m", "site_location_source"):
            batch.drop_column(column)
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TYPE IF EXISTS sitelocationprecision")
        op.execute("DROP TYPE IF EXISTS sitelocationsource")
