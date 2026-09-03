"""Migration 0015_project_required — the Lead/Project backfill.

Run as a real subprocess against a throwaway sqlite file with the schema as it
existed BEFORE this migration (customers/projects/leads tables, some leads with
project_id NULL, some already linked) — not a fresh empty database, so this
actually exercises the backfill logic against pre-existing data, the same way
it would run against a real deployment's database. A subprocess (rather than
calling alembic's Python API in-process) is used because app.config.settings
reads DATABASE_URL once at import time; a fresh process is the simplest way to
point a real alembic run at a scratch file without fighting that.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _run_alembic(*args: str, db_path: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{db_path}"}
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic/alembic.ini", *args],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True,
    )


def _seed_pre_migration_db(db_path: Path) -> None:
    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE customers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email VARCHAR NOT NULL UNIQUE,
            name VARCHAR
        );
        CREATE TABLE projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER NOT NULL,
            created_at DATETIME,
            updated_at DATETIME
        );
        CREATE TABLE leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER NOT NULL,
            project_id INTEGER
        );
        INSERT INTO customers (email, name) VALUES ('a@example.com', 'A'), ('b@example.com', 'B');
        -- Two orphaned leads for the SAME customer: each must get its OWN project.
        INSERT INTO leads (customer_id, project_id) VALUES (1, NULL);
        INSERT INTO leads (customer_id, project_id) VALUES (1, NULL);
        -- One lead that already has a project: must be left untouched.
        INSERT INTO projects (customer_id, created_at, updated_at) VALUES (2, '2026-01-01', '2026-01-01');
        INSERT INTO leads (customer_id, project_id) VALUES (2, 1);
        CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL);
        INSERT INTO alembic_version VALUES ('0014_agent_governance_overrides');
        """
    )
    conn.commit()
    conn.close()


@pytest.fixture
def pre_migration_db(tmp_path):
    db_path = tmp_path / "pre_migration.db"
    _seed_pre_migration_db(db_path)
    return db_path


class TestProjectBackfillMigration:
    def test_upgrade_backfills_orphaned_leads_with_distinct_projects(self, pre_migration_db):
        result = _run_alembic("upgrade", "0015_project_required", db_path=pre_migration_db)
        assert result.returncode == 0, result.stderr

        import sqlite3
        conn = sqlite3.connect(pre_migration_db)
        rows = conn.execute("SELECT id, customer_id, project_id FROM leads ORDER BY id").fetchall()
        conn.close()

        by_id = {r[0]: r for r in rows}
        # The two previously-orphaned leads (same customer) each got their OWN
        # project — never sharing one, and never left null.
        assert by_id[1][2] is not None
        assert by_id[2][2] is not None
        assert by_id[1][2] != by_id[2][2]
        # The lead that already had a project is untouched.
        assert by_id[3][2] == 1

    def test_upgrade_makes_project_id_not_null(self, pre_migration_db):
        result = _run_alembic("upgrade", "0015_project_required", db_path=pre_migration_db)
        assert result.returncode == 0, result.stderr

        import sqlite3
        conn = sqlite3.connect(pre_migration_db)
        schema = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='leads'"
        ).fetchone()[0]
        conn.close()
        assert "project_id INTEGER NOT NULL" in schema

    def test_no_data_lost_on_downgrade(self, pre_migration_db):
        _run_alembic("upgrade", "0015_project_required", db_path=pre_migration_db)
        result = _run_alembic("downgrade", "0014_agent_governance_overrides", db_path=pre_migration_db)
        assert result.returncode == 0, result.stderr

        import sqlite3
        conn = sqlite3.connect(pre_migration_db)
        rows = conn.execute("SELECT id, customer_id, project_id FROM leads ORDER BY id").fetchall()
        project_count = conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
        conn.close()

        # The constraint relaxes back to nullable, but the backfilled project_id
        # values — and the Project rows themselves — are NOT reverted or dropped.
        assert all(r[2] is not None for r in rows)
        assert project_count == 3  # 1 pre-existing + 2 backfilled
