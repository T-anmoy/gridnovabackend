"""BE-4: migration 0048 encrypts the PAN of applications that already exist (development
and staging data), and its downgrade decrypts it back. Run as subprocesses like
tests/test_migration_truth.py, on SQLite and (with TEST_DATABASE_URL) PostgreSQL."""
from __future__ import annotations

import os
import subprocess
import sys

from cryptography.fernet import Fernet
from sqlalchemy import create_engine, inspect, text

from tests.test_bootstrap_db import REPO_ROOT, _run_bootstrap, empty_database_url  # noqa: F401 — the fixture

BEFORE = "0047_be3_indexes"


def _alembic(database_url: str, *args: str, key: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "DATABASE_URL": database_url, "ENVIRONMENT": "development", "FERNET_ENCRYPTION_KEY": key}
    return subprocess.run([sys.executable, "-m", "alembic", "-c", "alembic/alembic.ini", *args],
                          cwd=REPO_ROOT, env=env, capture_output=True, text=True)


def _seed_application(engine) -> None:
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO territory_states (id, lgd_code, iso_code, name, is_active, created_at) "
                          "VALUES (1, 19, 'WB', 'West Bengal', true, '2026-10-01 00:00:00+00')"))
        conn.execute(text("INSERT INTO territory_districts (id, state_id, lgd_code, name, is_active, created_at) "
                          "VALUES (1, 1, 315, 'Kolkata', true, '2026-10-01 00:00:00+00')"))
        conn.execute(text(
            "INSERT INTO associate_applications (status, full_name, date_of_birth, address_line, state_id, "
            "district_id, pin_code, mobile, email, aadhaar_last4, pan, bank_account_encrypted, bank_account_last4, "
            "bank_ifsc, kyc_consent_version, kyc_consent_accepted_at, documents, submitted_at, status_changed_at, "
            "verification_call_completed, capability) VALUES ('submitted', 'A One', '1990-01-01', 'x', 1, 1, "
            "'700001', '9876543210', 'a@example.test', '1234', 'ABCPE1234F', 'enc', '9012', 'SBIN0001234', 'v', "
            "'2026-10-01 00:00:00+00', '{}', '2026-10-01 00:00:00+00', '2026-10-01 00:00:00+00', false, 'sales')"))


def test_existing_pans_are_encrypted_and_restored(empty_database_url):
    key = Fernet.generate_key().decode()
    assert _run_bootstrap(empty_database_url).returncode == 0
    down = _alembic(empty_database_url, "downgrade", BEFORE, key=key)
    assert down.returncode == 0, down.stderr
    engine = create_engine(empty_database_url)
    try:
        _seed_application(engine)
        refused = _alembic(empty_database_url, "upgrade", "head", key="")
        assert refused.returncode != 0 and "FERNET_ENCRYPTION_KEY" in refused.stderr
        if engine.dialect.name == "postgresql":   # transactional DDL: nothing half-done
            with engine.connect() as conn:
                assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == BEFORE
                assert conn.execute(text("SELECT pan FROM associate_applications")).scalar() == "ABCPE1234F"
        else:
            # SQLite runs migration DDL outside a transaction: start again from a clean 0047.
            engine.dispose()
            os.remove(empty_database_url.removeprefix("sqlite:///"))
            assert _run_bootstrap(empty_database_url).returncode == 0
            assert _alembic(empty_database_url, "downgrade", BEFORE, key=key).returncode == 0
            engine = create_engine(empty_database_url)
            _seed_application(engine)

        up = _alembic(empty_database_url, "upgrade", "head", key=key)
        assert up.returncode == 0, up.stderr
        columns = {c["name"] for c in inspect(engine).get_columns("associate_applications")}
        assert "pan" not in columns and {"pan_encrypted", "pan_last4"} <= columns
        with engine.connect() as conn:
            encrypted, last4 = conn.execute(text("SELECT pan_encrypted, pan_last4 FROM associate_applications")).one()
        assert "ABCPE1234F" not in encrypted and Fernet(key.encode()).decrypt(encrypted.encode()) == b"ABCPE1234F"
        assert last4 == "234F"

        down = _alembic(empty_database_url, "downgrade", BEFORE, key=key)
        assert down.returncode == 0, down.stderr
        engine.dispose()
        engine = create_engine(empty_database_url)
        with engine.connect() as conn:
            assert conn.execute(text("SELECT pan FROM associate_applications")).scalar() == "ABCPE1234F"
        assert "commission_entries" not in inspect(engine).get_table_names()
    finally:
        engine.dispose()
