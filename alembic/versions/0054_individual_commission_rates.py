"""BE-12: individual commission rates an Admin sets for one Associate.

  * associate_commission_rates — append-only (a trigger refuses UPDATE and DELETE):
    one row per change of an Associate's personal-sale rate, from effective_from on;
    rate_percent NULL means "back to the standard schedule". A reason (at least ten
    characters, internal) is required.
  * commission_entries.individual_rate_id — the individual rate an entry was rated at
    (NULL when rated at the standard schedule). Set once, like the amount: the ledger
    guard is recreated with it among the set-once columns.

Downgrade drops both and restores the BE-4 guard. It refuses while any entry was rated
at an individual rate: dropping the column would lose why that entry has its rate.

Revision ID: 0054_individual_commission_rates
Revises: 0053_attribution_until_completed
Create Date: 2026-10-23
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.memory.models import (
    ASSOCIATE_RATES_PG_FUNCTION, ASSOCIATE_RATES_PG_TRIGGER, ASSOCIATE_RATES_SQLITE_TRIGGERS,
    COMMISSION_ENTRIES_PG_TRIGGER, ENTRY_SET_ONCE_BE4, commission_entries_guard_sql,
)

revision = "0054_individual_commission_rates"
down_revision = "0053_attribution_until_completed"
branch_labels = None
depends_on = None

# Pinned here, not read from the models' current tuple, so later revisions cannot change
# what this one created.
_SET_ONCE = ENTRY_SET_ONCE_BE4 + ("individual_rate_id",)


def _guard(bind, set_once: tuple) -> None:
    pg_function, sqlite_triggers = commission_entries_guard_sql(set_once)
    if bind.dialect.name == "postgresql":
        op.execute(pg_function)   # CREATE OR REPLACE: the trigger keeps pointing at it
    else:
        op.execute("DROP TRIGGER IF EXISTS commission_entries_no_delete")
        op.execute("DROP TRIGGER IF EXISTS commission_entries_guard")
        for sql in sqlite_triggers:
            op.execute(sql)


def upgrade() -> None:
    bind = op.get_bind()
    pg = bind.dialect.name == "postgresql"
    op.create_table(
        "associate_commission_rates",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("associate_id", sa.Integer(), sa.ForeignKey("associates.id"), nullable=False),
        sa.Column("rate_key", sa.String(length=64), nullable=False),
        sa.Column("rate_percent", sa.Numeric(7, 4), nullable=True),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("set_by", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("rate_percent IS NULL OR (rate_percent >= 0 AND rate_percent <= 100)",
                           name="ck_associate_commission_rates_percent"),
        sa.CheckConstraint("length(trim(reason)) >= 10", name="ck_associate_commission_rates_reason"),
    )
    op.create_index("ix_associate_commission_rates_lookup", "associate_commission_rates",
                    ["associate_id", "rate_key", "effective_from"])
    if pg:
        op.execute(ASSOCIATE_RATES_PG_FUNCTION)
        op.execute(ASSOCIATE_RATES_PG_TRIGGER)
        op.add_column("commission_entries", sa.Column("individual_rate_id", sa.Integer(),
                                                      sa.ForeignKey("associate_commission_rates.id"), nullable=True))
    else:
        for sql in ASSOCIATE_RATES_SQLITE_TRIGGERS:
            op.execute(sql)
        # A plain ALTER TABLE (no batch rebuild): the table keeps its rows and its triggers.
        op.execute("ALTER TABLE commission_entries ADD COLUMN individual_rate_id INTEGER "
                   "REFERENCES associate_commission_rates (id)")
    _guard(bind, _SET_ONCE)


def downgrade() -> None:
    bind = op.get_bind()
    pg = bind.dialect.name == "postgresql"
    used = bind.execute(sa.text("SELECT count(*) FROM commission_entries WHERE individual_rate_id IS NOT NULL")).scalar()
    if used:
        raise RuntimeError(f"{used} commission entries were rated at an individual rate; downgrading would lose "
                           "which rate they were paid at. Refusing.")
    if pg:
        op.drop_column("commission_entries", "individual_rate_id")   # its foreign key goes with it
    else:
        # SQLite cannot drop a foreign-key column in place: the batch rebuild copies the
        # table, which loses its triggers and may lose its partial index — both restored.
        with op.batch_alter_table("commission_entries", recreate="always") as batch:
            batch.drop_column("individual_rate_id")
        op.execute("DROP INDEX IF EXISTS uq_commission_entries_earn_once")
        op.execute("CREATE UNIQUE INDEX uq_commission_entries_earn_once ON commission_entries "
                   "(payment_id, associate_id, earning_role) WHERE kind = 'earn'")
    _guard(bind, ENTRY_SET_ONCE_BE4)
    op.drop_index("ix_associate_commission_rates_lookup", table_name="associate_commission_rates")
    op.drop_table("associate_commission_rates")   # its triggers go with it
    if pg:
        op.execute("DROP FUNCTION IF EXISTS associate_commission_rates_append_only()")
