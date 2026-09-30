"""Payments ledger: payment milestones, payments (no cash mode), and the append-only
payment_ledger_entries table (a trigger refuses UPDATE/DELETE).

Revision ID: 0039_payments_ledger
Revises: 0038_installed_units
Create Date: 2026-09-29
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.memory.models import (
    LEDGER_APPEND_ONLY_PG_FUNCTION, LEDGER_APPEND_ONLY_PG_TRIGGER, LEDGER_APPEND_ONLY_SQLITE_TRIGGERS,
)

revision = "0039_payments_ledger"
down_revision = "0038_installed_units"
branch_labels = None
depends_on = None

_MODES = ("cheque", "demand_draft", "neft", "rtgs", "imps", "upi")
_STATUSES = ("recorded", "deposited", "verified", "dishonoured", "cancelled")
_KINDS = ("credit", "reversal")


def _enum(values, name, pg):
    return sa.Enum(*values, name=name).with_variant(postgresql.ENUM(*values, name=name, create_type=False),
                                                     "postgresql") if pg else sa.Enum(*values, name=name)


def upgrade() -> None:
    bind = op.get_bind()
    pg = bind.dialect.name == "postgresql"
    if pg:
        postgresql.ENUM(*_MODES, name="paymentmode").create(bind, checkfirst=True)
        postgresql.ENUM(*_STATUSES, name="paymentstatus").create(bind, checkfirst=True)
        postgresql.ENUM(*_KINDS, name="ledgerentrykind").create(bind, checkfirst=True)
    op.create_table(
        "payment_milestones",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("onboarding_id", sa.Integer(), sa.ForeignKey("customer_onboardings.id"), nullable=False),
        sa.Column("proposal_id", sa.Integer(), sa.ForeignKey("proposals.id"), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(), nullable=False),
        sa.Column("percent", sa.Numeric(5, 2), nullable=False),
        sa.Column("amount_due", sa.Numeric(12, 2), nullable=False),
        sa.Column("trigger", sa.String(), nullable=False),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("overdue_alerted_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("project_id", "number", name="uq_payment_milestones_number"),
        sa.CheckConstraint("amount_due >= 0", name="ck_payment_milestones_amount"),
    )
    op.create_index("ix_payment_milestones_project_id", "payment_milestones", ["project_id"])
    op.create_index("ix_payment_milestones_onboarding_id", "payment_milestones", ["onboarding_id"])
    op.create_table(
        "payments",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("onboarding_id", sa.Integer(), sa.ForeignKey("customer_onboardings.id"), nullable=False),
        sa.Column("milestone_id", sa.Integer(), sa.ForeignKey("payment_milestones.id"), nullable=True),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("mode", _enum(_MODES, "paymentmode", pg), nullable=False),
        sa.Column("reference", sa.String(length=100), nullable=False),
        sa.Column("reference_normalized", sa.String(length=100), nullable=False),
        sa.Column("bank_name", sa.String(length=120), nullable=True),
        sa.Column("bank_name_normalized", sa.String(length=120), nullable=False),
        sa.Column("instrument_date", sa.Date(), nullable=True),
        sa.Column("payer_name", sa.String(length=200), nullable=True),
        sa.Column("proof_key", sa.String(), nullable=True),
        sa.Column("proof_filename", sa.String(), nullable=True),
        sa.Column("proof_content_type", sa.String(), nullable=True),
        sa.Column("status", _enum(_STATUSES, "paymentstatus", pg), nullable=False),
        sa.Column("recorded_by", sa.String(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deposited_by", sa.String(), nullable=True),
        sa.Column("deposited_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verified_by", sa.String(), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dishonoured_by", sa.String(), nullable=True),
        sa.Column("dishonoured_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dishonour_reason", sa.Text(), nullable=True),
        sa.Column("cancelled_by", sa.String(), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_reason", sa.Text(), nullable=True),
        sa.Column("alerted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("amount > 0", name="ck_payments_amount_positive"),
    )
    op.create_index("ix_payments_project_id", "payments", ["project_id"])
    op.create_index("ix_payments_onboarding_id", "payments", ["onboarding_id"])
    op.create_index("uq_payments_instrument", "payments", ["mode", "reference_normalized", "bank_name_normalized"],
                    unique=True, sqlite_where=sa.text("status != 'cancelled'"),
                    postgresql_where=sa.text("status != 'cancelled'"))
    op.create_table(
        "payment_ledger_entries",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("payment_id", sa.Integer(), sa.ForeignKey("payments.id"), nullable=False),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("kind", _enum(_KINDS, "ledgerentrykind", pg), nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("reverses_entry_id", sa.Integer(), sa.ForeignKey("payment_ledger_entries.id"), nullable=True,
                  unique=True),
        sa.Column("actor", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("payment_id", "kind", name="uq_payment_ledger_one_per_kind"),
        sa.CheckConstraint("(kind = 'credit' AND amount > 0 AND reverses_entry_id IS NULL) OR "
                           "(kind = 'reversal' AND amount < 0 AND reverses_entry_id IS NOT NULL)",
                           name="ck_payment_ledger_shape"),
    )
    op.create_index("ix_payment_ledger_entries_payment_id", "payment_ledger_entries", ["payment_id"])
    op.create_index("ix_payment_ledger_entries_project_id", "payment_ledger_entries", ["project_id"])
    if pg:
        op.execute(LEDGER_APPEND_ONLY_PG_FUNCTION)
        op.execute(LEDGER_APPEND_ONLY_PG_TRIGGER)
    else:
        for sql in LEDGER_APPEND_ONLY_SQLITE_TRIGGERS:
            op.execute(sql)


def downgrade() -> None:
    pg = op.get_bind().dialect.name == "postgresql"
    op.drop_index("ix_payment_ledger_entries_project_id", table_name="payment_ledger_entries")
    op.drop_index("ix_payment_ledger_entries_payment_id", table_name="payment_ledger_entries")
    op.drop_table("payment_ledger_entries")   # its triggers go with it
    if pg:
        op.execute("DROP FUNCTION IF EXISTS payment_ledger_append_only()")
    op.drop_index("uq_payments_instrument", table_name="payments")
    op.drop_index("ix_payments_onboarding_id", table_name="payments")
    op.drop_index("ix_payments_project_id", table_name="payments")
    op.drop_table("payments")
    op.drop_index("ix_payment_milestones_onboarding_id", table_name="payment_milestones")
    op.drop_index("ix_payment_milestones_project_id", table_name="payment_milestones")
    op.drop_table("payment_milestones")
    if pg:
        for name in ("ledgerentrykind", "paymentstatus", "paymentmode"):
            op.execute(f"DROP TYPE IF EXISTS {name}")
