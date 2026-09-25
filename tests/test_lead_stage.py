"""Opportunity tracker: PATCH /leads/{id}/stage, and the automatic transitions
(proposal sent → proposal_sent, customer accepts → won) that only ever move forward."""
from datetime import datetime, timezone

import pytest

from app.memory.models import PipelineStage, ProposalStatus, TimelineEventType
from tests.conftest import (
    admin_headers, engineer_headers, management_headers, sales_headers, seed_employees,
)

STAGE = "/api/v1/dashboard/leads/{}/stage"


def _lead(memory, db_session, *, email="stage@example.com", stage=None, assigned_to=None):
    customer = memory.get_or_create_customer(email=email, name="C")
    lead = memory.create_lead(customer_id=customer.id)
    if stage is not None:
        lead.pipeline_stage = stage
    if assigned_to is not None:
        seed_employees(db_session, assigned_to)
        lead.assigned_employee_id = assigned_to
    db_session.commit()
    return lead.id


def _stage_of(memory, db_session, lead_id):
    db_session.expire_all()
    return memory.get_lead(lead_id).pipeline_stage


def _events(memory, db_session, lead_id):
    db_session.expire_all()
    return memory.get_lead_context(lead_id)["timeline"]


class TestRoleMatrix:
    @pytest.mark.parametrize("headers", [admin_headers(), management_headers()])
    def test_admin_and_management_may_move_any_lead(self, client, memory, db_session, headers):
        lid = _lead(memory, db_session, assigned_to="emp-sales-2")
        r = client.patch(STAGE.format(lid), json={"stage": "negotiation"}, headers=headers)
        assert r.status_code == 200, r.text
        assert r.json() == {"lead_id": lid, "previous_stage": "new",
                            "pipeline_stage": "negotiation", "reason": None}

    def test_sales_may_move_own_lead(self, client, memory, db_session):
        lid = _lead(memory, db_session, assigned_to="emp-sales-1")
        assert client.patch(STAGE.format(lid), json={"stage": "qualified"},
                            headers=sales_headers()).status_code == 200

    def test_sales_may_move_unassigned_lead(self, client, memory, db_session):
        lid = _lead(memory, db_session)
        assert client.patch(STAGE.format(lid), json={"stage": "qualified"},
                            headers=sales_headers()).status_code == 200

    def test_sales_may_not_move_another_reps_lead(self, client, memory, db_session):
        lid = _lead(memory, db_session, assigned_to="emp-sales-2")
        r = client.patch(STAGE.format(lid), json={"stage": "qualified"}, headers=sales_headers())
        assert r.status_code == 403
        assert _stage_of(memory, db_session, lid) == PipelineStage.new

    def test_engineer_forbidden(self, client, memory, db_session):
        lid = _lead(memory, db_session)
        assert client.patch(STAGE.format(lid), json={"stage": "qualified"},
                            headers=engineer_headers()).status_code == 403

    def test_unknown_lead_and_unknown_stage(self, client, memory, db_session):
        assert client.patch(STAGE.format(99999), json={"stage": "won"},
                            headers=admin_headers()).status_code == 404
        lid = _lead(memory, db_session)
        assert client.patch(STAGE.format(lid), json={"stage": "closed"},
                            headers=admin_headers()).status_code == 422


class TestReasonRules:
    def test_same_stage_is_409(self, client, memory, db_session):
        lid = _lead(memory, db_session, stage=PipelineStage.qualified)
        assert client.patch(STAGE.format(lid), json={"stage": "qualified"},
                            headers=admin_headers()).status_code == 409

    @pytest.mark.parametrize("reason", [None, "", "   "])
    def test_lost_requires_a_reason(self, client, memory, db_session, reason):
        lid = _lead(memory, db_session)
        r = client.patch(STAGE.format(lid), json={"stage": "lost", "reason": reason}, headers=admin_headers())
        assert r.status_code == 422
        assert _stage_of(memory, db_session, lid) == PipelineStage.new

    @pytest.mark.parametrize("terminal", [PipelineStage.won, PipelineStage.lost])
    def test_reopening_requires_a_reason(self, client, memory, db_session, terminal):
        lid = _lead(memory, db_session, stage=terminal)
        assert client.patch(STAGE.format(lid), json={"stage": "negotiation"},
                            headers=admin_headers()).status_code == 422
        r = client.patch(STAGE.format(lid), json={"stage": "negotiation", "reason": "Customer came back"},
                         headers=admin_headers())
        assert r.status_code == 200
        assert _stage_of(memory, db_session, lid) == PipelineStage.negotiation

    def test_won_needs_no_reason(self, client, memory, db_session):
        lid = _lead(memory, db_session, stage=PipelineStage.negotiation)
        assert client.patch(STAGE.format(lid), json={"stage": "won"}, headers=admin_headers()).status_code == 200

    def test_reason_is_audited_and_on_the_timeline_verbatim(self, client, memory, db_session):
        lid = _lead(memory, db_session)
        reason = "Chose a competitor — price was ₹40k lower"
        client.patch(STAGE.format(lid), json={"stage": "lost", "reason": reason}, headers=admin_headers())

        logs = [l for l in memory.list_audit_logs(entity_type="lead", entity_id=str(lid))
                if l.action == "stage_changed"]
        assert len(logs) == 1
        assert logs[0].performed_by == "emp-admin-1"
        assert logs[0].before_value == {"pipeline_stage": "new"}
        assert logs[0].after_value == {"pipeline_stage": "lost", "reason": reason}

        lost = [e for e in _events(memory, db_session, lid) if e.event_type == TimelineEventType.lost]
        assert len(lost) == 1 and reason in lost[0].description

    def test_non_terminal_move_gets_a_stage_changed_event(self, client, memory, db_session):
        lid = _lead(memory, db_session)
        client.patch(STAGE.format(lid), json={"stage": "qualified", "reason": "Called them"},
                     headers=admin_headers())
        events = [e for e in _events(memory, db_session, lid) if e.event_type == TimelineEventType.stage_changed]
        assert len(events) == 1
        assert "new" in events[0].description and "qualified" in events[0].description
        assert "Called them" in events[0].description


def _approved_proposal(memory, db_session, lead_id, *, version_content=None):
    p = memory.create_proposal_draft(
        lead_id=lead_id,
        content=version_content or {"sections": [{"id": "scope", "category": "scope", "body": "Install 8kW"}]},
        total_value=200_000, requires_manager_signoff=False, performed_by="emp-1",
    )
    memory.transition_proposal_status(proposal_id=p.id, new_status=ProposalStatus.approved,
                                      performed_by="emp-1", approved_by_employee_id="emp-admin-1")
    db_session.commit()
    return p.id


@pytest.mark.usefixtures("known_employees")
class TestAutomaticTransitions:
    def test_sending_moves_the_lead_to_proposal_sent(self, client, memory, db_session):
        lid = _lead(memory, db_session, stage=PipelineStage.proposal_draft)
        pid = _approved_proposal(memory, db_session, lid)
        assert client.post(f"/api/v1/dashboard/proposals/{pid}/send", headers=admin_headers()).status_code == 200
        assert _stage_of(memory, db_session, lid) == PipelineStage.proposal_sent
        events = [e for e in _events(memory, db_session, lid) if e.event_type == TimelineEventType.stage_changed]
        assert any("sent to the customer" in e.description for e in events)

    def test_customer_acceptance_moves_the_lead_to_won(self, client, memory, db_session):
        lid = _lead(memory, db_session, stage=PipelineStage.proposal_draft)
        pid = _approved_proposal(memory, db_session, lid)
        token = client.post(f"/api/v1/dashboard/proposals/{pid}/send", headers=admin_headers()).json()["accept_token"]
        assert client.post(f"/api/v1/proposals/accept/{token}").status_code == 200
        assert _stage_of(memory, db_session, lid) == PipelineStage.won
        assert any(e.event_type == TimelineEventType.won for e in _events(memory, db_session, lid))

    def test_send_does_not_pull_a_negotiating_lead_back(self, client, memory, db_session):
        lid = _lead(memory, db_session, stage=PipelineStage.negotiation)
        pid = _approved_proposal(memory, db_session, lid)
        client.post(f"/api/v1/dashboard/proposals/{pid}/send", headers=admin_headers())
        assert _stage_of(memory, db_session, lid) == PipelineStage.negotiation

    @pytest.mark.parametrize("terminal", [PipelineStage.won, PipelineStage.lost])
    def test_send_never_overwrites_won_or_lost(self, client, memory, db_session, terminal):
        lid = _lead(memory, db_session, stage=terminal)
        pid = _approved_proposal(memory, db_session, lid)
        client.post(f"/api/v1/dashboard/proposals/{pid}/send", headers=admin_headers())
        assert _stage_of(memory, db_session, lid) == terminal

    def test_accept_never_overwrites_lost(self, client, memory, db_session):
        lid = _lead(memory, db_session, stage=PipelineStage.proposal_draft)
        pid = _approved_proposal(memory, db_session, lid)
        token = client.post(f"/api/v1/dashboard/proposals/{pid}/send", headers=admin_headers()).json()["accept_token"]
        client.patch(STAGE.format(lid), json={"stage": "lost", "reason": "Went silent"}, headers=admin_headers())
        client.post(f"/api/v1/proposals/accept/{token}")
        assert _stage_of(memory, db_session, lid) == PipelineStage.lost

    @pytest.mark.parametrize("terminal", [PipelineStage.won, PipelineStage.lost, PipelineStage.proposal_sent])
    def test_manual_field_edit_never_regresses_the_stage(self, client, memory, db_session, terminal):
        lid = _lead(memory, db_session, stage=terminal)
        fields = {"name": "C", "email": "stage@example.com", "project_type": "rooftop",
                  "location": "Kolkata", "capacity_kw": 8}
        r = client.patch(f"/api/v1/dashboard/leads/{lid}/fields", json={"fields": fields}, headers=admin_headers())
        assert r.status_code == 200
        assert _stage_of(memory, db_session, lid) == terminal

    def test_manual_field_edit_still_advances_a_new_lead(self, client, memory, db_session):
        lid = _lead(memory, db_session)
        fields = {"name": "C", "email": "stage@example.com", "project_type": "rooftop",
                  "location": "Kolkata", "capacity_kw": 8}
        client.patch(f"/api/v1/dashboard/leads/{lid}/fields", json={"fields": fields}, headers=admin_headers())
        assert _stage_of(memory, db_session, lid) == PipelineStage.qualified

    def test_redraft_does_not_pull_a_sent_lead_back(self, memory, db_session):
        lid = _lead(memory, db_session, stage=PipelineStage.proposal_sent)
        memory.update_lead_fields(lead_id=lid, new_fields={}, performed_by="t", stage=PipelineStage.proposal_draft)
        db_session.commit()
        assert _stage_of(memory, db_session, lid) == PipelineStage.proposal_sent
