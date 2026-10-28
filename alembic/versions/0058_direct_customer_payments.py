"""BE-20: payments for direct customers (no associate).

  * payment_milestones.lead_id and payments.lead_id — the lead of a direct customer whose
    accepted proposal the schedule and the payment belong to (nullable FK, indexed). Every
    existing row is an associate customer's and keeps lead_id NULL; project_id stays the key
    of the ledger, receipts and the over-payment lock.
  * ck_payment_milestones_one_owner / ck_payments_one_owner — onboarding_id or lead_id,
    never both.
  * timelineeventtype gains payment_recorded, payment_verified, payment_dishonoured
    (PostgreSQL: ADD VALUE; SQLite stores them as text).

Downgrade drops the checks, indexes and columns. It refuses while any direct-customer
payment or milestone exists (they would lose the lead they belong to). The enum values are
left in place, as every earlier enum-extending revision does.

Revision ID: 0058_direct_customer_payments
Revises: 0057_proposal_studio
Create Date: 2026-10-28
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0058_direct_customer_payments"
down_revision = "0057_proposal_studio"
branch_labels = None
depends_on = None

_TABLES = ("payment_milestones", "payments")
_EVENTS = ("payment_recorded", "payment_verified", "payment_dishonoured")


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            for value in _EVENTS:
                op.execute(f"ALTER TYPE timelineeventtype ADD VALUE IF NOT EXISTS '{value}'")
    for table in _TABLES:
        with op.batch_alter_table(table) as batch:
            batch.add_column(sa.Column("lead_id", sa.Integer(), nullable=True))
            batch.create_foreign_key(f"fk_{table}_lead_id", "leads", ["lead_id"], ["id"])
            batch.create_check_constraint(f"ck_{table}_one_owner", "onboarding_id IS NULL OR lead_id IS NULL")
        op.create_index(f"ix_{table}_lead_id", table, ["lead_id"])


def downgrade() -> None:
    bind = op.get_bind()
    for table in _TABLES:
        held = bind.execute(sa.text(f"SELECT COUNT(*) FROM {table} WHERE lead_id IS NOT NULL")).scalar()
        if held:
            raise RuntimeError(f"{held} direct-customer row(s) in {table}; they cannot lose their lead.")
    for table in reversed(_TABLES):
        op.drop_index(f"ix_{table}_lead_id", table_name=table)
        with op.batch_alter_table(table) as batch:
            batch.drop_constraint(f"ck_{table}_one_owner", type_="check")
            batch.drop_constraint(f"fk_{table}_lead_id", type_="foreignkey")
            batch.drop_column("lead_id")
