"""scripts/dev_setup.py — one command from nothing to a working local database.

Run as a real subprocess against throwaway SQLite files (app.config.settings reads
DATABASE_URL once at import). The failure paths are unit-tested in-process.
"""
import hashlib
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_bootstrap_db import _head_revision

REPO_ROOT = Path(__file__).resolve().parents[1]


def _run(db: Path, *args, environment="development", script="scripts/dev_setup.py"):
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{db}", "ENVIRONMENT": environment}
    return subprocess.run([sys.executable, script, *args], cwd=REPO_ROOT, env=env, capture_output=True, text=True)


def _alembic(db: Path, *args):
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{db}", "ENVIRONMENT": "development"}
    return subprocess.run([sys.executable, "-m", "alembic", "-c", "alembic/alembic.ini", *args],
                          cwd=REPO_ROOT, env=env, capture_output=True, text=True)


def _scalar(db: Path, sql: str):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql).fetchone()[0]
    finally:
        conn.close()


def _digest(db: Path) -> str:
    return hashlib.sha256(db.read_bytes()).hexdigest()


def test_a_new_database_is_built_seeded_and_explained(tmp_path):
    db = tmp_path / "new.db"
    r = _run(db)
    assert r.returncode == 0, r.stderr + r.stdout
    out = r.stdout
    assert f"schema (new database) → {_head_revision()}" in out
    assert "2 states, 61 districts, 879 blocks" in out
    assert "uvicorn app.main:app --reload --port 8000" in out
    assert "NEXT_PUBLIC_API_URL=http://localhost:8000" in out
    assert _scalar(db, "SELECT version_num FROM alembic_version") == _head_revision()
    assert _scalar(db, "SELECT count(*) FROM territory_states WHERE name IN ('BIHAR', 'WEST BENGAL') "
                       "OR name IN ('Bihar', 'West Bengal')") == 2
    assert _scalar(db, "SELECT count(*) FROM equipment") == 0          # no dummy data unless asked


def test_it_is_safe_to_run_again(tmp_path):
    db = tmp_path / "again.db"
    assert _run(db).returncode == 0
    r = _run(db)
    assert r.returncode == 0, r.stderr
    assert f"schema {_head_revision()} → {_head_revision()}" in r.stdout
    assert _scalar(db, "SELECT count(*) FROM territory_states") == 2     # upserted, not duplicated


def test_dummy_data_is_optional_and_flagged(tmp_path):
    db = tmp_path / "dummy.db"
    assert _run(db, "--dummy-data").returncode == 0
    assert _scalar(db, "SELECT count(*) FROM equipment WHERE is_placeholder = 1") == 5
    assert _run(db, "--dummy-data").returncode == 0                       # idempotent
    assert _scalar(db, "SELECT count(*) FROM equipment") == 5


def test_a_database_behind_the_schema_is_upgraded_and_it_says_what_ran(tmp_path):
    db = tmp_path / "behind.db"
    assert _run(db).returncode == 0
    assert _alembic(db, "downgrade", "0040_receipts").returncode == 0
    assert _scalar(db, "SELECT version_num FROM alembic_version") == "0040_receipts"
    r = _run(db)
    assert r.returncode == 0, r.stderr + r.stdout
    assert f"schema 0040_receipts → {_head_revision()}" in r.stdout
    assert "existing schema at 0040_receipts — running alembic upgrade head" in r.stdout
    assert _scalar(db, "SELECT version_num FROM alembic_version") == _head_revision()


def test_a_database_the_app_created_itself_is_left_alone_with_instructions(tmp_path):
    """uvicorn's own create_all leaves tables with no recorded revision: bootstrap refuses,
    and dev_setup must stop without touching the file."""
    db = tmp_path / "unversioned.db"
    from sqlalchemy import create_engine
    import app.memory.models  # noqa: F401
    from app.memory.db import Base
    engine = create_engine(f"sqlite:///{db}")
    Base.metadata.create_all(bind=engine)
    engine.dispose()
    before = _digest(db)
    r = _run(db)
    assert r.returncode == 2
    assert "STOPPED" in r.stderr and "has NOT been deleted" in r.stderr
    assert f"cp {db} {db}.bak-" in r.stderr and f"mv {db} {db}.old-" in r.stderr
    assert "NEXT_PUBLIC_API_URL" not in r.stdout                           # no "ready" banner
    assert db.exists() and _digest(db) == before                           # byte-for-byte untouched


def test_it_refuses_in_production_and_creates_nothing(tmp_path):
    db = tmp_path / "prod.db"
    r = _run(db, environment="production")
    assert r.returncode == 2 and "REFUSED" in r.stderr and "production" in r.stderr
    assert not db.exists()


def test_unknown_options_are_refused(tmp_path):
    r = _run(tmp_path / "x.db", "--nuke")
    assert r.returncode == 2 and "usage" in r.stderr and not (tmp_path / "x.db").exists()


class TestFailurePaths:
    """In-process: a failing step stops with safe instructions and never deletes anything."""

    @pytest.fixture
    def dev_setup(self, monkeypatch, tmp_path):
        from app.config import settings
        from scripts import dev_setup as module
        db = tmp_path / "keep.db"
        db.write_bytes(b"precious")
        monkeypatch.setattr(settings, "database_url", f"sqlite:///{db}")
        monkeypatch.setattr(module, "_revision", lambda url: "0030_example")
        return module, db

    def test_a_failing_upgrade_prints_safe_instructions(self, dev_setup, monkeypatch, capsys):
        module, db = dev_setup

        def boom(url):
            raise RuntimeError("migration 0031 exploded")
        monkeypatch.setattr(module.bootstrap_db, "bootstrap", boom)
        assert module.main([]) == 1
        err = capsys.readouterr().err
        assert "migration 0031 exploded" in err and "NOT been deleted" in err
        assert f"cp {db} {db}.bak-" in err and "alembic -c alembic/alembic.ini upgrade head" in err
        assert db.read_bytes() == b"precious"

    def test_a_failing_territory_load_stops_before_dummy_data(self, dev_setup, monkeypatch, capsys):
        module, db = dev_setup
        monkeypatch.setattr(module.bootstrap_db, "bootstrap", lambda url: 0)
        monkeypatch.setattr(module.seed_territories, "main", lambda url: 2)
        called = []
        monkeypatch.setattr(module.seed_dummy_data, "main", lambda: called.append(1) or 0)
        assert module.main(["--dummy-data"]) == 1 and not called
        assert "territory data could not be loaded" in capsys.readouterr().err
        assert db.read_bytes() == b"precious"

    def test_production_is_refused_before_anything_runs(self, dev_setup, monkeypatch, capsys):
        from app.config import settings
        module, _ = dev_setup
        monkeypatch.setattr(settings, "environment", "production")
        monkeypatch.setattr(module.bootstrap_db, "bootstrap", lambda url: pytest.fail("must not run"))
        assert module.main([]) == 2 and "REFUSED" in capsys.readouterr().err
