"""Tests for the ProposalInputs intake API (/api/v1/proposal-inputs).

Covers submission, role gating, retrieval, list filtering, review transitions,
and the scheme-linked -> DCR-required auto-logic.
"""
from app.memory.models import LeadSource, TimelineEventType

from tests.conftest import admin_headers, engineer_headers, management_headers, sales_headers


def _new_lead(memory, db_session, email="proposal-inputs-test@example.com"):
    customer = memory.get_or_create_customer(email=email, name="Inputs Test")
    db_session.commit()
    lead = memory.create_lead(customer_id=customer.id, source=LeadSource.website_chat)
    db_session.commit()
    return lead.id


def _valid_payload(lead_id: int, **overrides) -> dict:
    payload = {
        "lead_id": lead_id,
        "customer_type": "individual",
        "project_family": "rooftop_solar",
        "state": "West Bengal",
        "district": "Kolkata",
        "pin_code": "700001",
        "site_type": "rooftop_residential",
        "connection_type": "on_grid",
    }
    payload.update(overrides)
    return payload


class TestSubmitProposalInputs:
    def test_requires_auth(self, client, memory, db_session):
        lead_id = _new_lead(memory, db_session, "no-auth@example.com")
        r = client.post("/api/v1/proposal-inputs/", json=_valid_payload(lead_id))
        assert r.status_code == 401

    def test_submit_valid_inputs(self, client, memory, db_session):
        lead_id = _new_lead(memory, db_session, "valid-submit@example.com")
        r = client.post(
            "/api/v1/proposal-inputs/",
            json=_valid_payload(lead_id),
            headers=sales_headers(),
        )
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "submitted"
        assert body["id"] is not None
        assert body["submitted_at"] is not None

        # A timeline event was created on the lead.
        ctx = memory.get_lead_context(lead_id)
        event_types = [e.event_type for e in ctx["timeline"]]
        assert TimelineEventType.proposal_inputs_submitted in event_types

    def test_missing_lead_returns_404(self, client):
        r = client.post(
            "/api/v1/proposal-inputs/",
            json=_valid_payload(999999),
            headers=sales_headers(),
        )
        assert r.status_code == 404

    def test_engineer_forbidden(self, client, memory, db_session):
        lead_id = _new_lead(memory, db_session, "engineer-block@example.com")
        r = client.post(
            "/api/v1/proposal-inputs/",
            json=_valid_payload(lead_id),
            headers=engineer_headers(),
        )
        assert r.status_code == 403

    def test_management_forbidden(self, client, memory, db_session):
        lead_id = _new_lead(memory, db_session, "mgmt-block@example.com")
        r = client.post(
            "/api/v1/proposal-inputs/",
            json=_valid_payload(lead_id),
            headers=management_headers(),
        )
        assert r.status_code == 403

    def test_dcr_forced_true_when_scheme_linked_to_government_scheme(self, client, memory, db_session):
        lead_id = _new_lead(memory, db_session, "dcr-scheme@example.com")
        r = client.post(
            "/api/v1/proposal-inputs/",
            json=_valid_payload(
                lead_id,
                scheme_linked=True,
                scheme_name="pm_surya_ghar",
                dcr_panels_required=False,  # client tried to submit False — should be overridden
            ),
            headers=sales_headers(),
        )
        assert r.status_code == 200
        inputs = memory.get_proposal_inputs(r.json()["id"])
        assert inputs.dcr_panels_required is True

    def test_dcr_not_forced_when_no_scheme(self, client, memory, db_session):
        lead_id = _new_lead(memory, db_session, "no-scheme@example.com")
        r = client.post(
            "/api/v1/proposal-inputs/",
            json=_valid_payload(lead_id, scheme_linked=False),
            headers=sales_headers(),
        )
        assert r.status_code == 200
        inputs = memory.get_proposal_inputs(r.json()["id"])
        assert inputs.dcr_panels_required is False


class TestGetProposalInputs:
    def test_get_by_id_returns_submitted_fields(self, client, memory, db_session):
        lead_id = _new_lead(memory, db_session, "get-by-id@example.com")
        create = client.post(
            "/api/v1/proposal-inputs/",
            json=_valid_payload(lead_id, district="Howrah", pin_code="711101"),
            headers=sales_headers(),
        )
        inputs_id = create.json()["id"]

        r = client.get(f"/api/v1/proposal-inputs/{inputs_id}", headers=sales_headers())
        assert r.status_code == 200
        body = r.json()
        assert body["lead_id"] == lead_id
        assert body["district"] == "Howrah"
        assert body["pin_code"] == "711101"
        assert body["status"] == "submitted"

    def test_get_nonexistent_returns_404(self, client):
        r = client.get("/api/v1/proposal-inputs/999999", headers=sales_headers())
        assert r.status_code == 404


class TestListProposalInputs:
    def test_list_default_filters_to_submitted(self, client, memory, db_session):
        lead_a = _new_lead(memory, db_session, "list-a@example.com")
        lead_b = _new_lead(memory, db_session, "list-b@example.com")
        create_a = client.post(
            "/api/v1/proposal-inputs/", json=_valid_payload(lead_a), headers=sales_headers()
        )
        create_b = client.post(
            "/api/v1/proposal-inputs/", json=_valid_payload(lead_b), headers=sales_headers()
        )
        # Move b out of 'submitted'.
        client.patch(
            f"/api/v1/proposal-inputs/{create_b.json()['id']}/review",
            json={"status": "under_review"},
            headers=sales_headers(),
        )

        r = client.get("/api/v1/proposal-inputs/", headers=sales_headers())
        assert r.status_code == 200
        body = r.json()
        ids = [item["id"] for item in body["items"]]
        assert create_a.json()["id"] in ids
        assert create_b.json()["id"] not in ids
        assert all(item["status"] == "submitted" for item in body["items"])

    def test_list_filter_by_explicit_status(self, client, memory, db_session):
        lead_id = _new_lead(memory, db_session, "explicit-status@example.com")
        create = client.post(
            "/api/v1/proposal-inputs/", json=_valid_payload(lead_id), headers=sales_headers()
        )
        client.patch(
            f"/api/v1/proposal-inputs/{create.json()['id']}/review",
            json={"status": "rejected", "rejection_reason": "Not viable"},
            headers=sales_headers(),
        )

        r = client.get("/api/v1/proposal-inputs/", params={"status": "rejected"}, headers=sales_headers())
        assert r.status_code == 200
        body = r.json()
        assert len(body["items"]) == 1
        assert body["items"][0]["id"] == create.json()["id"]

    def test_list_invalid_status_rejected(self, client):
        r = client.get("/api/v1/proposal-inputs/", params={"status": "not_a_status"}, headers=sales_headers())
        assert r.status_code == 422


class TestReviewProposalInputs:
    def test_approve(self, client, memory, db_session):
        lead_id = _new_lead(memory, db_session, "approve-me@example.com")
        create = client.post(
            "/api/v1/proposal-inputs/", json=_valid_payload(lead_id), headers=sales_headers()
        )
        inputs_id = create.json()["id"]

        r = client.patch(
            f"/api/v1/proposal-inputs/{inputs_id}/review",
            json={"status": "approved_for_proposal", "internal_notes": "Looks good"},
            headers=admin_headers(),
        )
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "approved_for_proposal"
        assert body["reviewed_at"] is not None
        assert body["reviewed_by_id"] == "emp-admin-1"
        assert body["internal_notes"] == "Looks good"

        ctx = memory.get_lead_context(lead_id)
        event_types = [e.event_type for e in ctx["timeline"]]
        assert TimelineEventType.proposal_inputs_reviewed in event_types

    def test_reject_with_reason(self, client, memory, db_session):
        lead_id = _new_lead(memory, db_session, "reject-me@example.com")
        create = client.post(
            "/api/v1/proposal-inputs/", json=_valid_payload(lead_id), headers=sales_headers()
        )
        inputs_id = create.json()["id"]

        r = client.patch(
            f"/api/v1/proposal-inputs/{inputs_id}/review",
            json={"status": "rejected", "rejection_reason": "Site not structurally viable"},
            headers=sales_headers(),
        )
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "rejected"
        assert body["rejection_reason"] == "Site not structurally viable"

    def test_review_forbidden_for_engineer(self, client, memory, db_session):
        lead_id = _new_lead(memory, db_session, "review-block@example.com")
        create = client.post(
            "/api/v1/proposal-inputs/", json=_valid_payload(lead_id), headers=sales_headers()
        )
        r = client.patch(
            f"/api/v1/proposal-inputs/{create.json()['id']}/review",
            json={"status": "approved_for_proposal"},
            headers=engineer_headers(),
        )
        assert r.status_code == 403

    def test_review_nonexistent_returns_404(self, client):
        r = client.patch(
            "/api/v1/proposal-inputs/999999/review",
            json={"status": "approved_for_proposal"},
            headers=admin_headers(),
        )
        assert r.status_code == 404
