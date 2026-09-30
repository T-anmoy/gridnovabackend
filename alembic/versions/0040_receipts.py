"""Payment receipts: a per-year atomic counter and one receipt per verified payment.

Revision ID: 0040_receipts
Revises: 0039_payments_ledger
Create Date: 2026-09-29
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0040_receipts"
down_revision = "0039_payments_ledger"
branch_labels = None
depends_on = None

_STATUSES = ("pending", "sent", "skipped", "held", "failed")


def upgrade() -> None:
    bind = op.get_bind()
    pg = bind.dialect.name == "postgresql"
    if pg:
        postgresql.ENUM(*_STATUSES, name="receiptemailstatus").create(bind, checkfirst=True)
        status = postgresql.ENUM(*_STATUSES, name="receiptemailstatus", create_type=False)
    else:
        status = sa.Enum(*_STATUSES, name="receiptemailstatus")
    op.create_table(
        "receipt_sequences",
        sa.Column("year", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("value", sa.Integer(), nullable=False),
    )
    op.create_table(
        "payment_receipts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("payment_id", sa.Integer(), sa.ForeignKey("payments.id"), nullable=False, unique=True),
        sa.Column("receipt_number", sa.String(length=40), nullable=False, unique=True),
        sa.Column("public_token", sa.String(length=64), nullable=False, unique=True),
        sa.Column("pdf_key", sa.String(), nullable=True),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("render_error", sa.Text(), nullable=True),
        sa.Column("email_status", status, nullable=False),
        sa.Column("email_skipped_reason", sa.String(), nullable=True),
        sa.Column("held_reason", sa.String(), nullable=True),
        sa.Column("emailed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("payment_receipts")
    op.drop_table("receipt_sequences")
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TYPE IF EXISTS receiptemailstatus")
