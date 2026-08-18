"""
Sales Agent conversational redesign — deterministic staging + the warm reply
generator's design constraints. Warmth itself needs a real LLM (mock mode here),
so these assert the mechanism: the stage is a pure function of collected_fields, and
the reply prompt always carries the guardrails.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.config import settings
from app.llm.client import llm_client
from app.agents.sales_agent import determine_conversation_stage
from app.memory.models import PipelineStage
from app.scheduling import google_calendar
from app.security.encryption import encrypt, generate_key


# ── Stage function (pure logic, no LLM) ──────────────────────────────────────────

class TestStageFunction:
    def test_opening_when_nothing_collected(self):
        assert determine_conversation_stage({}) == "opening"

    def test_opening_ignores_contact_only(self):
        # Contact but no situational context yet → still opening (learn situation first).
        assert determine_conversation_stage({"name": "A", "email": "a@b.com"}) == "opening"

    def test_contact_bridge_when_situational_but_no_contact(self):
        assert determine_conversation_stage(
            {"project_type": "rooftop", "capacity_kw": 20}
        ) == "contact_bridge"

    def test_understanding_when_both_partial(self):
        # Situational + contact present, but a required field (location) still missing.
        assert determine_conversation_stage(
            {"project_type": "rooftop", "capacity_kw": 20, "name": "A", "email": "a@b.com"}
        ) == "understanding"

    def test_meeting_offer_when_nothing_missing(self):
        assert determine_conversation_stage({
            "name": "A", "email": "a@b.com", "project_type": "rooftop",
            "location": "Kolkata", "capacity_kw": 20,
        }) == "meeting_offer"


# ── Reply prompt guardrails ──────────────────────────────────────────────────────

class TestReplyPromptConstraints:
    def test_opening_prompt_forbids_asking_contact(self):
        prompt = llm_client._build_reply_system_prompt("opening", {}, ["name", "email"], None)
        assert "CURRENT STAGE: opening" in prompt
        assert "Do NOT ask for their name or email yet" in prompt

    def test_prompt_always_forbids_price_and_commitment(self):
        for stage in ("opening", "understanding", "contact_bridge", "meeting_offer"):
            prompt = llm_client._build_reply_system_prompt(stage, {"capacity_kw": 500}, [], None)
            assert "Never quote a price" in prompt
            assert "claim something\nhas been sent/approved" in prompt or "sent/approved" in prompt

    def test_price_question_still_gets_no_price_guardrail(self, monkeypatch):
        # Even when the customer directly asks cost, the outgoing system prompt still
        # carries the no-price rule (the reply generator is never told to quote one).
        captured = {}

        def _capture(messages, lead_id, operation):
            captured["system"] = messages[0]["content"]
            return "ok"

        monkeypatch.setattr(llm_client, "_chat", _capture)
        llm_client.generate_reply(
            user_message="how much will a 10kW system cost me?",
            stage="understanding",
            collected_fields={"project_type": "rooftop"},
            missing_fields=["location"],
            conversation_history=[],
        )
        assert "Never quote a price" in captured["system"]
        assert "instruction" in captured["system"].lower()  # injection guard present

    def test_meeting_offer_prompt_lists_only_supplied_slots(self):
        prompt = llm_client._build_reply_system_prompt(
            "meeting_offer", {}, [], ["Tue 12 Aug, 11:00 IST"]
        )
        assert "Tue 12 Aug, 11:00 IST" in prompt
        assert "never invent or" in prompt

    def test_calibration_guidance_present(self):
        # The homeowner-vs-industrial calibration lives in the prompt (observing the
        # actual tone difference needs a real LLM — noted in the summary).
        prompt = llm_client._build_reply_system_prompt("opening", {}, ["name"], None)
        assert "homeowner" in prompt and "industrial" in prompt
        assert "dial back the playfulness" in prompt


# ── Warm degraded fallback (no LLM) ──────────────────────────────────────────────

class TestWarmFallback:
    def test_opening_fallback_is_not_a_form(self):
        reply = llm_client._fallback_reply("opening", ["name", "email"], None)
        assert "name" not in reply.lower() and "email" not in reply.lower()

    def test_contact_bridge_fallback_is_benefit_framed(self):
        reply = llm_client._fallback_reply("contact_bridge", ["name", "email"], None)
        assert "team" in reply.lower()

    def test_meeting_offer_fallback_uses_real_slots(self):
        reply = llm_client._fallback_reply("meeting_offer", [], ["Tue 12 Aug, 11:00 IST"])
        assert "Tue 12 Aug, 11:00 IST" in reply


# ── Integration: opening asks no contact; meeting_offer proactively offers slots ──

def _connect_sales(memory, db_session, emp_id="emp-sales-1"):
    if not settings.fernet_encryption_key:
        settings.fernet_encryption_key = generate_key()
    memory.get_or_create_employee(emp_id, emp_id, f"{emp_id}@gridnova.in", "sales")
    db_session.commit()
    memory.update_employee_google_credentials(
        employee_id=emp_id, encrypted_refresh_token=encrypt("tok"),
        calendar_id="primary", performed_by="test",
    )
    db_session.commit()


class TestStagedFlow:
    def test_opening_message_does_not_request_contact(self, memory, db_session, monkeypatch):
        from app.agents.sales_agent import sales_agent

        captured = {}

        def _capture(messages, lead_id, operation):
            if operation == "generate_reply":
                captured["system"] = messages[0]["content"]
            return "[MOCK] reply"

        monkeypatch.setattr(llm_client, "_chat", _capture)
        customer = memory.get_or_create_customer(email="open@example.com", name="Opener")
        db_session.commit()
        lead = memory.create_lead(customer_id=customer.id)
        db_session.commit()

        result = sales_agent.handle_message(memory, customer.id, lead.id, "thinking about solar")
        db_session.commit()
        # Opening stage → prompt explicitly forbids asking for contact details.
        assert "CURRENT STAGE: opening" in captured["system"]
        assert "Do NOT ask for their name or email yet" in captured["system"]
        assert result["offered_slots"] == []

    def test_meeting_offer_proactively_offers_real_slots(self, memory, db_session, monkeypatch):
        from app.agents.sales_agent import sales_agent

        _connect_sales(memory, db_session)
        monkeypatch.setattr(google_calendar, "get_busy_intervals", lambda *a, **k: [])
        # No explicit meeting intent — offering is driven purely by the meeting_offer stage.
        monkeypatch.setattr(
            llm_client, "extract_meeting_signal",
            lambda **k: {"intent": False, "confirmed_slot_iso": None},
        )
        monkeypatch.setattr(llm_client, "extract_fields", lambda **k: {})

        customer = memory.get_or_create_customer(email="ready@example.com", name="Ready")
        db_session.commit()
        lead = memory.create_lead(customer_id=customer.id)
        memory.update_lead_fields(
            lead_id=lead.id,
            new_fields={
                "name": "Ready", "email": "ready@example.com", "project_type": "rooftop",
                "location": "Kolkata", "capacity_kw": 20,
            },
            performed_by="test", stage=PipelineStage.qualified,
        )
        db_session.commit()

        result = sales_agent.handle_message(memory, customer.id, lead.id, "sounds good")
        db_session.commit()
        assert result["offered_slots"], "meeting_offer stage should proactively offer real slots"
        assert memory.get_lead(lead.id).pending_meeting_slots["slots"] == result["offered_slots"]
