"""Rename the 'subcontractor' employee role to 'operations' and add 'accounts'.

PostgreSQL: RENAME VALUE relabels the enum value in place, so every existing
employees row holding it now reads 'operations' — no row rewrite is needed and
none is attempted. ADD VALUE runs in an autocommit block, as 0023/0025 do,
because it cannot be used inside the transaction that adds it.

SQLite: the enum is a plain VARCHAR with no CHECK constraint, so existing rows
are relabelled with an UPDATE and 'accounts' needs nothing.

Audit-log history that recorded the old 'subcontractor' value is deliberately
left as written — it is a record of what was true at the time.

Revision ID: 0026_operations_accounts_roles
Revises: 0025_lead_source_contact
Create Date: 2026-09-27
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0026_operations_accounts_roles"
down_revision = "0025_lead_source_contact"
branch_labels = None
depends_on = None


def _pg_enum_labels(bind) -> set[str]:
    rows = bind.execute(sa.text(
        "SELECT e.enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid "
        "WHERE t.typname = 'employeerole'"
    ))
    return {r[0] for r in rows}


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        labels = _pg_enum_labels(bind)
        if "subcontractor" in labels and "operations" not in labels:
            op.execute("ALTER TYPE employeerole RENAME VALUE 'subcontractor' TO 'operations'")
        elif "operations" not in labels:
            # A database whose employeerole never gained 'subcontractor' (0019 was a
            # no-op there) still needs the new value.
            with op.get_context().autocommit_block():
                op.execute("ALTER TYPE employeerole ADD VALUE IF NOT EXISTS 'operations'")
        with op.get_context().autocommit_block():
            op.execute("ALTER TYPE employeerole ADD VALUE IF NOT EXISTS 'accounts'")
    else:
        op.execute("UPDATE employees SET role = 'operations' WHERE role = 'subcontractor'")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # 'accounts' cannot be dropped from the type without rebuilding it; it is
        # left in place, as every earlier enum-extending revision does. The rename
        # is reversible.
        labels = _pg_enum_labels(bind)
        if "operations" in labels and "subcontractor" not in labels:
            op.execute("ALTER TYPE employeerole RENAME VALUE 'operations' TO 'subcontractor'")
    else:
        op.execute("UPDATE employees SET role = 'subcontractor' WHERE role = 'operations'")
