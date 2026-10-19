"""BE-4 (Phase 5A): commissions, TDS and payouts — and the PAN encrypted at rest.

  * commission_rate_schedules / commission_rates — versioned rate schedules; a trigger
    refuses UPDATE and DELETE (a change is a new version).
  * commission_entries — the append-only commission ledger; a trigger refuses DELETE
    and any change to an entry's parties or (once set) its amount, rate, basis,
    agreement date or schedule. One earn entry per (payment, associate, role).
  * payout_batches / payout_lines — monthly payouts; at most one batch per month that
    is not cancelled (partial unique index).
  * commission_queries — associate queries on an entry; at most one open per entry.
  * associate_applications / preferred_associate_applications: `pan` (plaintext) is
    replaced by `pan_encrypted` (Fernet, like the bank account) + `pan_last4`. Existing
    rows are encrypted here, which needs FERNET_ENCRYPTION_KEY whenever a row exists
    (development and staging only — nothing is in production).

Downgrade drops the five tables, their triggers and enum types (commission history is
lost — this is a development/staging downgrade), and decrypts the PAN back into a
plaintext `pan` column (again needing the key when rows exist).

Revision ID: 0048_commissions
Revises: 0047_be3_indexes
Create Date: 2026-10-13
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.memory.models import (
    COMMISSION_ENTRIES_PG_FUNCTION, COMMISSION_ENTRIES_PG_TRIGGER, COMMISSION_ENTRIES_SQLITE_TRIGGERS,
    COMMISSION_SCHEDULES_PG_FUNCTION, COMMISSION_SCHEDULES_PG_TRIGGERS, COMMISSION_SCHEDULES_SQLITE_TRIGGERS,
)

revision = "0048_commissions"
down_revision = "0047_be3_indexes"
branch_labels = None
depends_on = None

_ENUMS = {
    "commissionrole": ("personal_sale", "territory_state", "territory_district", "territory_block"),
    "commissionentrykind": ("earn", "reversal", "recovery", "adjustment"),
    "commissionentrystatus": ("earned", "approved", "paid", "reversed"),
    "payoutbatchstatus": ("draft", "approved", "paid", "cancelled"),
    "commissionquerystatus": ("open", "answered"),
}
_APPLICATION_TABLES = ("associate_applications", "preferred_associate_applications")


def _enum(name: str, pg: bool):
    values = _ENUMS[name]
    return sa.Enum(*values, name=name).with_variant(postgresql.ENUM(*values, name=name, create_type=False),
                                                     "postgresql") if pg else sa.Enum(*values, name=name)


def upgrade() -> None:
    bind = op.get_bind()
    pg = bind.dialect.name == "postgresql"
    if pg:
        for name, values in _ENUMS.items():
            postgresql.ENUM(*values, name=name).create(bind, checkfirst=True)

    op.create_table(
        "commission_rate_schedules",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("version", sa.Integer(), nullable=False, unique=True),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("is_placeholder", sa.Boolean(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("published_by", sa.String(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_commission_rate_schedules_effective_from", "commission_rate_schedules", ["effective_from"])
    op.create_table(
        "commission_rates",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("schedule_id", sa.Integer(), sa.ForeignKey("commission_rate_schedules.id"), nullable=False),
        sa.Column("rate_key", sa.String(length=64), nullable=False),
        sa.Column("rate_percent", sa.Numeric(7, 4), nullable=False),
        sa.UniqueConstraint("schedule_id", "rate_key", name="uq_commission_rates_key"),
        sa.CheckConstraint("rate_percent >= 0 AND rate_percent <= 100", name="ck_commission_rates_percent"),
    )
    op.create_index("ix_commission_rates_schedule_id", "commission_rates", ["schedule_id"])

    op.create_table(
        "payout_batches",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("period", sa.String(length=7), nullable=False),
        sa.Column("financial_year", sa.String(length=7), nullable=False),
        sa.Column("status", _enum("payoutbatchstatus", pg), nullable=False),
        sa.Column("prepared_by", sa.String(), nullable=False),
        sa.Column("prepared_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approved_by", sa.String(), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approval_override_reason", sa.Text(), nullable=True),
        sa.Column("paid_by", sa.String(), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_by", sa.String(), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_reason", sa.Text(), nullable=True),
        sa.CheckConstraint("status NOT IN ('approved', 'paid') OR approved_by IS NOT NULL",
                           name="ck_payout_batches_approved"),
        sa.CheckConstraint("status != 'paid' OR paid_at IS NOT NULL", name="ck_payout_batches_paid"),
        sa.CheckConstraint("status != 'cancelled' OR cancel_reason IS NOT NULL", name="ck_payout_batches_cancelled"),
    )
    op.create_index("uq_payout_batches_period", "payout_batches", ["period"], unique=True,
                    sqlite_where=sa.text("status != 'cancelled'"), postgresql_where=sa.text("status != 'cancelled'"))

    op.create_table(
        "payout_lines",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("batch_id", sa.Integer(), sa.ForeignKey("payout_batches.id"), nullable=False),
        sa.Column("associate_id", sa.Integer(), sa.ForeignKey("associates.id"), nullable=False),
        *[sa.Column(name, sa.Numeric(12, 2), nullable=False)
          for name in ("gross", "recoveries", "carried_in", "taxable", "tds", "net", "carry_forward")],
        sa.Column("tds_rate_percent", sa.Numeric(7, 4), nullable=False),
        sa.Column("pan_on_file", sa.Boolean(), nullable=False),
        sa.Column("fy_taxable_before", sa.Numeric(12, 2), nullable=False),
        sa.Column("fy_tds_before", sa.Numeric(12, 2), nullable=False),
        sa.Column("account_holder_name", sa.String(), nullable=False),
        sa.Column("bank_account_last4", sa.String(length=4), nullable=False),
        sa.Column("bank_ifsc", sa.String(length=11), nullable=False),
        sa.Column("bank_reference", sa.String(length=100), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("batch_id", "associate_id", name="uq_payout_lines_associate"),
    )
    op.create_index("ix_payout_lines_batch_id", "payout_lines", ["batch_id"])
    op.create_index("ix_payout_lines_associate_id", "payout_lines", ["associate_id"])

    op.create_table(
        "commission_entries",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("associate_id", sa.Integer(), sa.ForeignKey("associates.id"), nullable=False),
        sa.Column("earning_role", _enum("commissionrole", pg), nullable=False),
        sa.Column("kind", _enum("commissionentrykind", pg), nullable=False),
        sa.Column("status", _enum("commissionentrystatus", pg), nullable=False),
        sa.Column("onboarding_id", sa.Integer(), sa.ForeignKey("customer_onboardings.id"), nullable=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id"), nullable=True),
        sa.Column("payment_id", sa.Integer(), sa.ForeignKey("payments.id"), nullable=True),
        sa.Column("basis_amount", sa.Numeric(12, 2), nullable=True),
        sa.Column("rate_percent", sa.Numeric(7, 4), nullable=True),
        sa.Column("holders", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("amount", sa.Numeric(12, 2), nullable=True),
        sa.Column("schedule_id", sa.Integer(), sa.ForeignKey("commission_rate_schedules.id"), nullable=True),
        sa.Column("pending_reason", sa.String(length=32), nullable=True),
        sa.Column("agreement_date", sa.Date(), nullable=True),
        sa.Column("reverses_entry_id", sa.Integer(), sa.ForeignKey("commission_entries.id"), nullable=True,
                  unique=True),
        sa.Column("batch_id", sa.Integer(), sa.ForeignKey("payout_batches.id"), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("rated_by", sa.String(), nullable=True),
        sa.Column("rated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approved_by", sa.String(), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(kind = 'earn' AND reverses_entry_id IS NULL AND payment_id IS NOT NULL "
            "AND (amount IS NULL OR amount >= 0)) OR "
            "(kind IN ('reversal', 'recovery') AND reverses_entry_id IS NOT NULL AND (amount IS NULL OR amount <= 0)) OR "
            "(kind = 'adjustment' AND reverses_entry_id IS NULL AND amount IS NOT NULL AND reason IS NOT NULL)",
            name="ck_commission_entries_shape"),
        sa.CheckConstraint("(amount IS NULL AND pending_reason IS NOT NULL) OR "
                           "(amount IS NOT NULL AND pending_reason IS NULL)", name="ck_commission_entries_pending"),
        sa.CheckConstraint("status != 'paid' OR batch_id IS NOT NULL", name="ck_commission_entries_paid"),
        sa.CheckConstraint("status NOT IN ('approved', 'paid') OR (approved_by IS NOT NULL AND amount IS NOT NULL)",
                           name="ck_commission_entries_approved"),
        sa.CheckConstraint("holders >= 1", name="ck_commission_entries_holders"),
    )
    op.create_index("ix_commission_entries_associate_status", "commission_entries", ["associate_id", "status"])
    op.create_index("ix_commission_entries_payment", "commission_entries", ["payment_id"])
    op.create_index("ix_commission_entries_project", "commission_entries", ["project_id"])
    op.create_index("ix_commission_entries_batch", "commission_entries", ["batch_id"])
    op.create_index("ix_commission_entries_created_at", "commission_entries", ["created_at"])
    op.create_index("uq_commission_entries_earn_once", "commission_entries",
                    ["payment_id", "associate_id", "earning_role"], unique=True,
                    sqlite_where=sa.text("kind = 'earn'"), postgresql_where=sa.text("kind = 'earn'"))

    op.create_table(
        "commission_queries",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("associate_id", sa.Integer(), sa.ForeignKey("associates.id"), nullable=False),
        sa.Column("entry_id", sa.Integer(), sa.ForeignKey("commission_entries.id"), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("status", _enum("commissionquerystatus", pg), nullable=False),
        sa.Column("response", sa.Text(), nullable=True),
        sa.Column("responded_by", sa.String(), nullable=True),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("overdue_alerted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status != 'answered' OR (response IS NOT NULL AND responded_by IS NOT NULL)",
                           name="ck_commission_queries_answered"),
    )
    op.create_index("ix_commission_queries_status_due", "commission_queries", ["status", "due_at"])
    op.create_index("ix_commission_queries_associate", "commission_queries", ["associate_id"])
    op.create_index("uq_commission_queries_one_open", "commission_queries", ["entry_id"], unique=True,
                    sqlite_where=sa.text("status = 'open'"), postgresql_where=sa.text("status = 'open'"))

    if pg:
        op.execute(COMMISSION_SCHEDULES_PG_FUNCTION)
        for sql in COMMISSION_SCHEDULES_PG_TRIGGERS:
            op.execute(sql)
        op.execute(COMMISSION_ENTRIES_PG_FUNCTION)
        op.execute(COMMISSION_ENTRIES_PG_TRIGGER)
    else:
        for sql in COMMISSION_SCHEDULES_SQLITE_TRIGGERS + COMMISSION_ENTRIES_SQLITE_TRIGGERS:
            op.execute(sql)

    _encrypt_pans(bind)


def _rows(bind, table: str, column: str) -> list:
    return list(bind.execute(sa.text(f"SELECT id, {column} FROM {table} ORDER BY id")))


def _encrypt_pans(bind) -> None:
    for table in _APPLICATION_TABLES:
        with op.batch_alter_table(table) as batch:
            batch.add_column(sa.Column("pan_encrypted", sa.Text(), nullable=True))
            batch.add_column(sa.Column("pan_last4", sa.String(length=4), nullable=True))
        rows = _rows(bind, table, "pan")
        if rows:
            from app.security.encryption import encrypt
            for row_id, pan in rows:
                bind.execute(sa.text(f"UPDATE {table} SET pan_encrypted = :enc, pan_last4 = :last4 WHERE id = :id"),
                             {"enc": encrypt(pan), "last4": pan[-4:], "id": row_id})
        with op.batch_alter_table(table) as batch:
            batch.alter_column("pan_encrypted", existing_type=sa.Text(), nullable=False)
            batch.alter_column("pan_last4", existing_type=sa.String(length=4), nullable=False)
            batch.drop_column("pan")


def _decrypt_pans(bind) -> None:
    for table in _APPLICATION_TABLES:
        with op.batch_alter_table(table) as batch:
            batch.add_column(sa.Column("pan", sa.String(length=10), nullable=True))
        rows = _rows(bind, table, "pan_encrypted")
        if rows:
            from app.security.encryption import decrypt
            for row_id, encrypted in rows:
                bind.execute(sa.text(f"UPDATE {table} SET pan = :pan WHERE id = :id"),
                             {"pan": decrypt(encrypted), "id": row_id})
        with op.batch_alter_table(table) as batch:
            batch.alter_column("pan", existing_type=sa.String(length=10), nullable=False)
            batch.drop_column("pan_last4")
            batch.drop_column("pan_encrypted")


def downgrade() -> None:
    bind = op.get_bind()
    pg = bind.dialect.name == "postgresql"
    _decrypt_pans(bind)
    op.drop_index("uq_commission_queries_one_open", table_name="commission_queries")
    op.drop_index("ix_commission_queries_associate", table_name="commission_queries")
    op.drop_index("ix_commission_queries_status_due", table_name="commission_queries")
    op.drop_table("commission_queries")
    for name in ("uq_commission_entries_earn_once", "ix_commission_entries_created_at", "ix_commission_entries_batch",
                 "ix_commission_entries_project", "ix_commission_entries_payment",
                 "ix_commission_entries_associate_status"):
        op.drop_index(name, table_name="commission_entries")
    op.drop_table("commission_entries")   # its triggers go with it
    op.drop_index("ix_payout_lines_associate_id", table_name="payout_lines")
    op.drop_index("ix_payout_lines_batch_id", table_name="payout_lines")
    op.drop_table("payout_lines")
    op.drop_index("uq_payout_batches_period", table_name="payout_batches")
    op.drop_table("payout_batches")
    op.drop_index("ix_commission_rates_schedule_id", table_name="commission_rates")
    op.drop_table("commission_rates")
    op.drop_index("ix_commission_rate_schedules_effective_from", table_name="commission_rate_schedules")
    op.drop_table("commission_rate_schedules")
    if pg:
        op.execute("DROP FUNCTION IF EXISTS commission_entries_guard()")
        op.execute("DROP FUNCTION IF EXISTS commission_schedules_append_only()")
        for name in _ENUMS:
            op.execute(f"DROP TYPE IF EXISTS {name}")
