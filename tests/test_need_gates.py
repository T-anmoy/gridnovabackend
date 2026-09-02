"""
Need gates — the agent calls the model only when there is something for it to do.

Two things are under test, and the second matters more than the first. That a gate
skips the obvious cases is easy. That it never skips a case the agent still had to
act on is what makes the optimisation safe, so most of what follows is the
"must still call" direction.
"""
from app.agents import harness, need
from app.agents.sales_agent import sales_agent
from app.governance import registry
from app.memory.memory_manager import MemoryManager
from app.memory.models import Channel, MessageRole, PipelineStage


PENDING = {"slots": ["2026-09-01T04:30:00+00:00"], "duration_minutes": 30}


# ── Field extraction ───────────────────────────────────────────────────────────

class TestFieldExtractionGate:
    def test_skips_a_bare_acknowledgement(self):
        for message in ["ok", "okay thanks", "thanks", "yes please", "great, thank you",
                        "sure", "got it", "hi", "cool"]:
            assert not need.needs_field_extraction(message), message

    def test_still_extracts_from_anything_carrying_information(self):
        for message in [
            "ok, it's 50 kW",                       # acknowledgement plus a fact
            "yes, priya@sunrisetextiles.in",        # acknowledgement plus an email
            "rooftop",                              # one word, but a field value
            "Surat",
            "my name is Priya Sharma",
            "the bill is about 3 lakh",
            "we're a textile plant looking at solar for the main shed",
        ]:
            assert need.needs_field_extraction(message), message

    def test_a_digit_or_an_email_always_forces_a_look(self):
        assert need.needs_field_extraction("ok 50")
        assert need.needs_field_extraction("yes a@b.co")

    def test_a_longer_message_is_always_worth_a_look(self):
        assert need.needs_field_extraction("yes yes yes yes yes yes")

    def test_an_empty_message_needs_nothing(self):
        assert not need.needs_field_extraction("")
        assert not need.needs_field_extraction("   ")


# ── Meeting signal ─────────────────────────────────────────────────────────────

class TestMeetingSignalGate:
    def test_skips_early_small_talk(self):
        for message in ["hi, I'm looking at solar", "it's a textile plant in Surat",
                        "about 3 lakh a month", "rooftop, 50 kW"]:
            assert not need.needs_meeting_signal(message, "opening", None), message

    def test_always_asks_while_slots_are_pending(self):
        """The case a keyword list can never cover: a bare "yes" is a customer
        picking a slot, and it contains no scheduling word at all."""
        for message in ["yes", "the first one", "that works", "ok"]:
            assert need.needs_meeting_signal(message, "understanding", PENDING), message

    def test_always_asks_at_the_meeting_offer_stage(self):
        assert need.needs_meeting_signal("anything else?", "meeting_offer", None)

    def test_asks_when_the_customer_mentions_meeting(self):
        for message in ["can we get on a call?", "could someone visit the site?",
                        "when are you free?", "how about tomorrow morning",
                        "let's schedule something", "is Friday ok?"]:
            assert need.needs_meeting_signal(message, "understanding", None), message

    def test_an_empty_pending_slot_record_does_not_count(self):
        assert not need.needs_meeting_signal("hello", "opening", {"slots": []})


# ── The saving is real, and visible ────────────────────────────────────────────

class TestTheSavingIsMeasured:
    def _lead(self, memory):
        customer = memory.get_or_create_customer(
            email="priya@sunrise.in", name="Priya", performed_by="test"
        )
        lead = memory.create_lead(customer_id=customer.id, performed_by="test")
        return customer, lead

    def test_an_early_turn_makes_fewer_calls_than_it_used_to(self, memory, db_session):
        """Three calls a turn was the old fixed cost. An opening message needs two."""
        customer, lead = self._lead(memory)
        db_session.commit()

        run = harness.run(
            registry.SALES_AGENT.id, registry.REPLY_TO_CUSTOMER,
            lambda: sales_agent.handle_message(
                memory=memory, customer_id=customer.id, lead_id=lead.id,
                message="hi, I'm looking at solar for our factory",
            ),
            memory=memory, lead_id=lead.id,
        )
        assert run.ok
        assert run.llm_calls == 2, "extraction + reply; no meeting signal needed yet"
        assert run.llm_calls_skipped == 1

    def test_an_acknowledgement_costs_only_the_reply(self, memory, db_session):
        customer, lead = self._lead(memory)
        db_session.commit()

        run = harness.run(
            registry.SALES_AGENT.id, registry.REPLY_TO_CUSTOMER,
            lambda: sales_agent.handle_message(
                memory=memory, customer_id=customer.id, lead_id=lead.id,
                message="ok, thanks",
            ),
            memory=memory, lead_id=lead.id,
        )
        assert run.llm_calls == 1, "only the reply itself was worth generating"
        assert run.llm_calls_skipped == 2

    def test_a_meeting_request_still_pays_for_the_signal(self, memory, db_session):
        """The gate must never save money on the turn that actually matters."""
        customer, lead = self._lead(memory)
        db_session.commit()

        run = harness.run(
            registry.SALES_AGENT.id, registry.REPLY_TO_CUSTOMER,
            lambda: sales_agent.handle_message(
                memory=memory, customer_id=customer.id, lead_id=lead.id,
                message="can we set up a call this week?",
            ),
            memory=memory, lead_id=lead.id,
        )
        assert run.llm_calls == 3, "extraction + meeting signal + reply"
        assert run.llm_calls_skipped == 0

    def test_a_confirmation_against_pending_slots_still_pays(self, memory, db_session):
        customer, lead = self._lead(memory)
        memory.set_pending_meeting_slots(lead.id, PENDING)
        db_session.commit()

        run = harness.run(
            registry.SALES_AGENT.id, registry.REPLY_TO_CUSTOMER,
            lambda: sales_agent.handle_message(
                memory=memory, customer_id=customer.id, lead_id=lead.id,
                message="yes",
            ),
            memory=memory, lead_id=lead.id,
        )
        # "yes" is an acknowledgement so extraction is skipped, but the meeting
        # signal must still be asked — the customer may be picking a slot.
        assert run.llm_calls == 2
        assert run.llm_calls_skipped == 1

    def test_skips_are_reported_per_agent(self, memory, db_session):
        from datetime import datetime, timedelta, timezone

        customer, lead = self._lead(memory)
        db_session.commit()
        harness.run(
            registry.SALES_AGENT.id, registry.REPLY_TO_CUSTOMER,
            lambda: sales_agent.handle_message(
                memory=memory, customer_id=customer.id, lead_id=lead.id, message="ok",
            ),
            memory=memory, lead_id=lead.id,
        )
        db_session.commit()

        runs = memory.list_audit_logs(entity_type="agent_run")
        assert runs
        assert runs[-1].after_value["llm_calls_skipped"] >= 1


# ── Nothing the agent decides changes ──────────────────────────────────────────

class TestGatesDoNotChangeBehaviour:
    def test_qualification_still_completes_with_the_gates_on(self, client, _isolated_db):
        lead = cust = None
        for message in ["hi, thinking about solar", "ok", "thanks",
                        "my name is Priya, priya@sunrise.in",
                        "rooftop, 50 kW, in Surat"]:
            body = {"message": message}
            if lead:
                body.update({"lead_id": lead, "customer_id": cust})
            r = client.post("/api/v1/chat/message", json=body).json()
            lead, cust = r["lead_id"], r["customer_id"]

        session = _isolated_db()
        try:
            fields = dict(MemoryManager(session).get_lead(lead).collected_fields or {})
        finally:
            session.close()
        # The acknowledgement turns skipped extraction; the informative ones did not.
        assert fields.get("email") == "priya@sunrise.in"
        assert fields.get("project_type") == "rooftop"
        assert fields.get("capacity_kw") == 50.0

    def test_every_turn_still_gets_a_reply(self, memory, db_session):
        customer, lead = (
            memory.get_or_create_customer(email="a@b.co", name="A", performed_by="t"),
            None,
        )
        lead = memory.create_lead(customer_id=customer.id, performed_by="t")
        db_session.commit()
        for message in ["ok", "thanks", "hi"]:
            result = sales_agent.handle_message(
                memory=memory, customer_id=customer.id, lead_id=lead.id, message=message,
            )
            assert result["reply_text"], message
