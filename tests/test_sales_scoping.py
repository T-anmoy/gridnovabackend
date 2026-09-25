"""One lead-visibility rule everywhere (app/auth/scoping.py): Sales reads and acts on
leads assigned to them or unassigned; Admin/Management see everything; Engineers keep
their technical views; Subcontractors see no leads."""
import io
from datetime import datetime, timedelta, timezone

import pytest

from app.memory.models import Employee, EmployeeRole, ProposalStatus
from tests.conftest import (
    STORAGE_URL_PREFIX, admin_headers, engineer_headers, management_headers, sales_headers, seed_employees,
)

DASH = "/api/v1/dashboard"
INPUTS = "/api/v1/proposal-inputs"
SALES = sales_headers("emp-sales-1")
SUB = {"X-Employee-Id": "emp-sub-1", "X-Employee-Role": "subcontractor"}


@pytest.fixture
def world(memory, db_session):
    """Three leads — own (emp-sales-1), another rep's (emp-sales-2), unassigned —
    each with an approved proposal that has a PDF, and a site assessment."""
    seed_employees(db_session, "emp-sales-1", "emp-sales-2", "emp-admin-1", "emp-1")
    out = {}
    for name, owner in (("own", "emp-sales-1"), ("other", "emp-sales-2"), ("unassigned", None)):
        customer = memory.get_or_create_customer(email=f"{name}@example.com", name=name.title())
        lead = memory.create_lead(customer_id=customer.id)
        lead.assigned_employee_id = owner
        memory.update_lead_fields(lead_id=lead.id, new_fields={
            "name": name, "email": f"{name}@example.com", "project_type": "rooftop",
            "location": "Kolkata", "capacity_kw": 5}, performed_by="t")
        p = memory.create_proposal_draft(lead_id=lead.id, content={}, total_value=100_000,
                                         requires_manager_signoff=False, performed_by="emp-1")
        memory.transition_proposal_status(proposal_id=p.id, new_status=ProposalStatus.approved,
                                          performed_by="emp-1", approved_by_employee_id="emp-admin-1",
                                          pdf_url=STORAGE_URL_PREFIX + f"proposals/{name}.pdf")
        inputs = memory.create_proposal_inputs(
            lead_id=lead.id, performed_by="t", customer_type="individual", project_family="rooftop_solar",
            state="West Bengal", district="Kolkata", pin_code="700001", site_type="rooftop_residential",
            connection_type="on_grid")
        db_session.commit()
        out[name] = {"lead": lead.id, "customer": customer.id, "proposal": p.id, "inputs": inputs.id}
    return out


def _ids(rows, key="id"):
    return {r[key] for r in rows}


# ── Lists ───────────────────────────────────────────────────────────────────────

class TestLeadList:
    def test_sales_sees_own_and_unassigned(self, client, world):
        ids = _ids(client.get(f"{DASH}/leads", headers=SALES).json())
        assert ids == {world["own"]["lead"], world["unassigned"]["lead"]}

    @pytest.mark.parametrize("assigned, expected", [
        ("me", {"own"}), ("unassigned", {"unassigned"}), ("any", {"own", "unassigned"}),
    ])
    def test_sales_assigned_filter(self, client, world, assigned, expected):
        ids = _ids(client.get(f"{DASH}/leads", params={"assigned": assigned}, headers=SALES).json())
        assert ids == {world[n]["lead"] for n in expected}

    @pytest.mark.parametrize("headers", [admin_headers(), management_headers()])
    @pytest.mark.parametrize("assigned, expected", [
        (None, {"own", "other", "unassigned"}), ("any", {"own", "other", "unassigned"}),
        ("unassigned", {"unassigned"}), ("me", set()),
    ])
    def test_admin_and_management(self, client, world, headers, assigned, expected):
        params = {"assigned": assigned} if assigned else {}
        ids = _ids(client.get(f"{DASH}/leads", params=params, headers=headers).json())
        assert ids == {world[n]["lead"] for n in expected}

    def test_engineer_list_unchanged(self, client, world):
        assert len(client.get(f"{DASH}/leads", headers=engineer_headers()).json()) == 3

    def test_invalid_filter_is_422(self, client, world):
        assert client.get(f"{DASH}/leads", params={"assigned": "theirs"}, headers=SALES).status_code == 422

    def test_subcontractor_sees_none(self, client, world):
        assert client.get(f"{DASH}/leads", headers=SUB).json() == []
        assert client.get(f"{DASH}/proposals", headers=SUB).json()["items"] == []
        assert client.get(f"{INPUTS}/", headers=SUB).json()["items"] == []
        assert client.get(f"{DASH}/activity/recent", headers=SUB).json()["items"] == []

    def test_subcontractor_cannot_list_meetings(self, client, world):
        assert client.get(f"{DASH}/meetings", headers=SUB).status_code == 403
        assert client.get(f"{DASH}/meetings", headers=admin_headers()).status_code == 200


class TestOtherLists:
    def test_proposals_list(self, client, world):
        ids = _ids(client.get(f"{DASH}/proposals", headers=SALES).json()["items"])
        assert ids == {world["own"]["proposal"], world["unassigned"]["proposal"]}
        assert len(client.get(f"{DASH}/proposals", headers=admin_headers()).json()["items"]) == 3

    def test_proposal_inputs_list(self, client, world):
        ids = _ids(client.get(f"{INPUTS}/", headers=SALES).json()["items"])
        assert ids == {world["own"]["inputs"], world["unassigned"]["inputs"]}
        assert client.get(f"{INPUTS}/", headers=admin_headers()).json()["total"] == 3
        other_lead = world["other"]["lead"]
        assert client.get(f"{INPUTS}/", params={"lead_id": other_lead}, headers=SALES).json()["items"] == []


# ── Single records: (method, path-builder, json) — own/unassigned allowed, other 403 ──

def _pdf(w):
    return f"{DASH}/proposals/{w['proposal']}/pdf"


READS = [
    ("GET", lambda w: f"{DASH}/leads/{w['lead']}", None),
    ("GET", lambda w: f"{DASH}/proposals/{w['proposal']}", None),
    ("GET", _pdf, None),
    ("GET", lambda w: f"{DASH}/proposals/{w['proposal']}/required-approval-tier", None),
    ("GET", lambda w: f"{INPUTS}/{w['inputs']}", None),
]
WRITES = [
    ("PATCH", lambda w: f"{DASH}/leads/{w['lead']}/fields", {"fields": {"timeline": "Q1"}}),
    ("PATCH", lambda w: f"{DASH}/leads/{w['lead']}/stage", {"stage": "negotiation"}),
    ("POST", lambda w: f"{DASH}/proposals/draft", "draft"),
    ("POST", lambda w: f"{DASH}/proposals/{w['proposal']}/approve", None),
    ("POST", lambda w: f"{DASH}/proposals/{w['proposal']}/request-revision", {"reason": "x"}),
    ("PATCH", lambda w: f"{DASH}/proposals/{w['proposal']}/adjust-pricing", {"new_value": 1, "reason": "x"}),
    ("POST", lambda w: f"{DASH}/proposals/{w['proposal']}/send", None),
    ("POST", lambda w: f"{DASH}/leads/{w['lead']}/schedule-meeting",
     {"scheduled_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()}),
    ("PATCH", lambda w: f"{INPUTS}/{w['inputs']}/review", {"status": "under_review"}),
    ("POST", lambda w: f"{INPUTS}/", "inputs"),
]
_INPUTS_BODY = {"customer_type": "individual", "project_family": "rooftop_solar", "state": "West Bengal",
                "district": "Kolkata", "pin_code": "700001", "site_type": "rooftop_residential",
                "connection_type": "on_grid"}


def _call(client, method, path, body, w, headers):
    if body == "draft":
        body = {"lead_id": w["lead"], "customer_id": w["customer"]}
    elif body == "inputs":
        body = {"lead_id": w["lead"], **_INPUTS_BODY}
    return client.request(method, path, json=body, headers=headers)


@pytest.mark.usefixtures("fake_storage")
class TestSingleRecordMatrix:
    @pytest.mark.parametrize("method, path, body", READS + WRITES)
    def test_sales_blocked_on_another_reps_lead(self, client, world, method, path, body):
        r = _call(client, method, path(world["other"]), body, world["other"], SALES)
        assert r.status_code == 403, r.text

    @pytest.mark.parametrize("which", ["own", "unassigned"])
    @pytest.mark.parametrize("method, path, body", READS + WRITES)
    def test_sales_allowed_on_own_and_unassigned(self, client, world, which, method, path, body):
        r = _call(client, method, path(world[which]), body, world[which], SALES)
        assert r.status_code != 403, (which, r.text)

    @pytest.mark.parametrize("headers", [admin_headers(), management_headers()])
    @pytest.mark.parametrize("method, path, body", READS)
    def test_admin_and_management_read_everything(self, client, world, headers, method, path, body):
        for w in world.values():
            assert _call(client, method, path(w), body, w, headers).status_code == 200

    def test_engineer_keeps_technical_views(self, client, world):
        w = world["other"]
        lead = client.get(f"{DASH}/leads/{w['lead']}", headers=engineer_headers())
        assert lead.status_code == 200 and set(lead.json()) == {"lead_id", "pipeline_stage", "collected_fields"}
        proposal = client.get(f"{DASH}/proposals/{w['proposal']}", headers=engineer_headers()).json()
        assert "content" not in proposal and "total_value" not in proposal
        assert client.get(_pdf(w), headers=engineer_headers()).status_code == 403

    @pytest.mark.parametrize("method, path, body", READS)
    def test_subcontractor_blocked(self, client, world, method, path, body):
        w = world["unassigned"]
        assert _call(client, method, path(w), body, w, SUB).status_code == 403

    def test_forbidden_message_is_clear(self, client, world):
        r = client.get(f"{DASH}/leads/{world['other']['lead']}", headers=SALES)
        assert r.json()["detail"] == "This lead is not assigned to you."
        r = client.get(f"{DASH}/proposals/{world['other']['proposal']}", headers=SALES)
        assert r.json()["detail"] == "This proposal is not assigned to you."

    def test_unknown_records_stay_404(self, client, world):
        assert client.get(f"{DASH}/leads/99999", headers=SALES).status_code == 404
        assert client.get(f"{DASH}/proposals/99999", headers=SALES).status_code == 404
        assert client.get(f"{INPUTS}/99999", headers=SALES).status_code == 404


@pytest.mark.usefixtures("fake_storage")
class TestAssessmentFiles:
    def _upload(self, client, inputs_id):
        r = client.post(f"{INPUTS}/{inputs_id}/upload", headers=admin_headers(),
                        data={"target": "document", "document_type": "bill"},
                        files={"file": ("b.pdf", io.BytesIO(b"%PDF"), "application/pdf")})
        return r.json()["files"][0]["file_ref"]

    def test_file_download_follows_the_lead(self, client, world):
        own = self._upload(client, world["own"]["inputs"])
        other = self._upload(client, world["other"]["inputs"])
        assert client.get(f"{INPUTS}/{world['own']['inputs']}/files/{own}", headers=SALES).status_code == 200
        assert client.get(f"{INPUTS}/{world['other']['inputs']}/files/{other}", headers=SALES).status_code == 403
        assert client.get(f"{INPUTS}/{world['other']['inputs']}/files/{other}",
                          headers=admin_headers()).status_code == 200

    def test_upload_to_another_reps_assessment_is_403(self, client, world):
        r = client.post(f"{INPUTS}/{world['other']['inputs']}/upload", headers=SALES,
                        data={"target": "document", "document_type": "bill"},
                        files={"file": ("b.pdf", io.BytesIO(b"%PDF"), "application/pdf")})
        assert r.status_code == 403


class TestActivityFeed:
    def test_sales_sees_only_activity_on_visible_leads(self, client, memory, db_session, world):
        db_session.add(Employee(id="emp-x", name="X", email="x@x.in", role=EmployeeRole.sales))
        memory._audit("employee", "emp-x", "role_changed", "emp-admin-1")
        db_session.commit()
        items = client.get(f"{DASH}/activity/recent", headers=SALES).json()["items"]
        visible = {str(world[n][k]) for n in ("own", "unassigned") for k in ("lead", "proposal", "inputs")}
        hidden = {str(world["other"][k]) for k in ("lead", "proposal", "inputs")}
        assert items
        for item in items:
            assert item["entity_type"] != "employee"
            if item["entity_type"] in ("lead", "proposal", "proposal_inputs"):
                assert item["entity_id"] in visible and item["entity_id"] not in hidden

    def test_admin_feed_unchanged(self, client, memory, db_session, world):
        memory._audit("employee", "emp-x", "role_changed", "emp-admin-1")
        db_session.commit()
        items = client.get(f"{DASH}/activity/recent", headers=admin_headers()).json()["items"]
        assert len(items) == 15
        assert any(i["entity_type"] == "employee" for i in items)
