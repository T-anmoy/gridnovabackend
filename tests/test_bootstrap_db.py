"""scripts/bootstrap_db.py — bringing a brand-new database to the current schema.

Run as a real subprocess (like tests/test_migrations.py) because app.config.settings
reads DATABASE_URL once at import time. Always exercised against a throwaway SQLite
file; the PostgreSQL case — the one production actually depends on — runs only when
TEST_DATABASE_URL is set and skips with a reason otherwise.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

from app.memory.db import Base
from tests.conftest import TEST_DATABASE_URL

REPO_ROOT = Path(__file__).resolve().parents[1]


def _head_revision() -> str:
    cfg = Config(str(REPO_ROOT / "alembic" / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    return ScriptDirectory.from_config(cfg).get_current_head()


def _run_bootstrap(database_url: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "DATABASE_URL": database_url, "ENVIRONMENT": "development"}
    return subprocess.run(
        [sys.executable, "scripts/bootstrap_db.py"],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True,
    )


def _alembic_versions(engine) -> list[str]:
    with engine.connect() as conn:
        return [row[0] for row in conn.execute(text("SELECT version_num FROM alembic_version"))]


def _empty(engine) -> None:
    """Drop every model table and alembic_version — a genuinely empty database."""
    Base.metadata.drop_all(bind=engine)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS alembic_version"))


@pytest.fixture(params=["sqlite", "postgresql"])
def empty_database_url(request, tmp_path):
    if request.param == "sqlite":
        yield f"sqlite:///{tmp_path / 'bootstrap.db'}"
        return
    if not TEST_DATABASE_URL:
        pytest.skip("PostgreSQL bootstrap check needs TEST_DATABASE_URL (see tests/conftest.py)")
    engine = create_engine(TEST_DATABASE_URL)
    try:
        # The autouse _isolated_db fixture has already created the schema for this
        # test; start from nothing, and leave nothing behind for the next test.
        _empty(engine)
        yield TEST_DATABASE_URL
    finally:
        _empty(engine)
        engine.dispose()


def test_fresh_database_is_created_and_stamped_at_head(empty_database_url):
    result = _run_bootstrap(empty_database_url)
    assert result.returncode == 0, result.stderr

    engine = create_engine(empty_database_url)
    try:
        tables = set(inspect(engine).get_table_names())
        assert set(Base.metadata.tables) <= tables
        assert _alembic_versions(engine) == [_head_revision()]
    finally:
        engine.dispose()
    assert "creating" in result.stdout and "stamping" in result.stdout


def test_second_run_upgrades_instead_of_recreating(empty_database_url):
    assert _run_bootstrap(empty_database_url).returncode == 0
    result = _run_bootstrap(empty_database_url)
    assert result.returncode == 0, result.stderr
    assert "alembic upgrade head" in result.stdout
    assert "creating" not in result.stdout

    engine = create_engine(empty_database_url)
    try:
        assert _alembic_versions(engine) == [_head_revision()]
    finally:
        engine.dispose()


def test_unversioned_existing_schema_is_refused_and_left_untouched(empty_database_url):
    engine = create_engine(empty_database_url)
    try:
        Base.metadata.create_all(bind=engine)
        before = set(inspect(engine).get_table_names())

        result = _run_bootstrap(empty_database_url)

        assert result.returncode == 2
        assert "REFUSED" in result.stderr
        after = set(inspect(engine).get_table_names())
        assert after == before
        assert "alembic_version" not in after
    finally:
        engine.dispose()
