"""BE-15: an unhandled 500 reaches the browser as a readable error, not a network failure.

The generic 500 used to leave the app from outside CORSMiddleware, so it carried no
Access-Control-Allow-Origin. UnhandledErrorMiddleware now sends it from inside CORS."""
import logging

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app.config import settings
from app.main import app
from app.security import error_responses

ALLOWED = "http://localhost:3000"
GENERIC = {"detail": "An internal error occurred."}


@pytest.fixture
def boom_client():
    def boom():
        raise RuntimeError("secret internal text /srv/app/x.py")

    def behind():
        raise OperationalError("SELECT leads.referred_by_associate_id", {},
                               Exception("no such column: leads_1.referred_by_associate_id"))

    app.add_api_route("/__be15/boom", boom)
    app.add_api_route("/__be15/behind", behind)
    error_responses.reset_schema_log()
    try:
        yield TestClient(app, raise_server_exceptions=False)
    finally:
        app.router.routes[:] = [r for r in app.router.routes if not getattr(r, "path", "").startswith("/__be15")]
        error_responses.reset_schema_log()


def test_500_carries_cors_for_an_allowed_origin(boom_client):
    r = boom_client.get("/__be15/boom", headers={"Origin": ALLOWED})
    assert r.status_code == 500
    assert r.headers["access-control-allow-origin"] == ALLOWED
    assert "Origin" in r.headers["vary"]
    assert r.headers["access-control-allow-credentials"] == "true"


@pytest.mark.parametrize("origin", ["https://evil.example", "http://localhost:3001", None])
def test_500_has_no_cors_for_other_or_missing_origin(boom_client, origin):
    r = boom_client.get("/__be15/boom", headers={"Origin": origin} if origin else {})
    assert r.status_code == 500
    assert "access-control-allow-origin" not in r.headers


def test_500_body_is_exactly_the_generic_message(boom_client):
    r = boom_client.get("/__be15/boom", headers={"Origin": ALLOWED})
    assert r.json() == GENERIC and r.text == '{"detail":"An internal error occurred."}'
    assert "secret" not in r.text and "srv" not in r.text and "Traceback" not in r.text
    assert not any(h.lower().startswith("x-debug") for h in r.headers)


def test_the_exception_is_still_raised_to_the_server(boom_client):
    """Sentry and uvicorn must still see it (the same as ServerErrorMiddleware)."""
    with pytest.raises(RuntimeError):
        TestClient(app).get("/__be15/boom")


def test_preflight_and_normal_responses_unchanged(client):
    url = "/api/v1/health"
    pre = client.options(url, headers={"Origin": ALLOWED, "Access-Control-Request-Method": "GET"})
    assert pre.status_code == 200 and pre.headers["access-control-allow-origin"] == ALLOWED
    ok = client.get(url, headers={"Origin": ALLOWED})
    assert ok.status_code == 200 and ok.headers["access-control-allow-origin"] == ALLOWED


def test_schema_behind_is_logged_once_in_development(boom_client, caplog, monkeypatch):
    monkeypatch.setattr(settings, "environment", "development")
    with caplog.at_level(logging.ERROR):
        for _ in range(3):
            r = boom_client.get("/__be15/behind", headers={"Origin": ALLOWED})
            assert r.status_code == 500 and r.json() == GENERIC
    hints = [m for m in caplog.messages if "database schema is behind this code" in m]
    assert hints == ["database schema is behind this code (run python scripts/dev_setup.py)"]


def test_schema_behind_hint_is_not_logged_in_production(boom_client, caplog, monkeypatch):
    monkeypatch.setattr(settings, "environment", "production")
    with caplog.at_level(logging.ERROR):
        r = boom_client.get("/__be15/behind", headers={"Origin": ALLOWED})
    assert r.status_code == 500 and r.json() == GENERIC
    assert not any("schema is behind" in m for m in caplog.messages)


def test_other_errors_do_not_log_the_schema_hint(boom_client, caplog, monkeypatch):
    monkeypatch.setattr(settings, "environment", "development")
    with caplog.at_level(logging.ERROR):
        boom_client.get("/__be15/boom")
    assert not any("schema is behind" in m for m in caplog.messages)
