"""BE-1 A7 — the X-Employee-* / X-Associate-Id dev bypass is accepted only when
ENVIRONMENT is exactly development or test.

It used to be refused only for the exact string "production", so a staging deploy — or
one configured with "prod" or "Production" — accepted forged staff headers. Boot now
rejects unknown values (tests/test_production_config.py); these tests also cover the
refusal itself for values that slip past it (set directly on the settings object).
"""
import pytest

from app.config import settings

STAFF = {"X-Employee-Id": "emp-admin-1", "X-Employee-Role": "admin"}
STAFF_ROUTE = "/api/v1/dashboard/leads"
ASSOCIATE_ROUTE = "/api/v1/associate/me"


@pytest.mark.parametrize("environment", ["staging", "production", "prod", "Production"])
def test_staff_dev_headers_are_refused(client, monkeypatch, environment):
    monkeypatch.setattr(settings, "environment", environment)
    r = client.get(STAFF_ROUTE, headers=STAFF)
    assert r.status_code == 403
    assert r.json()["detail"] == "Dev auth headers are only accepted in development and test."


@pytest.mark.parametrize("environment", ["staging", "production", "prod", "Production"])
def test_associate_dev_header_is_refused(client, monkeypatch, environment):
    monkeypatch.setattr(settings, "environment", environment)
    r = client.get(ASSOCIATE_ROUTE, headers={"X-Associate-Id": "GNA-WB-26-00001"})
    assert r.status_code == 403
    assert r.json()["detail"] == "Dev auth headers are only accepted in development and test."


@pytest.mark.parametrize("environment", ["development", "test"])
def test_staff_dev_headers_are_accepted(client, monkeypatch, environment):
    monkeypatch.setattr(settings, "environment", environment)
    assert client.get(STAFF_ROUTE, headers=STAFF).status_code == 200


def test_the_suite_runs_as_development():
    """tests/conftest.py sets ENVIRONMENT=development (unless the shell overrides it)."""
    assert settings.environment == "development"
    assert settings.allows_dev_auth
