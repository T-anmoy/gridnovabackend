"""Operations and Accounts roles — recognised, deliberately maximally restricted.

`operations` is the renamed `subcontractor` role (0026_operations_accounts_roles);
`accounts` is new. Neither has business logic yet, so the correct default is
deny-everything, not "inherits whatever the closest existing role happens to
allow." Two kinds of proof here, run for BOTH roles: a reflection-based sweep of
every can_*() gate on AuthenticatedEmployee (so a future gate added without
updating its allow-list is caught automatically), and live requests against
representative real endpoints in each named area — leads, proposals,
employees, governance, proposal-inputs.
"""
import inspect

import pytest

from app.auth import dependencies as deps
from app.auth.dependencies import AuthenticatedEmployee
from app.memory.models import EmployeeRole

RESTRICTED_ROLES = ("operations", "accounts")


@pytest.fixture(params=RESTRICTED_ROLES)
def role(request) -> str:
    return request.param


def _restricted(role: str) -> AuthenticatedEmployee:
    return AuthenticatedEmployee(employee_id="restricted-1", role=EmployeeRole(role))


def restricted_headers(role: str, employee_id: str = "emp-restricted-1") -> dict:
    return {"X-Employee-Id": f"{employee_id}-{role}", "X-Employee-Role": role}


class TestEveryPermissionGateDeniesByDefault:
    def test_every_can_method_returns_false(self, role):
        """Enumerates every can_*() method on AuthenticatedEmployee via
        reflection — not a hand-picked subset — and asserts each one denies a
        restricted role. A future gate that forgets to exclude this role fails
        this test immediately, without needing its own dedicated test."""
        employee = _restricted(role)
        gates = [
            name for name, _ in inspect.getmembers(AuthenticatedEmployee, predicate=inspect.isfunction)
            if name.startswith("can_")
        ]
        assert len(gates) >= 12, "expected the full set of existing permission gates"
        failures = [name for name in gates if getattr(employee, name)()]
        assert not failures, f"{role} was granted: {failures}"

    def test_every_is_role_property_is_false_except_none(self, role):
        employee = _restricted(role)
        assert not employee.is_admin
        assert not employee.is_sales
        assert not employee.is_management
        assert not employee.is_engineer

    def test_can_view_assigned_work_is_false_for_every_role(self):
        """Not a broken check — there is genuinely nothing to view yet."""
        for role in EmployeeRole:
            employee = AuthenticatedEmployee(employee_id="x", role=role)
            assert employee.can_view_assigned_work() is False


class TestRestrictedRolesDeniedOnRealEndpoints:
    """One representative, explicitly-gated endpoint per named area."""

    def test_leads_edit_denied(self, client, role):
        r = client.patch(
            "/api/v1/dashboard/leads/1/fields",
            json={"fields": {"name": "x"}},
            headers=restricted_headers(role),
        )
        assert r.status_code == 403

    def test_proposal_draft_trigger_denied(self, client, role):
        r = client.post(
            "/api/v1/dashboard/proposals/draft",
            json={"lead_id": 1, "customer_id": 1},
            headers=restricted_headers(role),
        )
        assert r.status_code == 403

    def test_employees_list_denied(self, client, role):
        r = client.get("/api/v1/dashboard/employees", headers=restricted_headers(role))
        assert r.status_code == 403

    def test_employee_role_change_denied(self, client, role):
        r = client.post(
            "/api/v1/dashboard/employees/someone/role",
            json={"role": "admin"},
            headers=restricted_headers(role),
        )
        assert r.status_code == 403

    def test_employee_deactivate_denied(self, client, role):
        r = client.patch(
            "/api/v1/dashboard/employees/someone/deactivate", headers=restricted_headers(role),
        )
        assert r.status_code == 403

    def test_governance_agents_list_denied(self, client, role):
        r = client.get("/api/v1/governance/agents", headers=restricted_headers(role))
        assert r.status_code == 403

    def test_governance_agent_toggle_denied(self, client, role):
        r = client.patch(
            "/api/v1/governance/agents/sales_agent", json={"enabled": False},
            headers=restricted_headers(role),
        )
        assert r.status_code == 403

    def test_legacy_governance_dashboard_view_denied(self, client, role):
        r = client.get("/api/v1/dashboard/governance/agents", headers=restricted_headers(role))
        assert r.status_code == 403

    def test_proposal_inputs_submit_denied(self, client, role):
        r = client.post(
            "/api/v1/proposal-inputs/",
            json={
                "lead_id": 1, "customer_type": "individual", "project_family": "rooftop_solar",
                "state": "West Bengal", "district": "Kolkata", "pin_code": "700001",
                "site_type": "rooftop_residential", "connection_type": "on_grid",
            },
            headers=restricted_headers(role),
        )
        assert r.status_code == 403

    def test_proposal_inputs_review_denied(self, client, role):
        r = client.patch(
            "/api/v1/proposal-inputs/1/review",
            json={"status": "approved_for_proposal"},
            headers=restricted_headers(role),
        )
        assert r.status_code == 403

    def test_audit_log_view_denied(self, client, role):
        r = client.get("/api/v1/dashboard/audit-log", headers=restricted_headers(role))
        assert r.status_code == 403

    def test_full_analytics_denied(self, client, role):
        r = client.get("/api/v1/dashboard/analytics/conversion-by-source", headers=restricted_headers(role))
        assert r.status_code == 403

    def test_customer_memory_query_denied(self, client, role):
        r = client.post(
            "/api/v1/dashboard/memory/ask", json={"question": "what do we know?"},
            headers=restricted_headers(role),
        )
        assert r.status_code == 403

    def test_schedule_meeting_denied(self, client, role):
        r = client.post(
            "/api/v1/dashboard/leads/1/schedule-meeting",
            json={"scheduled_at": "2026-01-01T10:00:00Z"},
            headers=restricted_headers(role),
        )
        assert r.status_code == 403


class TestRestrictedRolesSignInStillWorks:
    """Deny-by-default on PERMISSIONS, not on authentication itself — a
    restricted role is a real employee role and can sign in; they just can't
    do anything yet, which is the whole point of the scaffold."""

    def test_the_role_is_accepted_as_a_valid_employee_role(self, client, role):
        # Any endpoint that only requires *authentication* (not a specific
        # can_* grant) should still accept the role's credentials
        # rather than rejecting the role outright.
        r = client.get("/api/v1/dashboard/employees", headers=restricted_headers(role))
        assert r.status_code == 403  # denied by permission, not by an unknown-role 400/422

    def test_the_role_authenticates_and_is_provisioned(self, client, db_session, role):
        """An authentication-only endpoint answers 200 and reports the role — the
        role is recognised end to end, not merely tolerated."""
        from app.memory.models import Employee

        headers = restricted_headers(role)
        r = client.get("/api/v1/dashboard/employees/me", headers=headers)
        assert r.status_code == 200 and r.json()["role"] == role
        assert db_session.get(Employee, headers["X-Employee-Id"]).role == EmployeeRole(role)

    def test_jwt_with_the_role_claim_is_accepted(self, client, monkeypatch, role):
        monkeypatch.setattr(deps, "_verify_clerk_jwt", lambda token: {
            "sub": f"user_{role}", "public_metadata": {"role": role}})
        r = client.get("/api/v1/dashboard/employees/me", headers={"Authorization": "Bearer t"})
        assert r.status_code == 200 and r.json()["role"] == role


class TestSubcontractorValueIsGone:
    """The rename is complete: the old value is no longer a role anywhere."""

    def test_enum_has_no_subcontractor(self):
        assert "subcontractor" not in {r.value for r in EmployeeRole}
        assert {"operations", "accounts"} <= {r.value for r in EmployeeRole}

    def test_old_value_rejected_on_dev_header(self, client):
        r = client.get("/api/v1/dashboard/employees/me",
                       headers={"X-Employee-Id": "emp-old", "X-Employee-Role": "subcontractor"})
        assert r.status_code == 400

    def test_old_value_rejected_on_jwt(self, client, monkeypatch):
        monkeypatch.setattr(deps, "_verify_clerk_jwt", lambda token: {
            "sub": "user_old", "public_metadata": {"role": "subcontractor"}})
        r = client.get("/api/v1/dashboard/employees/me", headers={"Authorization": "Bearer t"})
        assert r.status_code == 403


class TestNewAdminPowers:
    """Approving associate applications, managing territories and managing seats
    are Admin-only."""

    @pytest.mark.parametrize("gate", ["can_review_associate_applications",
                                      "can_manage_territories", "can_manage_seats"])
    def test_admin_only(self, gate):
        for r in EmployeeRole:
            allowed = getattr(AuthenticatedEmployee(employee_id="x", role=r), gate)()
            assert allowed is (r == EmployeeRole.admin), (gate, r)
