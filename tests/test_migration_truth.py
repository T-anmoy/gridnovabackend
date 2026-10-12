"""T2 (BE-3): the schema the deploy builds is exactly the models, and the newest migrations
round-trip.

A fresh database is built the way the migration job builds it (scripts/bootstrap_db.py:
models + stamp head). The newest revisions are then downgraded and upgraded again, and
alembic's autogenerate comparison between the live database and Base.metadata must find
nothing — so every model change since BE-1 is in a migration and every migration is in
the models. PostgreSQL is the engine that matters (native enums, partial indexes,
CREATE INDEX CONCURRENTLY); it runs when TEST_DATABASE_URL is set. The SQLite variant
runs everywhere for the index round-trip.

Run as subprocesses, like tests/test_bootstrap_db.py, because app.config.settings reads
DATABASE_URL once at import time.
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect

from app.memory.db import Base
from tests.test_bootstrap_db import REPO_ROOT, _alembic_versions, _head_revision, _run_bootstrap, \
    empty_database_url  # noqa: F401 — the fixture

# The oldest revision whose downgrade + upgrade must reproduce the models exactly. Older
# revisions are history: the chain has no baseline and a fresh database never replays
# them (scripts/bootstrap_db.py).
ROUND_TRIP_FROM = "0044_site_survey"

BE3_INDEXES = {
    "leads": {"ix_leads_customer_id", "ix_leads_assigned_employee_id", "ix_leads_pipeline_stage_created_at"},
    "proposals": {"ix_proposals_lead_id_status", "ix_proposals_date_created"},
    "audit_logs": {"ix_audit_logs_entity_type_entity_id", "ix_audit_logs_created_at"},
    "timeline_events": {"ix_timeline_events_lead_id_created_at"},
    "conversation_messages": {"ix_conversation_messages_lead_id_created_at"},
    "meetings": {"ix_meetings_lead_id", "ix_meetings_scheduled_at"},
}


def _alembic(database_url: str, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "DATABASE_URL": database_url, "ENVIRONMENT": "development"}
    return subprocess.run([sys.executable, "-m", "alembic", "-c", "alembic/alembic.ini", *args],
                          cwd=REPO_ROOT, env=env, capture_output=True, text=True)


def _index_names(engine) -> dict[str, set[str]]:
    insp = inspect(engine)
    return {table: {ix["name"] for ix in insp.get_indexes(table)} for table in BE3_INDEXES}


def _drift(engine) -> list:
    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"compare_type": True})
        return compare_metadata(ctx, Base.metadata)


def test_bootstrap_builds_exactly_the_models_and_the_new_migrations_round_trip(empty_database_url):
    result = _run_bootstrap(empty_database_url)
    assert result.returncode == 0, result.stderr
    engine = create_engine(empty_database_url)
    try:
        assert _drift(engine) == []
        assert all(BE3_INDEXES[t] <= names for t, names in _index_names(engine).items())

        down = _alembic(empty_database_url, "downgrade", "0046_survey_review")
        assert down.returncode == 0, down.stderr
        assert all(not (BE3_INDEXES[t] & names) for t, names in _index_names(engine).items())

        down = _alembic(empty_database_url, "downgrade", ROUND_TRIP_FROM)
        assert down.returncode == 0, down.stderr
        up = _alembic(empty_database_url, "upgrade", "head")
        assert up.returncode == 0, up.stderr
        assert _alembic_versions(engine) == [_head_revision()]
        assert _drift(engine) == [], "models and migrations disagree"
    finally:
        engine.dispose()


def test_the_migration_job_is_a_no_op_the_second_time(empty_database_url):
    assert _run_bootstrap(empty_database_url).returncode == 0
    again = _run_bootstrap(empty_database_url)
    assert again.returncode == 0, again.stderr
    assert "alembic upgrade head" in again.stdout
    engine = create_engine(empty_database_url)
    try:
        assert _drift(engine) == []
    finally:
        engine.dispose()


def test_postgres_runs_here_when_configured():
    """Not a check — a visible marker in the test output of which engine T2 ran on."""
    from tests.conftest import TEST_DATABASE_URL
    if not TEST_DATABASE_URL:
        pytest.skip("T2 on PostgreSQL needs TEST_DATABASE_URL (CI's test-postgres job sets it)")
