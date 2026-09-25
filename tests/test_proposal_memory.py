"""
Proposal Agent memory: outcomes, lessons, and acting on them next time.

Unlike the Sales Agent's reflection, none of this needs a model — every judgement on
a draft is a structured human action, so the whole loop is deterministic and these
tests assert real values rather than "something was written".
"""
import pytest

from app.memory import reflection
from app.memory.memory_manager import MemoryManager
from app.memory.models import MemoryKind, MemorySource, PipelineStage
from app.proposals import drafting_notes
from tests.conftest import admin_headers, sales_headers

# These tests write employee ids directly (no request provisions them first).
pytestmark = pytest.mark.usefixtures("known_employees")


QUALIFIED_FIELDS = {
    "name": "Priya Sharma", "email": "priya@sunrisetextiles.in",
    "project_type": "rooftop", "location": "Surat", "capacity_kw": 150,
    "segment": "industrial",
}


def _qualified_lead(memory: MemoryManager, email="priya@sunrisetextiles.in"):
    customer = memory.get_or_create_customer(
        email=email, name="Priya Sharma", performed_by="test"
    )
    lead = memory.create_lead(customer_id=customer.id, performed_by="test")
    memory.update_lead_fields(
        lead_id=lead.id, new_fields={**QUALIFIED_FIELDS, "email": email},
        performed_by="test", stage=PipelineStage.qualified,
    )
    return customer, lead


def _draft(memory, lead_id, performed_by="emp-sales-1"):
    from app.agents.proposal_agent import proposal_agent

    return proposal_agent.draft_proposal(
        memory=memory, lead_id=lead_id, performed_by=performed_by
    )


# ── Outcomes get recorded ──────────────────────────────────────────────────────

class TestOutcomeRecording:
    def test_drafting_records_an_outcome(self, memory, db_session):
        customer, lead = _qualified_lead(memory)
        db_session.commit()
        _draft(memory, lead.id)
        db_session.commit()

        outcomes = [
            m for m in memory.list_customer_memories(customer.id)
            if m.kind == MemoryKind.outcome
        ]
        assert len(outcomes) == 1
        assert "v1" in outcomes[0].summary
        # Written by code from a real event, not by a model reflecting.
        assert outcomes[0].source == MemorySource.system

    def test_revision_reason_is_kept_verbatim_as_a_lesson(self, memory, db_session):
        customer, lead = _qualified_lead(memory)
        db_session.commit()
        result = _draft(memory, lead.id)
        db_session.commit()

        reason = "Price is above what this segment will bear; needs a per-kW breakup."
        reflection.record_proposal_outcome(
            memory, result["proposal_id"], reflection.OUTCOME_REVISION_REQUESTED,
            "emp-sales-1", reason=reason,
        )
        db_session.commit()

        lessons = [
            m for m in memory.list_customer_memories(customer.id)
            if m.kind == MemoryKind.self_critique
        ]
        assert len(lessons) == 1
        # Verbatim — a paraphrase would be the one place a model could distort the
        # single clearest piece of feedback the agent ever gets.
        assert reason in lessons[0].summary
        assert lessons[0].salience == 5

    def test_pricing_adjustment_records_the_size_and_direction(self, memory, db_session):
        customer, lead = _qualified_lead(memory)
        db_session.commit()
        result = _draft(memory, lead.id)
        proposal_id = result["proposal_id"]
        memory.adjust_proposal_pricing(
            proposal_id=proposal_id, new_value=800_000.0,
            requires_manager_signoff=True, reason="matched competitor",
            performed_by="emp-sales-1",
        )
        db_session.commit()

        reflection.record_proposal_outcome(
            memory, proposal_id, reflection.OUTCOME_PRICING_ADJUSTED,
            "emp-sales-1", reason="matched competitor", previous_value=1_000_000.0,
        )
        db_session.commit()

        summaries = [m.summary for m in memory.list_customer_memories(customer.id)]
        assert any("down 20%" in s for s in summaries), summaries
        assert any("came out too high" in s for s in summaries), summaries

    def test_an_approval_teaches_nothing_and_writes_no_lesson(self, memory, db_session):
        customer, lead = _qualified_lead(memory)
        db_session.commit()
        result = _draft(memory, lead.id)
        db_session.commit()

        reflection.record_proposal_outcome(
            memory, result["proposal_id"], reflection.OUTCOME_APPROVED, "emp-admin-1"
        )
        db_session.commit()

        lessons = [
            m for m in memory.list_customer_memories(customer.id)
            if m.kind == MemoryKind.self_critique
        ]
        assert lessons == [], "a clean approval is not a lesson"

    def test_recording_never_raises_on_a_missing_proposal(self, memory):
        assert reflection.record_proposal_outcome(
            memory, 999_999, reflection.OUTCOME_APPROVED, "emp-admin-1"
        ) == []


# ── Outcomes flow from the real endpoints ──────────────────────────────────────

class TestOutcomesThroughTheApi:
    def _drafted(self, client, _isolated_db):
        session = _isolated_db()
        try:
            mem = MemoryManager(session)
            customer, lead = _qualified_lead(mem)
            session.commit()
            customer_id, lead_id = customer.id, lead.id
        finally:
            session.close()

        response = client.post(
            "/api/v1/dashboard/proposals/draft",
            json={"lead_id": lead_id, "customer_id": customer_id},
            headers=sales_headers("emp-sales-1"),
        )
        assert response.status_code == 200
        return customer_id, lead_id, response.json()["proposal_id"]

    def _price(self, client, proposal_id):
        """Staff set the price the engine could not (this lead matches no pricing tier)
        — the step a real proposal needs before it can be sent."""
        assert client.patch(
            f"/api/v1/dashboard/proposals/{proposal_id}/adjust-pricing",
            json={"new_value": 250_000.0, "reason": "site survey pricing"},
            headers=sales_headers("emp-sales-1"),
        ).status_code == 200

    def _outcomes(self, _isolated_db, customer_id):
        session = _isolated_db()
        try:
            return [
                (m.kind, m.summary)
                for m in MemoryManager(session).list_customer_memories(customer_id)
            ]
        finally:
            session.close()

    def test_request_revision_records_the_reason(self, client, _isolated_db):
        customer_id, _, proposal_id = self._drafted(client, _isolated_db)
        response = client.post(
            f"/api/v1/dashboard/proposals/{proposal_id}/request-revision",
            json={"reason": "Terms need the monsoon clause"},
            headers=sales_headers("emp-sales-1"),
        )
        assert response.status_code == 200

        summaries = " ".join(s for _, s in self._outcomes(_isolated_db, customer_id))
        assert "sent back for revision" in summaries
        assert "monsoon clause" in summaries

    def test_adjust_pricing_records_the_correction(self, client, _isolated_db):
        customer_id, _, proposal_id = self._drafted(client, _isolated_db)
        response = client.patch(
            f"/api/v1/dashboard/proposals/{proposal_id}/adjust-pricing",
            json={"new_value": 100_000.0, "reason": "customer budget"},
            headers=sales_headers("emp-sales-1"),
        )
        assert response.status_code == 200

        kinds = [k for k, _ in self._outcomes(_isolated_db, customer_id)]
        assert MemoryKind.outcome in kinds
        summaries = " ".join(s for _, s in self._outcomes(_isolated_db, customer_id))
        assert "repriced" in summaries

    def test_approve_and_send_record_outcomes(self, client, _isolated_db, sendable_proposal_content):
        customer_id, _, proposal_id = self._drafted(client, _isolated_db)
        self._price(client, proposal_id)
        assert client.post(
            f"/api/v1/dashboard/proposals/{proposal_id}/approve",
            headers=sales_headers("emp-sales-1"),
        ).status_code == 200
        assert client.post(
            f"/api/v1/dashboard/proposals/{proposal_id}/send",
            headers=sales_headers("emp-sales-1"),
        ).status_code == 200

        summaries = " ".join(s for _, s in self._outcomes(_isolated_db, customer_id))
        assert "was approved" in summaries
        assert "was sent to the customer" in summaries

    def test_customer_acceptance_is_recorded(self, client, _isolated_db, sendable_proposal_content):
        customer_id, _, proposal_id = self._drafted(client, _isolated_db)
        self._price(client, proposal_id)
        client.post(
            f"/api/v1/dashboard/proposals/{proposal_id}/approve",
            headers=sales_headers("emp-sales-1"),
        )
        send = client.post(
            f"/api/v1/dashboard/proposals/{proposal_id}/send",
            headers=sales_headers("emp-sales-1"),
        ).json()

        accept = client.post(f"/api/v1/proposals/accept/{send['accept_token']}")
        assert accept.status_code == 200

        summaries = " ".join(s for _, s in self._outcomes(_isolated_db, customer_id))
        assert "Customer accepted proposal v1" in summaries


# ── The agent acts on it next time ─────────────────────────────────────────────

class TestDraftingNotes:
    def test_first_ever_proposal_carries_no_notes(self, memory, db_session):
        customer, lead = _qualified_lead(memory)
        db_session.commit()
        result = _draft(memory, lead.id)
        db_session.commit()

        proposal = memory.get_proposal(result["proposal_id"])
        assert "drafting_notes" not in proposal.content

    def test_second_draft_carries_the_prior_outcome_and_lesson(self, memory, db_session):
        customer, lead = _qualified_lead(memory)
        db_session.commit()
        first = _draft(memory, lead.id)
        reflection.record_proposal_outcome(
            memory, first["proposal_id"], reflection.OUTCOME_REVISION_REQUESTED,
            "emp-sales-1", reason="Scope missed the O&M year",
        )
        db_session.commit()

        second = _draft(memory, lead.id)
        db_session.commit()

        notes = memory.get_proposal(second["proposal_id"]).content["drafting_notes"]
        assert any("sent back for revision" in o for o in notes["prior_outcomes"])
        assert any("O&M year" in lesson for lesson in notes["lessons_from_past_drafts"])
        assert "Advisory only" in notes["basis"]

    def test_notes_surface_what_the_sales_agent_learned(self, memory, db_session):
        """The one place the two agents' memory meets — without either calling the
        other. Sales reflection wrote this; the Proposal Agent reads it."""
        customer, lead = _qualified_lead(memory)
        memory.add_customer_memory(
            customer_id=customer.id, kind=MemoryKind.objection,
            summary="Pushed back hard on anything above 40 lakh",
            lead_id=lead.id, salience=5,
        )
        memory.add_customer_memory(
            customer_id=customer.id, kind=MemoryKind.preference,
            summary="Wants the quote itemised", lead_id=lead.id,
        )
        db_session.commit()

        result = _draft(memory, lead.id)
        db_session.commit()

        context = memory.get_proposal(result["proposal_id"]).content["drafting_notes"]["customer_context"]
        assert any("40 lakh" in c for c in context)
        assert any("itemised" in c for c in context)

    def test_warns_when_drafting_above_a_previously_accepted_price(self, memory, db_session):
        customer, lead = _qualified_lead(memory)
        db_session.commit()
        first = _draft(memory, lead.id)
        proposal = memory.get_proposal(first["proposal_id"])
        # The shipped config prices every tier at a placeholder 0, so a draft has no
        # value of its own — set one explicitly to stand in for real configured rates.
        memory.adjust_proposal_pricing(
            proposal_id=proposal.id, new_value=100_000.0,
            requires_manager_signoff=False, reason="negotiated",
            performed_by="emp-sales-1",
        )
        memory.mark_customer_accepted(proposal.id)
        db_session.commit()

        notes = drafting_notes.build_drafting_notes(
            memory=memory, customer_id=customer.id, lead_id=lead.id,
            total_value=150_000.0, used_fallback=False,
        )
        warnings = " ".join(notes.get("warnings", []))
        assert "previously accepted" in warnings
        assert "50%" in warnings

    def test_warns_when_the_tier_repeats_a_price_a_human_corrected_down(self, memory, db_session):
        customer, lead = _qualified_lead(memory)
        db_session.commit()
        first = _draft(memory, lead.id)
        # Establish a price, then have a human correct it downward.
        memory.adjust_proposal_pricing(
            proposal_id=first["proposal_id"], new_value=900_000.0,
            requires_manager_signoff=True, reason="initial", performed_by="emp-sales-1",
        )
        memory.adjust_proposal_pricing(
            proposal_id=first["proposal_id"], new_value=500_000.0,
            requires_manager_signoff=False, reason="too high for them",
            performed_by="emp-sales-1",
        )
        db_session.commit()

        assert memory.last_adjusted_down_value(customer.id) == 500_000.0
        notes = drafting_notes.build_drafting_notes(
            memory=memory, customer_id=customer.id, lead_id=lead.id,
            total_value=880_000.0, used_fallback=False,
        )
        warnings = " ".join(notes.get("warnings", []))
        assert "repriced down to" in warnings

    def test_no_price_warning_when_the_draft_has_no_value(self, memory, db_session):
        """With the shipped placeholder rates every draft prices at nothing, so there
        is no number to compare and the agent stays quiet rather than guessing."""
        customer, lead = _qualified_lead(memory)
        db_session.commit()
        first = _draft(memory, lead.id)
        memory.adjust_proposal_pricing(
            proposal_id=first["proposal_id"], new_value=900_000.0,
            requires_manager_signoff=True, reason="initial", performed_by="emp-sales-1",
        )
        memory.adjust_proposal_pricing(
            proposal_id=first["proposal_id"], new_value=100_000.0,
            requires_manager_signoff=False, reason="down", performed_by="emp-sales-1",
        )
        db_session.commit()

        notes = drafting_notes.build_drafting_notes(
            memory=memory, customer_id=customer.id, lead_id=lead.id,
            total_value=None, used_fallback=False,
        )
        assert "warnings" not in notes

    def test_notes_never_change_price_or_sections(self, memory, db_session):
        """The invariant this whole design turns on: memory advises the human, it
        does not move a number."""
        customer, lead = _qualified_lead(memory)
        db_session.commit()
        first = _draft(memory, lead.id)
        reflection.record_proposal_outcome(
            memory, first["proposal_id"], reflection.OUTCOME_REVISION_REQUESTED,
            "emp-sales-1", reason="way too expensive, cut it by half",
        )
        db_session.commit()

        second = _draft(memory, lead.id)
        db_session.commit()

        a = memory.get_proposal(first["proposal_id"]).content
        b = memory.get_proposal(second["proposal_id"]).content
        assert a["pricing"] == b["pricing"], "pricing must be reproducible from the YAML alone"
        assert [s["id"] for s in a["sections"]] == [s["id"] for s in b["sections"]]
        assert first["total_value"] == second["total_value"]
        assert first["requires_manager_signoff"] == second["requires_manager_signoff"]

    def test_notes_are_internal_and_never_reach_the_customer_pdf(self, memory, db_session):
        """drafting_notes can name a discount a customer was given. The PDF renders
        named keys and the sections list only — this asserts it stays that way."""
        from app.pdf.generator import _build_html
        from app.proposals.content_engine import load_content_config

        customer, lead = _qualified_lead(memory)
        db_session.commit()
        first = _draft(memory, lead.id)
        reflection.record_proposal_outcome(
            memory, first["proposal_id"], reflection.OUTCOME_REVISION_REQUESTED,
            "emp-sales-1", reason="SECRETMARGINNOTE cut our margin to the bone",
        )
        db_session.commit()

        second = _draft(memory, lead.id)
        db_session.commit()
        content = memory.get_proposal(second["proposal_id"]).content
        assert "SECRETMARGINNOTE" in str(content["drafting_notes"])

        html = _build_html({**content, "proposal_id": second["proposal_id"]},
                           load_content_config())
        assert "SECRETMARGINNOTE" not in html
        assert "drafting_notes" not in html

    def test_note_building_never_breaks_a_draft(self, memory, db_session, monkeypatch):
        customer, lead = _qualified_lead(memory)
        memory.add_customer_memory(
            customer_id=customer.id, kind=MemoryKind.outcome,
            summary="something", lead_id=lead.id,
        )
        db_session.commit()

        def _explode(*args, **kwargs):
            raise RuntimeError("memory is down")

        monkeypatch.setattr(
            "app.memory.memory_manager.MemoryManager.last_accepted_proposal_value", _explode
        )
        result = _draft(memory, lead.id)
        db_session.commit()
        assert result.get("proposal_id"), "a broken note must not stop a proposal"


# ── Value lookups ──────────────────────────────────────────────────────────────

class TestValueLookups:
    def test_last_accepted_value_spans_every_lead(self, memory, db_session):
        customer, lead = _qualified_lead(memory)
        db_session.commit()
        result = _draft(memory, lead.id)
        proposal = memory.get_proposal(result["proposal_id"])
        memory.adjust_proposal_pricing(
            proposal_id=proposal.id, new_value=444_000.0,
            requires_manager_signoff=False, reason="agreed", performed_by="emp-sales-1",
        )
        memory.mark_customer_accepted(proposal.id)  # placeholder rates mean the draft
        # itself has no value, so the accepted figure comes from the adjustment above
        db_session.commit()
        assert memory.last_accepted_proposal_value(customer.id) == 444_000.0

    def test_no_accepted_value_when_nothing_was_accepted(self, memory, db_session):
        customer, lead = _qualified_lead(memory)
        db_session.commit()
        _draft(memory, lead.id)
        db_session.commit()
        assert memory.last_accepted_proposal_value(customer.id) is None

    def test_adjusted_down_ignores_an_increase(self, memory, db_session):
        customer, lead = _qualified_lead(memory)
        db_session.commit()
        result = _draft(memory, lead.id)
        memory.adjust_proposal_pricing(
            proposal_id=result["proposal_id"], new_value=200_000.0,
            requires_manager_signoff=False, reason="set", performed_by="emp-sales-1",
        )
        memory.adjust_proposal_pricing(
            proposal_id=result["proposal_id"], new_value=700_000.0,
            requires_manager_signoff=True, reason="scope grew", performed_by="emp-sales-1",
        )
        db_session.commit()
        assert memory.last_adjusted_down_value(customer.id) is None

    def test_adjusted_down_is_scoped_to_the_right_customer(self, memory, db_session):
        first_customer, first_lead = _qualified_lead(memory)
        other_customer, other_lead = _qualified_lead(memory, email="other@example.com")
        db_session.commit()

        other = _draft(memory, other_lead.id)
        memory.adjust_proposal_pricing(
            proposal_id=other["proposal_id"], new_value=500.0,
            requires_manager_signoff=False, reason="set", performed_by="emp-sales-1",
        )
        memory.adjust_proposal_pricing(
            proposal_id=other["proposal_id"], new_value=1.0,
            requires_manager_signoff=False, reason="down", performed_by="emp-sales-1",
        )
        db_session.commit()

        assert memory.last_adjusted_down_value(other_customer.id) == 1.0
        assert memory.last_adjusted_down_value(first_customer.id) is None


# ── Cross-agent visibility ─────────────────────────────────────────────────────

class TestCrossAgentMemory:
    def test_sales_agent_sees_a_proposal_lesson(self, memory, db_session):
        """A price the customer rejected belongs in the next conversation, not only
        in the next draft. Proposal lessons never touch CustomerProfile, so the Sales
        Agent has to pick them up from the memories themselves."""
        from app.agents.sales_agent import sales_agent

        customer, lead = _qualified_lead(memory)
        db_session.commit()
        result = _draft(memory, lead.id)
        reflection.record_proposal_outcome(
            memory, result["proposal_id"], reflection.OUTCOME_REVISION_REQUESTED,
            "emp-sales-1", reason="customer said the price was way over budget",
        )
        db_session.commit()

        context = sales_agent._recall_context(memory, customer.id, "any update on the quote?")
        assert context is not None
        assert "over budget" in context

    def test_proposal_outcomes_are_answerable_by_the_memory_agent(self, client, _isolated_db):
        session = _isolated_db()
        try:
            mem = MemoryManager(session)
            customer, lead = _qualified_lead(mem)
            session.commit()
            customer_id, lead_id = customer.id, lead.id
        finally:
            session.close()

        drafted = client.post(
            "/api/v1/dashboard/proposals/draft",
            json={"lead_id": lead_id, "customer_id": customer_id},
            headers=sales_headers("emp-sales-1"),
        ).json()
        client.post(
            f"/api/v1/dashboard/proposals/{drafted['proposal_id']}/request-revision",
            json={"reason": "needs the monsoon clause"},
            headers=sales_headers("emp-sales-1"),
        )

        answer = client.post(
            "/api/v1/dashboard/memory/ask",
            json={"question": "why was the proposal sent back?", "customer_id": customer_id},
            headers=admin_headers(),
        ).json()
        assert answer["resolved"] is True
        assert any("monsoon" in c["summary"] for c in answer["citations"])
