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
            assert "sent or approved" in prompt

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

    def test_matching_customer_energy_is_present(self):
        # Replaces the old per-segment (homeowner vs industrial) calibration with a
        # general rule: match whatever energy THIS customer actually brought, rather
        # than assuming tone from their segment.
        prompt = llm_client._build_reply_system_prompt("opening", {}, ["name"], None)
        assert "Match the customer's energy" in prompt
        assert "short, direct replies" in prompt


class TestToneCommitments:
    """The prompt rules that keep Nova from sounding like a chatbot.

    Each was added after watching the live model break it: three stacked questions in
    one breath, twelve exclamation marks across thirteen replies, and — worst — an
    example figure from the prompt itself quoted back to a customer whose actual bill
    was ten times smaller.
    """

    def _prompt(self, stage="opening"):
        return llm_client._build_reply_system_prompt(stage, {}, ["name"], None, None)

    def test_one_question_rule_is_stated_as_the_top_rule(self):
        prompt = self._prompt()
        assert "THE ONE RULE THAT MATTERS MOST" in prompt
        assert "Ask ONE question" in prompt

    def test_generic_filler_language_is_banned_by_name(self):
        prompt = self._prompt()
        assert "hollow filler" in prompt
        for banned in ("industry-leading", "trusted provider"):
            assert banned in prompt, banned

    def test_performed_enthusiasm_is_curbed(self):
        assert "Never perform enthusiasm that isn't earned" in self._prompt()

    def test_the_model_is_told_not_to_reuse_example_figures(self):
        """The live run had Nova tell a customer with a 40,000 bill that "four lakh
        a month is a serious number" — lifted straight from a prompt example. Every
        illustrative figure is now a placeholder, and the rule is stated outright."""
        prompt = self._prompt()
        assert "NUMBERS BELONG TO THE CONVERSATION" in prompt
        assert "repeat one to a customer" in prompt
        assert "<AMOUNT>" in prompt, "example figures must be placeholders, not numbers"

    def test_hospitality_guidance_is_present(self):
        prompt = self._prompt()
        assert "consultative advisor" in prompt
        assert "never oversell" in prompt
        assert "address the hesitation honestly" in prompt

    def test_every_message_leaves_a_clear_next_step(self):
        assert "leave the customer knowing exactly what happens" in self._prompt()

    def test_the_rulebook_is_a_cacheable_prefix(self):
        """Money, not tidiness. OpenAI caches a prompt prefix only from 1024 tokens
        up, and only on an exact match. With the per-turn context at the TOP the
        shared prefix was 64 tokens and nothing was ever cached; measured against the
        live model, moving it to the bottom took caching from 0% to ~49% of all input
        tokens. Anything that reintroduces per-turn text above the rulebook silently
        doubles the bill for every reply.
        """
        a = llm_client._build_reply_system_prompt("opening", {}, ["name"], None, None)
        b = llm_client._build_reply_system_prompt(
            "understanding", {"capacity_kw": 50}, ["email"], None, None
        )
        shared = 0
        while shared < min(len(a), len(b)) and a[shared] == b[shared]:
            shared += 1
        # Roughly 4 characters per token; the prefix must clear 1024 with room spare.
        assert shared > 4500, "shared prefix is only ~%s tokens" % (shared // 4)
        # And the per-turn context must still actually reach the model.
        assert "CURRENT STAGE: opening" in a
        assert "STILL NEEDED: name" in a
        assert a.index("THE ONE RULE") < a.index("CURRENT STAGE:"), (
            "rules must come before per-turn context, or the prefix stops caching"
        )

    def test_the_memory_block_stays_below_the_rulebook(self):
        """Memory varies per customer, so it belongs with the per-turn context. Above
        the rulebook it would break the cache prefix for every reply."""
        prompt = llm_client._build_reply_system_prompt(
            "understanding", {}, ["email"], None, "What YOU should do differently: slow down.",
        )
        assert "slow down" in prompt
        assert prompt.index("THE ONE RULE") < prompt.index("slow down")


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


@pytest.mark.parametrize("raw", ['["rooftop", "Surat"]', '"rooftop"', "42", "null"])
def test_extraction_that_is_not_an_object_extracts_nothing(raw, monkeypatch):
    """A model answering with a list or a bare value must cost the turn nothing: the
    agent reads the extraction as field -> value, so anything else is an empty one."""
    from app.llm import client as llm_module

    client = llm_module.LLMClient.__new__(llm_module.LLMClient)
    client._mock = False
    monkeypatch.setattr(client, "_chat", lambda messages, lead_id, operation: raw, raising=False)
    assert client.extract_fields("rooftop in Surat", []) == {}
