"""proposals.drafted_by (backfilled from the audit log) and customer_agreements.

drafted_by is backfilled for existing proposals from their own 'draft_created' audit
entry — the record of who actually triggered each draft — so the new approval rule
has real data for proposals drafted before this migration too. Proposals with no such
audit entry stay NULL.

Revision ID: 0033_drafter_and_agreements
Revises: 0032_project_stages
Create Date: 2026-09-27
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0033_drafter_and_agreements"
down_revision = "0032_project_stages"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("proposals") as batch:
        batch.add_column(sa.Column("drafted_by", sa.String(), nullable=True))
    op.execute(
        "UPDATE proposals SET drafted_by = ("
        "  SELECT a.performed_by FROM audit_logs a"
        "  WHERE a.entity_type = 'proposal' AND a.action = 'draft_created'"
        "    AND a.entity_id = CAST(proposals.id AS VARCHAR)"
        "  ORDER BY a.id LIMIT 1)"
        " WHERE drafted_by IS NULL"
    )
    op.create_table(
        "customer_agreements",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("onboarding_id", sa.Integer(), sa.ForeignKey("customer_onboardings.id"), nullable=False,
                  unique=True),
        sa.Column("proposal_id", sa.Integer(), sa.ForeignKey("proposals.id"), nullable=False),
        sa.Column("agreement_storage_key", sa.String(), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("generated_by", sa.String(), nullable=False),
        sa.Column("stamp_paper_number", sa.String(), nullable=True),
        sa.Column("stamp_paper_date", sa.Date(), nullable=True),
        sa.Column("signed_scan_key", sa.String(), nullable=True),
        sa.Column("signed_scan_filename", sa.String(), nullable=True),
        sa.Column("signed_scan_content_type", sa.String(), nullable=True),
        sa.Column("signed_uploaded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("signed_uploaded_by", sa.String(), nullable=True),
        sa.CheckConstraint(
            "(signed_scan_key IS NULL AND stamp_paper_number IS NULL AND stamp_paper_date IS NULL "
            "AND signed_uploaded_at IS NULL) OR (signed_scan_key IS NOT NULL AND stamp_paper_number IS NOT NULL "
            "AND stamp_paper_date IS NOT NULL AND signed_uploaded_at IS NOT NULL)",
            name="ck_customer_agreements_signed_together",
        ),
    )


def downgrade() -> None:
    op.drop_table("customer_agreements")
    with op.batch_alter_table("proposals") as batch:
        batch.drop_column("drafted_by")
