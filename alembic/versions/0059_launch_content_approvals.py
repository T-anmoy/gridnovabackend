"""BE-22: approved launch wording — approvals and revocations on placeholder_resolutions,
and the agreement wording a PDF was generated from.

  * placeholder_resolutions loses its one-row-per-entry rule (entry_id is indexed, not
    unique): an approved_text entry gathers approve → revoke → approve rows, the latest
    deciding. New columns: action (resolved | approved | revoked; existing rows are
    `resolved`), content_sha256 (the approved text's SHA-256) and legal_review_confirmed.
    The table is rebuilt (create, copy, drop, rename) because the unique constraint has
    no name to drop on SQLite, and a different generated name on PostgreSQL.
  * customer_agreements.wording_sha256 (nullable): the ONB-5 wording printed on the PDF.

Downgrade restores the old table; it refuses while any approval or revocation row
exists (they would no longer fit one row per entry).

Revision ID: 0059_launch_content_approvals
Revises: 0058_direct_customer_payments
Create Date: 2026-10-31
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0059_launch_content_approvals"
down_revision = "0058_direct_customer_payments"
branch_labels = None
depends_on = None


def _rebuild(columns: list, copy: str, index: bool) -> None:
    op.create_table("placeholder_resolutions_new", *columns)
    op.execute(copy)
    op.drop_table("placeholder_resolutions")
    op.rename_table("placeholder_resolutions_new", "placeholder_resolutions")
    if index:
        op.create_index("ix_placeholder_resolutions_entry_id", "placeholder_resolutions", ["entry_id"])


def upgrade() -> None:
    _rebuild([
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("entry_id", sa.String(length=40), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False, server_default="resolved"),
        sa.Column("resolved_by", sa.String(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=True),
        sa.Column("legal_review_confirmed", sa.Boolean(), nullable=False, server_default=sa.false()),
    ], "INSERT INTO placeholder_resolutions_new (id, entry_id, action, resolved_by, resolved_at, note) "
       "SELECT id, entry_id, 'resolved', resolved_by, resolved_at, note FROM placeholder_resolutions", index=True)
    if op.get_bind().dialect.name == "postgresql":
        op.execute("SELECT setval(pg_get_serial_sequence('placeholder_resolutions', 'id'), "
                   "COALESCE((SELECT MAX(id) FROM placeholder_resolutions), 0) + 1, false)")
    with op.batch_alter_table("customer_agreements") as batch:
        batch.add_column(sa.Column("wording_sha256", sa.String(length=64), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    decisions = bind.execute(sa.text(
        "SELECT COUNT(*) FROM placeholder_resolutions WHERE action <> 'resolved'")).scalar()
    if decisions:
        raise RuntimeError(f"{decisions} wording approval/revocation row(s) exist; the old table cannot hold them.")
    with op.batch_alter_table("customer_agreements") as batch:
        batch.drop_column("wording_sha256")
    op.drop_index("ix_placeholder_resolutions_entry_id", table_name="placeholder_resolutions")
    _rebuild([
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("entry_id", sa.String(length=40), nullable=False, unique=True),
        sa.Column("resolved_by", sa.String(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
    ], "INSERT INTO placeholder_resolutions_new (id, entry_id, resolved_by, resolved_at, note) "
       "SELECT id, entry_id, resolved_by, resolved_at, note FROM placeholder_resolutions", index=False)
    if bind.dialect.name == "postgresql":
        op.execute("SELECT setval(pg_get_serial_sequence('placeholder_resolutions', 'id'), "
                   "COALESCE((SELECT MAX(id) FROM placeholder_resolutions), 0) + 1, false)")
