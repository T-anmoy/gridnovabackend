"""D4 (BE-3): two executions of the migration job serialise on PostgreSQL.

scripts/bootstrap_db.py holds a session advisory lock for its whole run; a second run
waits for it, then finds the schema at head and runs a no-op `alembic upgrade head`.
PostgreSQL only (SQLite has no advisory locks and no concurrent deploys)."""
from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest
from sqlalchemy import create_engine, text

from scripts.bootstrap_db import BOOTSTRAP_LOCK_KEY
from tests.conftest import TEST_DATABASE_URL
from tests.test_bootstrap_db import REPO_ROOT, _alembic_versions, _empty, _head_revision


@pytest.fixture
def empty_postgres():
    if not TEST_DATABASE_URL:
        pytest.skip("the migration-job lock needs TEST_DATABASE_URL (PostgreSQL)")
    engine = create_engine(TEST_DATABASE_URL)
    _empty(engine)
    try:
        yield engine
    finally:
        _empty(engine)
        engine.dispose()


def test_a_second_run_waits_for_the_first(empty_postgres):
    env = {**os.environ, "DATABASE_URL": TEST_DATABASE_URL, "ENVIRONMENT": "development"}
    with empty_postgres.connect() as holder:      # "the first run", holding the lock
        holder.execute(text("SELECT pg_advisory_lock(:k)"), {"k": BOOTSTRAP_LOCK_KEY})
        holder.commit()
        second = subprocess.Popen([sys.executable, "scripts/bootstrap_db.py"], cwd=REPO_ROOT, env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            time.sleep(3)
            assert second.poll() is None, "the second run did not wait for the lock"
        finally:
            holder.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": BOOTSTRAP_LOCK_KEY})
            holder.commit()
        out, err = second.communicate(timeout=120)
    assert second.returncode == 0, err
    assert "migration lock held" in out
    assert _alembic_versions(empty_postgres) == [_head_revision()]
