"""BE-1 S5 — the proposal state machine and the accept link.

Before: accept_proposal checked only accepted_at and expiry, never status; tokens were
never revoked; request_revision moved a proposal from ANY status (even accepted). A
customer could accept a proposal staff had since sent back, or an older version.
"""
import itertools
import threading
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.memory.memory_manager import (
    PROPOSAL_TRANSITIONS, ProposalTransitionError, check_proposal_transition,
)
from app.memory.models import PipelineStage, ProposalStatus, TimelineEvent, TimelineEventType
from tests.conftest import TEST_DATABASE_URL, admin_headers

pytestmark = pytest.mark.usefixtures("known_employees")

ALLOWED = {
    ("draft", "pending_pricing_validation"), ("draft", "pending_approval"), ("draft", "approved"),
    ("draft", "revision_requested"),
    ("pending_pricing_validation", "pending_approval"), ("pending_pricing_validation", "approved"),
    ("pending_pricing_validation", "revision_requested"), ("pending_pricing_validation", "draft"),
    ("pending_approval", "approved"), ("pending_approval", "revision_requested"), ("pending_approval", "draft"),
    ("approved", "sent"), ("approved", "revision_requested"),
    ("sent", "accepted"), ("sent", "revision_requested"),
}


@pytest.mark.parametrize("current, target", list(itertools.product([s.value for s in ProposalStatus], repeat=2)))
def test_the_transition_table(current, target):
    if (current, target) in ALLOWED:
        check_proposal_transition(current, target)
    else:
        with pytest.raises(ProposalTransitionError):
            check_proposal_transition(current, target)


def test_the_table_covers_every_status():
    assert set(PROPOSAL_TRANSITIONS) == set(ProposalStatus)


def _lead(memory, db_session, email):
    customer = memory.get_or_create_customer(email=email, name="C")
    lead = memory.create_lead(customer_id=customer.id)
    memory.update_lead_fields(lead_id=lead.id, new_fields={"name": "C"}, performed_by="t",
                              stage=PipelineStage.qualified)
    db_session.commit()
    return lead


def _draft(memory, lead_id):
    return memory.create_proposal_draft(
        lead_id=lead_id, content={"sections": [{"id": "scope", "category": "scope", "body": "Install 8kW"}]},
        total_value=200_000, requires_manager_signoff=False, performed_by="emp-1")


def _send(memory, db_session, proposal_id):
    memory.transition_proposal_status(proposal_id=proposal_id, new_status=ProposalStatus.approved,
                                      performed_by="emp-1", approved_by_employee_id="emp-admin-1")
    memory.transition_proposal_status(proposal_id=proposal_id, new_status=ProposalStatus.sent,
                                      performed_by="emp-1", date_sent=datetime.now(timezone.utc))
    token = memory.generate_accept_token(proposal_id, performed_by="emp-1")
    db_session.commit()
    return token


def _sent(memory, db_session, email="sm@example.com"):
    lead = _lead(memory, db_session, email)
    proposal = _draft(memory, lead.id)
    db_session.commit()
    return lead, proposal.id, _send(memory, db_session, proposal.id)


def _accepted_events(db_session, lead_id) -> int:
    db_session.expire_all()
    return len(db_session.execute(select(TimelineEvent).where(
        TimelineEvent.lead_id == lead_id, TimelineEvent.event_type == TimelineEventType.customer_accepted)).all())


class TestRequestRevision:
    def test_an_accepted_proposal_cannot_be_sent_back(self, client, memory, db_session):
        _, pid, token = _sent(memory, db_session)
        assert client.post(f"/api/v1/proposals/accept/{token}").status_code == 200
        r = client.post(f"/api/v1/dashboard/proposals/{pid}/request-revision", json={"reason": "x"},
                        headers=admin_headers())
        assert r.status_code == 409 and r.json()["detail"]["code"] == "invalid_transition"
        db_session.expire_all()
        assert memory.get_proposal(pid).status == ProposalStatus.accepted

    def test_sending_back_a_sent_proposal_revokes_its_link(self, client, memory, db_session):
        _, pid, token = _sent(memory, db_session)
        r = client.post(f"/api/v1/dashboard/proposals/{pid}/request-revision", json={"reason": "wrong roof"},
                        headers=admin_headers())
        assert r.status_code == 200
        db_session.expire_all()
        assert memory.get_proposal(pid).accept_token is None
        for method, path in (("GET", f"/api/v1/proposals/accept/{token}"),
                             ("POST", f"/api/v1/proposals/accept/{token}"),
                             ("GET", f"/api/v1/public/proposals/{token}/pdf")):
            assert client.request(method, path).status_code == 404
        actions = [log.action for log in memory.list_audit_logs(entity_type="proposal", entity_id=str(pid))]
        assert "accept_token_revoked" in actions


class TestAcceptLink:
    def test_a_link_left_on_a_sent_back_proposal_is_refused(self, client, memory, db_session):
        """Rows sent back before tokens were revoked still hold one."""
        _, pid, token = _sent(memory, db_session)
        proposal = memory.get_proposal(pid)
        proposal.status = ProposalStatus.revision_requested  # as an older build left it
        db_session.commit()
        for method in ("GET", "POST"):
            r = client.request(method, f"/api/v1/proposals/accept/{token}")
            assert r.status_code == 409 and r.json()["detail"]["code"] == "proposal_not_available"
        assert client.get(f"/api/v1/public/proposals/{token}/pdf").json()["detail"]["code"] == "proposal_not_available"

    def test_an_older_version_is_superseded(self, client, memory, db_session):
        lead, pid, token = _sent(memory, db_session)
        _draft(memory, lead.id)  # v2 drafted after v1 went out
        db_session.commit()
        for method, path in (("GET", f"/api/v1/proposals/accept/{token}"),
                             ("POST", f"/api/v1/proposals/accept/{token}"),
                             ("GET", f"/api/v1/public/proposals/{token}/pdf")):
            r = client.request(method, path)
            assert r.status_code == 409 and r.json()["detail"]["code"] == "proposal_superseded", (method, path)
        db_session.expire_all()
        assert memory.get_proposal(pid).accepted_at is None

    def test_nothing_else_is_accepted_once_one_proposal_is(self, client, memory, db_session):
        lead, _, first_token = _sent(memory, db_session)
        assert client.post(f"/api/v1/proposals/accept/{first_token}").status_code == 200
        # A later version sent by mistake: the customer has already accepted one.
        second = _draft(memory, lead.id)
        db_session.commit()
        second_token = _send(memory, db_session, second.id)
        r = client.post(f"/api/v1/proposals/accept/{second_token}")
        assert r.status_code == 409 and r.json()["detail"]["code"] == "proposal_superseded"

    def test_an_approved_but_unsent_proposal_cannot_be_accepted(self, client, memory, db_session):
        lead = _lead(memory, db_session, "unsent@example.com")
        p = _draft(memory, lead.id)
        memory.transition_proposal_status(proposal_id=p.id, new_status=ProposalStatus.approved,
                                          performed_by="emp-1", approved_by_employee_id="emp-admin-1")
        token = memory.generate_accept_token(p.id, performed_by="emp-1")
        db_session.commit()
        r = client.post(f"/api/v1/proposals/accept/{token}")
        assert r.status_code == 409 and r.json()["detail"]["code"] == "proposal_not_available"

    def test_the_accepted_proposal_stays_viewable(self, client, memory, db_session):
        _, _, token = _sent(memory, db_session)
        client.post(f"/api/v1/proposals/accept/{token}")
        r = client.get(f"/api/v1/proposals/accept/{token}")
        assert r.status_code == 200 and r.json()["state"] == "accepted"

    def test_a_double_accept_records_one_acceptance(self, client, memory, db_session, monkeypatch):
        from app.notifications.notifier import notifier
        sent = []
        monkeypatch.setattr(notifier, "customer_accepted", lambda **k: sent.append(k))
        lead, _, token = _sent(memory, db_session)
        r1 = client.post(f"/api/v1/proposals/accept/{token}")
        r2 = client.post(f"/api/v1/proposals/accept/{token}")
        assert r1.json()["already_accepted"] is False and r2.json()["already_accepted"] is True
        assert _accepted_events(db_session, lead.id) == 1
        assert len(sent) == 1

    def test_expiry_uses_the_configured_validity(self, client, memory, db_session, monkeypatch):
        from app.rules import sales_policy
        monkeypatch.setattr(sales_policy, "proposal_validity_days", lambda: 0)
        _, _, token = _sent(memory, db_session)
        r = client.post(f"/api/v1/proposals/accept/{token}")
        assert r.status_code == 409 and r.json()["detail"]["code"] == "proposal_expired"


@pytest.mark.skipif(not TEST_DATABASE_URL, reason="row locks need PostgreSQL")
def test_two_simultaneous_accepts_record_one(memory, db_session, _isolated_db, monkeypatch):
    """Two requests for the same token at the same moment: the second waits on the
    row lock and then sees the first acceptance."""
    from starlette.requests import Request

    from app.api.routes import public_proposals
    from app.notifications.notifier import notifier

    monkeypatch.setattr(notifier, "customer_accepted", lambda **k: None)
    lead, _, token = _sent(memory, db_session, email="race@example.com")
    barrier, results = threading.Barrier(2), []

    def accept():
        session = _isolated_db()
        try:
            request = Request({"type": "http", "headers": [], "client": ("10.0.0.1", 1)})
            barrier.wait()
            results.append(public_proposals.accept_proposal(token, request, session)["already_accepted"])
        finally:
            session.close()

    threads = [threading.Thread(target=accept) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [False, True]
    assert _accepted_events(db_session, lead.id) == 1


class TestSuperseded:
    def test_a_newer_version_or_another_acceptance_supersedes(self, memory, db_session):
        from app.proposals import acceptance
        lead, pid, _ = _sent(memory, db_session, email="superseded@example.com")
        v1 = memory.get_proposal(pid)
        assert acceptance.superseded(db_session, v1) is False
        v2 = _draft(memory, lead.id)
        db_session.commit()
        assert acceptance.superseded(db_session, v1) is True     # newer version
        assert acceptance.superseded(db_session, v2) is False
        memory.mark_customer_accepted(pid)
        db_session.commit()
        assert acceptance.superseded(db_session, v2) is True     # another one accepted
