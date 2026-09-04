"""Subcontractor role — scaffolding, deliberately maximally restricted.

There is nothing for this role to access yet (Work Orders don't exist), so the
correct default is deny-everything, not "inherits whatever the closest existing
role happens to allow." Two kinds of proof here: a reflection-based sweep of
every can_*() gate on AuthenticatedEmployee (so a future gate added without
updating its allow-list is caught automatically), and live requests against
representative real endpoints in each named area — leads, proposals,
employees, governance, proposal-inputs.
"""
import inspect

from app.auth.dependencies import AuthenticatedEmployee
from app.memory.models import EmployeeRole


def _subcontractor() -> AuthenticatedEmployee:
    return AuthenticatedEmployee(employee_id="sub-1", role=EmployeeRole.subcontractor)


def subcontractor_headers(employee_id: str = "emp-sub-1") -> dict:
    return {"X-Employee-Id": employee_id, "X-Employee-Role": "subcontractor"}


class TestEveryPermissionGateDeniesByDefault:
    def test_every_can_method_returns_false_for_subcontractor(self):
        """Enumerates every can_*() method on AuthenticatedEmployee via
        reflection — not a hand-picked subset — and asserts each one denies a
        subcontractor. A future gate that forgets to exclude this role fails
        this test immediately, without needing its own dedicated test."""
        employee = _subcontractor()
        gates = [
            name for name, _ in inspect.getmembers(AuthenticatedEmployee, predicate=inspect.isfunction)
            if name.startswith("can_")
        ]
        assert len(gates) >= 12, "expected the full set of existing permission gates"
        failures = [name for name in gates if getattr(employee, name)()]
        assert not failures, f"subcontractor was granted: {failures}"

    def test_every_is_role_property_is_false_except_none(self):
        employee = _subcontractor()
        assert not employee.is_admin
        assert not employee.is_sales
        assert not employee.is_management
        assert not employee.is_engineer

    def test_can_view_assigned_work_is_false_for_every_role_not_just_subcontractor(self):
        """Not a broken check — there is genuinely nothing to view yet."""
        for role in EmployeeRole:
            employee = AuthenticatedEmployee(employee_id="x", role=role)
            assert employee.can_view_assigned_work() is False


class TestSubcontractorDeniedOnRealEndpoints:
    """One representative, explicitly-gated endpoint per named area."""

    def test_leads_edit_denied(self, client):
        r = client.patch(
            "/api/v1/dashboard/leads/1/fields",
            json={"fields": {"name": "x"}},
            headers=subcontractor_headers(),
        )
        assert r.status_code == 403

    def test_proposal_draft_trigger_denied(self, client):
        r = client.post(
            "/api/v1/dashboard/proposals/draft",
            json={"lead_id": 1, "customer_id": 1},
            headers=subcontractor_headers(),
        )
        assert r.status_code == 403

    def test_employees_list_denied(self, client):
        r = client.get("/api/v1/dashboard/employees", headers=subcontractor_headers())
        assert r.status_code == 403

    def test_employee_role_change_denied(self, client):
        r = client.post(
            "/api/v1/dashboard/employees/someone/role",
            json={"role": "admin"},
            headers=subcontractor_headers(),
        )
        assert r.status_code == 403

    def test_employee_deactivate_denied(self, client):
        r = client.patch(
            "/api/v1/dashboard/employees/someone/deactivate", headers=subcontractor_headers(),
        )
        assert r.status_code == 403

    def test_governance_agents_list_denied(self, client):
        r = client.get("/api/v1/governance/agents", headers=subcontractor_headers())
        assert r.status_code == 403

    def test_governance_agent_toggle_denied(self, client):
        r = client.patch(
            "/api/v1/governance/agents/sales_agent", json={"enabled": False},
            headers=subcontractor_headers(),
        )
        assert r.status_code == 403

    def test_legacy_governance_dashboard_view_denied(self, client):
        r = client.get("/api/v1/dashboard/governance/agents", headers=subcontractor_headers())
        assert r.status_code == 403

    def test_proposal_inputs_submit_denied(self, client):
        r = client.post(
            "/api/v1/proposal-inputs/",
            json={
                "lead_id": 1, "customer_type": "individual", "project_family": "rooftop_solar",
                "state": "West Bengal", "district": "Kolkata", "pin_code": "700001",
                "site_type": "rooftop_residential", "connection_type": "on_grid",
            },
            headers=subcontractor_headers(),
        )
        assert r.status_code == 403

    def test_proposal_inputs_review_denied(self, client):
        r = client.patch(
            "/api/v1/proposal-inputs/1/review",
            json={"status": "approved_for_proposal"},
            headers=subcontractor_headers(),
        )
        assert r.status_code == 403

    def test_audit_log_view_denied(self, client):
        r = client.get("/api/v1/dashboard/audit-log", headers=subcontractor_headers())
        assert r.status_code == 403

    def test_full_analytics_denied(self, client):
        r = client.get("/api/v1/dashboard/analytics/conversion-by-source", headers=subcontractor_headers())
        assert r.status_code == 403

    def test_customer_memory_query_denied(self, client):
        r = client.post(
            "/api/v1/dashboard/memory/ask", json={"question": "what do we know?"},
            headers=subcontractor_headers(),
        )
        assert r.status_code == 403

    def test_schedule_meeting_denied(self, client):
        r = client.post(
            "/api/v1/dashboard/leads/1/schedule-meeting",
            json={"scheduled_at": "2026-01-01T10:00:00Z"},
            headers=subcontractor_headers(),
        )
        assert r.status_code == 403


class TestSubcontractorSignInStillWorks:
    """Deny-by-default on PERMISSIONS, not on authentication itself — a
    subcontractor is a real employee role and can sign in; they just can't
    do anything yet, which is the whole point of the scaffold."""

    def test_the_role_is_accepted_as_a_valid_employee_role(self, client):
        # Any endpoint that only requires *authentication* (not a specific
        # can_* grant) should still accept a subcontractor's credentials
        # rather than rejecting the role outright.
        r = client.get("/api/v1/dashboard/employees", headers=subcontractor_headers())
        assert r.status_code == 403  # denied by permission, not by an unknown-role 400/422
