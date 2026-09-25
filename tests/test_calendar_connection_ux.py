"""Google Calendar connection, as the employee's browser and the dashboard see it:
the OAuth callback redirects back to the frontend, and the employees endpoints
expose connection state — never the token."""
from urllib.parse import parse_qs, urlparse

import pytest

from app.api.routes.dashboard import _sign_state
from app.config import settings
from app.memory.models import Employee
from app.scheduling import google_calendar
from app.security.encryption import generate_key
from tests.conftest import admin_headers, engineer_headers, sales_headers

CALLBACK = "/api/v1/dashboard/employees/me/calendar/callback"
REFRESH_TOKEN = "1//REFRESH-TOKEN-DO-NOT-LEAK"
FRONTEND = "https://www.gridnova.in"


@pytest.fixture(autouse=True)
def _calendar_settings(monkeypatch):
    monkeypatch.setattr(settings, "fernet_encryption_key", generate_key())
    monkeypatch.setattr(settings, "frontend_base_url", FRONTEND + "/")  # trailing slash tolerated


@pytest.fixture
def employee(memory, db_session):
    memory.get_or_create_employee("emp-cal", "Cal", "cal@gridnova.in", "sales")
    db_session.commit()
    return "emp-cal"


def _callback(client, **params):
    r = client.get(CALLBACK, params=params, follow_redirects=False)
    assert r.status_code == 303, r.text
    location = r.headers["location"]
    assert REFRESH_TOKEN not in location and REFRESH_TOKEN not in r.text
    parsed = urlparse(location)
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == f"{FRONTEND}/dashboard/meetings"
    return {k: v[0] for k, v in parse_qs(parsed.query).items()}


class TestCallbackRedirect:
    def test_success_stores_encrypted_token_and_redirects_connected(
        self, client, db_session, employee, monkeypatch
    ):
        monkeypatch.setattr(google_calendar, "exchange_code", lambda code: REFRESH_TOKEN)
        assert _callback(client, code="auth-code", state=_sign_state(employee)) == {"calendar": "connected"}

        db_session.expire_all()
        row = db_session.get(Employee, employee)
        assert row.google_calendar_connected is True
        assert row.google_refresh_token and REFRESH_TOKEN not in row.google_refresh_token

    @pytest.mark.parametrize("state", [None, "", "garbage", "ZW1wLWNhbDpiYWRzaWc="])
    def test_bad_or_missing_state(self, client, employee, state):
        params = {"code": "auth-code"} if state is None else {"code": "auth-code", "state": state}
        assert _callback(client, **params) == {"calendar": "error", "reason": "invalid_state"}

    def test_consent_declined(self, client, employee):
        q = _callback(client, error="access_denied", state=_sign_state(employee))
        assert q == {"calendar": "error", "reason": "access_denied"}

    def test_other_google_error_is_not_echoed(self, client, employee):
        q = _callback(client, error="<script>alert(1)</script>", state=_sign_state(employee))
        assert q == {"calendar": "error", "reason": "consent_failed"}

    def test_missing_code(self, client, employee):
        assert _callback(client, state=_sign_state(employee)) == {"calendar": "error", "reason": "missing_code"}

    def test_exchange_failure(self, client, employee, monkeypatch):
        def _boom(code):
            raise RuntimeError(f"token endpoint said no; token={REFRESH_TOKEN}")
        monkeypatch.setattr(google_calendar, "exchange_code", _boom)
        q = _callback(client, code="auth-code", state=_sign_state(employee))
        assert q == {"calendar": "error", "reason": "exchange_failed"}

    def test_not_configured(self, client, employee, monkeypatch):
        def _unconfigured(code):
            raise google_calendar.GoogleCalendarNotConfiguredError("not configured")
        monkeypatch.setattr(google_calendar, "exchange_code", _unconfigured)
        q = _callback(client, code="auth-code", state=_sign_state(employee))
        assert q == {"calendar": "error", "reason": "not_configured"}

    def test_signed_state_for_unknown_employee(self, client, monkeypatch):
        monkeypatch.setattr(google_calendar, "exchange_code", lambda code: REFRESH_TOKEN)
        q = _callback(client, code="auth-code", state=_sign_state("emp-nobody"))
        assert q == {"calendar": "error", "reason": "employee_not_found"}


def _connect(db_session, employee_id: str) -> None:
    row = db_session.get(Employee, employee_id)
    row.google_calendar_connected = True
    row.google_refresh_token = "ENCRYPTED-" + REFRESH_TOKEN
    db_session.commit()


class TestEmployeesList:
    def test_rows_carry_connection_state_but_never_the_token(self, client, db_session, employee):
        client.get("/api/v1/dashboard/employees", headers=admin_headers())  # provisions the admin
        _connect(db_session, employee)
        r = client.get("/api/v1/dashboard/employees", headers=admin_headers())
        assert r.status_code == 200
        rows = {e["id"]: e for e in r.json()}
        assert rows[employee]["google_calendar_connected"] is True
        assert rows["emp-admin-1"]["google_calendar_connected"] is False
        assert all("google_refresh_token" not in e for e in rows.values())
        assert REFRESH_TOKEN not in r.text

    def test_still_admin_only(self, client):
        assert client.get("/api/v1/dashboard/employees", headers=sales_headers()).status_code == 403


class TestEmployeesMe:
    def test_returns_own_record_and_nothing_else(self, client, db_session):
        r = client.get("/api/v1/dashboard/employees/me", headers=sales_headers("emp-me"))
        assert r.status_code == 200
        assert r.json() == {
            "id": "emp-me", "role": "sales", "is_active": True, "google_calendar_connected": False,
        }

    def test_reflects_a_connected_calendar_without_leaking_the_token(self, client, db_session):
        client.get("/api/v1/dashboard/employees/me", headers=sales_headers("emp-me"))
        _connect(db_session, "emp-me")
        r = client.get("/api/v1/dashboard/employees/me", headers=sales_headers("emp-me"))
        assert r.json()["google_calendar_connected"] is True
        assert REFRESH_TOKEN not in r.text and "refresh" not in r.text

    @pytest.mark.parametrize("headers, role", [
        (admin_headers(), "admin"), (engineer_headers(), "engineer"),
    ])
    def test_any_role_may_read_its_own_record(self, client, headers, role):
        r = client.get("/api/v1/dashboard/employees/me", headers=headers)
        assert r.status_code == 200 and r.json()["role"] == role

    def test_requires_authentication(self, client):
        assert client.get("/api/v1/dashboard/employees/me").status_code == 401

    def test_deactivated_employee_is_refused(self, client, memory, db_session):
        client.get("/api/v1/dashboard/employees/me", headers=sales_headers("emp-gone"))
        db_session.get(Employee, "emp-gone").is_active = False
        db_session.commit()
        assert client.get("/api/v1/dashboard/employees/me", headers=sales_headers("emp-gone")).status_code == 403
