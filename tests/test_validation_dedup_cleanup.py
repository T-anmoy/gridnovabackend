"""
Phase 6 validation/dedup cleanup:
1. Lead-level dedup — concurrent/duplicate first-messages from one customer fold
   into one open lead instead of minting a new one each time.
2. A client-supplied lead_id is validated to exist BEFORE any writes happen.
3. request-revision's `reason` requires min_length=1, matching adjust-pricing.
"""
from app.memory.models import LeadSource, PipelineStage, ProposalStatus
from tests.conftest import admin_headers


class TestSessionContinuity:
    """BE-1 S2: a conversation continues only through its session_token."""

    def test_the_token_continues_the_same_lead(self, client, db_session):
        from tests.chat_support import lead_of
        r1 = client.post("/api/v1/chat/message", json={
            "message": "first message", "email": "dedup-lead@example.com", "name": "Dedup Test",
        })
        assert r1.status_code == 200
        token = r1.json()["session_token"]
        r2 = client.post("/api/v1/chat/message", json={"message": "second message", "session_token": token})
        assert r2.status_code == 200
        assert r2.json()["session_token"] == token
        assert lead_of(db_session, token).id == lead_of(db_session, r2.json()["session_token"]).id

    def test_a_message_without_a_token_starts_a_new_conversation(self, client, db_session):
        """No more folding into "the customer's open lead" by email: an email cannot
        prove who is typing, so a tokenless message never joins an existing lead."""
        from tests.chat_support import lead_of
        r1 = client.post("/api/v1/chat/message", json={
            "message": "first message", "email": "fold@example.com", "name": "Fold"})
        r2 = client.post("/api/v1/chat/message", json={
            "message": "second message", "email": "fold@example.com", "name": "Fold"})
        assert r1.status_code == r2.status_code == 200
        assert r1.json()["session_token"] != r2.json()["session_token"]
        assert lead_of(db_session, r1.json()["session_token"]).id != lead_of(db_session, r2.json()["session_token"]).id

    def test_won_lead_does_not_block_a_new_inquiry(self, client, memory, db_session):
        from tests.chat_support import lead_of
        customer = memory.get_or_create_customer(email="repeat-customer@example.com", name="Repeat Customer")
        db_session.commit()
        old_lead = memory.create_lead(customer_id=customer.id, source=LeadSource.website_chat)
        memory.update_lead_fields(lead_id=old_lead.id, new_fields={}, performed_by="test",
                                  stage=PipelineStage.won)
        db_session.commit()

        r = client.post("/api/v1/chat/message", json={
            "message": "new inquiry", "email": "repeat-customer@example.com", "name": "Repeat Customer",
        })
        assert r.status_code == 200
        assert lead_of(db_session, r.json()["session_token"]).id != old_lead.id


class TestClientIdsAreIgnored:
    """lead_id / customer_id in the body are deprecated and ignored (BE-1 S2) — they
    used to be trusted, and they are sequential."""

    def test_unknown_lead_id_just_starts_a_new_conversation(self, client, db_session):
        from tests.chat_support import lead_of
        r = client.post("/api/v1/chat/message", json={"message": "hi", "lead_id": 999999})
        assert r.status_code == 200
        assert lead_of(db_session, r.json()["session_token"]).id != 999999

    def test_someone_elses_lead_and_customer_ids_are_ignored(self, client, memory, db_session):
        from tests.chat_support import customer_of, lead_of
        customer = memory.get_or_create_customer(email="lead-id-check@example.com", name="Check")
        db_session.commit()
        lead = memory.create_lead(customer_id=customer.id, source=LeadSource.website_chat)
        db_session.commit()
        r = client.post("/api/v1/chat/message", json={
            "message": "hi it's me", "lead_id": lead.id, "customer_id": customer.id,
        })
        assert r.status_code == 200
        token = r.json()["session_token"]
        assert lead_of(db_session, token).id != lead.id
        assert customer_of(db_session, token).id != customer.id
        assert memory.get_lead_context(lead.id)["messages"] == []

    def test_a_token_for_an_existing_lead_continues_it(self, client, memory, db_session):
        from tests.chat_support import customer_of, lead_of, token_for
        customer = memory.get_or_create_customer(email="lead-id-valid@example.com", name="Valid")
        db_session.commit()
        lead = memory.create_lead(customer_id=customer.id, source=LeadSource.website_chat)
        db_session.commit()
        token = token_for(memory, db_session, lead.id)
        r = client.post("/api/v1/chat/message", json={"message": "hi", "session_token": token})
        assert r.status_code == 200
        assert lead_of(db_session, token).id == lead.id
        assert customer_of(db_session, token).id == customer.id


class TestRevisionReasonValidation:
    def _sent_or_draft_proposal(self, memory, db_session):
        customer = memory.get_or_create_customer(email="revision-validation@example.com", name="Rev")
        db_session.commit()
        lead = memory.create_lead(customer_id=customer.id)
        memory.update_lead_fields(lead_id=lead.id, new_fields={"name": "Rev"}, performed_by="t",
                                  stage=PipelineStage.qualified)
        p = memory.create_proposal_draft(lead_id=lead.id, content={}, total_value=50_000,
                                         requires_manager_signoff=False, performed_by="emp-1")
        db_session.commit()
        return p.id

    def test_empty_reason_rejected(self, client, memory, db_session):
        pid = self._sent_or_draft_proposal(memory, db_session)
        r = client.post(f"/api/v1/dashboard/proposals/{pid}/request-revision",
                         json={"reason": ""}, headers=admin_headers())
        assert r.status_code == 422

    def test_nonempty_reason_still_works(self, client, memory, db_session):
        pid = self._sent_or_draft_proposal(memory, db_session)
        r = client.post(f"/api/v1/dashboard/proposals/{pid}/request-revision",
                         json={"reason": "needs a real quote"}, headers=admin_headers())
        assert r.status_code == 200
