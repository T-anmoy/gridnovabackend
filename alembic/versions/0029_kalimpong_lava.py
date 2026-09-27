"""Kalimpong: LGD block code 2862 is Lava, and code 7354 no longer exists.

The 2022 LGD mirror the territory seed came from listed 2862 as "KALIMPONG-II" and
Lava under 7354. The live LGD portal (checked 2026-09-27: 2862 returns Lava, 7354
returns no record), IGOD and a 2026-09-02 LGD snapshot all agree Kalimpong now has
four blocks — Gorubathan 2859, Kalimpong-I 2861, Lava 2862, Pedong 7355. So, in
place and never deleting:

  * row 2862 is renamed KALIMPONG-II → Lava (its code is kept — it is LGD's code
    for Lava);
  * row 7354 is switched off (is_active = false). Anything already referencing it
    keeps pointing at it; the audit entry records how many such references exist.

Each change writes an audit_logs entry with the reason, as the territory admin
routes do. Only rows in exactly the expected state are touched, so the migration
is a no-op on a database whose territories were never seeded (a fresh database is
then seeded from the corrected data/territories/blocks.csv).

Revision ID: 0029_kalimpong_lava
Revises: 0028_associates
Create Date: 2026-09-27
"""
from __future__ import annotations

from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

revision = "0029_kalimpong_lava"
down_revision = "0028_associates"
branch_labels = None
depends_on = None

ACTOR = "migration:0029_kalimpong_lava"
OLD_NAME, NEW_NAME = "KALIMPONG-II", "Lava"
RENAME_REASON = ("LGD code 2862 is now named Lava (live LGD portal, checked 2026-09-27; IGOD; "
                 "2026-09-02 LGD snapshot via villagecodes.in). Kalimpong-II is absent from IGOD and the snapshot.")
RETIRE_REASON = ("Stale code: the live LGD portal returns no record for 7354 (checked 2026-09-27); "
                 "Lava is LGD code 2862. Superseded, not deleted.")

_blocks = sa.table(
    "territory_blocks",
    sa.column("id", sa.Integer), sa.column("lgd_code", sa.Integer),
    sa.column("name", sa.String), sa.column("is_active", sa.Boolean),
)
_audit = sa.table(
    "audit_logs",
    sa.column("entity_type", sa.String), sa.column("entity_id", sa.String), sa.column("action", sa.String),
    sa.column("performed_by", sa.String), sa.column("before_value", sa.JSON), sa.column("after_value", sa.JSON),
    sa.column("created_at", sa.DateTime(timezone=True)),
)
# Every column that can point at a block.
_REFERENCES = (
    ("associate_applications", "block_id"),
    ("preferred_associate_applications", "block_id"),
    ("preferred_associate_applications", "requested_block_id"),
    ("associates", "territory_block_id"),
    ("territory_seats", "block_id"),
)


def _row(bind, code):
    return bind.execute(sa.select(_blocks).where(_blocks.c.lgd_code == code)).first()


def _audit_entry(bind, row_id, action, before, after):
    bind.execute(_audit.insert().values(
        entity_type="territory_block", entity_id=str(row_id), action=action, performed_by=ACTOR,
        before_value=before, after_value=after, created_at=datetime.now(timezone.utc),
    ))


def _reference_count(bind, row_id) -> int:
    return sum(
        bind.execute(sa.text(f"SELECT count(*) FROM {table} WHERE {column} = :id"), {"id": row_id}).scalar()
        for table, column in _REFERENCES
    )


def upgrade() -> None:
    bind = op.get_bind()
    lava = _row(bind, 2862)
    if lava is not None and lava.name == OLD_NAME:
        bind.execute(_blocks.update().where(_blocks.c.id == lava.id).values(name=NEW_NAME))
        _audit_entry(bind, lava.id, "renamed", {"name": OLD_NAME},
                     {"name": NEW_NAME, "lgd_code": 2862, "reason": RENAME_REASON})
    stale = _row(bind, 7354)
    if stale is not None and stale.is_active:
        bind.execute(_blocks.update().where(_blocks.c.id == stale.id).values(is_active=False))
        _audit_entry(bind, stale.id, "deactivated", {"is_active": True},
                     {"is_active": False, "lgd_code": 7354, "reason": RETIRE_REASON,
                      "existing_references": _reference_count(bind, stale.id)})


def downgrade() -> None:
    # Reverses only what upgrade() changed; the audit history is kept, and the
    # reversal is audited too.
    bind = op.get_bind()
    lava = _row(bind, 2862)
    if lava is not None and lava.name == NEW_NAME:
        bind.execute(_blocks.update().where(_blocks.c.id == lava.id).values(name=OLD_NAME))
        _audit_entry(bind, lava.id, "renamed", {"name": NEW_NAME}, {"name": OLD_NAME, "reason": "downgrade"})
    stale = _row(bind, 7354)
    if stale is not None and not stale.is_active:
        bind.execute(_blocks.update().where(_blocks.c.id == stale.id).values(is_active=True))
        _audit_entry(bind, stale.id, "reactivated", {"is_active": False}, {"is_active": True, "reason": "downgrade"})
