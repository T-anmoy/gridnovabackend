"""Site assessment → proposal path: a ProposalInputs row fills its lead's BLANK
required fields (only from honest sources), so an estimate-form lead can reach a
draft; plus accepted_at on the proposal summary."""
from datetime import datetime, timezone

import pytest

from app.memory.models import ProposalStatus, TimelineEventType
from tests.conftest import admin_headers, sales_headers

INPUTS = "/api/v1/proposal-inputs/"


def _inputs(lead_id: int, **overrides) -> dict:
    payload = {
        "lead_id": lead_id,
        "customer_type": "individual",
        "project_family": "rooftop_solar",
        "state": "West Bengal",
        "district": "Kolkata",
        "pin_code": "700001",
        "site_type": "rooftop_residential",
        "connection_type": "on_grid",
        "desired_output_kw": 6,
    }
    payload.update(overrides)
    return payload


def _estimate_lead(client, email="fill@example.com", **overrides) -> int:
    body = {"name": "Asha", "email": email, "state": "West Bengal", "city": "Kolkata"}
    body.update(overrides)
    r = client.post("/api/v1/public/estimate-request", json=body)
    assert r.status_code == 200
    return r.json()["lead_id"]


def _bare_lead(memory, db_session, fields=None, email="bare@example.com") -> int:
    customer = memory.get_or_create_customer(email=email, name="B")
    lead = memory.create_lead(customer_id=customer.id)
    if fields:
        memory.update_lead_fields(lead_id=lead.id, new_fields=fields, performed_by="t")
    db_session.commit()
    return lead.id


def _fields(memory, db_session, lead_id) -> dict:
    db_session.expire_all()
    return dict(memory.get_lead(lead_id).collected_fields or {})


class TestFillOnCreate:
    def test_estimate_form_lead_becomes_draftable(self, client, memory, db_session):
        lead_id = _estimate_lead(client)
        assert "project_type" not in _fields(memory, db_session, lead_id)

        r = client.post(INPUTS, json=_inputs(lead_id), headers=sales_headers())
        assert r.status_code == 200
        # location was already collected by the form ("Kolkata, West Bengal") — not re-filled.
        assert r.json()["lead_fields_filled"] == {"project_type": "rooftop", "capacity_kw": 6}

        review = client.patch(f"{INPUTS}{r.json()['id']}/review",
                              json={"status": "approved_for_proposal"}, headers=sales_headers())
        assert review.status_code == 200
        assert review.json()["lead_fields_filled"] == {}
        assert review.json()["proposal_draft"]["triggered"] is True

    def test_never_overwrites_a_collected_value(self, client, memory, db_session):
        lead_id = _bare_lead(memory, db_session, {"capacity_kw": 10, "project_type": "ground_mounted"})
        r = client.post(INPUTS, json=_inputs(lead_id), headers=sales_headers())
        assert r.json()["lead_fields_filled"] == {"location": "Kolkata, West Bengal"}
        fields = _fields(memory, db_session, lead_id)
        assert fields["capacity_kw"] == 10 and fields["project_type"] == "ground_mounted"

    @pytest.mark.parametrize("family, expected", [
        ("rooftop_solar", "rooftop"), ("ground_mounted", "ground_mounted"), ("water_pump", "solar_pump"),
    ])
    def test_mapped_families(self, client, memory, db_session, family, expected):
        lead_id = _bare_lead(memory, db_session)
        r = client.post(INPUTS, json=_inputs(lead_id, project_family=family), headers=sales_headers())
        assert r.json()["lead_fields_filled"]["project_type"] == expected

    @pytest.mark.parametrize("family", ["government_institutional", "street_lighting", "om_service", "consulting"])
    def test_unmapped_family_leaves_project_type_missing(self, client, memory, db_session, family):
        lead_id = _estimate_lead(client, email=f"{family}@example.com")
        r = client.post(INPUTS, json=_inputs(lead_id, project_family=family), headers=sales_headers())
        assert "project_type" not in r.json()["lead_fields_filled"]
        review = client.patch(f"{INPUTS}{r.json()['id']}/review",
                              json={"status": "approved_for_proposal"}, headers=sales_headers()).json()
        assert review["proposal_draft"]["triggered"] is False
        assert "project_type" in review["proposal_draft"]["missing_fields"]

    def test_capacity_is_never_derived_from_the_bill(self, client, memory, db_session):
        lead_id = _estimate_lead(client, email="bill@example.com", average_bill=6000)
        r = client.post(INPUTS, json=_inputs(lead_id, desired_output_kw=None, avg_monthly_bill_inr=6000,
                                             avg_monthly_units_kwh=700), headers=sales_headers())
        assert "capacity_kw" not in r.json()["lead_fields_filled"]
        assert "capacity_kw" not in _fields(memory, db_session, lead_id)
        review = client.patch(f"{INPUTS}{r.json()['id']}/review",
                              json={"status": "approved_for_proposal"}, headers=sales_headers()).json()
        assert review["proposal_draft"]["missing_fields"] == ["capacity_kw"]

    def test_provenance_is_audited_and_on_the_timeline(self, client, memory, db_session):
        lead_id = _bare_lead(memory, db_session)
        inputs_id = client.post(INPUTS, json=_inputs(lead_id), headers=sales_headers()).json()["id"]
        logs = [l for l in memory.list_audit_logs(entity_type="lead", entity_id=str(lead_id))
                if l.action == "fields_filled_from_inputs"]
        assert len(logs) == 1
        assert logs[0].after_value == {
            "proposal_inputs_id": inputs_id,
            "fields": {"project_type": "rooftop", "capacity_kw": 6, "location": "Kolkata, West Bengal"},
        }
        db_session.expire_all()
        events = [e for e in memory.get_lead_context(lead_id)["timeline"]
                  if e.event_type == TimelineEventType.fields_filled_from_inputs]
        assert len(events) == 1 and f"#{inputs_id}" in events[0].description

    def test_nothing_to_fill_records_nothing(self, client, memory, db_session):
        lead_id = _bare_lead(memory, db_session, {"capacity_kw": 5, "project_type": "rooftop", "location": "X"})
        r = client.post(INPUTS, json=_inputs(lead_id), headers=sales_headers())
        assert r.json()["lead_fields_filled"] == {}
        db_session.expire_all()
        assert not any(e.event_type == TimelineEventType.fields_filled_from_inputs
                       for e in memory.get_lead_context(lead_id)["timeline"])


class TestFillOnApprove:
    def test_approval_fills_blanks_left_since_submission(self, client, memory, db_session):
        lead_id = _estimate_lead(client, email="later@example.com")
        inputs_id = client.post(INPUTS, json=_inputs(lead_id), headers=sales_headers()).json()["id"]
        # A person clears the value the submission filled; approval fills it again.
        lead = memory.get_lead(lead_id)
        lead.collected_fields = {k: v for k, v in lead.collected_fields.items() if k != "capacity_kw"}
        db_session.commit()
        review = client.patch(f"{INPUTS}{inputs_id}/review",
                              json={"status": "approved_for_proposal"}, headers=sales_headers()).json()
        assert review["lead_fields_filled"] == {"capacity_kw": 6}
        assert review["proposal_draft"]["triggered"] is True

    @pytest.mark.parametrize("status_", ["under_review", "rejected"])
    def test_other_review_outcomes_fill_nothing(self, client, memory, db_session, status_):
        lead_id = _bare_lead(memory, db_session)
        inputs_id = client.post(INPUTS, json=_inputs(lead_id), headers=sales_headers()).json()["id"]
        lead = memory.get_lead(lead_id)
        lead.collected_fields = {}
        db_session.commit()
        review = client.patch(f"{INPUTS}{inputs_id}/review",
                              json={"status": status_, "rejection_reason": "x"}, headers=sales_headers()).json()
        assert review["lead_fields_filled"] == {}
        assert _fields(memory, db_session, lead_id) == {}


class TestReviewQueue:
    def test_status_filter(self, client, memory, db_session):
        lead_id = _bare_lead(memory, db_session)
        a = client.post(INPUTS, json=_inputs(lead_id), headers=sales_headers()).json()["id"]
        b = client.post(INPUTS, json=_inputs(lead_id), headers=sales_headers()).json()["id"]
        client.patch(f"{INPUTS}{b}/review", json={"status": "under_review"}, headers=sales_headers())
        submitted = client.get(INPUTS, params={"status": "submitted"}, headers=admin_headers()).json()
        assert [i["id"] for i in submitted["items"]] == [a]
        reviewing = client.get(INPUTS, params={"status": "under_review"}, headers=admin_headers()).json()
        assert [i["id"] for i in reviewing["items"]] == [b]


@pytest.mark.usefixtures("known_employees")
class TestAcceptedAtOnSummary:
    def test_summary_carries_accepted_at(self, client, memory, db_session):
        lead_id = _bare_lead(memory, db_session)
        p = memory.create_proposal_draft(lead_id=lead_id, content={}, total_value=100_000,
                                         requires_manager_signoff=False, performed_by="emp-1")
        memory.transition_proposal_status(proposal_id=p.id, new_status=ProposalStatus.approved,
                                          performed_by="emp-1", approved_by_employee_id="emp-admin-1")
        db_session.commit()
        rows = client.get("/api/v1/dashboard/proposals", headers=admin_headers()).json()
        row = next(r for r in rows["items"] if r["id"] == p.id)
        assert row["accepted_at"] is None

        token = client.post(f"/api/v1/dashboard/proposals/{p.id}/send", headers=admin_headers()).json()["accept_token"]
        client.post(f"/api/v1/proposals/accept/{token}")
        rows = client.get("/api/v1/dashboard/proposals", headers=admin_headers()).json()
        row = next(r for r in rows["items"] if r["id"] == p.id)
        assert row["accepted_at"] is not None
        assert datetime.fromisoformat(row["accepted_at"]).year == datetime.now(timezone.utc).year
