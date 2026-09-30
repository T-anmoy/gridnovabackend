"""Placeholder registry: manual resolutions of registry entries, and a flag for the
development-only dummy equipment catalogue.

Revision ID: 0036_placeholder_registry
Revises: 0035_formal_draft_failure
Create Date: 2026-09-29
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0036_placeholder_registry"
down_revision = "0035_formal_draft_failure"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "placeholder_resolutions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("entry_id", sa.String(length=40), nullable=False, unique=True),
        sa.Column("resolved_by", sa.String(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
    )
    with op.batch_alter_table("equipment") as batch:
        batch.add_column(sa.Column("is_placeholder", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    with op.batch_alter_table("equipment") as batch:
        batch.drop_column("is_placeholder")
    op.drop_table("placeholder_resolutions")
