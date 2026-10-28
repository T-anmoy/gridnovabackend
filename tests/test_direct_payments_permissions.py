"""BE-20 — the role matrix for direct customers' payments (app/api/routes/direct_payments.py),
and the shared per-payment routes acting on a direct customer's payment.

Every route of the router has a row. Each listed actor gets past authorisation (not 401/403)
and every other staff actor gets 403: Accounts and Admin record and act; Management and the
lead's Sales owner read; another Sales person, Operations, Engineer, Network Manager and the
Proposal Desk never see a direct customer's payments. An associate credential is never a
staff one, and nothing here is public (401). The lead's region applies (BE-19).

tests/test_permission_matrix_core.py reads COVERED_ROUTERS from here for its ratchet.
"""
from __future__ import annotations

import pytest

from app.memory.models import Employee, EmployeeRole, Lead, Project, TerritoryState
from tests.payments_support import territories  # noqa: F401 — fixture
# The end-to-end module's autouse environment (storage, Fernet key, receipt text and PDF
# stub, fresh public rate-limit windows, recording notifier) applies here too.
from tests.test_direct_payments_e2e import _env  # noqa: F401 — autouse fixture

COVERED_ROUTERS = ("direct_payments",)

D = "/api/v1/dashboard"
ACTORS = {
    "admin": {"X-Employee-Id": "emp-admin-1", "X-Employee-Role": "admin"},
    "management": {"X-Employee-Id": "emp-mgmt-1", "X-Employee-Role": "management"},
    "accounts": {"X-Employee-Id": "emp-acc-1", "X-Employee-Role": "accounts"},
    "sales_owner": {"X-Employee-Id": "emp-sales-1", "X-Employee-Role": "sales"},
    "sales_other": {"X-Employee-Id": "emp-sales-2", "X-Employee-Role": "sales"},
    "operations": {"X-Employee-Id": "emp-ops-1", "X-Employee-Role": "operations"},
    "engineer": {"X-Employee-Id": "emp-eng-1", "X-Employee-Role": "engineer"},
    "network_manager": {"X-Employee-Id": "emp-nm-1", "X-Employee-Role": "network_manager"},
    "proposal_desk": {"X-Employee-Id": "emp-desk-1", "X-Employee-Role": "proposal_desk"},
}
READ = {"admin", "management", "accounts", "sales_owner"}
PROCESS = {"admin", "accounts"}

L = f"{D}/leads/{{lead_id}}"
ROUTES = {
    ("GET", f"{L}/payments"): (READ, {}),
    ("POST", f"{L}/payments"): (PROCESS, {"json": {"amount": "10.00", "mode": "neft", "reference": "UTR-M-{actor}"}}),
    ("GET", f"{L}/payment-schedule"): (READ, {}),
    ("GET", f"{L}/payments/{{payment_id}}/receipt"): (READ, {}),
}
P = f"{D}/payments/{{payment_id}}"
SHARED = {
    ("GET", P): (READ, {}),
    ("GET", f"{P}/proof"): (READ, {}),
    ("POST", f"{P}/proof"): (PROCESS, {"files": {"file": ("p.pdf", b"%PDF-1.4 proof", "application/pdf")}}),
    ("POST", f"{P}/deposit"): (PROCESS, {}),
    ("POST", f"{P}/verify"): (PROCESS, {"json": {"reason": "matrix"}}),
    ("POST", f"{P}/dishonour"): (PROCESS, {"json": {"reason": "matrix"}}),
    ("POST", f"{P}/cancel"): (PROCESS, {"json": {"reason": "matrix"}}),
    ("POST", f"{P}/receipt/resend"): (PROCESS, {}),
}


@pytest.fixture
def scenario(client, db_session):
    """An accepted website customer whose lead is Sales' emp-sales-1, with one verified NEFT
    payment (receipt numbered) and one open cheque."""
    from tests.test_direct_payments_e2e import accepted_website_customer, cheque, neft, record, verify

    lead_id, _ = accepted_website_customer(client, db_session)
    lead = db_session.get(Lead, lead_id)
    lead.assigned_employee_id = "emp-sales-1"
    db_session.commit()
    paid = record(client, lead_id, neft("1000.00", "UTR-MATRIX-1")).json()["payment_id"]
    assert verify(client, paid).status_code == 200
    open_cheque = record(client, lead_id, cheque("1000.00", number="009001")).json()["payment_id"]
    return {"lead_id": lead_id, "payment_id": paid, "open_payment_id": open_cheque}


def _request(client, method, template, headers, kwargs, ids, actor):
    args = {}
    for k, v in kwargs.items():
        if k == "json":
            v = {key: (val.replace("{actor}", actor) if isinstance(val, str) else val) for key, val in v.items()}
        args[k] = v
    return client.request(method, template.format(**ids), headers=headers, follow_redirects=False, **args)


def test_every_direct_payment_route_has_a_row():
    from app.api.routes import direct_payments
    assert {(m, r.path) for r in direct_payments.router.routes for m in r.methods} == set(ROUTES)


@pytest.mark.parametrize("route", sorted(ROUTES), ids=lambda r: f"{r[0]} {r[1]}")
def test_direct_payment_route(client, scenario, route):
    allowed, kwargs = ROUTES[route]
    method, template = route
    for actor, headers in sorted(ACTORS.items(), key=lambda a: a[0] in allowed):   # refused actors first
        r = _request(client, method, template, headers, kwargs, scenario, actor)
        if actor in allowed:
            assert r.status_code not in (401, 403), (actor, r.status_code, r.text[:300])
        else:
            assert r.status_code == 403, (actor, r.status_code, r.text[:300])
    assert _request(client, method, template, {"Authorization": "Bearer associate-token"}, kwargs, scenario,
                    "associate").status_code == 401
    assert _request(client, method, template, {}, kwargs, scenario, "public").status_code == 401


@pytest.mark.parametrize("route", sorted(SHARED), ids=lambda r: f"{r[0]} {r[1]}")
def test_shared_payment_route_on_a_direct_payment(client, scenario, route):
    """The open cheque for actions (verify/deposit/cancel move it); the verified one for reads
    and the receipt."""
    allowed, kwargs = SHARED[route]
    method, template = route
    reads = method == "GET" or template.endswith("/receipt/resend") or template.endswith("/dishonour")
    ids = {**scenario, "payment_id": scenario["payment_id"] if reads else scenario["open_payment_id"]}
    for actor, headers in sorted(ACTORS.items(), key=lambda a: a[0] in allowed):
        r = _request(client, method, template, headers, kwargs, ids, actor)
        if actor in allowed:
            assert r.status_code not in (401, 403), (actor, r.status_code, r.text[:300])
        else:
            assert r.status_code == 403, (actor, r.status_code, r.text[:300])


def test_region_scope(client, scenario, db_session, territories):
    """An Accounts person working only Bihar does not see a West Bengal customer's payments;
    one working West Bengal (or every region) does."""
    from sqlalchemy import select
    project = db_session.get(Project, db_session.get(Lead, scenario["lead_id"]).project_id)
    assert project.state == "West Bengal"
    states = {s.name.title(): s.name for s in db_session.execute(select(TerritoryState)).scalars()}   # regions are names
    who = ACTORS["accounts"]
    assert client.get(f"{D}/leads/{scenario['lead_id']}/payments", headers=who).status_code == 200
    emp = db_session.get(Employee, "emp-acc-1")
    emp.regions = [states["Bihar"]]
    db_session.commit()
    assert client.get(f"{D}/leads/{scenario['lead_id']}/payments", headers=who).status_code == 403
    assert client.get(f"{D}/payments/{scenario['payment_id']}", headers=who).status_code == 403
    emp.regions = [states["West Bengal"]]
    db_session.commit()
    assert client.get(f"{D}/leads/{scenario['lead_id']}/payments", headers=who).status_code == 200


def test_payment_alerts_reach_the_leads_owner_only_for_a_direct_customer(db_session):
    """config/access_control.yaml: payment_recorded / payment_verified name the assigned
    employee. A direct customer's alert carries lead_id and reaches them; an associate
    customer's alert carries none, so its recipients are unchanged."""
    from app.memory.memory_manager import MemoryManager
    from app.notifications import alerts
    memory = MemoryManager(db_session)
    owner = Employee(id="emp-sales-9", name="Owner", email="owner@example.com", role=EmployeeRole.sales)
    db_session.add(owner)
    customer = memory.get_or_create_customer("alert.customer@example.test", "Alert Customer")
    lead = memory.create_lead(customer.id)
    lead.assigned_employee_id = owner.id
    db_session.commit()
    for event in ("payment_recorded", "payment_verified", "payment_dishonoured"):
        people, _ = alerts._people(db_session, event, {"lead_id": lead.id, "onboarding_id": None})
        assert "owner@example.com" in people, event
        people, _ = alerts._people(db_session, event, {"onboarding_id": 7})
        assert "owner@example.com" not in people, event
