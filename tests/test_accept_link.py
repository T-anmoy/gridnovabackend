"""
Phase 4 — customer-facing proposal accept-link (public, no auth).
Confirms the internal two-stage approval gate is completely unaffected.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.memory.models import PipelineStage, ProposalStatus
from tests.conftest import admin_headers, sales_headers


def _sent_proposal(memory, db_session, *, email="ac@example.com"):
    """Draft → approve → send a proposal, returning (proposal_id, accept_token)."""
    customer = memory.get_or_create_customer(email=email, name="C")
    db_session.commit()
    lead = memory.create_lead(customer_id=customer.id)
    memory.update_lead_fields(lead_id=lead.id, new_fields={"name": "C"}, performed_by="t",
                              stage=PipelineStage.qualified)
    proposal = memory.create_proposal_draft(
        lead_id=lead.id, content={"sections": [{"id": "scope", "category": "scope", "body": "Install 8kW"}]},
        total_value=200_000, requires_manager_signoff=False, performed_by="emp-1",
    )
    memory.transition_proposal_status(proposal_id=proposal.id, new_status=ProposalStatus.approved,
                                      performed_by="emp-1", approved_by_employee_id="emp-admin-1")
    memory.transition_proposal_status(proposal_id=proposal.id, new_status=ProposalStatus.sent,
                                      performed_by="emp-1", date_sent=datetime.now(timezone.utc))
    token = memory.generate_accept_token(proposal.id, performed_by="emp-1")
    db_session.commit()
    return proposal.id, token


class TestTokenGeneratedAtSend:
    def test_send_endpoint_generates_token(self, client, memory, db_session):
        customer = memory.get_or_create_customer(email="send@example.com", name="C")
        db_session.commit()
        lead = memory.create_lead(customer_id=customer.id)
        memory.update_lead_fields(lead_id=lead.id, new_fields={"name": "C"}, performed_by="t",
                                  stage=PipelineStage.qualified)
        p = memory.create_proposal_draft(lead_id=lead.id, content={}, total_value=100_000,
                                         requires_manager_signoff=False, performed_by="emp-1")
        db_session.commit()
        client.post(f"/api/v1/dashboard/proposals/{p.id}/approve", headers=admin_headers())
        client.post(f"/api/v1/dashboard/proposals/{p.id}/send", headers=admin_headers())
        db_session.expire_all()  # the token was written by the endpoint's own session
        assert memory.get_proposal(p.id).accept_token is not None


class TestPublicView:
    def test_valid_token_returns_summary(self, client, memory, db_session):
        pid, token = _sent_proposal(memory, db_session)
        r = client.get(f"/api/v1/proposals/accept/{token}")
        assert r.status_code == 200
        body = r.json()
        assert body["proposal_id"] == pid
        assert body["price"] == 200_000
        assert body["state"] == "acceptable"
        assert body["scope"]  # scope surfaced

    def test_unknown_token_404(self, client):
        assert client.get("/api/v1/proposals/accept/definitely-not-a-real-token").status_code == 404


class TestPublicAccept:
    def test_valid_accept(self, client, memory, db_session):
        pid, token = _sent_proposal(memory, db_session)
        r = client.post(f"/api/v1/proposals/accept/{token}")
        assert r.status_code == 200
        assert r.json()["already_accepted"] is False
        p = memory.get_proposal(pid)
        assert p.accepted_at is not None
        assert p.status == ProposalStatus.accepted

    def test_unknown_token_404(self, client):
        assert client.post("/api/v1/proposals/accept/nope").status_code == 404

    def test_idempotent_retry(self, client, memory, db_session):
        pid, token = _sent_proposal(memory, db_session)
        r1 = client.post(f"/api/v1/proposals/accept/{token}")
        first_accepted_at = memory.get_proposal(pid).accepted_at
        r2 = client.post(f"/api/v1/proposals/accept/{token}")
        assert r1.status_code == 200 and r2.status_code == 200
        assert r2.json()["already_accepted"] is True
        # accepted_at not overwritten on retry (no double-processing).
        assert memory.get_proposal(pid).accepted_at == first_accepted_at

    def test_expired_cannot_accept(self, client, memory, db_session):
        pid, token = _sent_proposal(memory, db_session)
        # Backdate the send well beyond the validity window.
        p = memory.get_proposal(pid)
        p.date_sent = datetime.now(timezone.utc) - timedelta(days=40)
        db_session.commit()
        r = client.post(f"/api/v1/proposals/accept/{token}")
        assert r.status_code == 409

    def test_customer_accept_audit_is_distinct(self, client, memory, db_session):
        pid, token = _sent_proposal(memory, db_session)
        client.post(f"/api/v1/proposals/accept/{token}")
        actions = {log.action for log in memory.list_audit_logs(entity_type="proposal", entity_id=str(pid))}
        assert "customer_accepted" in actions  # distinct from staff status_changed_to_approved


class TestRateLimit:
    def test_excess_requests_rejected(self, client, memory, db_session):
        _, token = _sent_proposal(memory, db_session)
        statuses = [client.get(f"/api/v1/proposals/accept/{token}").status_code for _ in range(35)]
        assert 429 in statuses


class TestApprovalGateUnaffected:
    def test_internal_approval_gate_still_enforced(self, client, memory, db_session):
        # A management token still cannot use the internal standard-approve path,
        # and sales still cannot manager-sign-off — the customer accept path is separate.
        customer = memory.get_or_create_customer(email="gate@example.com", name="C")
        db_session.commit()
        lead = memory.create_lead(customer_id=customer.id)
        memory.update_lead_fields(lead_id=lead.id, new_fields={"name": "C"}, performed_by="t",
                                  stage=PipelineStage.qualified)
        p = memory.create_proposal_draft(lead_id=lead.id, content={}, total_value=100_000,
                                         requires_manager_signoff=False, performed_by="emp-1")
        db_session.commit()
        from tests.conftest import management_headers
        assert client.post(f"/api/v1/dashboard/proposals/{p.id}/approve",
                           headers=management_headers()).status_code == 403
