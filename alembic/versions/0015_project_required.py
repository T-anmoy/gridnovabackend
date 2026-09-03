"""Backfill a Project for every pre-existing Lead that has none, then make
Lead.project_id NOT NULL. From this point every Lead has a real Project — see
MemoryManager.create_lead(), which now creates one automatically whenever the
caller doesn't supply project_id.

Data migration first, constraint second: a Lead is never left pointing at
nothing, and no existing Lead row is touched beyond gaining a project_id.

Revision ID: 0015_project_required
Revises: 0014_agent_governance_overrides
Create Date: 2026-09-03
"""
from __future__ import annotations

from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

revision = "0015_project_required"
down_revision = "0014_agent_governance_overrides"
branch_labels = None
depends_on = None

# Minimal table definitions for the data migration — deliberately not importing
# app.memory.models, which will keep changing after this migration is history.
# Real Column objects (not the lighter sa.table/sa.column clause helpers) so the
# `id` primary key is declared — needed for inserted_primary_key to resolve on
# both SQLite (lastrowid) and Postgres (RETURNING).
_metadata = sa.MetaData()
_leads = sa.Table(
    "leads",
    _metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("customer_id", sa.Integer),
    sa.Column("project_id", sa.Integer),
)
_projects = sa.Table(
    "projects",
    _metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("customer_id", sa.Integer),
    sa.Column("created_at", sa.DateTime(timezone=True)),
    sa.Column("updated_at", sa.DateTime(timezone=True)),
)


def upgrade() -> None:
    bind = op.get_bind()
    now = datetime.now(timezone.utc)

    orphans = bind.execute(
        sa.select(_leads.c.id, _leads.c.customer_id).where(_leads.c.project_id.is_(None))
    ).all()

    for lead_id, customer_id in orphans:
        result = bind.execute(
            _projects.insert().values(customer_id=customer_id, created_at=now, updated_at=now)
        )
        project_id = result.inserted_primary_key[0]
        bind.execute(
            _leads.update().where(_leads.c.id == lead_id).values(project_id=project_id)
        )

    with op.batch_alter_table("leads") as batch:
        batch.alter_column("project_id", existing_type=sa.Integer(), nullable=False)


def downgrade() -> None:
    with op.batch_alter_table("leads") as batch:
        batch.alter_column("project_id", existing_type=sa.Integer(), nullable=True)
    # The backfilled Project rows are left in place — each is a real, valid row
    # linked to a real customer, and dropping them would strand nothing; only the
    # NOT NULL constraint this migration added is reversed.
