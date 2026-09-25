"""GET /api/v1/health/ready — 200 only when the database answers and its schema is at
this build's Alembic head; otherwise 503 naming the failing check."""
import pytest
from sqlalchemy import text

from app.api.routes.health import _script_directory
from app.main import app as fastapi_app
from app.memory.db import get_db

READY = "/api/v1/health/ready"


def _stamp(db_session, *revisions):
    db_session.execute(text("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32) NOT NULL)"))
    db_session.execute(text("DELETE FROM alembic_version"))
    for rev in revisions:
        db_session.execute(text("INSERT INTO alembic_version (version_num) VALUES (:v)"), {"v": rev})
    db_session.commit()


@pytest.fixture(autouse=True)
def _drop_version_table(db_session):
    yield
    db_session.rollback()
    db_session.execute(text("DROP TABLE IF EXISTS alembic_version"))
    db_session.commit()


def test_ready_at_head(client, db_session):
    _stamp(db_session, _script_directory().get_current_head())
    r = client.get(READY)
    assert r.status_code == 200
    assert r.json() == {"status": "ready", "db": "ok", "schema": "at_head"}


def test_schema_behind(client, db_session):
    _stamp(db_session, "0022_site_assessment_fields")
    r = client.get(READY)
    assert r.status_code == 503
    assert r.json() == {"status": "not_ready", "db": "ok", "schema": "behind"}


def test_schema_never_initialised(client):
    r = client.get(READY)
    assert r.status_code == 503 and r.json()["schema"] == "not_initialized"


def test_unknown_revision(client, db_session):
    _stamp(db_session, "9999_from_another_branch")
    r = client.get(READY)
    assert r.status_code == 503 and r.json()["schema"] == "unknown_revision"


def test_database_down(client):
    class _Down:
        def execute(self, *a, **k):
            raise RuntimeError("could not connect to server at 10.0.0.5:5432 password=hunter2")

        def close(self):
            pass

    fastapi_app.dependency_overrides[get_db] = lambda: _Down()
    r = client.get(READY)
    assert r.status_code == 503
    assert r.json() == {"status": "not_ready", "db": "unreachable", "schema": "unknown"}
    assert "hunter2" not in r.text and "10.0.0.5" not in r.text


def test_no_connection_details_ever(client, db_session):
    _stamp(db_session, _script_directory().get_current_head())
    body = client.get(READY).text
    for secret in ("sqlite", "postgresql", "password", "@", "DATABASE_URL"):
        assert secret not in body


def test_liveness_is_unchanged_and_cheap(client):
    r = client.get("/api/v1/health")
    assert r.status_code == 200 and set(r.json()) == {"status", "environment", "llm_mode"}
