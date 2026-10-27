"""BE-17 — access control: the permission catalogue, several roles per person, the database
as the source of truth, the Network Manager, staff invitations, offboarding, second
approvals, role-routed alerts, enforced two-step sign-in and regions.

The role × permission table below is written out by hand on purpose: it is the
contract config/access_control.yaml must match. A change to who may do what changes
this file too.
"""
from __future__ import annotations

import importlib
import inspect
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import app.auth.dependencies as deps
from app.auth import access
from app.auth.dependencies import AuthenticatedEmployee
from app.memory.models import (
    ApprovalRequest, ApprovalRequestStatus, AuditLog, Employee, EmployeeRole, EmployeeRoleGrant, Lead,
    LeadSource, PipelineStage, StaffInvitation, StaffInvitationStatus,
)
from tests.conftest import admin_headers
from tests.test_bootstrap_db import empty_database_url  # noqa: F401 — the fixture

D = "/api/v1/dashboard"
ME = f"{D}/employees/me"
REPO = Path(__file__).resolve().parents[1]

# ── The contract: every role × permission ────────────────────────────────────────

PRE_BE17 = {   # every can_* gate → the roles that held it before BE-17 (unchanged)
    "proposals.approve_standard": {"admin", "sales"},
    "proposals.manager_signoff": {"admin", "management"},
    "proposals.partner_signoff": {"admin"},
    "leads.edit": {"admin", "sales"},
    "leads.change_stage": {"admin", "management", "sales"},
    "leads.create": {"admin", "management", "sales"},
    "leads.assign": {"admin", "management"},
    "proposals.draft": {"admin", "sales"},
    "proposals.send": {"admin", "sales"},
    "audit.view": {"admin", "management"},
    "staff.manage": {"admin"},
    "analytics.view_full": {"admin", "management"},
    "meetings.schedule": {"admin", "sales"},
    "pricing.adjust": {"admin", "sales"},
    "customers.memory_query": {"admin", "sales", "management"},
    "proposal_inputs.manage": {"admin", "sales"},
    "proposal_inputs.review": {"admin", "sales", "engineer"},
    "projects.record_stage": {"admin", "sales"},
    "associates.applications.review": {"admin"},
    "onboardings.view": {"admin", "management", "sales"},
    "surveys.view": {"admin", "management", "sales", "engineer"},
    "surveys.review": {"admin", "engineer"},
    "proposals.clear_reverification": {"admin", "management", "engineer"},
    "onboardings.verify": {"admin", "sales"},
    "associates.attribution.resolve": {"admin"},
    "territories.manage": {"admin"},
    "associates.seats.manage": {"admin"},
    "payments.view": {"admin", "management", "accounts", "sales"},
    "payments.record": {"admin", "accounts", "sales"},
    "payments.process": {"admin", "accounts"},
    "commissions.view": {"admin", "management", "accounts"},
    "commissions.process": {"admin", "accounts"},
    "commissions.rates.manage": {"admin"},
    "commissions.individual_rates.view": {"admin", "management"},
    "payouts.bank_file.export": {"accounts"},
    "associates.network_summary.view": {"admin", "management"},
    "execution.view": {"admin", "management", "sales", "engineer", "operations"},
    "execution.verify": {"admin", "operations"},
    "execution.record": {"admin", "operations"},
    "service_requests.handle": {"admin", "operations", "engineer"},
}
REPLACED_CHECKS = {   # the direct role checks BE-17 turned into permissions (same roles as before)
    "leads.view_all": {"admin", "management", "engineer"},
    "leads.view_assigned": {"admin", "sales"},
    "leads.view_commercial": {"admin", "management", "sales"},
    "leads.view_technical": {"admin", "engineer"},
    "leads.be_assigned": {"admin", "sales"},
    "leads.stale_check.run": {"admin"},
    "meetings.view": {"admin", "management", "sales"},
    "meetings.view_all": {"admin", "management"},
    "meetings.host": {"admin", "sales"},
    "surveys.view_prices": {"admin", "management", "sales"},
    "execution.view_all": {"admin", "management", "engineer"},
    "execution.view_in_progress": {"admin", "operations"},
    "execution.mode.switch": {"admin"},
    "execution.admin_override": {"admin"},
    "execution.dcr_override": {"admin"},
    "payments.view_all": {"admin", "management", "accounts"},
    "payments.segregation_override": {"admin"},
    "payouts.segregation_override": {"admin"},
    "equipment.manage": {"admin"},
    "equipment.procurement_price.view": {"admin", "management", "sales"},
    "placeholders.view": {"admin", "management"},
    "placeholders.resolve": {"admin"},
    "archive.manage": {"admin"},
    "governance.manage": {"admin"},
    "records.conversations.read_any": {"admin"},
    "records.commissions.read": {"admin", "accounts"},
    "onboardings.view_all": {"admin"},
    "audit.view_associates": {"admin"},
}
NETWORK_MANAGER = {
    "leads.view_all", "leads.view_commercial", "pricing.adjust",
    "associates.applications.review", "associates.attribution.resolve", "associates.seats.manage",
    "associates.network_summary.view", "territories.manage", "onboardings.view", "onboardings.view_all",
    "surveys.view", "audit.view_associates", "staff.manage",
}

# BE-18: the Proposal Studio. Proposal Desk works only there; Management views and approves.
STUDIO = {
    "studio.view": {"admin", "management", "proposal_desk"},
    "studio.view_all": {"admin", "management", "proposal_desk"},
    "studio.edit": {"admin", "proposal_desk"},
    "studio.price": {"admin", "proposal_desk"},
    "studio.approve": {"admin", "management"},
    "studio.library.manage": {"admin", "proposal_desk"},
    "studio.convert": {"admin", "proposal_desk"},
}


def expected_matrix() -> dict[str, set[str]]:
    out = {r.value: set() for r in EmployeeRole}
    for table in (PRE_BE17, REPLACED_CHECKS, STUDIO):
        for perm, roles in table.items():
            for role in roles:
                out[role].add(perm)
    out["network_manager"] = set(NETWORK_MANAGER)
    return out


GATE_PERMISSION = {
    "can_approve_standard": "proposals.approve_standard", "can_approve_manager_signoff": "proposals.manager_signoff",
    "can_approve_partner_signoff": "proposals.partner_signoff", "can_edit_lead": "leads.edit",
    "can_change_lead_stage": "leads.change_stage", "can_create_lead": "leads.create", "can_assign_lead": "leads.assign",
    "can_trigger_proposal_draft": "proposals.draft", "can_send_proposal": "proposals.send",
    "can_view_audit_logs": "audit.view", "can_manage_employees": "staff.manage",
    "can_view_full_analytics": "analytics.view_full", "can_schedule_meeting": "meetings.schedule",
    "can_adjust_pricing": "pricing.adjust", "can_query_customer_memory": "customers.memory_query",
    "can_manage_proposal_inputs": "proposal_inputs.manage", "can_review_proposal_inputs": "proposal_inputs.review",
    "can_record_project_stage": "projects.record_stage",
    "can_review_associate_applications": "associates.applications.review",
    "can_view_onboardings": "onboardings.view", "can_view_surveys": "surveys.view",
    "can_review_surveys": "surveys.review", "can_clear_reverification": "proposals.clear_reverification",
    "can_verify_onboarding": "onboardings.verify", "can_resolve_attribution": "associates.attribution.resolve",
    "can_manage_territories": "territories.manage", "can_manage_seats": "associates.seats.manage",
    "can_view_payments": "payments.view", "can_record_payment": "payments.record",
    "can_process_payments": "payments.process", "can_view_commissions": "commissions.view",
    "can_process_commissions": "commissions.process", "can_manage_commission_rates": "commissions.rates.manage",
    "can_view_individual_rates": "commissions.individual_rates.view",
    "can_export_payout_bank_file": "payouts.bank_file.export",
    "can_view_network_summary": "associates.network_summary.view", "can_view_execution": "execution.view",
    "can_verify_execution": "execution.verify", "can_record_execution": "execution.record",
    "can_handle_service_requests": "service_requests.handle",
}


class TestCatalogue:
    def test_every_role_holds_exactly_the_expected_permissions(self):
        expected = expected_matrix()
        for role in EmployeeRole:
            got = set(access.load().roles[role.value].permissions)
            assert got == expected[role.value], (role.value, sorted(got ^ expected[role.value]))

    def test_admin_is_everything_but_the_bank_file(self):
        assert set(access.load().roles["admin"].permissions) == set(access.load().permissions) - {
            "payouts.bank_file.export"}

    def test_every_gate_maps_to_exactly_one_permission(self):
        src = inspect.getsource(AuthenticatedEmployee)
        gates = dict(re.findall(r'def (can_\w+)\(self\) -> bool:\n(?:        """[\s\S]*?"""\n)?'
                                r'        return self\.has\("([\w.]+)"\)\n', src))
        assert gates == GATE_PERMISSION
        assert len(set(gates.values())) == len(gates)

    @pytest.mark.parametrize("role", [r for r in EmployeeRole])
    @pytest.mark.parametrize("gate", sorted(GATE_PERMISSION))
    def test_each_gate_answers_from_the_matrix(self, role, gate):
        employee = AuthenticatedEmployee(employee_id="x", role=role)
        assert getattr(employee, gate)() is (GATE_PERMISSION[gate] in expected_matrix()[role.value])

    def test_no_permission_is_unused(self):
        code = "\n".join(p.read_text() for p in (REPO / "app").rglob("*.py"))
        for perm in access.load().permissions:
            assert f'"{perm}"' in code, f"{perm} is never checked in app/"

    def test_an_unknown_permission_is_a_bug_not_a_no(self):
        with pytest.raises(KeyError):
            AuthenticatedEmployee(employee_id="x", role=EmployeeRole.admin).holds("leads.teleport")

    def test_exclusive_roles_cannot_combine(self):
        assert access.combination_problem({"sales", "engineer"})
        assert access.combination_problem({"engineer", "operations"})
        assert access.combination_problem(set()) is not None
        assert access.combination_problem({"admin", "accounts"}) is None
        assert access.combination_problem({"management", "sales"}) is None

    def test_matrix_route_for_any_staff(self, client):
        r = client.get(f"{D}/access/matrix", headers={"X-Employee-Id": "emp-o", "X-Employee-Role": "operations"})
        assert r.status_code == 200
        body = r.json()
        assert {p["id"] for p in body["permissions"]} == set(access.load().permissions)
        nm = next(r for r in body["roles"] if r["id"] == "network_manager")
        assert set(nm["permissions"]) == NETWORK_MANAGER and nm["can_assign_roles"] == ["sales", "engineer",
                                                                                       "operations"]


# ── Several roles ────────────────────────────────────────────────────────────────

def multi(employee_id: str, *roles: str, two_factor: bool = True) -> dict:
    h = {"X-Employee-Id": employee_id, "X-Employee-Role": roles[0], "X-Employee-Roles": ",".join(roles)}
    if not two_factor:
        h["X-Employee-Two-Factor"] = "off"
    return h


class TestUnion:
    def test_admin_plus_accounts_can_export_the_bank_file_admin_alone_cannot(self):
        both = AuthenticatedEmployee(employee_id="t", role=EmployeeRole.admin,
                                     roles=[EmployeeRole.admin, EmployeeRole.accounts])
        assert both.can_export_payout_bank_file() and both.can_manage_employees()
        assert not AuthenticatedEmployee(employee_id="t", role=EmployeeRole.admin).can_export_payout_bank_file()

    def test_management_plus_sales_sees_every_lead(self):
        from app.auth.scoping import lead_scope
        e = AuthenticatedEmployee(employee_id="ms", role=EmployeeRole.management,
                                  roles=[EmployeeRole.management, EmployeeRole.sales])
        assert lead_scope(e).everything and e.can_send_proposal() and e.can_assign_lead()

    def test_engineer_plus_management_is_not_technical_only(self):
        e = AuthenticatedEmployee(employee_id="em", role=EmployeeRole.management,
                                  roles=[EmployeeRole.management, EmployeeRole.engineer])
        assert not e.technical_only and e.can_review_surveys()
        assert AuthenticatedEmployee(employee_id="e", role=EmployeeRole.engineer).technical_only

    def test_me_reports_roles_and_permissions(self, client):
        r = client.get(ME, headers=multi("emp-tanmoy", "admin", "accounts"))
        assert r.status_code == 200
        body = r.json()
        assert body["roles"] == ["accounts", "admin"] and body["role"] == "admin"
        assert "payouts.bank_file.export" in body["permissions"] and body["two_factor_ok"] is True
        assert body["requires_two_factor"] is True


# ── The database decides ─────────────────────────────────────────────────────────

def token(monkeypatch, sub: str, **claims):
    payload = {"sub": sub, "public_metadata": claims.pop("metadata", {}), **claims}
    monkeypatch.setattr(deps, "_verify_clerk_jwt", lambda t: payload)
    return {"Authorization": "Bearer t"}


def seed(db_session, employee_id: str, *roles: str, email: str | None = None, active: bool = True) -> Employee:
    emp = Employee(id=employee_id, name=employee_id, email=email or f"{employee_id}@gridnova.test",
                   role=EmployeeRole(access.primary_role(roles)), is_active=active)
    db_session.add(emp)
    db_session.flush()
    for role in roles:
        db_session.add(EmployeeRoleGrant(employee_id=employee_id, role=role, granted_by="test"))
    db_session.commit()
    return emp


def admins(db_session, *ids: str) -> None:
    for employee_id in ids:
        seed(db_session, employee_id, "admin")


class TestDatabaseAuthority:
    def test_role_removed_is_refused_on_the_next_request_with_the_same_token(self, client, db_session, monkeypatch):
        admins(db_session, "user_a1", "user_a2", "user_a3")
        h = token(monkeypatch, "user_a3", metadata={"role": "admin"}, fva=[1, 1])
        assert client.get(f"{D}/employees", headers=h).status_code == 200
        r = client.put(f"{D}/employees/user_a3/roles", json={"roles": ["sales"]}, headers=admin_headers())
        assert r.status_code == 202   # removing Admin waits for a second Admin
        # Approve as another Admin (not the requester).
        req_id = r.json()["approval_request"]["approval_request_id"]
        approve = client.post(f"{D}/approvals/{req_id}/approve", headers={"X-Employee-Id": "user_a1",
                                                                           "X-Employee-Role": "admin"})
        assert approve.status_code == 200, approve.text
        # The token still says admin; the database says sales.
        assert client.get(f"{D}/employees", headers=h).status_code == 403
        assert client.get(ME, headers=h).json()["roles"] == ["sales"]

    def test_deactivated_is_refused_on_the_next_request(self, client, db_session, monkeypatch):
        seed(db_session, "user_s1", "sales")
        h = token(monkeypatch, "user_s1", metadata={"role": "sales"})
        assert client.get(ME, headers=h).status_code == 200
        bans = []
        monkeypatch.setattr("app.auth.clerk_client.ban_user", lambda uid: bans.append(uid) or True)
        r = client.patch(f"{D}/employees/user_s1/deactivate", headers=admin_headers())
        assert r.status_code == 200 and r.json()["clerk_banned"] is True and r.json()["sessions_revoked"] is True
        assert bans == ["user_s1"]
        assert client.get(ME, headers=h).status_code == 403
        actions = [a.action for a in db_session.query(AuditLog).filter_by(entity_id="user_s1")]
        assert "deactivated" in actions and "sessions_revoked" in actions

    def test_reactivate_unbans_and_keeps_roles(self, client, db_session, monkeypatch):
        seed(db_session, "user_r1", "engineer", active=False)
        unbans = []
        monkeypatch.setattr("app.auth.clerk_client.unban_user", lambda uid: unbans.append(uid) or True)
        r = client.patch(f"{D}/employees/user_r1/reactivate", headers=admin_headers())
        assert r.status_code == 200 and r.json()["roles"] == ["engineer"] and r.json()["clerk_unbanned"] is True
        assert unbans == ["user_r1"]

    def test_a_ban_failure_leaves_the_deactivation_standing(self, client, db_session, monkeypatch):
        from app.auth.clerk_client import ClerkSyncError
        seed(db_session, "user_s2", "sales")

        def _fail(uid):
            raise ClerkSyncError("down")
        monkeypatch.setattr("app.auth.clerk_client.ban_user", _fail)
        r = client.patch(f"{D}/employees/user_s2/deactivate", headers=admin_headers())
        assert r.status_code == 200 and r.json()["clerk_banned"] is False and r.json()["clerk_error"]
        db_session.expire_all()
        assert db_session.get(Employee, "user_s2").is_active is False

    def test_a_role_claim_alone_is_not_invited(self, client, monkeypatch):
        r = client.get(ME, headers=token(monkeypatch, "user_new", metadata={"role": "sales"}, email="x@gmail.com"))
        assert r.status_code == 403 and r.json()["detail"]["code"] == "not_invited"

    def test_bootstrap_admin_path_is_audited(self, client, db_session, monkeypatch):
        r = client.get(ME, headers=token(monkeypatch, "user_boot", metadata={"role": "admin"}, email="b@gridnova.in"))
        assert r.status_code == 200 and r.json()["roles"] == ["admin"]
        grant = db_session.query(AuditLog).filter_by(entity_id="user_boot", action="role_granted").one()
        assert grant.performed_by == "bootstrap"

    def test_an_invited_user_never_takes_the_bootstrap_path(self, client, db_session, monkeypatch):
        # Their invitation was revoked; their Clerk metadata still says admin.
        inv = invitation(db_session, "rev@gridnova.in", ["admin"], status=StaffInvitationStatus.revoked)
        h = token(monkeypatch, "user_rev", metadata={"role": "admin", "staff_invitation_id": inv.id},
                  email="rev@gridnova.in")
        r = client.get(ME, headers=h)
        assert r.status_code == 403 and r.json()["detail"]["code"] == "not_invited"


# ── Network Manager ──────────────────────────────────────────────────────────────

NM = {"X-Employee-Id": "emp-nm-1", "X-Employee-Role": "network_manager"}


class TestNetworkManager:
    @pytest.mark.parametrize("path", ["/associate-applications", "/associates", "/territory-seats",
                                      "/network/summary", "/onboardings", "/audit-log", "/employees",
                                      "/staff/invitations", "/leads", "/proposals"])
    def test_allowed(self, client, path):
        assert client.get(f"{D}{path}", headers=NM).status_code == 200

    @pytest.mark.parametrize("method,path", [
        ("GET", "/payments"), ("GET", "/commissions"), ("GET", "/commission-rate-schedules"),
        ("GET", "/payout-batches"), ("GET", "/meetings"), ("GET", "/analytics/rep-performance"),
        ("GET", "/placeholders"), ("POST", "/internal/check-stale-leads"),
    ])
    def test_forbidden(self, client, method, path):
        r = client.request(method, f"{D}{path}", headers=NM)
        assert r.status_code == 403, (path, r.status_code)

    def test_audit_log_is_associate_records_only(self, client, db_session):
        db_session.add_all([AuditLog(entity_type="associate", entity_id="GNA-1", action="x", performed_by="a"),
                            AuditLog(entity_type="payment", entity_id="1", action="y", performed_by="a")])
        db_session.commit()
        kinds = {i["entity_type"] for i in client.get(f"{D}/audit-log", headers=NM).json()["items"]}
        assert kinds == {"associate"}
        assert client.get(f"{D}/audit-log?entity_type=payment", headers=NM).json()["items"] == []

    @pytest.mark.parametrize("role", ["admin", "management", "accounts", "network_manager"])
    def test_cannot_invite_above_its_scope(self, client, role):
        r = client.post(f"{D}/staff/invitations", json={"name": "X", "email": f"{role}@gmail.com", "roles": [role]},
                        headers=NM)
        assert r.status_code == 403 and r.json()["detail"]["code"] == "role_not_assignable"

    def test_invites_sales(self, client):
        r = client.post(f"{D}/staff/invitations", json={"name": "Rep", "email": "Rep@Gmail.com", "roles": ["sales"]},
                        headers=NM)
        assert r.status_code == 201 and r.json()["email"] == "rep@gmail.com" and r.json()["status"] == "pending"

    def test_cannot_promote_or_touch_an_admin(self, client, db_session):
        seed(db_session, "emp-sales-7", "sales")
        admins(db_session, "emp-admin-7")
        assert client.put(f"{D}/employees/emp-sales-7/roles", json={"roles": ["management"]},
                          headers=NM).status_code == 403
        assert client.put(f"{D}/employees/emp-sales-7/roles", json={"roles": ["operations"]},
                          headers=NM).status_code == 200
        assert client.patch(f"{D}/employees/emp-admin-7/deactivate", headers=NM).status_code == 403
        assert client.patch(f"{D}/employees/emp-sales-7/deactivate", headers=NM).status_code == 200

    def test_sees_every_associate_sourced_customer_but_no_survey_prices(self):
        e = AuthenticatedEmployee(employee_id="nm", role=EmployeeRole.network_manager)
        from app.auth.scoping import lead_scope, onboarding_lead_scope
        assert onboarding_lead_scope(e).everything and lead_scope(e).everything
        assert not e.holds("surveys.view_prices") and not e.can_view_payments()

    @pytest.mark.usefixtures("known_employees")
    def test_opens_any_proposal_and_adjusts_its_price_but_never_approves_or_sends(self, client, memory, db_session):
        from tests.test_adjust_pricing import _seed_proposal
        _, _, pid = _seed_proposal(memory, db_session, email="nm-prop@example.com", total_value=100_000,
                                   assigned_to="emp-sales-1")
        detail = client.get(f"{D}/proposals/{pid}", headers=NM)
        assert detail.status_code == 200 and detail.json()["total_value"] == 100_000
        assert detail.json()["customer"]["email"] == "nm-prop@example.com"
        assert client.get(f"{D}/proposals/{pid}/document", headers=NM).status_code == 200
        assert client.get(f"{D}/proposals/{pid}/pdf", headers=NM).status_code == 200
        r = client.patch(f"{D}/proposals/{pid}/adjust-pricing", json={"new_value": 120_000, "reason": "owner"},
                         headers=NM)
        assert r.status_code == 200 and r.json()["total_value"] == 120_000
        for action in ("approve", "manager-sign-off", "send"):
            assert client.post(f"{D}/proposals/{pid}/{action}", json={}, headers=NM).status_code == 403, action
        no_2fa = {**NM, "X-Employee-Two-Factor": "off"}
        r = client.patch(f"{D}/proposals/{pid}/adjust-pricing", json={"new_value": 110_000, "reason": "x"},
                         headers=no_2fa)
        assert r.status_code == 403 and r.json()["detail"]["code"] == "two_factor_required"


# ── Role changes, the floor, handover ───────────────────────────────────────────

class TestRoleChanges:
    def test_replace_roles_audits_each_grant_and_revocation(self, client, db_session):
        seed(db_session, "emp-x", "sales")
        r = client.put(f"{D}/employees/emp-x/roles", json={"roles": ["management", "sales"]}, headers=admin_headers())
        assert r.status_code == 200 and r.json()["roles"] == ["management", "sales"] and r.json()["role"] == "management"
        acts = {(a.action, a.after_value["role"]) for a in db_session.query(AuditLog).filter_by(entity_id="emp-x")}
        assert ("role_granted", "management") in acts
        r = client.put(f"{D}/employees/emp-x/roles", json={"roles": ["management"]}, headers=admin_headers())
        assert ("role_revoked", "sales") in {(a.action, a.after_value["role"])
                                             for a in db_session.query(AuditLog).filter_by(entity_id="emp-x")}

    def test_exclusive_combination_refused(self, client, db_session):
        seed(db_session, "emp-y", "sales")
        r = client.put(f"{D}/employees/emp-y/roles", json={"roles": ["sales", "engineer"]}, headers=admin_headers())
        assert r.status_code == 422 and r.json()["detail"]["code"] == "role_combination_not_supported"

    def test_two_admin_floor(self, client, db_session):
        admins(db_session, "emp-admin-1", "emp-admin-2")
        r = client.patch(f"{D}/employees/emp-admin-2/deactivate", headers=admin_headers())
        assert r.status_code == 409 and r.json()["detail"]["code"] == "admin_floor"
        admins(db_session, "emp-admin-3")
        assert client.patch(f"{D}/employees/emp-admin-2/deactivate", headers=admin_headers()).status_code == 200

    def test_cannot_remove_own_admin_role(self, client, db_session):
        admins(db_session, "emp-admin-1", "emp-admin-2", "emp-admin-3")
        r = client.put(f"{D}/employees/emp-admin-1/roles", json={"roles": ["sales"]}, headers=admin_headers())
        assert r.status_code == 409 and r.json()["detail"]["code"] == "own_admin_role"

    def test_floor_also_guards_a_role_change(self, client, db_session):
        admins(db_session, "emp-admin-1", "emp-admin-2")
        r = client.put(f"{D}/employees/emp-admin-2/roles", json={"roles": ["sales"]}, headers=admin_headers())
        assert r.status_code == 409 and r.json()["detail"]["code"] == "admin_floor"

    def test_legacy_role_route_shares_the_rules(self, client, db_session):
        seed(db_session, "emp-z", "sales")
        r = client.post(f"{D}/employees/emp-z/role", json={"role": "accounts"}, headers=admin_headers())
        assert r.status_code == 202 and r.json()["status"] == "pending_approval"
        db_session.expire_all()
        assert db_session.get(Employee, "emp-z").role == EmployeeRole.sales

    def test_deactivate_returns_the_handover_and_reassign_moves_only_open_leads(self, client, db_session, memory):
        seed(db_session, "emp-sales-5", "sales")
        seed(db_session, "emp-sales-6", "sales")
        customer = memory.get_or_create_customer(email="h@example.com", name="H")
        open_lead = memory.create_lead(customer_id=customer.id, source=LeadSource.other)
        won = memory.create_lead(customer_id=customer.id, source=LeadSource.other)
        for lead in (open_lead, won):
            lead.assigned_employee_id = "emp-sales-5"
        won.pipeline_stage = PipelineStage.won
        db_session.commit()
        r = client.patch(f"{D}/employees/emp-sales-5/deactivate", headers=admin_headers())
        assert [x["lead_id"] for x in r.json()["handover"]["open_leads"]] == [open_lead.id]
        r = client.post(f"{D}/employees/emp-sales-5/reassign", json={"to_employee_id": "emp-sales-6"},
                        headers=admin_headers())
        assert r.status_code == 200 and r.json()["lead_ids"] == [open_lead.id]
        db_session.expire_all()
        assert db_session.get(Lead, open_lead.id).assigned_employee_id == "emp-sales-6"
        assert db_session.get(Lead, won.id).assigned_employee_id == "emp-sales-5"
        assert db_session.query(AuditLog).filter_by(entity_id="emp-sales-5", action="leads_reassigned").count() == 1

    def test_reassign_refuses_someone_who_takes_no_leads(self, client, db_session):
        seed(db_session, "emp-sales-8", "sales")
        seed(db_session, "emp-ops-8", "operations")
        r = client.post(f"{D}/employees/emp-sales-8/reassign", json={"to_employee_id": "emp-ops-8"},
                        headers=admin_headers())
        assert r.status_code == 422 and r.json()["detail"]["code"] == "not_assignable"


# ── Invitations ──────────────────────────────────────────────────────────────────

def invitation(db_session, email, roles, *, status=StaffInvitationStatus.pending, expires_in=timedelta(days=7),
               invited_by="emp-admin-1") -> StaffInvitation:
    import uuid
    now = datetime.now(timezone.utc)
    inv = StaffInvitation(id=uuid.uuid4().hex, email=email, name="Invited", roles=roles, status=status,
                          invited_by=invited_by, created_at=now, expires_at=now + expires_in)
    db_session.add(inv)
    db_session.commit()
    return inv


class TestInvitations:
    def test_create_sends_a_clerk_invitation_with_the_roles(self, client, monkeypatch):
        sent = []
        monkeypatch.setattr("app.auth.clerk_client.create_staff_invitation",
                            lambda email, md, redirect, days: sent.append((email, md, days)) or (True, "inv_1"))
        r = client.post(f"{D}/staff/invitations", json={"name": "Abhijit", "email": "ab@gridnova.in",
                                                        "roles": ["network_manager"]}, headers=admin_headers())
        assert r.status_code == 201 and r.json()["clerk_status"] == "sent"
        [(email, md, days)] = sent
        assert email == "ab@gridnova.in" and md["roles"] == ["network_manager"] and md["role"] == "network_manager"
        assert md["staff_invitation_id"] == r.json()["invitation_id"] and days == 7

    def test_clerk_refusal_saves_nothing(self, client, db_session, monkeypatch):
        from app.auth.clerk_client import ClerkSyncError

        def _fail(*a):
            raise ClerkSyncError("exists")
        monkeypatch.setattr("app.auth.clerk_client.create_staff_invitation", _fail)
        r = client.post(f"{D}/staff/invitations", json={"name": "A", "email": "a@gmail.com", "roles": ["sales"]},
                        headers=admin_headers())
        assert r.status_code == 502 and db_session.query(StaffInvitation).count() == 0

    def test_duplicate_email_and_existing_staff(self, client, db_session):
        body = {"name": "A", "email": "dup@gmail.com", "roles": ["sales"]}
        assert client.post(f"{D}/staff/invitations", json=body, headers=admin_headers()).status_code == 201
        r = client.post(f"{D}/staff/invitations", json=body, headers=admin_headers())
        assert r.status_code == 409 and r.json()["detail"]["code"] == "invitation_exists"
        seed(db_session, "emp-q", "sales", email="staff@gmail.com")
        r = client.post(f"{D}/staff/invitations", json={**body, "email": "STAFF@gmail.com"}, headers=admin_headers())
        assert r.status_code == 409 and r.json()["detail"]["code"] == "already_staff"

    def test_admin_invitation_waits_for_a_second_admin(self, client, db_session):
        r = client.post(f"{D}/staff/invitations", json={"name": "R", "email": "rp@gridnova.in", "roles": ["admin"]},
                        headers=admin_headers())
        assert r.status_code == 202 and db_session.query(StaffInvitation).count() == 0

    def test_resend_refreshes_expiry_and_replaces_the_clerk_invitation(self, client, db_session, monkeypatch):
        inv = invitation(db_session, "re@gmail.com", ["sales"], expires_in=timedelta(days=-1))
        inv.clerk_invitation_id = "inv_old"
        db_session.commit()
        revoked = []
        monkeypatch.setattr("app.auth.clerk_client.revoke_invitation", lambda i: revoked.append(i) or True)
        monkeypatch.setattr("app.auth.clerk_client.create_staff_invitation", lambda *a: (True, "inv_new"))
        assert client.get(f"{D}/staff/invitations?status=expired", headers=admin_headers()).json()["items"]
        r = client.post(f"{D}/staff/invitations/{inv.id}/resend", headers=admin_headers())
        assert r.status_code == 200 and r.json()["status"] == "pending" and revoked == ["inv_old"]
        assert datetime.fromisoformat(r.json()["expires_at"]) > datetime.now(timezone.utc) + timedelta(days=6)

    def test_revoke_here_and_in_clerk(self, client, db_session, monkeypatch):
        inv = invitation(db_session, "rv@gmail.com", ["sales"])
        inv.clerk_invitation_id = "inv_rv"
        db_session.commit()
        revoked = []
        monkeypatch.setattr("app.auth.clerk_client.revoke_invitation", lambda i: revoked.append(i) or True)
        r = client.delete(f"{D}/staff/invitations/{inv.id}", headers=admin_headers())
        assert r.status_code == 200 and r.json()["status"] == "revoked" and revoked == ["inv_rv"]

    def test_accept_by_verified_email_creates_the_employee_with_the_roles(self, client, db_session, monkeypatch):
        inv = invitation(db_session, "new@gmail.com", ["engineer"])
        monkeypatch.setattr("app.auth.clerk_client.verified_emails", lambda uid: ["new@gmail.com"])
        r = client.get(ME, headers=token(monkeypatch, "user_new1", metadata={"staff_invitation_id": inv.id,
                                                                             "role": "engineer"}))
        assert r.status_code == 200 and r.json()["roles"] == ["engineer"]
        db_session.expire_all()
        assert db_session.get(StaffInvitation, inv.id).status == StaffInvitationStatus.accepted
        assert db_session.get(Employee, "user_new1").email == "new@gmail.com"

    def test_an_unverified_address_is_not_accepted(self, client, db_session, monkeypatch):
        inv = invitation(db_session, "unv@gmail.com", ["sales"])
        monkeypatch.setattr("app.auth.clerk_client.verified_emails", lambda uid: [])
        r = client.get(ME, headers=token(monkeypatch, "user_unv", metadata={"staff_invitation_id": inv.id},
                                         email="unv@gmail.com"))
        assert r.status_code == 403 and r.json()["detail"]["code"] == "not_invited"

    def test_expired_invitation(self, client, db_session, monkeypatch):
        invitation(db_session, "old@gmail.com", ["sales"], expires_in=timedelta(hours=-1))
        monkeypatch.setattr("app.auth.clerk_client.verified_emails", lambda uid: ["old@gmail.com"])
        r = client.get(ME, headers=token(monkeypatch, "user_old"))
        assert r.status_code == 403 and r.json()["detail"]["code"] == "invitation_expired"

    def test_inviter_scope_on_resend_and_revoke(self, client, db_session):
        inv = invitation(db_session, "acc@gmail.com", ["accounts"])
        assert client.post(f"{D}/staff/invitations/{inv.id}/resend", headers=NM).status_code == 403
        assert client.delete(f"{D}/staff/invitations/{inv.id}", headers=NM).status_code == 403

    def test_management_cannot_invite(self, client):
        r = client.post(f"{D}/staff/invitations", json={"name": "A", "email": "m@gmail.com", "roles": ["sales"]},
                        headers={"X-Employee-Id": "emp-mgmt-1", "X-Employee-Role": "management"})
        assert r.status_code == 403


# ── Maker–checker ────────────────────────────────────────────────────────────────

class TestSecondApproval:
    def _request_admin_grant(self, client, db_session):
        admins(db_session, "emp-admin-1", "emp-admin-2")
        seed(db_session, "emp-m", "management")
        r = client.put(f"{D}/employees/emp-m/roles", json={"roles": ["admin", "management"]},
                       headers=admin_headers())
        assert r.status_code == 202
        return r.json()["approval_request"]["approval_request_id"]

    def test_nothing_changes_while_pending_and_the_requester_cannot_approve(self, client, db_session):
        req_id = self._request_admin_grant(client, db_session)
        db_session.expire_all()
        assert db_session.query(EmployeeRoleGrant).filter_by(employee_id="emp-m").count() == 1
        r = client.post(f"{D}/approvals/{req_id}/approve", headers=admin_headers())
        assert r.status_code == 403 and r.json()["detail"]["code"] == "self_approval"

    def test_a_second_admin_applies_it(self, client, db_session):
        req_id = self._request_admin_grant(client, db_session)
        r = client.post(f"{D}/approvals/{req_id}/approve", json={"note": "ok"},
                        headers={"X-Employee-Id": "emp-admin-2", "X-Employee-Role": "admin"})
        assert r.status_code == 200 and r.json()["status"] == "approved"
        db_session.expire_all()
        assert sorted(g.role for g in db_session.query(EmployeeRoleGrant).filter_by(employee_id="emp-m")) == [
            "admin", "management"]

    def test_an_accounts_holder_cannot_approve_a_role_grant(self, client, db_session):
        req_id = self._request_admin_grant(client, db_session)
        r = client.post(f"{D}/approvals/{req_id}/approve", headers={"X-Employee-Id": "emp-acc", "X-Employee-Role":
                                                                     "accounts"})
        assert r.status_code == 403

    def test_reject_and_expiry(self, client, db_session):
        req_id = self._request_admin_grant(client, db_session)
        other = {"X-Employee-Id": "emp-admin-2", "X-Employee-Role": "admin"}
        assert client.post(f"{D}/approvals/{req_id}/reject", headers=other).json()["status"] == "rejected"
        req2 = client.put(f"{D}/employees/emp-m/roles", json={"roles": ["admin", "management"]},
                          headers=admin_headers()).json()["approval_request"]["approval_request_id"]
        row = db_session.get(ApprovalRequest, req2)
        row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        db_session.commit()
        r = client.post(f"{D}/approvals/{req2}/approve", headers=other)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "approval_expired"
        db_session.expire_all()
        assert db_session.get(ApprovalRequest, req2).status == ApprovalRequestStatus.expired

    def test_stale_request_is_refused(self, client, db_session):
        req_id = self._request_admin_grant(client, db_session)
        client.put(f"{D}/employees/emp-m/roles", json={"roles": ["sales"]}, headers=admin_headers())
        r = client.post(f"{D}/approvals/{req_id}/approve", headers={"X-Employee-Id": "emp-admin-2",
                                                                     "X-Employee-Role": "admin"})
        assert r.status_code == 409 and r.json()["detail"]["code"] == "approval_stale"

    def test_switch_off_applies_at_once(self, client, db_session, monkeypatch):
        cfg = access.load()
        monkeypatch.setitem(cfg.second_approval, "role.accounts", False)
        seed(db_session, "emp-acc2", "sales")
        r = client.put(f"{D}/employees/emp-acc2/roles", json={"roles": ["accounts", "sales"]}, headers=admin_headers())
        assert r.status_code == 200

    def test_commission_switch_on_defers_the_rate_publish(self, client, db_session, monkeypatch):
        cfg = access.load()
        monkeypatch.setitem(cfg.second_approval, "commissions.standard_rate_publish", True)
        from app.commissions import rates
        body = {"effective_from": "2030-01-01", "is_placeholder": True,
                "rates": {k: "1" for k in rates.RATE_KEYS}}
        r = client.post(f"{D}/commission-rate-schedules", json=body, headers=admin_headers())
        assert r.status_code == 202
        assert db_session.query(ApprovalRequest).one().action == "commissions.standard_rate_publish"

    def test_a_leavers_pending_requests_are_withdrawn(self, client, db_session):
        req_id = self._request_admin_grant(client, db_session)
        admins(db_session, "emp-admin-3", "emp-admin-4")
        r = client.patch(f"{D}/employees/emp-admin-1/deactivate", headers={"X-Employee-Id": "emp-admin-2",
                                                                           "X-Employee-Role": "admin"})
        assert r.status_code == 200 and r.json()["approval_requests_withdrawn"] == [req_id]
        db_session.expire_all()
        assert db_session.get(ApprovalRequest, req_id).status == ApprovalRequestStatus.cancelled

    def test_eligible_approvers_are_notified(self, client, db_session, monkeypatch):
        events = []
        monkeypatch.setattr("app.notifications.notifier.notifier.notify", lambda e, p: events.append((e, p)))
        self._request_admin_grant(client, db_session)
        [(event, payload)] = [e for e in events if e[0] == "approval_requested"]
        assert payload["recipients"] == ["emp-admin-2@gridnova.test"]


# ── Two-step sign-in ─────────────────────────────────────────────────────────────

class TestTwoFactor:
    def test_sensitive_refused_without_a_second_factor(self, client, db_session, monkeypatch):
        admins(db_session, "user_2fa")
        r = client.get(f"{D}/employees", headers=token(monkeypatch, "user_2fa", fva=[3, -1]))
        assert r.status_code == 403 and r.json()["detail"]["code"] == "two_factor_required"

    def test_sensitive_allowed_with_it(self, client, db_session, monkeypatch):
        admins(db_session, "user_2fb")
        assert client.get(f"{D}/employees", headers=token(monkeypatch, "user_2fb", fva=[3, 2])).status_code == 200

    def test_non_sensitive_stays_open(self, client, db_session, monkeypatch):
        admins(db_session, "user_2fc")
        h = token(monkeypatch, "user_2fc")
        assert client.get(ME, headers=h).json()["two_factor_ok"] is False
        assert client.get(f"{D}/leads", headers=h).status_code == 200
        assert client.get(f"{D}/access/matrix", headers=h).status_code == 200

    def test_a_role_without_the_requirement_is_not_blocked(self, client, db_session, monkeypatch):
        seed(db_session, "user_sales_2f", "sales")
        e = AuthenticatedEmployee(employee_id="s", role=EmployeeRole.sales, two_factor_ok=False)
        assert e.can_adjust_pricing()   # sensitive, but Sales does not require two-step

    def test_dev_header_can_switch_it_off(self, client):
        r = client.get(f"{D}/employees", headers=multi("emp-admin-1", "admin", two_factor=False))
        assert r.status_code == 403 and r.json()["detail"]["code"] == "two_factor_required"


# ── Alerts ───────────────────────────────────────────────────────────────────────

class TestAlerts:
    def test_role_holders_plus_fixed_addresses_deduplicated(self, db_session):
        from app.notifications import alerts
        seed(db_session, "acc-1", "accounts", email="accounts.person@gridnova.in")
        seed(db_session, "acc-2", "accounts", email="Shared@gridnova.in")
        to = alerts.recipients("payment_recorded", {}, ["shared@gridnova.in"])
        assert to == ["shared@gridnova.in", "accounts.person@gridnova.in"]

    def test_deactivated_employee_never_emailed(self, db_session):
        from app.notifications import alerts
        seed(db_session, "acc-3", "accounts", email="gone@gridnova.in", active=False)
        assert alerts.recipients("payment_recorded", {}, ["gone@gridnova.in", "info@gridnova.in"]) == [
            "info@gridnova.in"]

    def test_assigned_employee_included(self, db_session, memory):
        from app.notifications import alerts
        seed(db_session, "emp-sales-a", "sales", email="rep@gridnova.in")
        customer = memory.get_or_create_customer(email="c@example.com", name="C")
        lead = memory.create_lead(customer_id=customer.id, source=LeadSource.other)
        lead.assigned_employee_id = "emp-sales-a"
        db_session.commit()
        assert "rep@gridnova.in" in alerts.recipients("new_lead", {"lead_id": lead.id}, [])

    def test_missing_holder_warns_once(self, db_session, caplog, monkeypatch):
        from app.notifications import alerts
        monkeypatch.setattr(alerts, "_warned", set())
        with caplog.at_level(logging.WARNING, logger="app.notifications.alerts"):
            assert alerts.recipients("receipt_blocked", {}, []) == []
            alerts.recipients("receipt_blocked", {}, [])
        assert sum("reaches nobody" in r.getMessage() for r in caplog.records) == 1

    def test_the_alerts_page(self, client, db_session):
        seed(db_session, "acc-4", "accounts", email="a4@gridnova.in")
        events = {e["event"]: e for e in client.get(f"{D}/access/alerts", headers=admin_headers()).json()["events"]}
        assert events["payment_recorded"]["active_holders"] == {"accounts": 1}
        assert "associate_application_submitted" in events


# ── Regions ──────────────────────────────────────────────────────────────────────

class TestRegions:
    def test_unknown_region_refused(self, client, db_session):
        seed(db_session, "emp-ops-r", "operations")
        r = client.put(f"{D}/employees/emp-ops-r/regions", json={"regions": ["Atlantis"]}, headers=admin_headers())
        assert r.status_code == 422 and r.json()["detail"]["code"] == "unknown_region"

    def test_no_regions_means_every_region(self, db_session):
        from app.auth.scoping import region_state_ids
        e = AuthenticatedEmployee(employee_id="x", role=EmployeeRole.network_manager)
        assert region_state_ids(db_session, e) is None


# ── Migration and the bootstrap script ───────────────────────────────────────────

class TestMigration:
    """0056 down and up on a real database (SQLite always, PostgreSQL with TEST_DATABASE_URL),
    with the backfill checked on rows that existed before it."""

    def test_down_and_up_with_backfill(self, empty_database_url):
        from sqlalchemy import create_engine, inspect, text

        from tests.test_bootstrap_db import _run_bootstrap
        from tests.test_migration_truth import _alembic, _drift

        assert len("0056_access_control") <= 32
        assert _run_bootstrap(empty_database_url).returncode == 0
        engine = create_engine(empty_database_url)
        try:
            down = _alembic(empty_database_url, "downgrade", "0055_proposal_document_reference")
            assert down.returncode == 0, down.stderr
            insp = inspect(engine)
            assert not {"employee_roles", "staff_invitations", "approval_requests"} & set(insp.get_table_names())
            assert "regions" not in {c["name"] for c in insp.get_columns("employees")}
            with engine.begin() as conn:
                for eid, role in (("e-admin", "admin"), ("e-sales", "sales"), ("e-acc", "accounts")):
                    conn.execute(text("INSERT INTO employees (id, name, email, role, is_active, "
                                      "google_calendar_connected, created_at) VALUES (:i, :i, :e, :r, true, false, :t)"),
                                 {"i": eid, "e": f"{eid}@x.test", "r": role, "t": datetime.now(timezone.utc)})
            up = _alembic(empty_database_url, "upgrade", "head")
            assert up.returncode == 0, up.stderr
            with engine.connect() as conn:
                rows = sorted(conn.execute(text("SELECT employee_id, role, granted_by FROM employee_roles")).all())
            assert [tuple(r) for r in rows] == [("e-acc", "accounts", "migration_0056"),
                                                ("e-admin", "admin", "migration_0056"),
                                                ("e-sales", "sales", "migration_0056")]
            assert _drift(engine) == []
            with engine.begin() as conn:
                conn.execute(text("DELETE FROM employee_roles"))
                conn.execute(text("DELETE FROM employees"))
        finally:
            engine.dispose()


class TestGrantAdmin:
    def _module(self):
        return importlib.import_module("scripts.grant_admin")

    def test_refused_outside_the_migration_job(self):
        m = self._module()
        assert m.allowed_here("development", None) and m.allowed_here("test", None)
        assert not m.allowed_here("production", None) and not m.allowed_here("staging", "some-other-job")
        assert m.allowed_here("production", "gridnova-migrate")

    def test_idempotent_invitation_then_grant(self, db_session):
        m = self._module()
        m.grant(db_session, "Shouvik@GridNova.in", "Shouvik Biswas")
        db_session.commit()
        m.grant(db_session, "shouvik@gridnova.in", "Shouvik Biswas")
        db_session.commit()
        assert db_session.query(StaffInvitation).filter_by(email="shouvik@gridnova.in").count() == 1
        seed(db_session, "user_tanmoy", "accounts", email="tanmoyhalderxyz@gmail.com")
        m.grant(db_session, "tanmoyhalderxyz@gmail.com", "")
        db_session.commit()
        assert sorted(g.role for g in db_session.query(EmployeeRoleGrant).filter_by(employee_id="user_tanmoy")) == [
            "accounts", "admin"]
        assert "already" in m.grant(db_session, "tanmoyhalderxyz@gmail.com", "")
        grants = db_session.query(AuditLog).filter_by(entity_id="user_tanmoy", action="role_granted").all()
        assert [g.performed_by for g in grants] == ["bootstrap"]
