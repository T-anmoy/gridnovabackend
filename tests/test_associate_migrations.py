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
        -- Stand-ins for tables later revisions reference by foreign key (a real
        -- database has the full tables; SQLite's batch mode reflects the targets).
        CREATE TABLE customers (id INTEGER PRIMARY KEY);
        CREATE TABLE projects (id INTEGER PRIMARY KEY);
        CREATE TABLE leads (id INTEGER PRIMARY KEY);
        CREATE TABLE proposal_inputs (id INTEGER PRIMARY KEY);
        CREATE TABLE proposals (id INTEGER PRIMARY KEY);
        CREATE TABLE equipment (id INTEGER PRIMARY KEY, brand VARCHAR);
        INSERT INTO equipment (id, brand) VALUES (1, 'Existing');
        CREATE TABLE audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, entity_type VARCHAR NOT NULL, entity_id VARCHAR NOT NULL,
            action VARCHAR NOT NULL, performed_by VARCHAR NOT NULL, before_value JSON, after_value JSON,
            created_at DATETIME);
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
        for path in (REPO_ROOT / "alembic" / "versions").glob("00[23][0-9]_*.py"):
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
        CREATE TABLE IF NOT EXISTS audit_logs (
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


class TestOnboardingTables:
    """0030_associate_onboarding on SQLite: tables in, and cleanly out again."""

    def test_upgrade_and_downgrade(self, db_at_0025):
        assert _run_alembic("upgrade", "0030_associate_onboarding", db_path=db_at_0025).returncode == 0
        conn = sqlite3.connect(db_at_0025)
        try:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            assert {"customer_onboardings", "onboarding_attribution_flags"} <= tables
        finally:
            conn.close()
        result = _run_alembic("downgrade", "0029_kalimpong_lava", db_path=db_at_0025)
        assert result.returncode == 0, result.stderr
        conn = sqlite3.connect(db_at_0025)
        try:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            assert not tables & {"customer_onboardings", "onboarding_attribution_flags"}
        finally:
            conn.close()


class TestScoringColumns:
    def test_upgrade_and_downgrade(self, db_at_0025):
        assert _run_alembic("upgrade", "0031_onboarding_scoring", db_path=db_at_0025).returncode == 0
        conn = sqlite3.connect(db_at_0025)
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(customer_onboardings)")}
            assert {"score", "score_band", "score_factors", "response_due_at", "escalation_level"} <= cols
        finally:
            conn.close()
        result = _run_alembic("downgrade", "0030_associate_onboarding", db_path=db_at_0025)
        assert result.returncode == 0, result.stderr
        conn = sqlite3.connect(db_at_0025)
        try:
            assert "score" not in {r[1] for r in conn.execute("PRAGMA table_info(customer_onboardings)")}
        finally:
            conn.close()


class TestProjectStagesTable:
    def test_upgrade_and_downgrade(self, db_at_0025):
        assert _run_alembic("upgrade", "0032_project_stages", db_path=db_at_0025).returncode == 0
        conn = sqlite3.connect(db_at_0025)
        try:
            assert "project_stage_events" in {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
        finally:
            conn.close()
        result = _run_alembic("downgrade", "0031_onboarding_scoring", db_path=db_at_0025)
        assert result.returncode == 0, result.stderr
        conn = sqlite3.connect(db_at_0025)
        try:
            assert "project_stage_events" not in {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
        finally:
            conn.close()



class TestDrafterAndAgreements:
    def test_drafted_by_backfilled_from_the_audit_log(self, db_at_0025):
        assert _run_alembic("upgrade", "0032_project_stages", db_path=db_at_0025).returncode == 0
        conn = sqlite3.connect(db_at_0025)
        conn.executescript("""
            INSERT INTO proposals (id) VALUES (1), (2);
            INSERT INTO audit_logs (entity_type, entity_id, action, performed_by)
                VALUES ('proposal', '1', 'draft_created', 'emp-sales-1'),
                       ('proposal', '1', 'status_changed_to_approved', 'emp-admin-1');
        """)
        conn.commit()
        conn.close()
        result = _run_alembic("upgrade", "0033_drafter_and_agreements", db_path=db_at_0025)
        assert result.returncode == 0, result.stderr
        conn = sqlite3.connect(db_at_0025)
        try:
            assert dict(conn.execute("SELECT id, drafted_by FROM proposals")) == {1: "emp-sales-1", 2: None}
            assert "customer_agreements" in {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
        finally:
            conn.close()
        result = _run_alembic("downgrade", "0032_project_stages", db_path=db_at_0025)
        assert result.returncode == 0, result.stderr


class TestEstimatesAndPreparedStage:
    def test_upgrade_and_downgrade(self, db_at_0025):
        assert _run_alembic("upgrade", "0034_estimates_and_prepared", db_path=db_at_0025).returncode == 0
        conn = sqlite3.connect(db_at_0025)
        try:
            assert "indicative_estimates" in {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
        finally:
            conn.close()
        result = _run_alembic("downgrade", "0033_drafter_and_agreements", db_path=db_at_0025)
        assert result.returncode == 0, result.stderr


class TestDraftFailureColumns:
    def test_upgrade_and_downgrade(self, db_at_0025):
        assert _run_alembic("upgrade", "0035_formal_draft_failure", db_path=db_at_0025).returncode == 0
        conn = sqlite3.connect(db_at_0025)
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(customer_onboardings)")}
            assert {"formal_draft_failed_at", "formal_draft_failure"} <= cols
        finally:
            conn.close()
        result = _run_alembic("downgrade", "0034_estimates_and_prepared", db_path=db_at_0025)
        assert result.returncode == 0, result.stderr


class TestPlaceholderRegistry:
    def test_upgrade_and_downgrade(self, db_at_0025):
        assert _run_alembic("upgrade", "0036_placeholder_registry", db_path=db_at_0025).returncode == 0
        conn = sqlite3.connect(db_at_0025)
        try:
            assert "placeholder_resolutions" in {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
            # Existing catalogue rows are real, not dummies.
            assert conn.execute("SELECT is_placeholder FROM equipment WHERE id = 1").fetchone() == (0,)
        finally:
            conn.close()
        result = _run_alembic("downgrade", "0035_formal_draft_failure", db_path=db_at_0025)
        assert result.returncode == 0, result.stderr


class TestExecutionStages:
    def test_upgrade_backfills_projects_past_the_agreement_and_downgrades(self, db_at_0025):
        assert _run_alembic("upgrade", "0036_placeholder_registry", db_path=db_at_0025).returncode == 0
        conn = sqlite3.connect(db_at_0025)
        try:
            conn.executescript(
                """
                INSERT INTO territory_states (id, lgd_code, name, iso_code, is_active, created_at)
                    VALUES (1, 19, 'WEST BENGAL', 'WB', 1, '2026-09-01');
                INSERT INTO territory_districts (id, state_id, lgd_code, name, is_active, created_at)
                    VALUES (1, 1, 664, 'Alipurduar', 1, '2026-09-01');
                INSERT INTO projects (id) VALUES (1), (2), (3);
                """)
            conn.execute("PRAGMA foreign_keys = OFF")
            for pk, kind, capability, level in ((1, "associate", "execution", None), (2, "associate", "sales", None),
                                                (3, "preferred_associate", None, "district_head")):
                _insert(conn, "associates", id=pk, associate_id=f"GNA-WB-26-{pk:05d}", associate_type=kind,
                        status="active", capability=capability, level=level, state_id=1,
                        **({"associate_application_id": pk} if kind == "associate" else {"preferred_application_id": pk}))
                _insert(conn, "customer_onboardings", id=pk, customer_id=pk, lead_id=pk, project_id=pk,
                        proposal_inputs_id=pk, owner_associate_id=pk, status="submitted", state_id=1, district_id=1)
            # Projects 1 and 3 are past the agreement; project 2 is not.
            for project, stage in ((1, "agreement_signed"), (2, "accepted"), (3, "agreement_signed")):
                conn.execute("INSERT INTO project_stage_events (project_id, stage, recorded_by, recorded_at, "
                             "evidence) VALUES (?, ?, 'x', '2026-09-10 10:00:00', '{}')", (project, stage))
            conn.commit()
        finally:
            conn.close()
        result = _run_alembic("upgrade", "0037_execution_stages", db_path=db_at_0025)
        assert result.returncode == 0, result.stderr
        conn = sqlite3.connect(db_at_0025)
        try:
            rows = conn.execute("SELECT project_id, onboarding_id, execution_mode, started_at, files "
                                "FROM project_executions ORDER BY project_id").fetchall()
            assert rows == [(1, 1, "associate", "2026-09-10 10:00:00", "{}"),
                            (3, 3, "associate", "2026-09-10 10:00:00", "{}")]
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
            assert {"stage_submissions", "photo_fingerprints", "uq_stage_submissions_one_pending"} <= tables
        finally:
            conn.close()
        result = _run_alembic("downgrade", "0036_placeholder_registry", db_path=db_at_0025)
        assert result.returncode == 0, result.stderr


def _insert(conn, table: str, **values) -> None:
    """Insert a row, filling every other NOT NULL column that has no default with a
    plain value of its type — these tests only care about the columns they name."""
    for _, name, ctype, notnull, default, pk in conn.execute(f"PRAGMA table_info({table})").fetchall():
        if name in values or not notnull or default is not None or pk:
            continue
        ctype = (ctype or "").upper()
        values[name] = 0 if ("INT" in ctype or "BOOL" in ctype) else (
            "2026-09-01 00:00:00" if ("DATE" in ctype or "TIME" in ctype) else f"x-{name}-{values.get('id', 0)}")
    keys = list(values)
    conn.execute(f"INSERT INTO {table} ({', '.join(keys)}) VALUES ({', '.join('?' * len(keys))})",
                 [values[k] for k in keys])


class TestInstalledUnits:
    def test_upgrade_and_downgrade(self, db_at_0025):
        assert _run_alembic("upgrade", "0038_installed_units", db_path=db_at_0025).returncode == 0
        conn = sqlite3.connect(db_at_0025)
        try:
            indexes = {r[1]: r[2] for r in conn.execute("PRAGMA index_list(installed_units)")}
            # The system-wide uniqueness on the normalised serial is in the database itself.
            unique = [name for name, is_unique in indexes.items() if is_unique]
            cols = [conn.execute(f"PRAGMA index_info({name})").fetchall()[0][2] for name in unique]
            assert "serial_normalized" in cols
            exec_cols = {r[1] for r in conn.execute("PRAGMA table_info(project_executions)")}
            assert {"dcr_alerted_at", "dcr_override_reason", "dcr_override_by", "dcr_override_at"} <= exec_cols
        finally:
            conn.close()
        result = _run_alembic("downgrade", "0037_execution_stages", db_path=db_at_0025)
        assert result.returncode == 0, result.stderr


class TestPaymentsAndReceipts:
    def test_upgrade_and_downgrade(self, db_at_0025):
        assert _run_alembic("upgrade", "0040_receipts", db_path=db_at_0025).returncode == 0
        conn = sqlite3.connect(db_at_0025)
        try:
            names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
            assert {"payment_milestones", "payments", "payment_ledger_entries", "receipt_sequences",
                    "payment_receipts", "uq_payments_instrument", "payment_ledger_no_update",
                    "payment_ledger_no_delete"} <= names
        finally:
            conn.close()
        result = _run_alembic("downgrade", "0038_installed_units", db_path=db_at_0025)
        assert result.returncode == 0, result.stderr
        conn = sqlite3.connect(db_at_0025)
        try:
            assert "payments" not in {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
        finally:
            conn.close()


class TestPhase4aFollowups:
    def test_upgrade_backfills_voided_serials_and_downgrades(self, db_at_0025):
        assert _run_alembic("upgrade", "0040_receipts", db_path=db_at_0025).returncode == 0
        conn = sqlite3.connect(db_at_0025)
        try:
            conn.execute("PRAGMA foreign_keys = OFF")
            _insert(conn, "installed_units", id=1, project_id=1, onboarding_id=1, category="other",
                    serial_number="  ｓｎ-77 a ", serial_normalized=None, status="voided")
            _insert(conn, "installed_units", id=2, project_id=1, onboarding_id=1, category="other",
                    serial_number="LIVE-1", serial_normalized="LIVE-1", status="entered")
            conn.commit()
        finally:
            conn.close()
        result = _run_alembic("upgrade", "0041_phase4a_followups", db_path=db_at_0025)
        assert result.returncode == 0, result.stderr
        conn = sqlite3.connect(db_at_0025)
        try:
            # The raw text is untouched; the voided row remembers its normalised form.
            rows = conn.execute("SELECT id, serial_number, serial_normalized, voided_serial_normalized "
                                "FROM installed_units ORDER BY id").fetchall()
            assert rows == [(1, "  ｓｎ-77 a ", None, "SN-77A"), (2, "LIVE-1", "LIVE-1", None)]
            for table in ("payments", "payment_milestones"):
                info = {r[1]: r for r in conn.execute(f"PRAGMA table_info({table})")}
                assert info["onboarding_id"][3] == 0 and info["project_id"][3] == 1   # nullable / required
            assert "previous_payment_id" in {r[1] for r in conn.execute("PRAGMA table_info(payments)")}
            sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'uq_payments_instrument'").fetchone()[0]
            assert "dishonoured" in sql
        finally:
            conn.close()
        result = _run_alembic("downgrade", "0040_receipts", db_path=db_at_0025)
        assert result.returncode == 0, result.stderr
        conn = sqlite3.connect(db_at_0025)
        try:
            info = {r[1]: r for r in conn.execute("PRAGMA table_info(payments)")}
            assert info["onboarding_id"][3] == 1 and "previous_payment_id" not in info
        finally:
            conn.close()


class TestSiteLocation:
    def test_upgrade_backfills_legacy_rows_and_downgrades(self, db_at_0025):
        assert _run_alembic("upgrade", "0041_phase4a_followups", db_path=db_at_0025).returncode == 0
        conn = sqlite3.connect(db_at_0025)
        try:
            conn.execute("PRAGMA foreign_keys = OFF")
            _insert(conn, "customer_onboardings", id=1, lead_id=1, customer_id=1, project_id=1, proposal_inputs_id=1,
                    owner_associate_id=1, status="submitted", state_id=1, district_id=1, customer_mobile="9830000001",
                    site_latitude=26.49, site_longitude=89.52)
            _insert(conn, "customer_onboardings", id=2, lead_id=2, customer_id=1, project_id=1, proposal_inputs_id=2,
                    owner_associate_id=1, status="draft", state_id=1, district_id=1, customer_mobile="9830000002")
            conn.commit()
        finally:
            conn.close()
        result = _run_alembic("upgrade", "0042_site_location", db_path=db_at_0025)
        assert result.returncode == 0, result.stderr
        conn = sqlite3.connect(db_at_0025)
        try:
            rows = conn.execute("SELECT id, site_location_source, site_location_verified, site_location_confirmed_at "
                                "FROM customer_onboardings ORDER BY id").fetchall()
            assert rows == [(1, "unknown_legacy", 0, None), (2, None, 0, None)]
            # Coordinates without a source are refused by the database.
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute("UPDATE customer_onboardings SET site_latitude = 26.5, site_longitude = 89.5 WHERE id = 2")
        finally:
            conn.close()
        result = _run_alembic("downgrade", "0041_phase4a_followups", db_path=db_at_0025)
        assert result.returncode == 0, result.stderr
        conn = sqlite3.connect(db_at_0025)
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(customer_onboardings)")}
            assert "site_location_source" not in cols and "site_latitude" in cols
            assert conn.execute("SELECT site_latitude FROM customer_onboardings WHERE id = 1").fetchone() == (26.49,)
        finally:
            conn.close()


class TestUnmatchedUnitsMigration:
    def test_upgrade_and_downgrade(self, db_at_0025):
        assert _run_alembic("upgrade", "0042_site_location", db_path=db_at_0025).returncode == 0
        result = _run_alembic("upgrade", "0043_unmatched_units", db_path=db_at_0025)
        assert result.returncode == 0, result.stderr
        conn = sqlite3.connect(db_at_0025)
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(installed_units)")}
            assert {"brand_as_printed", "model_as_printed"} <= cols
            sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'installed_units'").fetchone()[0]
            assert "ck_installed_units_make_known" in sql and "ck_installed_units_equipment_required" not in sql
            conn.execute("PRAGMA foreign_keys = OFF")
            # An unmatched module needs both printed fields.
            with pytest.raises(sqlite3.IntegrityError):
                _insert(conn, "installed_units", id=1, project_id=1, onboarding_id=1, category="module",
                        serial_number="S1", serial_normalized="S1", status="entered", brand_as_printed="B")
            _insert(conn, "installed_units", id=2, project_id=1, onboarding_id=1, category="module",
                    serial_number="S2", serial_normalized="S2", status="entered", brand_as_printed="B",
                    model_as_printed="M")
            conn.commit()
        finally:
            conn.close()
        # The old constraint cannot come back while an unmatched unit exists — by design.
        assert _run_alembic("downgrade", "0042_site_location", db_path=db_at_0025).returncode != 0
        conn = sqlite3.connect(db_at_0025)
        try:
            # SQLite DDL is not transactional: the refused batch copy leaves its temp table.
            conn.execute("DROP TABLE IF EXISTS _alembic_tmp_installed_units")
            conn.execute("UPDATE installed_units SET category = 'other' WHERE id = 2")
            conn.commit()
        finally:
            conn.close()
        result = _run_alembic("downgrade", "0042_site_location", db_path=db_at_0025)
        assert result.returncode == 0, result.stderr
