"""Add agent_governance_overrides — a DB-backed kill-switch override per agent.

DISABLED_AGENTS (env var, app/config.py) has been the only way to switch an agent
off, which means a real incident needs a redeploy to act on. A row in this table
for an agent id takes precedence over the env var, so app/governance/policy.py can
be toggled at runtime through app/api/routes/agent_governance.py. No row for an
agent means "no override" — behaviour is unchanged, still driven by the env var.

Revision ID: 0014_agent_governance_overrides
Revises: 0013_employee_is_active
Create Date: 2026-09-03
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0014_agent_governance_overrides"
down_revision = "0013_employee_is_active"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_governance_overrides",
        sa.Column("agent_id", sa.String(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_by", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("agent_id"),
    )


def downgrade() -> None:
    op.drop_table("agent_governance_overrides")
