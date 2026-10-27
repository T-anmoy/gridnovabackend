"""BE-17: access control — several roles per person, staff invitations, second approval,
regions, and the Network Manager role.

  * employeerole gains 'network_manager' (PostgreSQL: ADD VALUE; SQLite stores it as text).
  * employee_roles (employee_id, role, granted_by, granted_at) — the roles a person
    holds; the database decides access on every request. Backfilled with one row per
    employee from employees.role, which stays as the primary role.
  * employees.regions — the states a person works in (NULL = every region).
  * staff_invitations — dashboard invitations, one pending per address
    (uq_staff_invitations_pending_email, partial).
  * approval_requests — maker–checker requests (ix_approval_requests_status_created).

Downgrade drops the three tables and the column. 'network_manager' cannot be dropped from
the PostgreSQL type without rebuilding it and is left in place, as every earlier enum-
extending revision does; a downgrade refuses while any employee still holds it as their
primary role.

Revision ID: 0056_access_control
Revises: 0055_proposal_document_reference
Create Date: 2026-10-27
"""
from __future__ import annotations

from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

revision = "0056_access_control"
down_revision = "0055_proposal_document_reference"
branch_labels = None
depends_on = None

_PENDING = sa.text("status = 'pending'")


def _dt():
    return sa.DateTime(timezone=True)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute("ALTER TYPE employeerole ADD VALUE IF NOT EXISTS 'network_manager'")

    op.add_column("employees", sa.Column("regions", sa.JSON(), nullable=True))

    op.create_table(
        "employee_roles",
        sa.Column("employee_id", sa.String(), sa.ForeignKey("employees.id"), primary_key=True),
        sa.Column("role", sa.String(length=32), primary_key=True),
        sa.Column("granted_by", sa.String(), nullable=False),
        sa.Column("granted_at", _dt(), nullable=False),
    )
    now = datetime.now(timezone.utc)
    # employees.role is a native enum on PostgreSQL: cast it to text for the new column.
    role_text = "CAST(role AS TEXT)" if bind.dialect.name == "postgresql" else "role"
    bind.execute(sa.text(
        f"INSERT INTO employee_roles (employee_id, role, granted_by, granted_at) "
        f"SELECT id, {role_text}, 'migration_0056', :now FROM employees"
    ), {"now": now})

    op.create_table(
        "staff_invitations",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("roles", sa.JSON(), nullable=False),
        sa.Column("status", sa.Enum("pending", "accepted", "revoked", "expired", name="staffinvitationstatus",
                                    native_enum=False, length=16), nullable=False),
        sa.Column("invited_by", sa.String(), nullable=False),
        sa.Column("clerk_invitation_id", sa.String(), nullable=True),
        sa.Column("clerk_status", sa.String(length=16), nullable=True),
        sa.Column("created_at", _dt(), nullable=False),
        sa.Column("expires_at", _dt(), nullable=False),
        sa.Column("resent_at", _dt(), nullable=True),
        sa.Column("accepted_employee_id", sa.String(), nullable=True),
        sa.Column("accepted_at", _dt(), nullable=True),
        sa.Column("revoked_by", sa.String(), nullable=True),
        sa.Column("revoked_at", _dt(), nullable=True),
    )
    op.create_index("ix_staff_invitations_email", "staff_invitations", ["email"])
    op.create_index("uq_staff_invitations_pending_email", "staff_invitations", ["email"], unique=True,
                    sqlite_where=_PENDING, postgresql_where=_PENDING)

    op.create_table(
        "approval_requests",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.Enum("pending", "approved", "rejected", "cancelled", "expired", name="approvalrequeststatus",
                                    native_enum=False, length=16), nullable=False),
        sa.Column("requested_by", sa.String(), nullable=False),
        sa.Column("created_at", _dt(), nullable=False),
        sa.Column("expires_at", _dt(), nullable=False),
        sa.Column("decided_by", sa.String(), nullable=True),
        sa.Column("decided_at", _dt(), nullable=True),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.Column("result", sa.JSON(), nullable=True),
    )
    op.create_index("ix_approval_requests_status_created", "approval_requests", ["status", "created_at"])


def downgrade() -> None:
    bind = op.get_bind()
    held = bind.execute(sa.text("SELECT COUNT(*) FROM employees WHERE CAST(role AS TEXT) = 'network_manager'")).scalar()
    if held:
        raise RuntimeError(f"{held} employee(s) hold network_manager as their primary role; change it first.")
    op.drop_index("ix_approval_requests_status_created", table_name="approval_requests")
    op.drop_table("approval_requests")
    op.drop_index("uq_staff_invitations_pending_email", table_name="staff_invitations")
    op.drop_index("ix_staff_invitations_email", table_name="staff_invitations")
    op.drop_table("staff_invitations")
    op.drop_table("employee_roles")
    with op.batch_alter_table("employees") as batch:
        batch.drop_column("regions")
