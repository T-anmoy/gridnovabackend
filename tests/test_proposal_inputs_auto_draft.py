"""Approving a ProposalInputs record (status -> approved_for_proposal) now
triggers a real Proposal draft via the same orchestrator.request_proposal_draft()
the dashboard's manual "Draft proposal" button uses.

The one thing this must get right: ProposalInputs (site assessment data) and
Lead.collected_fields (what app.rules.business_rules.REQUIRED_FIELDS gates on)
are separate schemas. Approving inputs for a lead nobody has qualified yet is a
valid action in its own right, so it must succeed (still 200) even when the
draft can't proceed — the outcome is reported back, never silently dropped and
never allowed to fail the review itself.
"""
from app.memory.models import LeadSource, TimelineEventType

from tests.conftest import admin_headers, sales_headers


def _new_lead(memory, db_session, email):
    customer = memory.get_or_create_customer(email=email, name="Inputs Test")
    db_session.commit()
    lead = memory.create_lead(customer_id=customer.id, source=LeadSource.website_chat)
    db_session.commit()
    return lead.id


def _qualify(memory, db_session, lead_id):
    """Give the Lead what evaluate_lead()/REQUIRED_FIELDS actually gates on —
    simulating what chat qualification (or a manual dashboard edit) would have
    already done before ProposalInputs ever entered the picture."""
    memory.update_lead_fields(
        lead_id=lead_id, performed_by="test",
        new_fields={
            "name": "Inputs Test", "email": "qualified@example.com",
            "project_type": "rooftop", "location": "Kolkata", "capacity_kw": 5,
        },
    )
    db_session.commit()


def _submit_inputs(client, lead_id) -> int:
    payload = {
        "lead_id": lead_id, "customer_type": "individual", "project_family": "rooftop_solar",
        "state": "West Bengal", "district": "Kolkata", "pin_code": "700001",
        "site_type": "rooftop_residential", "connection_type": "on_grid",
    }
    r = client.post("/api/v1/proposal-inputs/", json=payload, headers=sales_headers())
    assert r.status_code == 200
    return r.json()["id"]


class TestApprovalTriggersDraftWhenLeadIsQualified:
    def test_approve_creates_a_real_proposal(self, client, memory, db_session):
        lead_id = _new_lead(memory, db_session, "auto-draft-success@example.com")
        _qualify(memory, db_session, lead_id)
        inputs_id = _submit_inputs(client, lead_id)

        r = client.patch(
            f"/api/v1/proposal-inputs/{inputs_id}/review",
            json={"status": "approved_for_proposal"},
            headers=admin_headers(),
        )
        assert r.status_code == 200
        draft = r.json()["proposal_draft"]
        assert draft["triggered"] is True
        assert draft["proposal_id"] is not None

        proposals, total = memory.list_proposals(lead_id=lead_id)
        assert total == 1
        assert proposals[0].id == draft["proposal_id"]

    def test_auto_draft_timeline_event_is_recorded(self, client, memory, db_session):
        lead_id = _new_lead(memory, db_session, "auto-draft-timeline@example.com")
        _qualify(memory, db_session, lead_id)
        inputs_id = _submit_inputs(client, lead_id)

        client.patch(
            f"/api/v1/proposal-inputs/{inputs_id}/review",
            json={"status": "approved_for_proposal"},
            headers=admin_headers(),
        )

        ctx = memory.get_lead_context(lead_id)
        event_types = [e.event_type for e in ctx["timeline"]]
        assert TimelineEventType.proposal_auto_drafted_from_inputs in event_types
        # The generic draft-created event still fires too — this is additive,
        # not a replacement of the existing behaviour.
        assert TimelineEventType.proposal_draft_created in event_types


class TestApprovalIsSafeWhenLeadIsNotYetQualified:
    def test_review_still_succeeds_without_a_qualified_lead(self, client, memory, db_session):
        """The pre-existing behaviour (tests/test_proposal_inputs.py::test_approve
        uses an unqualified lead) must not regress: the review itself is a valid
        action independent of whether a draft can proceed."""
        lead_id = _new_lead(memory, db_session, "auto-draft-unqualified@example.com")
        inputs_id = _submit_inputs(client, lead_id)

        r = client.patch(
            f"/api/v1/proposal-inputs/{inputs_id}/review",
            json={"status": "approved_for_proposal"},
            headers=admin_headers(),
        )
        assert r.status_code == 200
        assert r.json()["status"] == "approved_for_proposal"

    def test_no_proposal_is_created_and_the_reason_is_reported(self, client, memory, db_session):
        lead_id = _new_lead(memory, db_session, "auto-draft-no-proposal@example.com")
        inputs_id = _submit_inputs(client, lead_id)

        r = client.patch(
            f"/api/v1/proposal-inputs/{inputs_id}/review",
            json={"status": "approved_for_proposal"},
            headers=admin_headers(),
        )
        draft = r.json()["proposal_draft"]
        assert draft["triggered"] is False
        assert draft["missing_fields"]

        proposals, total = memory.list_proposals(lead_id=lead_id)
        assert total == 0


class TestNoTriggerForOtherStatusTransitions:
    def test_rejecting_does_not_trigger_a_draft(self, client, memory, db_session):
        lead_id = _new_lead(memory, db_session, "reject-no-draft@example.com")
        _qualify(memory, db_session, lead_id)
        inputs_id = _submit_inputs(client, lead_id)

        r = client.patch(
            f"/api/v1/proposal-inputs/{inputs_id}/review",
            json={"status": "rejected", "rejection_reason": "Not viable"},
            headers=admin_headers(),
        )
        assert r.status_code == 200
        assert "proposal_draft" not in r.json()
        _, total = memory.list_proposals(lead_id=lead_id)
        assert total == 0

    def test_moving_to_under_review_does_not_trigger_a_draft(self, client, memory, db_session):
        lead_id = _new_lead(memory, db_session, "under-review-no-draft@example.com")
        _qualify(memory, db_session, lead_id)
        inputs_id = _submit_inputs(client, lead_id)

        r = client.patch(
            f"/api/v1/proposal-inputs/{inputs_id}/review",
            json={"status": "under_review"},
            headers=admin_headers(),
        )
        assert r.status_code == 200
        assert "proposal_draft" not in r.json()
