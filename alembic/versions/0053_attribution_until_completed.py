"""BE-11: the attribution lock lasts until the project is completed or closed.

Associate Terms A7 (approved by Grid Nova on 21 Oct 2026) removed the 90-day lock
period. The lock is now derived (app/onboarding/service.py attribution()): a verified
onboarding holds the customer until handover is complete, the lead is marked lost, or
an Admin closes the onboarding. Nothing stores an end date any more.

  * customer_onboardings.attribution_locked_until is dropped. Every verified onboarding
    stays locked after the upgrade (the lock follows from status = verified), including
    those whose 90 days had already run out.
  * ck_customer_onboardings_verified_after_call now requires verified_at (when the lock
    started) instead of attribution_locked_until. A verified row without verified_at
    (none is expected) is backfilled from updated_at / created_at first.

Downgrade restores the column as the old code wrote it: verified_at + 90 days for
verified onboardings, NULL otherwise, and the old constraint.

Revision ID: 0053_attribution_until_completed
Revises: 0052_associate_referrals
Create Date: 2026-10-23
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0053_attribution_until_completed"
down_revision = "0052_associate_referrals"
branch_labels = None
depends_on = None

_CK = "ck_customer_onboardings_verified_after_call"
_CALL = ("status != 'verified' OR (verification_call_completed = true AND verification_call_note IS NOT NULL "
         "AND verified_by_employee_id IS NOT NULL AND {lock} IS NOT NULL)")


def upgrade() -> None:
    op.execute("UPDATE customer_onboardings SET verified_at = COALESCE(updated_at, created_at, CURRENT_TIMESTAMP) "
               "WHERE status = 'verified' AND verified_at IS NULL")
    with op.batch_alter_table("customer_onboardings") as batch:
        batch.drop_constraint(_CK, type_="check")
        batch.drop_column("attribution_locked_until")
        batch.create_check_constraint(_CK, _CALL.format(lock="verified_at"))


def downgrade() -> None:
    with op.batch_alter_table("customer_onboardings") as batch:
        batch.drop_constraint(_CK, type_="check")
        batch.add_column(sa.Column("attribution_locked_until", sa.DateTime(timezone=True), nullable=True))
    plus_90 = ("verified_at + INTERVAL '90 days'" if op.get_bind().dialect.name == "postgresql"
               else "datetime(verified_at, '+90 days')")
    op.execute(f"UPDATE customer_onboardings SET attribution_locked_until = {plus_90} WHERE status = 'verified'")
    with op.batch_alter_table("customer_onboardings") as batch:
        batch.create_check_constraint(_CK, _CALL.format(lock="attribution_locked_until"))
