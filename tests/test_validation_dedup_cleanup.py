"""
Phase 6 validation/dedup cleanup:
1. Lead-level dedup — concurrent/duplicate first-messages from one customer fold
   into one open lead instead of minting a new one each time.
2. A client-supplied lead_id is validated to exist BEFORE any writes happen.
3. request-revision's `reason` requires min_length=1, matching adjust-pricing.
"""
from app.memory.models import LeadSource, PipelineStage, ProposalStatus
from tests.conftest import admin_headers


class TestLeadLevelDedup:
    def test_second_message_same_email_no_lead_id_reuses_open_lead(self, client, db_session):
        r1 = client.post("/api/v1/chat/message", json={
            "message": "first message", "email": "dedup-lead@example.com", "name": "Dedup Test",
        })
        assert r1.status_code == 200
        lead_id_1 = r1.json()["lead_id"]

        # A second "first message" with the same identity and no lead_id — as if the
        # frontend hasn't stored the lead_id from the first response yet.
        r2 = client.post("/api/v1/chat/message", json={
            "message": "second message", "email": "dedup-lead@example.com", "name": "Dedup Test",
        })
        assert r2.status_code == 200
        assert r2.json()["lead_id"] == lead_id_1

    # Genuine multi-threaded concurrency (several near-simultaneous first-messages
    # racing for the same customer) is exercised live against a real, file-backed
    # server/DB rather than here — pytest's TestClient shares a single in-memory
    # SQLite DBAPI connection (StaticPool) across threads, which isn't reliably
    # thread-safe at the driver level regardless of application logic, unlike a
    # real deployment's separate per-session connections. See the fix's live
    # re-verification notes for the actual concurrent-request reproduction.

    def test_won_lead_does_not_block_a_new_inquiry(self, client, memory, db_session):
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
        assert r.json()["lead_id"] != old_lead.id


class TestLeadIdValidatedBeforeWrites:
    def test_nonexistent_lead_id_rejected_cleanly(self, client, db_session):
        from sqlalchemy import func, select
        from app.memory.models import Customer

        before = db_session.execute(select(func.count(Customer.id))).scalar_one()
        r = client.post("/api/v1/chat/message", json={"message": "hi", "lead_id": 999999})
        assert r.status_code == 404
        after = db_session.execute(select(func.count(Customer.id))).scalar_one()
        # No orphan customer created as a side effect of the rejected request.
        assert after == before

    def test_nonexistent_lead_id_rejected_even_with_customer_id_also_supplied(self, client, memory, db_session):
        customer = memory.get_or_create_customer(email="lead-id-check@example.com", name="Check")
        db_session.commit()
        r = client.post("/api/v1/chat/message", json={
            "message": "hi", "lead_id": 999999, "customer_id": customer.id,
        })
        assert r.status_code == 404

    def test_valid_lead_id_still_works(self, client, memory, db_session):
        customer = memory.get_or_create_customer(email="lead-id-valid@example.com", name="Valid")
        db_session.commit()
        lead = memory.create_lead(customer_id=customer.id, source=LeadSource.website_chat)
        db_session.commit()
        r = client.post("/api/v1/chat/message", json={"message": "hi", "lead_id": lead.id})
        assert r.status_code == 200
        assert r.json()["lead_id"] == lead.id
        assert r.json()["customer_id"] == customer.id


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
