"""Add Project.reference_id (GNE-{STATE}-{TYPE}-{YEAR}-{SEQUENCE}, e.g.
GNE-WB-RES-2026-00125), Project.state, Project.project_family, and the
project_sequence_counters table backing the atomic per-bucket sequence — see
app/memory/reference_id.py for the generation logic new Projects use.

Backfills every existing Project with a reference_id, in creation-date order,
using the SAME bucket keying (state code, type code, year) and the SAME
counter table the application uses going forward — so a brand-new Project
created right after this migration continues the sequence rather than
colliding with a backfilled one. Deliberately reimplemented here rather than
imported from app.memory.reference_id, matching this repo's existing
migration convention of not depending on application code that will keep
changing after this migration is history.

Every pre-existing row lacks a state and project_family today (those columns
did not exist before this migration), so in practice almost all backfilled
IDs land in the "XX-GEN-{year}" bucket — an honest reflection of what this
data actually contains, not a bug in the backfill.

Revision ID: 0018_project_reference_id
Revises: 0017_proposal_auto_drafted_event
Create Date: 2026-09-04
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0018_project_reference_id"
down_revision = "0017_proposal_auto_drafted_event"
branch_labels = None
depends_on = None

_PROJECT_FAMILY = sa.Enum(
    "rooftop_solar", "ground_mounted", "government_institutional",
    "street_lighting", "water_pump", "om_service", "consulting",
    name="projectfamily",
)

_STATE_CODES = {"west bengal": "WB", "bihar": "BR"}
_TYPE_CODES = {
    "rooftop_solar": "ROF",
    "ground_mounted": "GRM",
    "government_institutional": "GOV",
    "street_lighting": "STL",
    "water_pump": "PMP",
    "om_service": "OMS",
    "consulting": "CON",
}
_UNKNOWN_STATE = "XX"
_UNKNOWN_TYPE = "GEN"


def _state_code(state) -> str:
    if not state or not str(state).strip():
        return _UNKNOWN_STATE
    normalized = str(state).strip().lower()
    if normalized in _STATE_CODES:
        return _STATE_CODES[normalized]
    fallback = str(state).strip()[:2].upper()
    return fallback or _UNKNOWN_STATE


def _type_code(project_family) -> str:
    if not project_family:
        return _UNKNOWN_TYPE
    return _TYPE_CODES.get(str(project_family), _UNKNOWN_TYPE)


_metadata = sa.MetaData()
_projects = sa.Table(
    "projects",
    _metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("reference_id", sa.String()),
    sa.Column("state", sa.String()),
    sa.Column("project_family", sa.String()),
    sa.Column("created_at", sa.DateTime(timezone=True)),
)
_counters = sa.Table(
    "project_sequence_counters",
    _metadata,
    sa.Column("counter_key", sa.String, primary_key=True),
    sa.Column("value", sa.Integer),
)


def upgrade() -> None:
    bind = op.get_bind()
    # projectfamily already exists (created by 0010_proposal_inputs, ProposalInputs'
    # own project_family column) — checkfirst keeps this safe to run either way.
    _PROJECT_FAMILY.create(bind, checkfirst=True)

    with op.batch_alter_table("projects") as batch:
        batch.add_column(sa.Column("reference_id", sa.String(), nullable=True))
        batch.add_column(sa.Column("state", sa.String(), nullable=True))
        batch.add_column(sa.Column("project_family", _PROJECT_FAMILY, nullable=True))

    op.create_table(
        "project_sequence_counters",
        sa.Column("counter_key", sa.String(), nullable=False),
        sa.Column("value", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("counter_key"),
    )

    # Backfill, in creation-date order, using the exact same atomic upsert the
    # application uses — so the counters this leaves behind are exactly what a
    # newly-created Project's own call to the same SQL would expect to find.
    rows = bind.execute(
        sa.select(_projects.c.id, _projects.c.state, _projects.c.project_family, _projects.c.created_at)
        .order_by(_projects.c.created_at, _projects.c.id)
    ).all()

    upsert_sql = sa.text(
        "INSERT INTO project_sequence_counters (counter_key, value) VALUES (:key, 1) "
        "ON CONFLICT(counter_key) DO UPDATE SET value = project_sequence_counters.value + 1 "
        "RETURNING value"
    )

    for project_id, state, project_family, created_at in rows:
        year = created_at.year if created_at is not None else 0
        st = _state_code(state)
        tp = _type_code(project_family)
        key = f"{st}-{tp}-{year}"
        seq = bind.execute(upsert_sql, {"key": key}).first()[0]
        reference_id = f"GNE-{st}-{tp}-{year}-{seq:05d}"
        bind.execute(
            _projects.update().where(_projects.c.id == project_id).values(reference_id=reference_id)
        )

    with op.batch_alter_table("projects") as batch:
        batch.alter_column("reference_id", existing_type=sa.String(), nullable=False)
        batch.create_unique_constraint("uq_projects_reference_id", ["reference_id"])
    op.create_index("ix_projects_reference_id", "projects", ["reference_id"])


def downgrade() -> None:
    op.drop_index("ix_projects_reference_id", table_name="projects")
    with op.batch_alter_table("projects") as batch:
        batch.drop_constraint("uq_projects_reference_id", type_="unique")
        batch.drop_column("reference_id")
        batch.drop_column("state")
        batch.drop_column("project_family")
    op.drop_table("project_sequence_counters")

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # Leave the projectfamily TYPE in place — 0010_proposal_inputs' own
        # column still depends on it.
        pass
