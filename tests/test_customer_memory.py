"""
Customer memory: episodic recall, self-reflection, and answering questions later.

These run in mock mode like the rest of the suite, so they exercise the real
pipeline — reflection writes memories, retrieval ranks them, the endpoint answers
from them — without needing an API key.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.memory import recall, reflection
from app.memory.memory_manager import MemoryManager
from app.memory.models import Channel, MemoryKind, MessageRole, PipelineStage
from tests.conftest import admin_headers, engineer_headers, sales_headers

# These tests write employee ids directly (no request provisions them first).
pytestmark = pytest.mark.usefixtures("known_employees")


# ── Helpers ────────────────────────────────────────────────────────────────────

def _seed_conversation(memory: MemoryManager, *, email="priya@sunrisetextiles.in",
                       name="Priya Sharma", company="Sunrise Textiles"):
    """A customer with a lead and a short conversation on it."""
    customer = memory.get_or_create_customer(email=email, name=name, performed_by="test")
    if company:
        memory.update_customer(customer.id, performed_by="test", company=company)
    lead = memory.create_lead(customer_id=customer.id, performed_by="test")
    memory.update_lead_fields(
        lead_id=lead.id,
        new_fields={
            "name": name, "email": email, "project_type": "rooftop",
            "location": "Surat", "capacity_kw": 150, "segment": "industrial",
            "budget": "45 lakh", "timeline": "before March",
        },
        performed_by="test",
    )
    for role, text in [
        (MessageRole.customer, "We run a textile unit in Surat and the bills are brutal."),
        (MessageRole.agent, "That's a good amount of daytime load to work with."),
        (MessageRole.customer, "The roof has a water tank on one side, does that matter?"),
        (MessageRole.agent, "It can — we'd plan around the shading it throws."),
    ]:
        memory.log_message(
            channel=Channel.website, role=role, content=text,
            lead_id=lead.id, customer_id=customer.id,
        )
    return customer, lead


# ── Reflection writes memory ───────────────────────────────────────────────────

class TestReflection:
    def test_reflection_writes_memories_and_profile(self, memory, db_session):
        customer, lead = _seed_conversation(memory)
        db_session.commit()

        result = reflection.reflect_on_lead(memory, lead.id, trigger=reflection.TRIGGER_QUALIFIED)
        db_session.commit()

        assert result["memories_written"] > 0
        memories = memory.list_customer_memories(customer.id)
        assert memories, "reflection should have written at least one memory"

        profile = memory.get_customer_profile(customer.id)
        assert profile is not None
        assert profile.reflection_count == 1
        assert profile.last_reflected_at is not None
        # The agent's own notes are kept apart from what the customer said.
        assert profile.agent_notes is not None

    def test_memories_carry_facts_that_outlive_the_chat_window(self, memory, db_session):
        customer, lead = _seed_conversation(memory)
        db_session.commit()
        reflection.reflect_on_lead(memory, lead.id)
        db_session.commit()

        summaries = " ".join(m.summary.lower() for m in memory.list_customer_memories(customer.id))
        # The budget was collected on the lead; it must be individually retrievable.
        assert "45 lakh" in summaries
        # And the thing the customer actually said is kept as an episode.
        assert any(m.kind == MemoryKind.episode for m in memory.list_customer_memories(customer.id))

    def test_reflection_is_incremental_and_does_not_duplicate(self, memory, db_session):
        customer, lead = _seed_conversation(memory)
        db_session.commit()

        reflection.reflect_on_lead(memory, lead.id)
        db_session.commit()
        first_count = memory.count_customer_memories(customer.id)

        # Re-running with no new messages must add nothing.
        second = reflection.reflect_on_lead(memory, lead.id)
        db_session.commit()
        assert second["memories_written"] == 0
        assert second["skipped"] == "no new messages since last reflection"
        assert memory.count_customer_memories(customer.id) == first_count

    def test_new_messages_reflect_again(self, memory, db_session):
        customer, lead = _seed_conversation(memory)
        db_session.commit()
        reflection.reflect_on_lead(memory, lead.id)
        db_session.commit()

        memory.log_message(
            channel=Channel.website, role=MessageRole.customer,
            content="Also our MD signs off on anything above 20 lakh.",
            lead_id=lead.id, customer_id=customer.id,
        )
        db_session.commit()

        result = reflection.reflect_on_lead(memory, lead.id)
        db_session.commit()
        assert result["memories_written"] > 0
        assert "signs off" in " ".join(
            m.summary.lower() for m in memory.list_customer_memories(customer.id)
        )

    def test_reflection_never_raises_on_a_missing_lead(self, memory):
        result = reflection.reflect_on_lead(memory, 999_999)
        assert result["skipped"] == "lead not found"
        assert result["memories_written"] == 0

    def test_filler_self_critique_is_not_stored(self):
        assert reflection._is_filler("nothing to note")
        assert reflection._is_filler("Nothing to note.")
        assert reflection._is_filler("none")
        assert not reflection._is_filler("I asked for their email before understanding the project.")


# ── Milestone triggers ─────────────────────────────────────────────────────────

class TestMilestones:
    def test_qualification_triggers_reflection_through_the_chat_endpoint(self, client, _isolated_db):
        session = _isolated_db()
        try:
            mem = MemoryManager(session)
            customer = mem.get_or_create_customer(
                email="raj@example.com", name="Raj", performed_by="test"
            )
            lead = mem.create_lead(customer_id=customer.id, performed_by="test")
            # Everything but capacity — the next message completes qualification.
            mem.update_lead_fields(
                lead_id=lead.id,
                new_fields={"name": "Raj", "email": "raj@example.com",
                            "project_type": "rooftop", "location": "Pune"},
                performed_by="test",
            )
            session.commit()
            customer_id, lead_id = customer.id, lead.id
        finally:
            session.close()

        response = client.post("/api/v1/chat/message", json={
            "message": "We're looking at a 60kW rooftop system.",
            "customer_id": customer_id, "lead_id": lead_id,
        })
        assert response.status_code == 200
        assert response.json()["is_qualified"] is True

        session = _isolated_db()
        try:
            mem = MemoryManager(session)
            assert mem.count_customer_memories(customer_id) > 0, (
                "crossing into qualified should have triggered a reflection"
            )
            assert mem.get_customer_profile(customer_id) is not None
        finally:
            session.close()

    def test_no_reflection_before_a_milestone(self, client, _isolated_db):
        response = client.post("/api/v1/chat/message", json={
            "message": "hi, just browsing about solar",
        })
        assert response.status_code == 200
        customer_id = response.json()["customer_id"]

        session = _isolated_db()
        try:
            mem = MemoryManager(session)
            assert mem.count_customer_memories(customer_id) == 0
        finally:
            session.close()

    def test_interval_trigger_fires_on_a_long_conversation(self, memory, db_session, monkeypatch):
        monkeypatch.setattr(
            "app.memory.reflection.settings.memory_reflect_every_n_messages", 3
        )
        customer, lead = _seed_conversation(memory)
        db_session.commit()
        # _seed_conversation logs 2 customer messages; add a third.
        assert reflection.should_reflect_on_interval(memory, lead.id) is False
        memory.log_message(
            channel=Channel.website, role=MessageRole.customer,
            content="One more thing about the roof.",
            lead_id=lead.id, customer_id=customer.id,
        )
        db_session.commit()
        assert reflection.should_reflect_on_interval(memory, lead.id) is True

    def test_quiet_lead_triggers_reflection(self, memory, db_session):
        from app.jobs.stale_leads import check_stale_leads

        customer, lead = _seed_conversation(memory)
        memory.update_lead_fields(
            lead_id=lead.id, new_fields={}, performed_by="test",
            stage=PipelineStage.qualifying,
        )
        memory.record_customer_message(
            lead.id, when=datetime.now(timezone.utc) - timedelta(days=6)
        )
        db_session.commit()

        counts = check_stale_leads(db_session)
        assert counts["lead_stale_5d"] == 1
        assert memory.count_customer_memories(customer.id) > 0


# ── Retrieval ──────────────────────────────────────────────────────────────────

class TestRetrieval:
    def test_keyword_ranking_prefers_matching_memories(self, memory, db_session):
        customer, lead = _seed_conversation(memory)
        memory.add_customer_memory(
            customer_id=customer.id, kind=MemoryKind.objection,
            summary="Worried about shading from the water tank on the roof",
            tags=["shading", "roof", "tank"], salience=5, lead_id=lead.id,
        )
        memory.add_customer_memory(
            customer_id=customer.id, kind=MemoryKind.preference,
            summary="Prefers to be called after 6pm",
            tags=["call", "evening"], salience=2, lead_id=lead.id,
        )
        db_session.commit()

        ranked = recall.rank_memories(
            memory.list_customer_memories(customer.id), "any worries about shading?", top_k=1
        )
        assert "shading" in ranked[0].summary.lower()

    def test_salience_breaks_the_tie_when_nothing_matches(self, memory, db_session):
        customer, lead = _seed_conversation(memory)
        memory.add_customer_memory(
            customer_id=customer.id, kind=MemoryKind.episode,
            summary="Low importance note", tags=[], salience=1, lead_id=lead.id,
        )
        big = memory.add_customer_memory(
            customer_id=customer.id, kind=MemoryKind.episode,
            summary="High importance note", tags=[], salience=5, lead_id=lead.id,
        )
        db_session.commit()

        ranked = recall.rank_memories([
            m for m in memory.list_customer_memories(customer.id)
            if "importance" in m.summary
        ], "tell me about them", top_k=1)
        assert ranked[0].id == big.id

    def test_recency_weight_decays_but_never_to_zero(self):
        now = datetime.now(timezone.utc)
        fresh = recall._recency_weight(now, now)
        old = recall._recency_weight(now - timedelta(days=900), now)
        assert fresh > old >= recall._RECENCY_FLOOR

    def test_context_block_labels_agent_opinion(self, memory, db_session):
        customer, lead = _seed_conversation(memory)
        memory.add_customer_memory(
            customer_id=customer.id, kind=MemoryKind.self_critique,
            summary="Asked for the email too early in the conversation",
            lead_id=lead.id, salience=3,
        )
        db_session.commit()

        retrieved = recall.retrieve(memory, customer.id, "what did we get wrong?")
        assert "ASSISTANT'S OPINION" in retrieved["context_block"]

    def test_context_block_carries_structured_fields(self, memory, db_session):
        customer, lead = _seed_conversation(memory)
        db_session.commit()
        block = recall.retrieve(memory, customer.id, "what capacity?")["context_block"]
        assert "capacity_kw: 150" in block
        assert "Surat" in block

    def test_retrieval_stays_on_keywords_when_semantic_is_off(self, memory, db_session):
        customer, _ = _seed_conversation(memory)
        db_session.commit()
        assert recall.retrieve(memory, customer.id, "anything")["mode"] == "keyword"


# ── Customer resolution ────────────────────────────────────────────────────────

class TestCustomerResolution:
    def test_resolves_by_name(self, memory, db_session):
        customer, _ = _seed_conversation(memory)
        db_session.commit()
        found, candidates = recall.resolve_customer(memory, "What does Priya Sharma want?")
        assert found is not None and found.id == customer.id

    def test_resolves_by_email(self, memory, db_session):
        customer, _ = _seed_conversation(memory)
        db_session.commit()
        found, _ = recall.resolve_customer(memory, "anything on priya@sunrisetextiles.in?")
        assert found is not None and found.id == customer.id

    def test_resolves_by_company(self, memory, db_session):
        customer, _ = _seed_conversation(memory)
        db_session.commit()
        found, _ = recall.resolve_customer(memory, "remind me about Sunrise Textiles")
        assert found is not None and found.id == customer.id

    def test_ambiguous_name_returns_candidates_not_a_guess(self, memory, db_session):
        _seed_conversation(memory, email="priya.a@example.com", name="Priya Sharma", company=None)
        _seed_conversation(memory, email="priya.b@example.com", name="Priya Shah", company=None)
        db_session.commit()
        found, candidates = recall.resolve_customer(memory, "what about Priya?")
        assert found is None
        assert len(candidates) >= 2

    def test_explicit_id_always_wins(self, memory, db_session):
        customer, _ = _seed_conversation(memory)
        db_session.commit()
        found, _ = recall.resolve_customer(memory, "who is this", customer_id=customer.id)
        assert found.id == customer.id


# ── The ask endpoint ───────────────────────────────────────────────────────────

class TestAskEndpoint:
    def _seed_via_client(self, _isolated_db):
        session = _isolated_db()
        try:
            mem = MemoryManager(session)
            customer, lead = _seed_conversation(mem)
            session.commit()
            reflection.reflect_on_lead(mem, lead.id)
            session.commit()
            return customer.id, lead.id
        finally:
            session.close()

    def test_answers_a_question_about_a_customer(self, client, _isolated_db):
        customer_id, _ = self._seed_via_client(_isolated_db)
        response = client.post(
            "/api/v1/dashboard/memory/ask",
            json={"question": "What is the budget for Sunrise Textiles?"},
            headers=sales_headers(),
        )
        assert response.status_code == 200
        body = response.json()
        assert body["resolved"] is True
        assert body["customer_id"] == customer_id
        assert body["citations"], "an answer should cite the records it used"
        assert "45 lakh" in body["answer"]

    def test_answers_by_explicit_customer_id(self, client, _isolated_db):
        customer_id, _ = self._seed_via_client(_isolated_db)
        response = client.post(
            "/api/v1/dashboard/memory/ask",
            json={"question": "what do we know?", "customer_id": customer_id},
            headers=admin_headers(),
        )
        assert response.status_code == 200
        assert response.json()["resolved"] is True

    def test_unknown_customer_asks_rather_than_guesses(self, client, _isolated_db):
        self._seed_via_client(_isolated_db)
        response = client.post(
            "/api/v1/dashboard/memory/ask",
            json={"question": "what about someone we never met?"},
            headers=sales_headers(),
        )
        assert response.status_code == 200
        body = response.json()
        assert body["resolved"] is False
        assert "couldn't tell which customer" in body["answer"]

    def test_engineer_cannot_query_customer_memory(self, client, _isolated_db):
        self._seed_via_client(_isolated_db)
        response = client.post(
            "/api/v1/dashboard/memory/ask",
            json={"question": "what is the budget?"},
            headers=engineer_headers(),
        )
        assert response.status_code == 403

    def test_requires_authentication(self, client, _isolated_db):
        self._seed_via_client(_isolated_db)
        response = client.post(
            "/api/v1/dashboard/memory/ask", json={"question": "anything?"}
        )
        assert response.status_code in (401, 403)

    def test_missing_customer_id_is_404(self, client):
        response = client.post(
            "/api/v1/dashboard/memory/ask",
            json={"question": "hello", "customer_id": 999_999},
            headers=admin_headers(),
        )
        assert response.status_code == 404

    def test_sales_cannot_read_another_reps_customer(self, client, _isolated_db):
        session = _isolated_db()
        try:
            mem = MemoryManager(session)
            customer, lead = _seed_conversation(mem)
            mem.assign_lead_if_unassigned(lead.id, employee_id="emp-other", performed_by="test")
            session.commit()
            customer_id = customer.id
        finally:
            session.close()

        response = client.post(
            "/api/v1/dashboard/memory/ask",
            json={"question": "what is the budget?", "customer_id": customer_id},
            headers=sales_headers("emp-sales-1"),
        )
        assert response.status_code == 403


# ── The memory view + manual reflection endpoints ──────────────────────────────

class TestMemoryEndpoints:
    def test_memory_view_returns_profile_and_memories(self, client, _isolated_db):
        session = _isolated_db()
        try:
            mem = MemoryManager(session)
            customer, lead = _seed_conversation(mem)
            session.commit()
            reflection.reflect_on_lead(mem, lead.id)
            session.commit()
            customer_id = customer.id
        finally:
            session.close()

        response = client.get(
            f"/api/v1/dashboard/customers/{customer_id}/memory", headers=admin_headers()
        )
        assert response.status_code == 200
        body = response.json()
        assert body["profile"] is not None
        assert body["memory_count"] > 0
        assert body["memories"]
        assert body["customer"]["company"] == "Sunrise Textiles"

    def test_memory_view_filters_by_kind(self, client, _isolated_db):
        session = _isolated_db()
        try:
            mem = MemoryManager(session)
            customer, lead = _seed_conversation(mem)
            mem.add_customer_memory(
                customer_id=customer.id, kind=MemoryKind.objection,
                summary="Concerned about monsoon downtime", lead_id=lead.id,
            )
            session.commit()
            customer_id = customer.id
        finally:
            session.close()

        response = client.get(
            f"/api/v1/dashboard/customers/{customer_id}/memory?kind=objection",
            headers=admin_headers(),
        )
        assert response.status_code == 200
        kinds = {m["kind"] for m in response.json()["memories"]}
        assert kinds == {"objection"}

    def test_memory_view_rejects_an_unknown_kind(self, client, _isolated_db):
        session = _isolated_db()
        try:
            mem = MemoryManager(session)
            customer, _ = _seed_conversation(mem)
            session.commit()
            customer_id = customer.id
        finally:
            session.close()

        response = client.get(
            f"/api/v1/dashboard/customers/{customer_id}/memory?kind=nonsense",
            headers=admin_headers(),
        )
        assert response.status_code == 422

    def test_manual_reflection_endpoint(self, client, _isolated_db):
        session = _isolated_db()
        try:
            mem = MemoryManager(session)
            customer, lead = _seed_conversation(mem)
            session.commit()
            customer_id, lead_id = customer.id, lead.id
        finally:
            session.close()

        response = client.post(
            f"/api/v1/dashboard/leads/{lead_id}/reflect", headers=admin_headers()
        )
        assert response.status_code == 200
        assert response.json()["memories_written"] > 0

        session = _isolated_db()
        try:
            assert MemoryManager(session).count_customer_memories(customer_id) > 0
        finally:
            session.close()


# ── Memory manager surface ─────────────────────────────────────────────────────

class TestMemoryManagerSurface:
    def test_memories_are_append_only_history(self, memory, db_session):
        customer, lead = _seed_conversation(memory)
        first = memory.add_customer_memory(
            customer_id=customer.id, kind=MemoryKind.preference,
            summary="Wants it done before March", lead_id=lead.id,
        )
        second = memory.add_customer_memory(
            customer_id=customer.id, kind=MemoryKind.preference,
            summary="Now wants it done before June", lead_id=lead.id,
        )
        db_session.commit()
        assert first.id != second.id
        assert memory.count_customer_memories(customer.id) == 2

    def test_salience_is_clamped(self, memory, db_session):
        customer, lead = _seed_conversation(memory)
        high = memory.add_customer_memory(
            customer_id=customer.id, kind=MemoryKind.episode,
            summary="x", salience=99, lead_id=lead.id,
        )
        low = memory.add_customer_memory(
            customer_id=customer.id, kind=MemoryKind.episode,
            summary="y", salience=-4, lead_id=lead.id,
        )
        db_session.commit()
        assert high.salience == 5 and low.salience == 1

    def test_profile_update_is_partial_not_destructive(self, memory, db_session):
        customer, _ = _seed_conversation(memory)
        memory.upsert_customer_profile(
            customer_id=customer.id, summary="First pass", traits=["cost-driven"],
        )
        db_session.commit()
        memory.upsert_customer_profile(customer_id=customer.id, next_best_action="Call them")
        db_session.commit()

        profile = memory.get_customer_profile(customer.id)
        assert profile.summary == "First pass"
        assert profile.traits == ["cost-driven"]
        assert profile.next_best_action == "Call them"
        assert profile.reflection_count == 2

    def test_dossier_spans_every_lead_for_the_customer(self, memory, db_session):
        customer, first_lead = _seed_conversation(memory)
        second = memory.create_lead(customer_id=customer.id, performed_by="test")
        db_session.commit()
        dossier = memory.get_customer_dossier(customer.id)
        assert {lead.id for lead in dossier["leads"]} == {first_lead.id, second.id}

    def test_find_customers_matches_name_email_and_company(self, memory, db_session):
        customer, _ = _seed_conversation(memory)
        db_session.commit()
        assert memory.find_customers("priya")[0].id == customer.id
        assert memory.find_customers("sunrisetextiles.in")[0].id == customer.id
        assert memory.find_customers("Sunrise")[0].id == customer.id
        assert memory.find_customers("") == []


# ── Semantic path (flag on) ────────────────────────────────────────────────────

class TestSemanticSearch:
    """The embedding path can't reach a real API in tests, so these substitute a
    tiny deterministic embedder. What is under test is the wiring and the ranking,
    which is exactly the part that would otherwise only be exercised in production.
    """

    @staticmethod
    def _fake_embed(text: str):
        """Three-dimensional bag-of-concepts: shading, price, timing."""
        lowered = (text or "").lower()
        return [
            float(any(w in lowered for w in ("shade", "shading", "tank", "tree"))),
            float(any(w in lowered for w in ("price", "cost", "budget", "lakh"))),
            float(any(w in lowered for w in ("when", "march", "timeline", "soon"))),
        ]

    def test_memories_are_embedded_when_the_flag_is_on(self, memory, db_session, monkeypatch):
        monkeypatch.setattr("app.memory.reflection.settings.memory_semantic_search", True)
        monkeypatch.setattr("app.llm.client.llm_client.embed", self._fake_embed)

        customer, lead = _seed_conversation(memory)
        db_session.commit()
        reflection.reflect_on_lead(memory, lead.id)
        db_session.commit()

        memories = memory.list_customer_memories(customer.id)
        assert any(m.embedding for m in memories), "flag on should store vectors"
        assert all(m.embedding_model for m in memories if m.embedding)

    def test_no_embeddings_are_written_when_the_flag_is_off(self, memory, db_session):
        customer, lead = _seed_conversation(memory)
        db_session.commit()
        reflection.reflect_on_lead(memory, lead.id)
        db_session.commit()
        assert all(m.embedding is None for m in memory.list_customer_memories(customer.id))

    def test_semantic_ranking_beats_literal_wording(self, memory, db_session, monkeypatch):
        monkeypatch.setattr("app.config.settings.memory_semantic_search", True)
        monkeypatch.setattr("app.memory.recall.settings.memory_semantic_search", True)
        monkeypatch.setattr("app.llm.client.llm_client.embed", self._fake_embed)

        customer, lead = _seed_conversation(memory)
        shading = memory.add_customer_memory(
            customer_id=customer.id, kind=MemoryKind.objection,
            summary="The water tank throws a shadow over part of the roof",
            tags=["tank"], salience=3, lead_id=lead.id,
            embedding=self._fake_embed("tank shading roof"), embedding_model="fake",
        )
        memory.add_customer_memory(
            customer_id=customer.id, kind=MemoryKind.preference,
            summary="Wants the quote itemised", tags=["quote"], salience=3,
            lead_id=lead.id,
            embedding=self._fake_embed("price cost quote"), embedding_model="fake",
        )
        db_session.commit()

        # The question shares no words with the winning memory — only meaning.
        question = "is anything going to shade the array?"
        ranked = recall.rank_memories(
            [m for m in memory.list_customer_memories(customer.id) if m.embedding],
            question, top_k=1, question_vector=self._fake_embed(question),
        )
        assert ranked[0].id == shading.id

    def test_retrieval_reports_which_path_ran(self, memory, db_session, monkeypatch):
        monkeypatch.setattr("app.memory.recall.settings.memory_semantic_search", True)
        monkeypatch.setattr("app.llm.client.llm_client.embed", self._fake_embed)

        customer, lead = _seed_conversation(memory)
        memory.add_customer_memory(
            customer_id=customer.id, kind=MemoryKind.episode, summary="embedded one",
            lead_id=lead.id, embedding=self._fake_embed("cost"), embedding_model="fake",
        )
        db_session.commit()
        assert recall.retrieve(memory, customer.id, "what about cost?")["mode"] == "semantic"

    def test_unembedded_memories_still_rank_when_the_flag_is_on(self, memory, db_session, monkeypatch):
        """Turning the flag on must not strand memories written before it — they
        simply fall back to keyword scoring."""
        monkeypatch.setattr("app.memory.recall.settings.memory_semantic_search", True)
        monkeypatch.setattr("app.llm.client.llm_client.embed", self._fake_embed)

        customer, lead = _seed_conversation(memory)
        old = memory.add_customer_memory(
            customer_id=customer.id, kind=MemoryKind.objection,
            summary="Concerned about monsoon downtime", tags=["monsoon", "downtime"],
            salience=5, lead_id=lead.id,  # no embedding — written before the flag
        )
        db_session.commit()

        retrieved = recall.retrieve(memory, customer.id, "monsoon downtime worries")
        assert old.id in [m.id for m in retrieved["memories"]]
        # Nothing here was embedded, so the honest label is keyword.
        assert retrieved["mode"] == "keyword"


# ── Regressions found by running the thing end to end ──────────────────────────

class TestResolutionRegressions:
    def test_possessive_name_still_resolves(self, memory, db_session):
        """"What is Priya Sharma's bill?" used to resolve nothing: the possessive
        was carried into the lookup, which never matches a stored name."""
        customer, _ = _seed_conversation(memory)
        db_session.commit()
        found, _ = recall.resolve_customer(memory, "What is Priya Sharma's electricity bill?")
        assert found is not None and found.id == customer.id

    def test_question_openers_are_not_treated_as_names(self):
        fragments = recall._identity_fragments("What is the budget?")
        assert fragments == []

    def test_full_name_is_tried_before_its_parts(self):
        fragments = recall._identity_fragments("What is Priya Sharma's electricity bill?")
        assert fragments[0] == "Priya Sharma"
        assert "Priya" in fragments and "Sharma" in fragments

    def test_surname_only_question_resolves(self, memory, db_session):
        customer, _ = _seed_conversation(memory)
        db_session.commit()
        found, _ = recall.resolve_customer(memory, "How is Sharma doing?")
        assert found is not None and found.id == customer.id

    def test_mock_placeholder_critique_is_never_stored(self, memory, db_session):
        """Mock mode's placeholder self-critique is filler and must not land on
        every lead as a permanent memory."""
        customer, lead = _seed_conversation(memory)
        db_session.commit()
        reflection.reflect_on_lead(memory, lead.id)
        db_session.commit()

        critiques = [
            m for m in memory.list_customer_memories(customer.id)
            if m.kind == MemoryKind.self_critique
        ]
        assert critiques == [], "the [MOCK] placeholder critique should be filtered out"


# ── The loop closes: memory changes the next conversation ──────────────────────

class TestMemoryActsOnNextConversation:
    """Reflection writing a note down is only half of it. These assert the other
    half — that the note reaches the reply generator and changes what the agent is
    told before it speaks."""

    def _profiled_customer(self, memory, db_session):
        customer, lead = _seed_conversation(memory)
        memory.upsert_customer_profile(
            customer_id=customer.id,
            summary="Runs a textile unit in Surat, driven by the bill not sustainability.",
            traits=["cost-driven"],
            open_questions=["Who signs off above 20 lakh?"],
            agent_notes="I asked for the email before understanding the project.",
            next_best_action="Get the roof drawings, then offer a site visit.",
        )
        memory.add_customer_memory(
            customer_id=customer.id, kind=MemoryKind.objection,
            summary="Worried the water tank will shade part of the array",
            tags=["shading", "tank", "roof"], salience=5, lead_id=lead.id,
        )
        db_session.commit()
        return customer, lead

    def test_recall_context_carries_the_self_critique_as_a_directive(self, memory, db_session):
        from app.agents.sales_agent import sales_agent

        customer, _ = self._profiled_customer(memory, db_session)
        context = sales_agent._recall_context(memory, customer.id, "any update?")

        assert context is not None
        assert "What YOU should do differently" in context
        assert "asked for the email before understanding" in context
        assert "Get the roof drawings" in context
        assert "Who signs off above 20 lakh?" in context

    def test_recall_context_is_ranked_against_what_was_just_said(self, memory, db_session):
        from app.agents.sales_agent import sales_agent

        customer, lead = self._profiled_customer(memory, db_session)
        for i in range(8):
            memory.add_customer_memory(
                customer_id=customer.id, kind=MemoryKind.episode,
                summary=f"Unrelated filler note {i}", salience=3, lead_id=lead.id,
            )
        db_session.commit()

        context = sales_agent._recall_context(
            memory, customer.id, "will the tank shade the panels?"
        )
        assert "water tank will shade" in context, "the relevant memory must survive the cut"

    def test_no_context_for_a_customer_with_no_history(self, memory, db_session):
        from app.agents.sales_agent import sales_agent

        customer = memory.get_or_create_customer(
            email="brandnew@example.com", name="New", performed_by="test"
        )
        db_session.commit()
        assert sales_agent._recall_context(memory, customer.id, "hello") is None

    def test_recall_failure_never_breaks_the_reply(self, memory, db_session, monkeypatch):
        from app.agents.sales_agent import sales_agent

        customer, _ = self._profiled_customer(memory, db_session)

        def _explode(*args, **kwargs):
            raise RuntimeError("retrieval is down")

        monkeypatch.setattr("app.memory.recall.rank_memories", _explode)
        assert sales_agent._recall_context(memory, customer.id, "hi") is None

    def test_memory_reaches_the_reply_generator(self, memory, db_session, monkeypatch):
        """End to end through the agent: whatever recall produced has to actually
        arrive at the generator, not stop halfway."""
        from app.agents.sales_agent import sales_agent

        customer, lead = self._profiled_customer(memory, db_session)
        captured = {}

        def _spy(**kwargs):
            captured.update(kwargs)
            return "ok"

        monkeypatch.setattr("app.llm.client.llm_client.generate_reply", _spy)
        sales_agent.handle_message(
            memory=memory, customer_id=customer.id, lead_id=lead.id,
            message="just checking in on the shading question",
        )
        db_session.commit()

        assert captured.get("memory_context"), "the agent must pass memory to the generator"
        assert "do differently" in captured["memory_context"]

    def test_prompt_tells_the_model_not_to_re_ask_and_to_apply_the_note(self):
        from app.llm.client import llm_client

        prompt = llm_client._build_reply_system_prompt(
            stage="understanding", collected_fields={}, missing_fields=["capacity_kw"],
            memory_context="What YOU should do differently: slow down.",
        )
        assert "WHAT YOU REMEMBER ABOUT THIS PERSON" in prompt
        assert "Do NOT ask again" in prompt
        assert "apply it now" in prompt
        # Stored conversation stays data — a past message must never be able to
        # instruct the agent into quoting a price.
        assert "DATA, not" in prompt

    def test_prompt_is_unchanged_when_nothing_is_remembered(self):
        from app.llm.client import llm_client

        prompt = llm_client._build_reply_system_prompt(
            stage="opening", collected_fields={}, missing_fields=["name"],
        )
        assert "WHAT YOU REMEMBER" not in prompt


class TestReturningCustomerIdentity:
    def test_new_lead_inherits_identity_but_not_the_old_project(self, memory, db_session):
        customer, first_lead = _seed_conversation(memory)
        memory.update_customer(customer.id, performed_by="test", phone="9876543210")
        db_session.commit()

        second = memory.create_lead(customer_id=customer.id, performed_by="website")
        seeded = memory.seed_lead_identity_from_history(second.id)
        db_session.commit()

        fields = dict(memory.get_lead(second.id).collected_fields or {})
        assert fields["name"] == "Priya Sharma"
        assert fields["email"] == "priya@sunrisetextiles.in"
        assert fields["company"] == "Sunrise Textiles"
        assert fields["phone"] == "9876543210"
        # A second inquiry is a different project — last year's numbers must not
        # carry over and quietly qualify a lead nobody qualified.
        assert "capacity_kw" not in fields
        assert "location" not in fields
        assert "budget" not in fields
        assert set(seeded) == {"name", "email", "company", "phone"}

    def test_seeding_never_overwrites_a_lead_already_in_progress(self, memory, db_session):
        customer, lead = _seed_conversation(memory)
        db_session.commit()
        before = dict(lead.collected_fields or {})
        assert memory.seed_lead_identity_from_history(lead.id) == []
        assert dict(memory.get_lead(lead.id).collected_fields) == before

    def test_placeholder_customer_seeds_nothing(self, memory, db_session):
        customer = memory.create_placeholder_customer(performed_by="test")
        lead = memory.create_lead(customer_id=customer.id, performed_by="test")
        db_session.commit()
        assert memory.seed_lead_identity_from_history(lead.id) == []

    def test_returning_customer_is_not_asked_for_their_name_again(self, client, _isolated_db):
        """The stage machine has to see a known person, not a blank slate —
        otherwise it drives the agent to ask for a name its memory already holds."""
        from app.agents.sales_agent import determine_conversation_stage

        session = _isolated_db()
        try:
            mem = MemoryManager(session)
            customer, lead = _seed_conversation(mem)
            mem.update_lead_fields(
                lead_id=lead.id, new_fields={}, performed_by="test",
                stage=PipelineStage.won,  # closed, so a new inquiry opens a new lead
            )
            session.commit()
            customer_id = customer.id
        finally:
            session.close()

        response = client.post("/api/v1/chat/message", json={
            "message": "Hi, we're looking at another rooftop site",
            "customer_id": customer_id,
        })
        assert response.status_code == 200
        new_lead_id = response.json()["lead_id"]

        session = _isolated_db()
        try:
            mem = MemoryManager(session)
            fields = dict(mem.get_lead(new_lead_id).collected_fields or {})
            assert fields.get("name") == "Priya Sharma"
            assert fields.get("email") == "priya@sunrisetextiles.in"
            # Identity known, project not — so the agent explores the new project
            # instead of bridging for contact details it already has.
            assert determine_conversation_stage(fields) != "contact_bridge"
        finally:
            session.close()
