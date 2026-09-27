"""Migrations 0026-0028 (roles, territories, associates) against a real SQLite file.

Same approach as tests/test_migrations.py: a throwaway database carrying only the
pre-existing tables these revisions touch, stamped at the revision before them, and
a real `alembic upgrade` run in a subprocess (app.config.settings reads DATABASE_URL
once at import, so a fresh process is the simplest way to point alembic at it).

The PostgreSQL upgrade from the committed head is exercised separately against a
real server — see the Phase 2A report.
"""
import json
import os
import sqlite3
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


@pytest.fixture
def db_at_0025(tmp_path):
    db_path = tmp_path / "at_0025.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE employees (
            id VARCHAR PRIMARY KEY, name VARCHAR NOT NULL, email VARCHAR NOT NULL UNIQUE,
            role VARCHAR(13) NOT NULL, created_at DATETIME, is_active BOOLEAN NOT NULL DEFAULT 1,
            google_calendar_connected BOOLEAN NOT NULL DEFAULT 0,
            google_refresh_token TEXT, calendar_id VARCHAR
        );
        INSERT INTO employees (id, name, email, role) VALUES
            ('emp-sub', 'Sub', 'sub@x.test', 'subcontractor'),
            ('emp-adm', 'Adm', 'adm@x.test', 'admin');
        CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL);
        INSERT INTO alembic_version VALUES ('0025_lead_source_contact');
        """
    )
    conn.commit()
    conn.close()
    return db_path


def _roles(db_path: Path) -> dict:
    conn = sqlite3.connect(db_path)
    try:
        return dict(conn.execute("SELECT id, role FROM employees"))
    finally:
        conn.close()


class TestOperationsAccountsRoles:
    def test_upgrade_relabels_subcontractor_rows(self, db_at_0025):
        result = _run_alembic("upgrade", "0026_operations_accounts_roles", db_path=db_at_0025)
        assert result.returncode == 0, result.stderr
        assert _roles(db_at_0025) == {"emp-sub": "operations", "emp-adm": "admin"}

    def test_downgrade_restores_the_old_label(self, db_at_0025):
        assert _run_alembic("upgrade", "0026_operations_accounts_roles", db_path=db_at_0025).returncode == 0
        result = _run_alembic("downgrade", "0025_lead_source_contact", db_path=db_at_0025)
        assert result.returncode == 0, result.stderr
        assert _roles(db_at_0025) == {"emp-sub": "subcontractor", "emp-adm": "admin"}

    def test_revision_ids_fit_alembic_version_column(self):
        for path in (REPO_ROOT / "alembic" / "versions").glob("002[6-9]_*.py"):
            revision = path.stem
            assert len(revision) <= 32, revision


class TestTerritoryAndAssociateTables:
    def test_upgrade_to_head_and_back(self, db_at_0025):
        result = _run_alembic("upgrade", "head", db_path=db_at_0025)
        assert result.returncode == 0, result.stderr
        conn = sqlite3.connect(db_at_0025)
        try:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            assert {"territory_states", "territory_districts", "territory_blocks", "associate_applications",
                    "preferred_associate_applications", "associate_identity_claims", "associate_id_counters",
                    "associates", "territory_seats"} <= tables
            triggers = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
            assert "trg_associates_id_immutable" in triggers
            indexes = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
            assert {"uq_territory_seats_state_head", "uq_territory_seats_district_head",
                    "uq_territory_seats_holder"} <= indexes
        finally:
            conn.close()

        result = _run_alembic("downgrade", "0026_operations_accounts_roles", db_path=db_at_0025)
        assert result.returncode == 0, result.stderr
        conn = sqlite3.connect(db_at_0025)
        try:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            assert not tables & {"territory_states", "associates", "territory_seats"}
        finally:
            conn.close()


class TestKalimpongLava:
    """0029_kalimpong_lava: in-place rename of 2862 to Lava and retirement of 7354."""

    AUDIT_TABLE = """
        CREATE TABLE audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, entity_type VARCHAR NOT NULL, entity_id VARCHAR NOT NULL,
            action VARCHAR NOT NULL, performed_by VARCHAR NOT NULL, before_value JSON, after_value JSON,
            created_at DATETIME);
    """

    def _at_0028(self, db_path, seeded: bool):
        conn = sqlite3.connect(db_path)
        conn.executescript(self.AUDIT_TABLE)
        conn.commit()
        conn.close()
        assert _run_alembic("upgrade", "0028_associates", db_path=db_path).returncode == 0
        if seeded:
            conn = sqlite3.connect(db_path)
            conn.executescript("""
                INSERT INTO territory_states (id, lgd_code, iso_code, name, is_active, created_at)
                    VALUES (1, 19, 'WB', 'WEST BENGAL', 1, '2026-09-01');
                INSERT INTO territory_districts (id, state_id, lgd_code, name, is_active, created_at)
                    VALUES (1, 1, 702, 'KALIMPONG', 1, '2026-09-01');
                INSERT INTO territory_blocks (id, district_id, lgd_code, name, is_active, created_at) VALUES
                    (1, 1, 2861, 'KALIMPONG-I', 1, '2026-09-01'),
                    (2, 1, 2862, 'KALIMPONG-II', 1, '2026-09-01'),
                    (3, 1, 7354, 'Lava', 1, '2026-09-01');
            """)
            conn.commit()
            conn.close()

    def _state(self, db_path):
        conn = sqlite3.connect(db_path)
        try:
            blocks = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT lgd_code, name, is_active FROM territory_blocks")}
            audits = [(r[0], r[1], json.loads(r[2])) for r in conn.execute(
                "SELECT entity_id, action, after_value FROM audit_logs ORDER BY id")]
            return blocks, audits
        finally:
            conn.close()

    def test_upgrade_renames_2862_and_retires_7354(self, db_at_0025):
        self._at_0028(db_at_0025, seeded=True)
        result = _run_alembic("upgrade", "0029_kalimpong_lava", db_path=db_at_0025)
        assert result.returncode == 0, result.stderr
        blocks, audits = self._state(db_at_0025)
        assert blocks == {2861: ("KALIMPONG-I", 1), 2862: ("Lava", 1), 7354: ("Lava", 0)}  # nothing deleted
        assert [(a[0], a[1]) for a in audits] == [("2", "renamed"), ("3", "deactivated")]
        assert "2026-09-27" in audits[0][2]["reason"] and audits[1][2]["existing_references"] == 0

    def test_is_a_no_op_on_an_unseeded_database(self, db_at_0025):
        self._at_0028(db_at_0025, seeded=False)
        assert _run_alembic("upgrade", "0029_kalimpong_lava", db_path=db_at_0025).returncode == 0
        assert self._state(db_at_0025) == ({}, [])

    def test_running_the_seed_afterwards_changes_nothing_more(self, db_at_0025):
        """Deploy order is migrate, then seed: the corrected blocks.csv agrees with the
        migrated row, and the loader never re-activates or deletes 7354."""
        self._at_0028(db_at_0025, seeded=True)
        assert _run_alembic("upgrade", "0029_kalimpong_lava", db_path=db_at_0025).returncode == 0
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session
        from app.associates.territory_seed import seed_territories
        engine = create_engine(f"sqlite:///{db_at_0025}")
        with Session(engine) as db:
            seed_territories(db)
            db.commit()
        engine.dispose()
        blocks, _ = self._state(db_at_0025)
        assert blocks[2862] == ("Lava", 1) and blocks[7354] == ("Lava", 0)

    def test_downgrade_reverses_and_is_audited(self, db_at_0025):
        self._at_0028(db_at_0025, seeded=True)
        assert _run_alembic("upgrade", "0029_kalimpong_lava", db_path=db_at_0025).returncode == 0
        result = _run_alembic("downgrade", "0028_associates", db_path=db_at_0025)
        assert result.returncode == 0, result.stderr
        blocks, audits = self._state(db_at_0025)
        assert blocks == {2861: ("KALIMPONG-I", 1), 2862: ("KALIMPONG-II", 1), 7354: ("Lava", 1)}
        assert [a[1] for a in audits] == ["renamed", "deactivated", "renamed", "reactivated"]
