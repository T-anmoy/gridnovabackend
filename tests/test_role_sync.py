"""The stored employee role follows the login token (auth_sync), deactivation is never
undone by signing in, and the admin role change updates Clerk and the DB together."""
import pytest

import app.auth.dependencies as deps
from app.auth.clerk_client import ClerkSyncError
from app.memory.models import Employee, EmployeeRole
from tests.conftest import admin_headers

ME = "/api/v1/dashboard/employees/me"


def _as(employee_id, role):
    return {"X-Employee-Id": employee_id, "X-Employee-Role": role}


def _role_audits(memory, employee_id):
    return [l for l in memory.list_audit_logs(entity_type="employee", entity_id=employee_id)
            if l.action == "role_changed"]


def _stored(db_session, employee_id):
    db_session.expire_all()
    return db_session.get(Employee, employee_id)


class TestAuthSync:
    def test_role_change_is_picked_up_on_the_next_request(self, client, memory, db_session):
        assert client.get(ME, headers=_as("emp-r", "engineer")).status_code == 200
        assert _stored(db_session, "emp-r").role == EmployeeRole.engineer

        assert client.get(ME, headers=_as("emp-r", "sales")).status_code == 200
        assert _stored(db_session, "emp-r").role == EmployeeRole.sales
        [audit] = _role_audits(memory, "emp-r")
        assert audit.performed_by == "auth_sync"
        assert audit.before_value == {"role": "engineer"} and audit.after_value == {"role": "sales"}

    def test_unchanged_role_writes_no_audit(self, client, memory, db_session):
        client.get(ME, headers=_as("emp-s", "sales"))
        client.get(ME, headers=_as("emp-s", "sales"))
        assert _role_audits(memory, "emp-s") == []

    def test_deactivated_employee_stays_deactivated_and_untouched(self, client, memory, db_session):
        client.get(ME, headers=_as("emp-d", "sales"))
        _stored(db_session, "emp-d").is_active = False
        db_session.commit()

        assert client.get(ME, headers=_as("emp-d", "admin")).status_code == 403
        row = _stored(db_session, "emp-d")
        assert row.is_active is False and row.role == EmployeeRole.sales
        assert _role_audits(memory, "emp-d") == []

    def test_jwt_path_syncs_too(self, client, memory, db_session, monkeypatch):
        db_session.add(Employee(id="user_clerk1", name="C", email="c@gridnova.in", role=EmployeeRole.sales))
        db_session.commit()
        monkeypatch.setattr(deps, "_verify_clerk_jwt", lambda token: {
            "sub": "user_clerk1", "public_metadata": {"role": "management"}, "email": "c@gridnova.in"})
        r = client.get(ME, headers={"Authorization": "Bearer test-token"})
        assert r.status_code == 200 and r.json()["role"] == "management"
        assert _stored(db_session, "user_clerk1").role == EmployeeRole.management
        assert _role_audits(memory, "user_clerk1")[0].performed_by == "auth_sync"

    def test_assignment_uses_the_synced_role(self, client, memory, db_session):
        # Stored as engineer at first sign-in, promoted to sales in Clerk later.
        client.get(ME, headers=_as("emp-promoted", "engineer"))
        lead_id = client.post("/api/v1/dashboard/leads", json={"name": "L", "phone": "9000000001",
                                                                "source": "other"}, headers=admin_headers()).json()["lead_id"]
        assign = {"employee_id": "emp-promoted"}
        assert client.patch(f"/api/v1/dashboard/leads/{lead_id}/assign", json=assign,
                            headers=admin_headers()).status_code == 422
        client.get(ME, headers=_as("emp-promoted", "sales"))
        assert client.patch(f"/api/v1/dashboard/leads/{lead_id}/assign", json=assign,
                            headers=admin_headers()).status_code == 200


class TestAdminRoleChange:
    def test_updates_clerk_and_the_db_row(self, client, memory, db_session, monkeypatch):
        calls = []
        monkeypatch.setattr("app.api.routes.dashboard.update_user_role",
                            lambda user_id, role: calls.append((user_id, role)) or True)
        client.get(ME, headers=_as("emp-t", "sales"))
        r = client.post("/api/v1/dashboard/employees/emp-t/role", json={"role": "management"},
                        headers=admin_headers())
        assert r.status_code == 200 and r.json()["clerk_synced"] is True and r.json()["local_mirror"] is True
        assert calls == [("emp-t", "management")]
        assert _stored(db_session, "emp-t").role == EmployeeRole.management

    def test_clerk_failure_changes_nothing(self, client, memory, db_session, monkeypatch):
        def _fail(user_id, role):
            raise ClerkSyncError("down")
        monkeypatch.setattr("app.api.routes.dashboard.update_user_role", _fail)
        client.get(ME, headers=_as("emp-u", "sales"))
        r = client.post("/api/v1/dashboard/employees/emp-u/role", json={"role": "admin"}, headers=admin_headers())
        assert r.status_code == 502
        assert _stored(db_session, "emp-u").role == EmployeeRole.sales
