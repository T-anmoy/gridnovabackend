"""Territory master: territory_states → territory_districts → territory_blocks,
keyed by Local Government Directory (LGD) codes.

Schema only. The rows are loaded by scripts/seed_territories.py from
data/territories/*.csv (idempotent; run it after every deploy).

Revision ID: 0027_territories
Revises: 0026_operations_accounts_roles
Create Date: 2026-09-27
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0027_territories"
down_revision = "0026_operations_accounts_roles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "territory_states",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("lgd_code", sa.Integer(), nullable=False, unique=True),
        sa.Column("iso_code", sa.String(length=2), nullable=False, unique=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "territory_districts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("state_id", sa.Integer(), sa.ForeignKey("territory_states.id"), nullable=False),
        sa.Column("lgd_code", sa.Integer(), nullable=False, unique=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_territory_districts_state_id", "territory_districts", ["state_id"])
    op.create_table(
        "territory_blocks",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("district_id", sa.Integer(), sa.ForeignKey("territory_districts.id"), nullable=False),
        sa.Column("lgd_code", sa.Integer(), nullable=False, unique=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_territory_blocks_district_id", "territory_blocks", ["district_id"])


def downgrade() -> None:
    op.drop_index("ix_territory_blocks_district_id", table_name="territory_blocks")
    op.drop_table("territory_blocks")
    op.drop_index("ix_territory_districts_state_id", table_name="territory_districts")
    op.drop_table("territory_districts")
    op.drop_table("territory_states")
