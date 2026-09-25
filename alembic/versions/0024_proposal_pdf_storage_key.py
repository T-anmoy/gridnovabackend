"""Add proposals.pdf_storage_key — the PDF's object key in the private R2 bucket, from
which short-lived signed download links are made. pdf_url stays for compatibility.

No data backfill here: the key of a pre-existing row is derived at read time from
its pdf_url by stripping the deployment's {endpoint}/{bucket}/ prefix, which depends
on runtime STORAGE_* settings a migration should not read (app/storage/r2.py
key_from_url). Additive only.

Revision ID: 0024_proposal_pdf_storage_key
Revises: 0023_lead_workflow_events
Create Date: 2026-09-25
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0024_proposal_pdf_storage_key"
down_revision = "0023_lead_workflow_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("proposals") as batch:
        batch.add_column(sa.Column("pdf_storage_key", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("proposals") as batch:
        batch.drop_column("pdf_storage_key")
