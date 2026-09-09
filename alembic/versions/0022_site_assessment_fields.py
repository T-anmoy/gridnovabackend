"""Expand ProposalInputs with fields identified in a gap analysis against a
real professional rooftop-solar site-survey checklist: 6-month consumption
history, DISCOM consumer classification (domestic/industrial,
normal/VIP), DISCOM-sanctioned load, roof detail (ownership, count,
future expansion, age), structured installation-photo slots, and
staff/customer signature capture.

Consumer Account Number was already covered by the pre-existing
consumer_number column (see 0010_proposal_inputs) — not duplicated here.

Additive only: no existing column is touched, dropped, or renamed.

Revision ID: 0022_site_assessment_fields
Revises: 0021_lead_source_booking
Create Date: 2026-09-07
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0022_site_assessment_fields"
down_revision = "0021_lead_source_booking"
branch_labels = None
depends_on = None

_DISCOM_CONSUMER_CATEGORY = sa.Enum("domestic", "industrial", name="discomconsumercategory")
_CONSUMER_PRIORITY_CLASS = sa.Enum("normal", "vip", name="consumerpriorityclass")


def upgrade() -> None:
    bind = op.get_bind()
    _DISCOM_CONSUMER_CATEGORY.create(bind, checkfirst=True)
    _CONSUMER_PRIORITY_CLASS.create(bind, checkfirst=True)

    with op.batch_alter_table("proposal_inputs") as batch:
        # Roof detail
        batch.add_column(sa.Column("roof_ownership_confirmed", sa.Boolean(), nullable=True))
        batch.add_column(sa.Column("roofs_available_count", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("roofs_considered_count", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("future_roof_expansion_planned", sa.Boolean(), nullable=True))
        batch.add_column(sa.Column("roof_age_years", sa.Float(), nullable=True))

        # Load & output
        batch.add_column(sa.Column("consumption_history", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("sanctioned_load_kw", sa.Float(), nullable=True))

        # DISCOM consumer classification
        batch.add_column(sa.Column("discom_consumer_category", _DISCOM_CONSUMER_CATEGORY, nullable=True))
        batch.add_column(
            sa.Column(
                "consumer_priority_class", _CONSUMER_PRIORITY_CLASS,
                nullable=True, server_default="normal",
            )
        )

        # Structured installation photos + signatures
        batch.add_column(sa.Column("installation_photo_refs", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("staff_signature_ref", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("customer_signature_ref", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("proposal_inputs") as batch:
        batch.drop_column("customer_signature_ref")
        batch.drop_column("staff_signature_ref")
        batch.drop_column("installation_photo_refs")
        batch.drop_column("consumer_priority_class")
        batch.drop_column("discom_consumer_category")
        batch.drop_column("sanctioned_load_kw")
        batch.drop_column("consumption_history")
        batch.drop_column("roof_age_years")
        batch.drop_column("future_roof_expansion_planned")
        batch.drop_column("roofs_considered_count")
        batch.drop_column("roofs_available_count")
        batch.drop_column("roof_ownership_confirmed")

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        _CONSUMER_PRIORITY_CLASS.drop(bind, checkfirst=True)
        _DISCOM_CONSUMER_CATEGORY.drop(bind, checkfirst=True)
