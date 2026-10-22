"""BE-10: Associate referral links.

  * leads.referred_by_associate_id — the active Associate whose referral link (?ref=GNA-…)
    a public estimate request or call booking came through (nullable FK, indexed).
    Information only: attribution is unchanged (app/associates/referrals.py, REF-1).
  * leads.share_with_referrer — the customer agreed the referring Associate may see
    their mobile number. False for every existing lead.

Downgrade drops both columns (which leads were referred is lost — a development/staging
downgrade).

Revision ID: 0052_associate_referrals
Revises: 0051_nova_records
Create Date: 2026-10-22
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0052_associate_referrals"
down_revision = "0051_nova_records"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("leads") as batch:
        batch.add_column(sa.Column("referred_by_associate_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("share_with_referrer", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.create_foreign_key("fk_leads_referred_by_associate_id", "associates",
                                 ["referred_by_associate_id"], ["id"])
    op.create_index("ix_leads_referred_by_associate_id", "leads", ["referred_by_associate_id"])


def downgrade() -> None:
    op.drop_index("ix_leads_referred_by_associate_id", table_name="leads")
    with op.batch_alter_table("leads") as batch:
        batch.drop_constraint("fk_leads_referred_by_associate_id", type_="foreignkey")
        batch.drop_column("share_with_referrer")
        batch.drop_column("referred_by_associate_id")
