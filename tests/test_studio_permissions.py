"""BE-18 — the Proposal Studio's role matrix. Every route of app/api/routes/studio.py has a
row; each listed actor gets past authorisation (not 401/403) and every other staff role gets
403. The actors are the brief's: Proposal Desk alone, Proposal Desk + Sales, Admin and
Management; Sales, Engineer, Operations, Accounts and Network Manager never reach the Studio.
An associate credential is never a staff one (401).

tests/test_permission_matrix_core.py reads COVERED_ROUTERS from here for its ratchet.
"""
from __future__ import annotations

import pytest

from app.memory.models import StudioLibraryItem
from tests.studio_support import D, issued_residential, new_proposal

COVERED_ROUTERS = ("studio",)

ACTORS = {
    "proposal_desk": {"X-Employee-Id": "emp-desk-1", "X-Employee-Role": "proposal_desk"},
    "proposal_desk+sales": {"X-Employee-Id": "emp-desk-sales", "X-Employee-Role": "sales",
                            "X-Employee-Roles": "proposal_desk"},
    "admin": {"X-Employee-Id": "emp-admin-1", "X-Employee-Role": "admin"},
    "management": {"X-Employee-Id": "emp-mgmt-1", "X-Employee-Role": "management"},
    "sales": {"X-Employee-Id": "emp-sales-1", "X-Employee-Role": "sales"},
    "engineer": {"X-Employee-Id": "emp-eng-1", "X-Employee-Role": "engineer"},
    "operations": {"X-Employee-Id": "emp-ops-1", "X-Employee-Role": "operations"},
    "accounts": {"X-Employee-Id": "emp-acct-1", "X-Employee-Role": "accounts"},
    "network_manager": {"X-Employee-Id": "emp-nm-1", "X-Employee-Role": "network_manager"},
}
DESKS = {"proposal_desk", "proposal_desk+sales"}
VIEW = DESKS | {"admin", "management"}          # studio.view / studio.view_all
EDIT = DESKS | {"admin"}                        # studio.edit, studio.price, library, convert
APPROVE = {"admin", "management"}               # studio.approve
JOBS = {"admin"}                                # the internal job without the key: Admin only

P = f"{D}/studio/proposals/{{proposal_id}}"
ROUTES = {
    ("GET", f"{D}/studio/options"): (VIEW, {}),
    ("GET", f"{D}/studio/customers"): (VIEW, {}),
    ("POST", f"{D}/studio/customers"): (EDIT, {"json": {"name": "Matrix Customer", "mobile": "9830011111",
                                                         "address_lines": ["1 Road"]}}),
    ("GET", f"{D}/studio/customers/{{customer_id}}"): (VIEW, {}),
    ("PATCH", f"{D}/studio/customers/{{customer_id}}"): (EDIT, {"json": {"attention": "x"}}),
    ("GET", f"{D}/studio/proposals"): (VIEW, {}),
    ("POST", f"{D}/studio/proposals"): (EDIT, {"json": {"customer_id": "{customer_id}",
                                                         "project_type": "commercial_rooftop"}}),
    ("GET", P): (VIEW, {}),
    ("PATCH", f"{P}/document"): (EDIT, {"json": {"base_revision": 0, "section": "notes_free", "value": []}}),
    ("POST", f"{P}/acknowledge-warnings"): (EDIT, {"json": {"fingerprints": ["x"]}}),
    ("POST", f"{P}/spelling/ignore"): (EDIT, {"json": {"word": "matrix"}}),
    ("POST", f"{P}/submit"): (EDIT, {}),
    ("POST", f"{P}/approve"): (APPROVE, {"json": {}}),
    ("POST", f"{P}/reject"): (APPROVE, {"json": {"comment": "x"}}),
    ("POST", f"{P}/issue"): (EDIT, {}),
    ("GET", f"{P}/preview.pdf"): (VIEW, {}),
    ("GET", f"{P}/pdf"): (VIEW, {}),
    ("GET", f"{P}/pdf-link"): (VIEW, {}),
    ("POST", f"{P}/sections/{{section}}/reset"): (EDIT, {"json": {"base_revision": 0}}),
    ("POST", f"{P}/email"): (EDIT, {"json": {}}),
    ("POST", f"{P}/whatsapp-link"): (EDIT, {}),
    ("POST", f"{P}/revise"): (EDIT, {}),
    ("POST", f"{P}/duplicate"): (EDIT, {"json": {}}),
    ("POST", f"{P}/mark-accepted"): (EDIT, {"data": {"accepted_on": "2026-10-27"},
                                            "files": {"signed_copy": ("s.pdf", b"%PDF-1.4 x", "application/pdf")}}),
    ("POST", f"{P}/mark-lost"): (EDIT, {"json": {"reason": "matrix"}}),
    ("POST", f"{P}/convert"): (EDIT, {}),
    ("GET", f"{P}/events"): (VIEW, {}),
    ("GET", f"{P}/versions/{{a}}/diff/{{b}}"): (VIEW, {}),
    ("POST", f"{P}/suggest-from-notes"): (EDIT, {"json": {"text": "Call Mr. Das on 9830000009"}}),
    ("POST", f"{P}/save-as-template"): (EDIT, {"json": {"name": "Matrix template"}}),
    ("GET", f"{D}/studio/library"): (VIEW, {}),
    ("POST", f"{D}/studio/library"): (EDIT, {"json": {"kind": "note", "name": "Matrix", "body": {"text": "A note."}}}),
    ("PATCH", f"{D}/studio/library/{{item_id}}"): (EDIT, {"json": {"active": True}}),
    ("GET", f"{D}/studio/summary"): (VIEW, {}),
    ("GET", f"{D}/studio/import-template.xlsx"): (EDIT, {}),
    ("POST", f"{D}/studio/import"): (EDIT, {"files": {"file": ("x.csv", b"a,b", "text/csv")}}),
    ("POST", f"{D}/internal/check-studio-reminders"): (JOBS, {}),
}


@pytest.fixture
def scenario(client, db_session, fake_storage, monkeypatch):
    from tests.proposal_document_support import require_weasyprint
    require_weasyprint()                     # the scenario issues a proposal
    from app.config import settings
    from app.security.encryption import generate_key
    monkeypatch.setattr(settings, "fernet_encryption_key", generate_key())
    out, _ = issued_residential(client)
    draft = new_proposal(client)
    item = StudioLibraryItem(kind="note", name="Seed", body={"text": "Seed."}, created_by="t")
    db_session.add(item)
    db_session.commit()
    return {"proposal_id": out["proposal"]["id"], "customer_id": draft["proposal"]["customer"]["id"],
            "item_id": item.id, "a": 1, "b": 1, "section": "boq"}


def _fill(value, ids):
    if isinstance(value, str):
        out = value.format(**ids)
        return int(out) if value.startswith("{") and out.isdigit() else out
    if isinstance(value, dict):
        return {k: _fill(v, ids) for k, v in value.items()}
    return value


def test_every_studio_route_has_a_row():
    from app.api.routes import studio
    ops = {(m, r.path) for r in studio.router.routes for m in r.methods}
    assert ops == set(ROUTES)


@pytest.mark.parametrize("route", sorted(ROUTES), ids=lambda r: f"{r[0]} {r[1]}")
def test_studio_route(client, scenario, route):
    allowed, kwargs = ROUTES[route]
    method, path = route
    url = path.format(**scenario)
    for actor, headers in ACTORS.items():
        args = {k: (_fill(v, scenario) if k == "json" else v) for k, v in kwargs.items()}
        r = client.request(method, url, headers=headers, follow_redirects=False, **args)
        if actor in allowed:
            assert r.status_code not in (401, 403), (actor, method, url, r.status_code, r.text[:300])
        else:
            assert r.status_code == 403, (actor, method, url, r.status_code, r.text[:300])


def test_an_associate_credential_is_not_staff(client):
    r = client.get(f"{D}/studio/proposals", headers={"Authorization": "Bearer associate-token"})
    assert r.status_code == 401


def test_proposal_desk_alone_sees_nothing_outside_the_studio(client):
    desk = ACTORS["proposal_desk"]
    for path in (f"{D}/leads", f"{D}/proposals/1", f"{D}/payments", f"{D}/analytics", f"{D}/surveys",
                 f"{D}/commissions/entries", f"{D}/employees"):
        r = client.get(path, headers=desk)
        assert r.status_code in (403, 404) or (r.status_code == 200 and not r.json().get("items")), (path, r.text[:200])
    assert client.get(f"{D}/access/matrix", headers=desk).status_code == 200      # "Who can do what"
    assert client.get(f"{D}/studio/proposals", headers=desk).status_code == 200


def test_proposal_desk_needs_no_second_factor_but_admin_pricing_does(client):
    from tests.studio_support import edit
    view = new_proposal(client)
    pid = view["proposal"]["id"]
    off = {"X-Employee-Two-Factor": "off"}
    r = edit(client, pid, "price", {"mode": "none"}, headers={**ACTORS["proposal_desk"], **off})
    assert r.status_code == 200, r.text
    r = edit(client, pid, "price", {"mode": "none"}, headers={**ACTORS["admin"], **off})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "two_factor_required"
