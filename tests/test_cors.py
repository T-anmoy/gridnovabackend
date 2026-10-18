"""CORS: the frontend's dev origin is accepted in development, and production must name
its origins explicitly (it refuses to start with a localhost one). Behaviour reported by
HF-A, not changed by it."""
import pytest

from app.config import Settings, production_config_problems
from tests.test_production_config import GOOD

URL = "/api/v1/public/territories/states"


def _preflight(client, origin):
    return client.options(URL, headers={"Origin": origin, "Access-Control-Request-Method": "GET"})


def test_development_accepts_the_frontend_origin(client):
    r = _preflight(client, "http://localhost:3000")
    assert r.status_code == 200 and r.headers["access-control-allow-origin"] == "http://localhost:3000"
    # ... and the real request carries the header too.
    assert client.get(URL, headers={"Origin": "http://localhost:3000"}).headers[
        "access-control-allow-origin"] == "http://localhost:3000"


@pytest.mark.parametrize("origin", ["http://127.0.0.1:3000", "http://localhost:3001", "https://evil.example"])
def test_other_origins_are_not_allowed(client, origin):
    r = _preflight(client, origin)
    assert "access-control-allow-origin" not in r.headers


def test_the_development_defaults_are_exactly_these():
    assert Settings(_env_file=None).allowed_origins == ["http://localhost:3000", "https://www.gridnova.in"]


def test_production_cannot_run_on_the_default_origins():
    """The defaults include localhost, so production must set ALLOWED_ORIGINS itself."""
    without = {k: v for k, v in GOOD.items() if k != "allowed_origins"}
    errors, _ = production_config_problems(Settings(_env_file=None, **without))
    assert any("ALLOWED_ORIGINS contains a localhost origin" in e for e in errors)


def test_production_accepts_explicit_origins():
    errors, _ = production_config_problems(Settings(_env_file=None, **GOOD))
    assert not any("ALLOWED_ORIGINS" in e for e in errors)

def test_retry_after_is_readable_by_the_dashboard(client):
    """A 429 from Nova Records carries Retry-After; the browser only lets the dashboard read it
    when the response exposes it."""
    r = client.get(URL, headers={"Origin": "http://localhost:3000"})
    exposed = [h.strip().lower() for h in r.headers.get("access-control-expose-headers", "").split(",")]
    assert "retry-after" in exposed
