"""BE-1 security and money-path fixes.

  * chat_sessions — a website chat conversation bound to a secret (only its SHA-256 is
    stored); the chat no longer trusts lead/customer ids from the browser (S2).
  * oauth_state_nonces — single-use nonces for the Google Calendar OAuth state (S7).
  * payment_receipts.voided_at / void_reason — a receipt is void once its payment is
    dishonoured after verification (C3).
  * timelineeventtype gains 'possible_returning_customer' — a chat visitor gave contact
    details that belong to another customer; noted for staff, never merged (S2).

Downgrade drops the two tables and the two columns (chat sessions and OAuth nonces are
short-lived working state; void marks are lost, and the audit log keeps the "voided"
entries). The enum value is left in place, as every earlier enum-extending revision in
this chain does: PostgreSQL cannot drop it without rebuilding the type.

Revision ID: 0045_be1_security
Revises: 0044_site_survey
Create Date: 2026-10-10
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0045_be1_security"
down_revision = "0044_site_survey"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute("ALTER TYPE timelineeventtype ADD VALUE IF NOT EXISTS 'possible_returning_customer'")

    op.create_table(
        "chat_sessions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("lead_id", sa.Integer(), sa.ForeignKey("leads.id"), nullable=False),
        sa.Column("customer_id", sa.Integer(), sa.ForeignKey("customers.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_chat_sessions_token_hash", "chat_sessions", ["token_hash"], unique=True)

    op.create_table(
        "oauth_state_nonces",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("nonce_hash", sa.String(length=64), nullable=False, unique=True),
        sa.Column("employee_id", sa.String(), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
    )

    with op.batch_alter_table("payment_receipts") as batch:
        batch.add_column(sa.Column("voided_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("void_reason", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("payment_receipts") as batch:
        batch.drop_column("void_reason")
        batch.drop_column("voided_at")
    op.drop_table("oauth_state_nonces")
    op.drop_index("ix_chat_sessions_token_hash", table_name="chat_sessions")
    op.drop_table("chat_sessions")
