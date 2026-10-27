"""T5 (BE-3): the role matrix for the routers tests/test_permission_matrix.py does not
cover — the dashboard core (leads, proposals, analytics, employees, meetings, memory,
governance views), proposal inputs, equipment and agent governance.

Same contract as the original matrix: every route of a covered router has a row; each
listed role gets past authorisation (not 401/403), every other staff role gets 403, and
an associate credential is never a staff one (401). It has its own module (and its own,
lighter scenario: a lead, a proposal, a site assessment) because the original matrix's
scenario is the associate/onboarding world.

The ratchet at the bottom lists every operation in the app that NO matrix covers. A new
route outside a covered router fails it until it gets a row; the list may only shrink.
"""
from __future__ import annotations

import importlib

import pytest

from app.memory.models import Equipment, EquipmentCategory, LeadSource, Proposal, ProposalStatus
from tests.conftest import seed_employees

ROLES = ("admin", "management", "sales", "engineer", "operations", "accounts")
ALL = set(ROLES)
ADMIN = {"admin"}
ANALYSTS = {"admin", "management"}                     # can_view_full_analytics / audit logs
SELLERS = {"admin", "sales"}                           # draft, approve, send, edit, meetings
LEAD_READERS = {"admin", "management", "engineer", "sales"}   # can_access_lead
STAGE_CHANGERS = {"admin", "management", "sales"}
MEMORY_READERS = {"admin", "management", "sales"}
INPUT_REVIEWERS = {"admin", "engineer", "sales"}
NOT_ENGINEER = ALL - {"engineer"}                      # engineers never see pricing or analytics
PRICE_READERS = {"admin", "management", "sales"}       # lead readers minus engineers

COVERED_ROUTERS = ("dashboard", "proposal_inputs", "equipment", "agent_governance", "proposal_document",
                   "access_admin")
APPROVERS = {"admin", "accounts"}                     # staff.manage / commissions.process (BE-17 approvals)
DOCUMENT_EDITORS = {"admin", "sales", "engineer"}      # config/proposal_document.yaml edit_permissions
D = "/api/v1/dashboard"
PDF = b"%PDF-1.4 matrix test"

STAFF = {
    ("GET", f"{D}/leads"): (ALL, {}),
    ("GET", f"{D}/leads/export"): (ANALYSTS, {}),
    ("GET", f"{D}/proposals/export"): (ANALYSTS, {}),
    ("GET", f"{D}/leads/{{lead_id}}"): (LEAD_READERS, {}),
    ("PATCH", f"{D}/leads/{{lead_id}}/fields"): (SELLERS, {"json": {"fields": {"notes": "matrix"}}}),
    ("PATCH", f"{D}/leads/{{lead_id}}/stage"): (STAGE_CHANGERS, {"json": {"stage": "qualifying"}}),
    ("POST", f"{D}/leads"): (STAGE_CHANGERS, {"json": {"name": "Matrix Lead", "source": "referral",
                                                        "phone": "9800000001"}}),
    ("PATCH", f"{D}/leads/{{lead_id}}/assign"): (ANALYSTS, {"json": {"employee_id": "emp-sales-1"}}),
    ("POST", f"{D}/proposals/draft"): (SELLERS, {"json": {"lead_id": "{lead_id}", "customer_id": "{customer_id}"}}),
    ("POST", f"{D}/proposals/{{proposal_id}}/approve"): (SELLERS, {}),
    ("POST", f"{D}/proposals/{{proposal_id}}/manager-sign-off"): (ANALYSTS, {}),
    ("POST", f"{D}/proposals/{{proposal_id}}/request-revision"): (SELLERS, {"json": {"reason": "x"}}),
    ("PATCH", f"{D}/proposals/{{proposal_id}}/adjust-pricing"): (SELLERS, {"json": {"new_value": 1000.0,
                                                                                   "reason": "x"}}),
    ("POST", f"{D}/proposals/{{proposal_id}}/send"): (SELLERS, {}),
    ("GET", f"{D}/proposals/{{proposal_id}}/pdf"): (PRICE_READERS, {}),
    ("GET", f"{D}/proposals"): (ALL, {}),
    ("GET", f"{D}/proposals/{{proposal_id}}"): (LEAD_READERS, {}),
    ("GET", f"{D}/proposals/{{proposal_id}}/required-approval-tier"): (PRICE_READERS, {}),
    # BE-13 — the structured document (Engineers: technical part; edits per section)
    ("GET", f"{D}/proposals/{{proposal_id}}/document"): (LEAD_READERS, {}),
    ("PATCH", f"{D}/proposals/{{proposal_id}}/document"): (DOCUMENT_EDITORS, {"json": {
        "base_revision": 0, "section": "boq", "value": []}}),
    ("GET", f"{D}/proposals/{{proposal_id}}/preview.pdf"): (PRICE_READERS, {}),
    ("GET", f"{D}/analytics"): (NOT_ENGINEER, {}),
    ("GET", f"{D}/analytics/conversion-by-source"): (ANALYSTS, {}),
    ("GET", f"{D}/analytics/deal-size-by-tier"): (ANALYSTS, {}),
    ("GET", f"{D}/analytics/rep-performance"): (ANALYSTS, {}),
    ("GET", f"{D}/analytics/pipeline-trend"): (ANALYSTS, {}),
    ("GET", f"{D}/employees"): (ADMIN, {}),
    ("GET", f"{D}/employees/assignable"): (ANALYSTS, {}),
    ("GET", f"{D}/employees/me"): (ALL, {}),
    ("POST", f"{D}/employees/{{employee_id}}/role"): (ADMIN, {"json": {"role": "sales"}}),
    ("PATCH", f"{D}/employees/{{employee_id}}/deactivate"): (ADMIN, {}),
    ("PATCH", f"{D}/employees/{{employee_id}}/reactivate"): (ADMIN, {}),
    ("GET", f"{D}/audit-log"): (ANALYSTS, {}),
    ("GET", f"{D}/activity/recent"): (ALL, {}),
    ("GET", f"{D}/overview-stats"): (ALL, {}),
    ("GET", f"{D}/employees/me/calendar/connect"): (ALL, {}),
    ("POST", f"{D}/leads/{{lead_id}}/schedule-meeting"): (SELLERS, {"json": {"scheduled_at":
                                                                             "2030-01-07T05:00:00Z"}}),
    ("GET", f"{D}/meetings"): (STAGE_CHANGERS, {}),          # admin, management, sales
    ("POST", f"{D}/internal/check-stale-leads"): (ADMIN, {}),
    ("POST", f"{D}/memory/ask"): (MEMORY_READERS, {"json": {"question": "What did they ask about?",
                                                            "customer_id": "{customer_id}"}}),
    ("GET", f"{D}/customers/{{customer_id}}/memory"): (MEMORY_READERS, {}),
    ("POST", f"{D}/leads/{{lead_id}}/reflect"): (MEMORY_READERS, {}),
    ("GET", f"{D}/governance/agents"): (ANALYSTS, {}),
    ("GET", f"{D}/governance/runs"): (ANALYSTS, {}),
    ("GET", f"{D}/governance/activity"): (ANALYSTS, {}),
    # proposal inputs (site assessment)
    ("POST", "/api/v1/proposal-inputs/"): (SELLERS, {"json": {
        "lead_id": "{lead_id}", "customer_type": "individual", "project_family": "rooftop_solar",
        "state": "West Bengal", "district": "Kolkata", "pin_code": "700001", "site_type": "rooftop_residential",
        "connection_type": "on_grid"}}),
    ("GET", "/api/v1/proposal-inputs/{inputs_id}"): (LEAD_READERS, {}),
    ("GET", "/api/v1/proposal-inputs/"): (ALL, {}),
    ("PATCH", "/api/v1/proposal-inputs/{inputs_id}/review"): (INPUT_REVIEWERS, {"json": {"status": "under_review"}}),
    ("POST", "/api/v1/proposal-inputs/{inputs_id}/upload"): (SELLERS, {
        "files": {"file": ("bill.pdf", PDF, "application/pdf")},
        "data": {"target": "document", "document_type": "electricity_bill"}}),
    ("GET", "/api/v1/proposal-inputs/{inputs_id}/files/{file_ref}"): (LEAD_READERS, {}),
    # equipment catalogue
    ("GET", "/api/v1/equipment/"): (ALL, {}),
    ("POST", "/api/v1/equipment/"): (ADMIN, {"json": {"category": "module", "brand": "Matrix",
                                                      "model_name": "M-{role}"}}),
    ("PATCH", "/api/v1/equipment/{equipment_id}"): (ADMIN, {"json": {"specification": "x"}}),
    # agent governance (kill switch)
    ("GET", "/api/v1/governance/agents"): (ADMIN, {}),
    ("PATCH", "/api/v1/governance/agents/{agent_id}"): (ADMIN, {"json": {"enabled": True}}),
    ("GET", "/api/v1/governance/agents/{agent_id}/audit"): (ADMIN, {}),
    # BE-17 access control (app/api/routes/access_admin.py). The Network Manager — not one
    # of ROLES here — is covered in tests/test_access_control.py.
    ("GET", f"{D}/access/matrix"): (ALL, {}),
    ("GET", f"{D}/access/alerts"): (ALL, {}),
    ("POST", f"{D}/staff/invitations"): (ADMIN, {"json": {"name": "Matrix", "email": "matrix-{role}@gmail.com",
                                                         "roles": ["sales"]}}),
    ("GET", f"{D}/staff/invitations"): (ADMIN, {}),
    ("POST", f"{D}/staff/invitations/{{invitation_id}}/resend"): (ADMIN, {}),
    ("DELETE", f"{D}/staff/invitations/{{invitation_id}}"): (ADMIN, {}),
    ("PUT", f"{D}/employees/{{employee_id}}/roles"): (ADMIN, {"json": {"roles": ["sales"]}}),
    ("PUT", f"{D}/employees/{{employee_id}}/regions"): (ADMIN, {"json": {"regions": []}}),
    ("GET", f"{D}/employees/{{employee_id}}/handover"): (ADMIN, {}),
    ("POST", f"{D}/employees/{{employee_id}}/reassign"): (ADMIN, {"json": {"to_employee_id": "emp-sales-1"}}),
    ("GET", f"{D}/approvals"): (APPROVERS, {}),
    ("POST", f"{D}/approvals/{{request_id}}/approve"): (APPROVERS, {}),
    ("POST", f"{D}/approvals/{{request_id}}/reject"): (APPROVERS, {}),
    ("POST", f"{D}/approvals/{{request_id}}/cancel"): (APPROVERS, {}),
}

# Not staff routes: Google redirects the browser here after consent; it is authenticated
# by its single-use `state`, never by a staff credential (app/scheduling/oauth_state.py).
STATE_AUTHENTICATED = {("GET", f"{D}/employees/me/calendar/callback")}


def staff(role: str) -> dict:
    return {"X-Employee-Id": f"emp-{role}-1", "X-Employee-Role": role}


ASSOCIATE = {"X-Associate-Id": "GNA-WB-26-99999"}


@pytest.fixture
def scenario(client, db_session, memory, fake_storage, monkeypatch):
    from app.storage import r2 as r2_module
    from tests.conftest import STORAGE_URL_PREFIX
    monkeypatch.setattr(r2_module, "upload_bytes", lambda key, body, ct: f"{STORAGE_URL_PREFIX}{key}")
    seed_employees(db_session, *(f"emp-{r}-1" for r in ROLES), "emp-target-1")
    customer = memory.get_or_create_customer(email="matrix@example.com", name="Matrix Customer")
    db_session.commit()
    lead = memory.create_lead(customer_id=customer.id, source=LeadSource.website_chat)   # unassigned
    db_session.commit()
    proposal = Proposal(lead_id=lead.id, version=1, status=ProposalStatus.pending_approval, content={},
                        total_value=100000.0, requires_manager_signoff=False)
    equipment = Equipment(category=EquipmentCategory.module, brand="Matrix", model_name="M-1")
    db_session.add_all([proposal, equipment])
    db_session.commit()
    inputs = client.post("/api/v1/proposal-inputs/", headers=staff("sales"), json={
        "lead_id": lead.id, "customer_type": "individual", "project_family": "rooftop_solar",
        "state": "West Bengal", "district": "Kolkata", "pin_code": "700001", "site_type": "rooftop_residential",
        "connection_type": "on_grid"})
    assert inputs.status_code == 200, inputs.text
    inputs_id = inputs.json()["id"]
    upload = client.post(f"/api/v1/proposal-inputs/{inputs_id}/upload", headers=staff("sales"),
                         files={"file": ("bill.pdf", PDF, "application/pdf")},
                         data={"target": "document", "document_type": "electricity_bill"})
    assert upload.status_code == 200, upload.text
    from app.api.routes.proposal_inputs import uploaded_files
    from app.memory.models import ProposalInputs
    file_ref = uploaded_files(db_session.get(ProposalInputs, inputs_id))[0]["file_ref"]
    return {"lead_id": lead.id, "customer_id": customer.id, "proposal_id": proposal.id, "inputs_id": inputs_id,
            "equipment_id": equipment.id, "employee_id": "emp-target-1", "agent_id": "sales_agent",
            "file_ref": file_ref, "invitation_id": "no-such-invitation", "request_id": "no-such-request"}


def _fill(node, values: dict, role: str):
    if isinstance(node, dict):
        return {k: _fill(v, values, role) for k, v in node.items()}
    if isinstance(node, str):
        if node.startswith("{") and node.endswith("}") and node[1:-1] in values:
            return values[node[1:-1]]
        return node.replace("{role}", role)
    return node


def _call(client, method, template, who, kwargs, values, role="x"):
    path = template.format(**values)
    kw = {k: (_fill(v, values, role) if k in ("json", "data") else v) for k, v in kwargs.items()}
    return client.request(method, path, headers=who, **kw)


class TestMatrixIsComplete:
    def test_every_covered_route_has_a_row(self):
        routes = set()
        for name in COVERED_ROUTERS:
            for r in importlib.import_module(f"app.api.routes.{name}").router.routes:
                for method in r.methods:
                    routes.add((method, r.path))
        known = set(STAFF) | STATE_AUTHENTICATED
        assert routes == known, f"missing: {sorted(routes - known)}; stale: {sorted(known - routes)}"


@pytest.mark.parametrize("route", sorted(STAFF), ids=lambda r: f"{r[0]} {r[1]}")
def test_staff_route(client, scenario, route):
    method, template = route
    allowed, kwargs = STAFF[route]
    for role in sorted(ROLES, key=lambda r: r in allowed):     # refused roles first
        r = _call(client, method, template, staff(role), kwargs, scenario, role)
        if role in allowed:
            assert r.status_code not in (401, 403), f"{role} refused: {r.status_code} {r.text[:200]}"
        else:
            assert r.status_code == 403, f"{role} got {r.status_code} {r.text[:200]}"
    assert _call(client, method, template, ASSOCIATE, kwargs, scenario).status_code == 401
    assert _call(client, method, template, {}, kwargs, scenario).status_code == 401


def test_the_calendar_callback_needs_no_staff_credential_but_a_valid_state(client):
    r = client.get(f"{D}/employees/me/calendar/callback", params={"code": "x", "state": "forged"},
                   follow_redirects=False)
    assert r.status_code not in (401, 403)
    assert r.status_code in (302, 303, 307, 400) or r.is_redirect


# ── Ratchet: operations no matrix covers ─────────────────────────────────────────

# Public and health routes are covered by their own suites (rate limits, tokens, CORS),
# not by a role matrix. This list may only shrink: a route added to any of these
# routers, or a router nobody covers, fails the test below until it is handled.
UNCOVERED = {
    ("POST", "/api/v1/chat/message"),
    ("GET", "/api/v1/health"), ("GET", "/api/v1/health/ready"),
}
UNCOVERED_ROUTERS = ("chat", "health", "public_intake", "public_booking", "public_proposals",
                     "public_receipts", "public_associates", "public_handover")


def _all_operations() -> dict[str, set]:
    from pathlib import Path
    out = {}
    for path in sorted((Path(__file__).resolve().parents[1] / "app" / "api" / "routes").glob("*.py")):
        if path.stem == "__init__":
            continue
        module = importlib.import_module(f"app.api.routes.{path.stem}")
        out[path.stem] = {(m, r.path) for r in module.router.routes for m in r.methods}
    return out


def test_every_router_is_covered_by_a_matrix_or_listed_as_uncovered():
    from tests import test_permission_matrix as original
    covered = set(original.COVERED_ROUTERS) | set(COVERED_ROUTERS)
    routers = set(_all_operations())
    assert routers - covered - set(UNCOVERED_ROUTERS) == set(), \
        "a router no permission matrix covers — add it to a matrix's COVERED_ROUTERS"
    assert set(UNCOVERED_ROUTERS) & covered == set(), "now covered: remove it from UNCOVERED_ROUTERS"


def test_the_uncovered_operations_only_shrink():
    ops = _all_operations()
    from tests import test_permission_matrix as original
    public_rows = set(original.PUBLIC)
    uncovered_now = {op for name in UNCOVERED_ROUTERS for op in ops.get(name, set())} - public_rows
    expected = UNCOVERED | EXPECTED_PUBLIC_UNCOVERED
    assert uncovered_now <= expected, f"new uncovered operation(s): {sorted(uncovered_now - expected)}"
    assert expected <= uncovered_now, f"covered or gone — remove from the list: {sorted(expected - uncovered_now)}"


# The public_* operations as of BE-3. Each is unauthenticated by design and has its own
# suite (rate limits, tokens); the role matrix only says that a credential changes nothing.
EXPECTED_PUBLIC_UNCOVERED = {
    ("GET", "/api/v1/proposals/accept/{token}"), ("POST", "/api/v1/proposals/accept/{token}"),
    ("GET", "/api/v1/public/booking/availability"), ("POST", "/api/v1/public/booking"),
    ("GET", "/api/v1/public/kyc-consent"), ("GET", "/api/v1/public/preferred/seat-availability"),
    ("GET", "/api/v1/public/proposals/{token}/pdf"), ("GET", "/api/v1/public/receipts/{token}/pdf"),
    ("GET", "/api/v1/public/territories/states"),
    ("GET", "/api/v1/public/territories/states/{state_lgd_code}/districts"),
    ("GET", "/api/v1/public/territories/districts/{district_lgd_code}/blocks"),
    ("GET", "/api/v1/public/verify/{associate_id}"),
    ("POST", "/api/v1/public/associates/apply"), ("POST", "/api/v1/public/preferred/apply"),
    ("POST", "/api/v1/public/contact"), ("POST", "/api/v1/public/estimate-request"),
    # BE-5: the customer's handover link (token-gated; tests/test_handover_link_pdf.py, test_service_requests.py).
    ("GET", "/api/v1/public/handover/{token}"), ("GET", "/api/v1/public/handover/{token}/pdf"),
    ("GET", "/api/v1/public/handover/{token}/service-requests"),
    ("POST", "/api/v1/public/handover/{token}/service-requests"),
}
