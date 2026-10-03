"""Phase 4A follow-ups: payments and milestones keyed by project (onboarding_id becomes
nullable), a bounced cheque can be re-presented (previous_payment_id; dishonoured
payments release the instrument's unique reference), and voided serials remember their
normalised value so the same serial on another project can be flagged.

Downgrade note: restoring NOT NULL on onboarding_id, or the old unique-index predicate,
fails if rows written since then (a payment with no onboarding, a re-presented cheque)
exist — by design; nothing is deleted to make a downgrade succeed.

Revision ID: 0041_phase4a_followups
Revises: 0040_receipts
Create Date: 2026-09-30
"""
from __future__ import annotations

import re
import unicodedata

import sqlalchemy as sa
from alembic import op

revision = "0041_phase4a_followups"
down_revision = "0040_receipts"
branch_labels = None
depends_on = None

_ZERO_WIDTH = re.compile("[​‌‍⁠﻿]")


def _normalize(raw: str) -> str:
    """app/execution/register.normalize_serial, frozen here so this migration never
    changes meaning if that function later does."""
    text = _ZERO_WIDTH.sub("", unicodedata.normalize("NFKC", raw or ""))
    return re.sub(r"\s+", "", text, flags=re.UNICODE).upper()


def upgrade() -> None:
    op.drop_index("uq_payments_instrument", table_name="payments")
    with op.batch_alter_table("payment_milestones") as batch:
        batch.alter_column("onboarding_id", existing_type=sa.Integer(), nullable=True)
    with op.batch_alter_table("payments") as batch:
        batch.alter_column("onboarding_id", existing_type=sa.Integer(), nullable=True)
        batch.add_column(sa.Column("previous_payment_id", sa.Integer(), nullable=True))
        # Named (SQLite batch mode needs a name) with the name PostgreSQL would give it.
        batch.create_foreign_key("payments_previous_payment_id_fkey", "payments", ["previous_payment_id"], ["id"])
    op.create_index("uq_payments_instrument", "payments", ["mode", "reference_normalized", "bank_name_normalized"],
                    unique=True, sqlite_where=sa.text("status NOT IN ('cancelled', 'dishonoured')"),
                    postgresql_where=sa.text("status NOT IN ('cancelled', 'dishonoured')"))

    op.add_column("installed_units", sa.Column("voided_serial_normalized", sa.String(length=80), nullable=True))
    op.create_index("ix_installed_units_voided_serial_normalized", "installed_units", ["voided_serial_normalized"])
    bind = op.get_bind()
    for unit_id, serial in bind.execute(sa.text(
            "SELECT id, serial_number FROM installed_units WHERE status = 'voided'")).fetchall():
        bind.execute(sa.text("UPDATE installed_units SET voided_serial_normalized = :n WHERE id = :i"),
                     {"n": _normalize(serial), "i": unit_id})


def downgrade() -> None:
    op.drop_index("ix_installed_units_voided_serial_normalized", table_name="installed_units")
    op.drop_column("installed_units", "voided_serial_normalized")
    op.drop_index("uq_payments_instrument", table_name="payments")
    with op.batch_alter_table("payments") as batch:
        batch.drop_constraint("payments_previous_payment_id_fkey", type_="foreignkey")
        batch.drop_column("previous_payment_id")
        batch.alter_column("onboarding_id", existing_type=sa.Integer(), nullable=False)
    with op.batch_alter_table("payment_milestones") as batch:
        batch.alter_column("onboarding_id", existing_type=sa.Integer(), nullable=False)
    op.create_index("uq_payments_instrument", "payments", ["mode", "reference_normalized", "bank_name_normalized"],
                    unique=True, sqlite_where=sa.text("status != 'cancelled'"),
                    postgresql_where=sa.text("status != 'cancelled'"))
