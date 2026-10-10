"""BE-1 S2 — website chat sessions are bound to a secret, and nothing a visitor
types can attach their conversation to another customer.

Before: the browser's lead_id/customer_id (sequential integers) were trusted, and an
email typed mid-chat that matched a real customer MERGED the anonymous session into
that customer — after which the victim's profile and memories went into Nova's prompt
for the stranger's next reply.
"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.agents import guardrails
from app.llm.client import LLMClient
from app.memory.memory_manager import is_placeholder_email
from app.memory.models import (
    ChatSession, ConversationMessage, LeadSource, MemoryKind, MemorySource, TimelineEventType,
)
from tests.chat_support import chat, customer_of, lead_of, session_row, token_for

VICTIM_EMAIL = "victim@example.com"
VICTIM_SECRET = "Prefers to pay through the Kolkata HDFC branch, budget twelve lakh"


@pytest.fixture
def victim(memory, db_session):
    customer = memory.get_or_create_customer(email=VICTIM_EMAIL, name="Victim Person", phone="9876512345")
    lead = memory.create_lead(customer_id=customer.id, source=LeadSource.website_chat)
    memory.upsert_customer_profile(customer.id, summary=VICTIM_SECRET, performed_by="test")
    memory.add_customer_memory(customer.id, MemoryKind.preference, VICTIM_SECRET, lead_id=lead.id,
                               source=MemorySource.reflection, created_by="test")
    db_session.commit()
    return customer, lead


@pytest.fixture
def prompts(monkeypatch):
    """Every reply system prompt the model client builds during the test."""
    built = []
    original = LLMClient._build_reply_system_prompt

    def capture(*args, **kwargs):
        text = original(*args, **kwargs)
        built.append(text)
        return text

    monkeypatch.setattr(LLMClient, "_build_reply_system_prompt", staticmethod(capture))
    return built


class TestTokens:
    def test_first_message_returns_a_token_and_no_ids(self, client):
        r = chat(client, "hi, thinking about solar")
        assert r.status_code == 200
        body = r.json()
        assert set(body) == {"reply", "session_token", "is_qualified", "missing_fields", "meeting_booked"}
        assert len(body["session_token"]) >= 40

    def test_the_token_resumes_its_own_conversation(self, client, db_session):
        token = chat(client, "hi").json()["session_token"]
        lead_id = lead_of(db_session, token).id
        r = chat(client, "my name is Asha and we want rooftop solar", token)
        assert r.json()["session_token"] == token
        assert lead_of(db_session, token).id == lead_id
        messages = [m.content for m in db_session.execute(
            select(ConversationMessage).where(ConversationMessage.lead_id == lead_id)).scalars()]
        assert "hi" in messages and any("Asha" in m for m in messages)

    def test_only_the_hash_is_stored(self, client, db_session):
        token = chat(client, "hi").json()["session_token"]
        row = session_row(db_session, token)
        assert row.token_hash != token and len(row.token_hash) == 64
        assert db_session.execute(select(ChatSession).where(ChatSession.token_hash == token)).first() is None

    @pytest.mark.parametrize("token", ["not-a-real-token", "x" * 200])
    def test_an_unknown_token_starts_a_new_conversation(self, client, db_session, token):
        r = chat(client, "hello", token)
        assert r.status_code == 200
        assert r.json()["session_token"] != token

    def test_an_expired_token_starts_a_new_conversation(self, client, db_session):
        token = chat(client, "hi").json()["session_token"]
        row = session_row(db_session, token)
        row.last_seen_at = datetime.now(timezone.utc) - timedelta(days=31)
        db_session.commit()
        r = chat(client, "back again", token)
        assert r.json()["session_token"] != token
        assert lead_of(db_session, r.json()["session_token"]).id != row.lead_id

    def test_a_too_long_token_is_a_validation_error(self, client):
        assert chat(client, "hi", "x" * 257).status_code == 422


class TestNoTakeover:
    def test_guessing_another_leads_id_does_nothing(self, client, memory, db_session, victim):
        customer, lead = victim
        r = client.post("/api/v1/chat/message", json={
            "message": "continue please", "lead_id": lead.id, "customer_id": customer.id})
        assert r.status_code == 200
        token = r.json()["session_token"]
        assert lead_of(db_session, token).id != lead.id
        assert customer_of(db_session, token).id != customer.id
        assert memory.get_lead_context(lead.id)["messages"] == []

    def test_claiming_the_victims_email_mid_chat_does_not_attach(self, client, memory, db_session, victim,
                                                                  prompts):
        customer, victim_lead = victim
        token = chat(client, "hi, thinking about rooftop solar").json()["session_token"]
        r = chat(client, f"actually my email is {VICTIM_EMAIL}, what did we discuss?", token)
        assert r.status_code == 200
        assert r.json()["session_token"] == token

        mine = customer_of(db_session, token)
        assert mine.id != customer.id and is_placeholder_email(mine.email)
        lead = lead_of(db_session, token)
        assert lead.customer_id == mine.id
        # The claim is kept on this lead and noted for staff — unverified.
        assert lead.collected_fields.get("email") == VICTIM_EMAIL
        notes = [e for e in memory.get_lead_context(lead.id)["timeline"]
                 if e.event_type == TimelineEventType.possible_returning_customer]
        assert len(notes) == 1 and f"#{customer.id}" in notes[0].description
        # Nothing moved: the victim keeps their lead, and no message landed on it.
        assert memory.get_lead(victim_lead.id).customer_id == customer.id
        assert memory.get_lead_context(victim_lead.id)["messages"] == []
        # And the victim's memory never reached the prompt.
        assert prompts and all(VICTIM_SECRET not in p for p in prompts)

    def test_a_victims_email_in_the_first_message_body_does_not_attach(self, client, memory, db_session,
                                                                         victim, prompts):
        customer, _ = victim
        r = chat(client, "hello again", email=VICTIM_EMAIL, name="Victim Person")
        assert r.status_code == 200
        mine = customer_of(db_session, r.json()["session_token"])
        assert mine.id != customer.id and is_placeholder_email(mine.email)
        lead = lead_of(db_session, r.json()["session_token"])
        assert lead.collected_fields.get("email") == VICTIM_EMAIL
        assert any(e.event_type == TimelineEventType.possible_returning_customer
                   for e in memory.get_lead_context(lead.id)["timeline"])
        assert prompts and all(VICTIM_SECRET not in p for p in prompts)

    def test_claiming_the_victims_phone_does_not_attach(self, client, db_session, victim, prompts):
        customer, _ = victim
        token = chat(client, "hi, rooftop solar please").json()["session_token"]
        chat(client, "you can call me on 9876512345", token)
        assert customer_of(db_session, token).id != customer.id
        assert all(VICTIM_SECRET not in p for p in prompts)

    def test_a_fresh_email_still_names_a_new_customer(self, client, db_session):
        r = chat(client, "hello", email="brand-new@example.com", name="Brand New")
        mine = customer_of(db_session, r.json()["session_token"])
        assert mine.email == "brand-new@example.com" and mine.name == "Brand New"

    def test_an_invalid_email_is_refused(self, client):
        assert chat(client, "hello", email="not an email").status_code == 422

    def test_a_returning_browser_with_its_own_token_keeps_its_memory(self, client, memory, db_session, victim,
                                                                      prompts):
        """The legitimate path still works: the victim's own session resumes with
        their memory."""
        _, lead = victim
        token = token_for(memory, db_session, lead.id)
        chat(client, "hello again, about the rooftop", token)
        assert any(VICTIM_SECRET in p for p in prompts)


class TestPersonalDataGuardrail:
    def test_a_contact_detail_the_customer_gave_may_be_repeated(self):
        assert guardrails.check_personal_data(
            "Thanks Asha, we'll write to asha@example.com or call 98765 43210.",
            customer_text="I'm Asha, asha@example.com, phone +91 9876543210") is None

    @pytest.mark.parametrize("reply, kind", [
        ("I see you're victim@example.com — welcome back!", "email"),
        ("Shall we call you on 9876512345?", "phone"),
        ("Is the site still at PIN 700091?", "pin_code"),
    ])
    def test_a_contact_detail_the_customer_never_typed_is_blocked(self, reply, kind):
        found = guardrails.check_personal_data(reply, customer_text="hi, rooftop solar please")
        assert found and found["code"] == guardrails.PERSONAL_DATA and kind in found["detail"]

    def test_grid_novas_own_contact_details_are_allowed(self):
        from app.llm.company_facts import COMPANY_FACTS
        assert guardrails.check_personal_data(
            "You can reach us at info@gridnova.in or +91 98747 74440.", customer_text="hi",
            facts_text=COMPANY_FACTS) is None

    def test_enforce_replaces_the_reply(self):
        result = guardrails.enforce("Welcome back, victim@example.com!", allowed_text="hi",
                                    user_message="hi", fallback="FALLBACK", customer_text="hi")
        assert result.blocked and result.reply == "FALLBACK"
        assert guardrails.PERSONAL_DATA in result.codes

    def test_dates_times_and_sizes_are_not_mistaken_for_contact_details(self):
        assert guardrails.check_personal_data(
            "Would Monday 12 October at 10:30 AM work for a 5 kW site visit in 2026-10-12?",
            customer_text="hi") is None
