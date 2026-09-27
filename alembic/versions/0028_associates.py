"""Associate registration and identity: the two application tables, identity claims
(duplicate-registration guard), the Associate ID counter, associates, and territory
seats with their database-enforced limits (2 State Heads per state, 1 District Head
per district) — plus the trigger that makes associates.associate_id immutable.

Enum types are created explicitly with checkfirst and the columns reference them
with create_type=False, so a type shared by several tables (applicationstatus,
territorylevel, ...) is created exactly once on PostgreSQL. On SQLite enums are
plain VARCHARs.

Revision ID: 0028_associates
Revises: 0027_territories
Create Date: 2026-09-27
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0028_associates"
down_revision = "0027_territories"
branch_labels = None
depends_on = None

_ENUMS = {
    "associatetype": ("associate", "preferred_associate"),
    "associatecapability": ("sales", "execution"),
    "territorylevel": ("state_head", "district_head", "block"),
    "applicationstatus": ("submitted", "under_review", "needs_info", "approved", "rejected"),
    "associatestatus": ("active", "suspended", "deactivated"),
    "clerkinvitestatus": ("sent", "skipped", "failed"),
}


def _enum(name: str):
    values = _ENUMS[name]
    return sa.Enum(*values, name=name).with_variant(
        postgresql.ENUM(*values, name=name, create_type=False), "postgresql",
    )


def _ts(name: str, nullable: bool = True) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def _application_columns() -> list:
    return [
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("status", _enum("applicationstatus"), nullable=False),
        sa.Column("full_name", sa.String(), nullable=False),
        sa.Column("date_of_birth", sa.Date(), nullable=False),
        sa.Column("address_line", sa.Text(), nullable=False),
        sa.Column("state_id", sa.Integer(), sa.ForeignKey("territory_states.id"), nullable=False),
        sa.Column("district_id", sa.Integer(), sa.ForeignKey("territory_districts.id"), nullable=False),
        sa.Column("block_id", sa.Integer(), sa.ForeignKey("territory_blocks.id"), nullable=True),
        sa.Column("pin_code", sa.String(length=6), nullable=False),
        sa.Column("mobile", sa.String(length=10), nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("aadhaar_last4", sa.String(length=4), nullable=False),
        sa.Column("pan", sa.String(length=10), nullable=False),
        sa.Column("bank_account_encrypted", sa.Text(), nullable=False),
        sa.Column("bank_account_last4", sa.String(length=4), nullable=False),
        sa.Column("bank_ifsc", sa.String(length=11), nullable=False),
        sa.Column("kyc_consent_version", sa.String(), nullable=False),
        _ts("kyc_consent_accepted_at", nullable=False),
        sa.Column("documents", sa.JSON(), nullable=False),
        _ts("submitted_at", nullable=False),
        _ts("status_changed_at", nullable=False),
        sa.Column("needs_info_note", sa.Text(), nullable=True),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column("verification_call_completed", sa.Boolean(), nullable=False),
        sa.Column("verification_call_note", sa.Text(), nullable=True),
        _ts("decided_at"),
        sa.Column("decided_by_employee_id", sa.String(), sa.ForeignKey("employees.id"), nullable=True),
    ]


def _application_checks(table: str) -> list:
    return [
        sa.CheckConstraint("status != 'rejected' OR rejection_reason IS NOT NULL",
                           name=f"ck_{table}_rejected_has_reason"),
        sa.CheckConstraint(
            "status != 'approved' OR (verification_call_completed = true "
            "AND verification_call_note IS NOT NULL AND decided_by_employee_id IS NOT NULL)",
            name=f"ck_{table}_approved_after_call",
        ),
    ]


_SEAT_HELD = "released_at IS NULL"

_PG_FUNCTION = """
CREATE OR REPLACE FUNCTION associates_id_immutable() RETURNS trigger AS $$
BEGIN
    IF NEW.associate_id IS DISTINCT FROM OLD.associate_id THEN
        RAISE EXCEPTION 'associate_id is immutable once issued';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for name, values in _ENUMS.items():
            postgresql.ENUM(*values, name=name).create(bind, checkfirst=True)

    op.create_table(
        "associate_applications",
        *_application_columns(),
        sa.Column("capability", _enum("associatecapability"), nullable=False),
        *_application_checks("associate_applications"),
    )
    op.create_index("ix_associate_applications_status", "associate_applications", ["status"])

    op.create_table(
        "preferred_associate_applications",
        *_application_columns(),
        sa.Column("requested_level", _enum("territorylevel"), nullable=False),
        sa.Column("requested_state_id", sa.Integer(), sa.ForeignKey("territory_states.id"), nullable=False),
        sa.Column("requested_district_id", sa.Integer(), sa.ForeignKey("territory_districts.id"), nullable=True),
        sa.Column("requested_block_id", sa.Integer(), sa.ForeignKey("territory_blocks.id"), nullable=True),
        sa.Column("business_type", sa.String(), nullable=False),
        sa.Column("gstin", sa.String(length=15), nullable=True),
        sa.Column("office_address", sa.Text(), nullable=False),
        sa.Column("prior_experience", sa.Text(), nullable=False),
        sa.Column("team_size", sa.Integer(), nullable=False),
        sa.Column("references", sa.JSON(), nullable=False),
        *_application_checks("preferred_associate_applications"),
    )
    op.create_index("ix_preferred_associate_applications_status", "preferred_associate_applications", ["status"])

    op.create_table(
        "associate_identity_claims",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("value_hash", sa.String(length=64), nullable=False),
        sa.Column("application_type", _enum("associatetype"), nullable=False),
        sa.Column("application_id", sa.Integer(), nullable=False),
        _ts("created_at", nullable=False),
        sa.UniqueConstraint("kind", "value_hash", name="uq_associate_identity_claim"),
    )

    op.create_table(
        "associate_id_counters",
        sa.Column("counter_key", sa.String(), primary_key=True),
        sa.Column("value", sa.Integer(), nullable=False),
    )

    op.create_table(
        "associates",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("associate_id", sa.String(), nullable=False, unique=True),
        sa.Column("associate_type", _enum("associatetype"), nullable=False),
        sa.Column("status", _enum("associatestatus"), nullable=False),
        sa.Column("associate_application_id", sa.Integer(),
                  sa.ForeignKey("associate_applications.id"), nullable=True, unique=True),
        sa.Column("preferred_application_id", sa.Integer(),
                  sa.ForeignKey("preferred_associate_applications.id"), nullable=True, unique=True),
        sa.Column("full_name", sa.String(), nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("mobile", sa.String(length=10), nullable=False),
        sa.Column("state_id", sa.Integer(), sa.ForeignKey("territory_states.id"), nullable=False),
        sa.Column("capability", _enum("associatecapability"), nullable=True),
        sa.Column("level", _enum("territorylevel"), nullable=True),
        sa.Column("territory_state_id", sa.Integer(), sa.ForeignKey("territory_states.id"), nullable=True),
        sa.Column("territory_district_id", sa.Integer(), sa.ForeignKey("territory_districts.id"), nullable=True),
        sa.Column("territory_block_id", sa.Integer(), sa.ForeignKey("territory_blocks.id"), nullable=True),
        sa.Column("photo_storage_key", sa.String(), nullable=True),
        sa.Column("photo_content_type", sa.String(), nullable=True),
        sa.Column("clerk_user_id", sa.String(), nullable=True, unique=True),
        sa.Column("clerk_invitation_id", sa.String(), nullable=True),
        sa.Column("clerk_invite_status", _enum("clerkinvitestatus"), nullable=True),
        sa.Column("id_card_storage_key", sa.String(), nullable=True),
        sa.Column("welcome_letter_storage_key", sa.String(), nullable=True),
        _ts("documents_generated_at"),
        sa.Column("documents_error", sa.Text(), nullable=True),
        _ts("approved_at", nullable=False),
        sa.Column("approved_by_employee_id", sa.String(), sa.ForeignKey("employees.id"), nullable=False),
        _ts("status_changed_at", nullable=False),
        sa.Column("status_reason", sa.Text(), nullable=True),
        _ts("created_at", nullable=False),
        sa.CheckConstraint(
            "(associate_type = 'associate' AND associate_application_id IS NOT NULL "
            "AND preferred_application_id IS NULL AND capability IS NOT NULL AND level IS NULL) OR "
            "(associate_type = 'preferred_associate' AND preferred_application_id IS NOT NULL "
            "AND associate_application_id IS NULL AND level IS NOT NULL)",
            name="ck_associates_type_shape",
        ),
    )

    op.create_table(
        "territory_seats",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("level", _enum("territorylevel"), nullable=False),
        sa.Column("state_id", sa.Integer(), sa.ForeignKey("territory_states.id"), nullable=False),
        sa.Column("district_id", sa.Integer(), sa.ForeignKey("territory_districts.id"), nullable=True),
        sa.Column("block_id", sa.Integer(), sa.ForeignKey("territory_blocks.id"), nullable=True),
        sa.Column("seat_no", sa.Integer(), nullable=True),
        sa.Column("holder_id", sa.Integer(), sa.ForeignKey("associates.id"), nullable=False),
        _ts("claimed_at", nullable=False),
        _ts("released_at"),
        sa.Column("released_by", sa.String(), nullable=True),
        sa.Column("release_reason", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "(level = 'state_head' AND district_id IS NULL AND block_id IS NULL "
            "AND seat_no BETWEEN 1 AND 2) OR "
            "(level = 'district_head' AND district_id IS NOT NULL AND block_id IS NULL "
            "AND seat_no BETWEEN 1 AND 1) OR "
            "(level = 'block' AND district_id IS NOT NULL AND block_id IS NOT NULL AND seat_no IS NULL)",
            name="ck_territory_seats_shape",
        ),
    )
    for name, cols, where in (
        ("uq_territory_seats_state_head", ["state_id", "seat_no"], f"level = 'state_head' AND {_SEAT_HELD}"),
        ("uq_territory_seats_district_head", ["district_id", "seat_no"], f"level = 'district_head' AND {_SEAT_HELD}"),
        ("uq_territory_seats_holder", ["holder_id"], _SEAT_HELD),
    ):
        op.create_index(name, "territory_seats", cols, unique=True,
                        postgresql_where=sa.text(where), sqlite_where=sa.text(where))

    if bind.dialect.name == "postgresql":
        op.execute(_PG_FUNCTION)
        op.execute("CREATE TRIGGER trg_associates_id_immutable BEFORE UPDATE ON associates "
                   "FOR EACH ROW EXECUTE FUNCTION associates_id_immutable()")
    elif bind.dialect.name == "sqlite":
        op.execute("CREATE TRIGGER trg_associates_id_immutable BEFORE UPDATE OF associate_id ON associates "
                   "WHEN NEW.associate_id IS NOT OLD.associate_id "
                   "BEGIN SELECT RAISE(ABORT, 'associate_id is immutable once issued'); END")


def downgrade() -> None:
    bind = op.get_bind()
    for name in ("uq_territory_seats_holder", "uq_territory_seats_district_head", "uq_territory_seats_state_head"):
        op.drop_index(name, table_name="territory_seats")
    op.drop_table("territory_seats")
    op.execute("DROP TRIGGER IF EXISTS trg_associates_id_immutable" +
               (" ON associates" if bind.dialect.name == "postgresql" else ""))
    op.drop_table("associates")
    op.drop_table("associate_id_counters")
    op.drop_table("associate_identity_claims")
    op.drop_index("ix_preferred_associate_applications_status", table_name="preferred_associate_applications")
    op.drop_table("preferred_associate_applications")
    op.drop_index("ix_associate_applications_status", table_name="associate_applications")
    op.drop_table("associate_applications")
    if bind.dialect.name == "postgresql":
        op.execute("DROP FUNCTION IF EXISTS associates_id_immutable()")
        for name in _ENUMS:
            op.execute(f"DROP TYPE IF EXISTS {name}")
