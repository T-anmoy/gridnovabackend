"""Add customer_memories and customer_profiles — the Sales Agent's episodic and
self-reflection memory.

customer_memories is append-only history (what happened, what the customer wants,
what they objected to, what the agent concluded). customer_profiles is the single
rewritten-in-place view per customer, including the agent's notes on its own
handling. Additive only: no existing table is touched.

Revision ID: 0011_customer_memory
Revises: 0010_proposal_inputs
Create Date: 2026-08-30
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011_customer_memory"
down_revision = "0010_proposal_inputs"
branch_labels = None
depends_on = None

_MEMORY_KIND = sa.Enum(
    "episode", "preference", "objection", "commitment", "insight", "self_critique",
    name="memorykind",
)
_MEMORY_SOURCE = sa.Enum("reflection", "system", "employee", name="memorysource")


def upgrade() -> None:
    bind = op.get_bind()
    _MEMORY_KIND.create(bind, checkfirst=True)
    _MEMORY_SOURCE.create(bind, checkfirst=True)

    op.create_table(
        "customer_memories",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("lead_id", sa.Integer(), nullable=True),
        sa.Column("kind", _MEMORY_KIND, nullable=False),
        sa.Column("source", _MEMORY_SOURCE, nullable=False, server_default="reflection"),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("tags", sa.JSON(), nullable=True),
        sa.Column("salience", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(), nullable=False, server_default="sales_agent"),
        sa.Column("embedding", sa.JSON(), nullable=True),
        sa.Column("embedding_model", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"]),
        sa.ForeignKeyConstraint(["lead_id"], ["leads.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_customer_memories_customer", "customer_memories", ["customer_id", "created_at"]
    )
    op.create_index("ix_customer_memories_lead", "customer_memories", ["lead_id"])

    op.create_table(
        "customer_profiles",
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("traits", sa.JSON(), nullable=True),
        sa.Column("open_questions", sa.JSON(), nullable=True),
        sa.Column("agent_notes", sa.Text(), nullable=True),
        sa.Column("next_best_action", sa.Text(), nullable=True),
        sa.Column("last_reflected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reflection_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_reflected_message_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"]),
        sa.PrimaryKeyConstraint("customer_id"),
    )


def downgrade() -> None:
    op.drop_table("customer_profiles")
    op.drop_index("ix_customer_memories_lead", table_name="customer_memories")
    op.drop_index("ix_customer_memories_customer", table_name="customer_memories")
    op.drop_table("customer_memories")

    bind = op.get_bind()
    _MEMORY_SOURCE.drop(bind, checkfirst=True)
    _MEMORY_KIND.drop(bind, checkfirst=True)
