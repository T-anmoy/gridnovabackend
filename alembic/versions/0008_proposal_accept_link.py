"""Add proposal accept-link fields (accept_token, accepted_at) and the new
'accepted' proposalstatus + 'customer_accepted' timelineeventtype enum values.

Revision ID: 0008_proposal_accept_link
Revises: 0007_proposal_approved_at
Create Date: 2026-08-11
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008_proposal_accept_link"
down_revision = "0007_proposal_approved_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("proposals") as batch:
        batch.add_column(sa.Column("accept_token", sa.String(), nullable=True))
        batch.add_column(sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True))
        batch.create_unique_constraint("uq_proposals_accept_token", ["accept_token"])

    # New enum values (Postgres native enums must be extended explicitly; no-op on SQLite).
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("COMMIT")
        op.execute("ALTER TYPE proposalstatus ADD VALUE IF NOT EXISTS 'accepted'")
        op.execute("ALTER TYPE timelineeventtype ADD VALUE IF NOT EXISTS 'customer_accepted'")


def downgrade() -> None:
    with op.batch_alter_table("proposals") as batch:
        batch.drop_constraint("uq_proposals_accept_token", type_="unique")
        batch.drop_column("accepted_at")
        batch.drop_column("accept_token")
    # Enum values are left in place (Postgres cannot easily drop a single enum value).
