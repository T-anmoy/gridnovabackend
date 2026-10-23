"""BE-13: the proposal document's unique reference.

  * proposals.document_reference — printed on the customer PDF, allocated when a version
    is approved: GN/DD/MM/YYYY/NN (per-day sequence), a revision of an issued proposal
    adds -R1, -R2. Unique (uq_proposals_document_reference).
  * proposals.document_date — the date printed with it (IST day of approval).
  * proposal_reference_sequences — the per-day counter, advanced atomically
    (INSERT … ON CONFLICT … RETURNING, like receipt_sequences).

The structured document itself lives in proposals.content (JSON) and needs no schema.

Revision ID: 0055_proposal_document_reference
Revises: 0054_individual_commission_rates
Create Date: 2026-10-24
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0055_proposal_document_reference"
down_revision = "0054_individual_commission_rates"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "proposal_reference_sequences",
        sa.Column("day", sa.Date(), primary_key=True),
        sa.Column("value", sa.Integer(), nullable=False),
    )
    op.add_column("proposals", sa.Column("document_reference", sa.String(length=40), nullable=True))
    op.add_column("proposals", sa.Column("document_date", sa.Date(), nullable=True))
    op.create_index("uq_proposals_document_reference", "proposals", ["document_reference"], unique=True)


def downgrade() -> None:
    op.drop_index("uq_proposals_document_reference", table_name="proposals")
    with op.batch_alter_table("proposals") as batch:
        batch.drop_column("document_date")
        batch.drop_column("document_reference")
    op.drop_table("proposal_reference_sequences")
