"""
Resilience tests:
- LLM failure triggers deterministic fallback (not an error to customer)
- R2 upload failure does not block proposal approval
- Rate limiting rejects requests beyond threshold
"""
import pytest
from unittest.mock import patch, MagicMock
from app.memory.models import PipelineStage, ProposalStatus
from tests.conftest import sales_headers, admin_headers


def _make_qualified_lead(memory, db_session):
    customer = memory.get_or_create_customer(email="resilience@example.com", name="Res User")
    db_session.commit()
    lead = memory.create_lead(customer_id=customer.id)
    memory.update_lead_fields(
        lead_id=lead.id,
        new_fields={
            "name": "Res User",
            "email": "resilience@example.com",
            "project_type": "commercial",
            "location": "Kolkata",
            "capacity_kw": 100,
            "budget": 300_000,
            "timeline": "6 months",
            "monthly_electricity_bill": 50000,
        },
        performed_by="test",
        stage=PipelineStage.qualified,
    )
    db_session.commit()
    return customer.id, lead.id


class TestLLMFallback:
    def test_llm_failure_returns_fallback_question_not_500(self, client):
        """When LLM extraction and reply both fail, customer gets a deterministic question."""
        with patch("app.llm.client.LLMClient.extract_fields", side_effect=RuntimeError("LLM down")), \
             patch("app.llm.client.LLMClient.generate_reply", side_effect=RuntimeError("LLM down")):
            # generate_reply raises → fallback kicks in inside generate_reply wrapper
            pass  # extract_fields failure returns {} → missing fields intact

        # Actual test: patch only the internal _chat to simulate failure
        with patch("app.llm.client.LLMClient._chat", side_effect=Exception("Connection refused")):
            r = client.post("/api/v1/chat/message", json={
                "message": "I need solar for my factory",
                "email": "fallback_test@example.com",
                "name": "Fallback Tester",
            })
        assert r.status_code == 200
        data = r.json()
        # Must return a reply (fallback question), not crash
        assert len(data["reply"]) > 0
        # Lead still created
        assert data["lead_id"] is not None

    def test_chat_still_works_in_mock_mode(self, client):
        """Default test config has LLM_API_KEY unset → mock mode active."""
        r = client.post("/api/v1/chat/message", json={
            "message": "Tell me about solar panels",
            "email": "mocktest@example.com",
            "name": "Mock Tester",
        })
        assert r.status_code == 200
        # Mock replies are prefixed
        assert "[MOCK]" in r.json()["reply"] or len(r.json()["reply"]) > 0


class TestR2UploadResilience:
    def test_approval_succeeds_even_when_r2_upload_fails(self, client, memory, db_session, monkeypatch):
        """R2 upload failure must not block the approval HTTP response."""
        cid, lid = _make_qualified_lead(memory, db_session)

        # Draft a proposal
        r = client.post("/api/v1/dashboard/proposals/draft", json={
            "lead_id": lid, "customer_id": cid,
        }, headers=sales_headers())
        assert r.status_code == 200
        pid = r.json()["proposal_id"]

        # A genuinely transient failure (unlike a missing-config one) still retries
        # with back-off — bound that so this test doesn't spend several real
        # seconds asleep in the retry loop, which now runs synchronously within
        # the approve() call itself under tests (see conftest.py's
        # _start_pdf_generation monkeypatch).
        monkeypatch.setattr("app.api.routes.dashboard._PDF_GENERATION_TIMEOUT_SECONDS", 0.3)

        # Approve — with R2 upload stubbed to fail
        with patch("app.pdf.generator.upload_to_r2", side_effect=RuntimeError("R2 unreachable")):
            r = client.post(f"/api/v1/dashboard/proposals/{pid}/approve", headers=sales_headers())

        # Approval must still succeed
        assert r.status_code == 200
        assert r.json()["status"] == "approved"

        # pdf_url endpoint returns "pdf_pending" state — PDF generation already ran
        # (synchronously, under tests) by the time approve() returned above.
        r = client.get(f"/api/v1/dashboard/proposals/{pid}/pdf", headers=admin_headers())
        assert r.status_code == 200
        # Either pending or None — approval was not blocked
        assert r.json().get("status") == "pdf_pending" or r.json().get("pdf_url") is None


class TestRateLimiting:
    def test_rate_limit_rejects_excess_requests(self, client):
        """More than 20 requests/minute per IP should be rejected with 429."""
        # The limiter uses per-IP tracking. In tests all requests come from testclient.
        # We need to exceed the limit of 20/minute.
        responses = []
        for i in range(25):
            r = client.post("/api/v1/chat/message", json={
                "message": f"Hello attempt {i}",
                "email": f"ratetest{i}@example.com",
                "name": f"Rate Tester {i}",
            })
            responses.append(r.status_code)

        # At least one 429 must appear in the responses
        assert 429 in responses, f"Expected at least one 429, got: {set(responses)}"
