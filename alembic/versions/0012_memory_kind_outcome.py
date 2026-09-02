"""Add the 'outcome' memory kind — what humans did with a proposal the agent drafted
(approved, sent back for revision, repriced, sent, accepted). That is the Proposal
Agent's feedback signal, and it is a different question from 'what did the customer
say', so it gets its own kind rather than being folded into 'episode'.

Postgres needs the enum type extended. SQLite renders the same Enum as a plain
VARCHAR with no CHECK constraint (confirmed against the generated DDL), and the new
value is shorter than the longest existing one, so there is nothing to alter there.

Revision ID: 0012_memory_kind_outcome
Revises: 0011_customer_memory
Create Date: 2026-08-30
"""
from __future__ import annotations

from alembic import op

revision = "0012_memory_kind_outcome"
down_revision = "0011_customer_memory"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # IF NOT EXISTS keeps this safe to re-run against a partially migrated
        # database. It cannot run inside a transaction on older PostgreSQL, so it
        # is issued on its own connection-level statement.
        op.execute("ALTER TYPE memorykind ADD VALUE IF NOT EXISTS 'outcome'")


def downgrade() -> None:
    # PostgreSQL cannot drop a value from an enum type, and rebuilding the type
    # would mean rewriting every customer_memories row that uses it. Rows written
    # as 'outcome' are left in place; the application simply stops producing new
    # ones once this revision is rolled back.
    pass
