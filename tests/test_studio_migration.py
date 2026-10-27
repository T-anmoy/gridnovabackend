"""BE-18: migration 0057_proposal_studio up and down, on SQLite and (with TEST_DATABASE_URL)
PostgreSQL. Run as subprocesses like tests/test_migration_truth.py."""
from __future__ import annotations

from sqlalchemy import create_engine, inspect, text

from tests.test_bootstrap_db import _run_bootstrap, empty_database_url  # noqa: F401 — the fixture
from tests.test_migration_truth import _alembic, _drift

BEFORE = "0056_access_control"
TABLES = {"studio_customers", "studio_proposals", "studio_proposal_versions", "studio_events", "studio_library_items"}


def test_up_down_up(empty_database_url):
    assert _run_bootstrap(empty_database_url).returncode == 0
    engine = create_engine(empty_database_url)
    try:
        assert TABLES <= set(inspect(engine).get_table_names())
        down = _alembic(empty_database_url, "downgrade", BEFORE)
        assert down.returncode == 0, down.stderr
        engine.dispose()
        assert not TABLES & set(inspect(engine).get_table_names())
        up = _alembic(empty_database_url, "upgrade", "head")
        assert up.returncode == 0, up.stderr
        engine.dispose()
        assert TABLES <= set(inspect(engine).get_table_names())
        indexes = {ix["name"] for ix in inspect(engine).get_indexes("studio_proposal_versions")}
        assert {"uq_studio_versions_reference", "ix_studio_versions_status_valid"} <= indexes
        assert _drift(engine) == []
        if engine.dialect.name == "postgresql":
            with engine.connect() as conn:
                labels = set(conn.execute(text("SELECT unnest(enum_range(NULL::employeerole))::text")).scalars())
                sources = set(conn.execute(text("SELECT unnest(enum_range(NULL::leadsource))::text")).scalars())
            assert "proposal_desk" in labels and "proposal_studio" in sources
    finally:
        engine.dispose()


def test_downgrade_refused_while_someone_holds_the_role(empty_database_url):
    assert _run_bootstrap(empty_database_url).returncode == 0
    engine = create_engine(empty_database_url)
    try:
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO employees (id, name, email, role, is_active, google_calendar_connected, "
                              "created_at) VALUES ('emp-desk', 'Desk', 'desk@example.test', 'sales', true, false, "
                              "'2026-10-27 00:00:00+00')"))
            conn.execute(text("INSERT INTO employee_roles (employee_id, role, granted_by, granted_at) VALUES "
                              "('emp-desk', 'proposal_desk', 'test', '2026-10-27 00:00:00+00')"))
        down = _alembic(empty_database_url, "downgrade", BEFORE)
        assert down.returncode != 0 and "proposal_desk" in down.stderr
        assert TABLES <= set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
