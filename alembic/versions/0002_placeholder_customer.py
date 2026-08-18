"""Allow customers.name to be NULL so a chat can start with no identity (a
placeholder customer that gets filled in once name/email are extracted).

Revision ID: 0002_placeholder_customer
Revises: 0001_scheduling_and_fallback
Create Date: 2026-08-11
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_placeholder_customer"
down_revision = "0001_scheduling_and_fallback"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("customers") as batch:
        batch.alter_column("name", existing_type=sa.String(), nullable=True)


def downgrade() -> None:
    # Backfill any NULL names before restoring NOT NULL.
    op.execute("UPDATE customers SET name = '' WHERE name IS NULL")
    with op.batch_alter_table("customers") as batch:
        batch.alter_column("name", existing_type=sa.String(), nullable=False)
