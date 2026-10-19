"""BE-7 N3 — the staff memory answer is checked deterministically against the records it
was generated from (app/agents/grounding.py): every citation must exist, every stated
fact must be on record. A failing answer is retried once with the failures listed;
if it fails again the employee gets the records themselves and `ungrounded_answer`
is written to the audit log."""
import pytest

from app.agents import grounding
from app.agents.memory_agent import memory_agent
from app.llm import client as llm_module
from app.memory.models import MemoryKind, MemorySource

CONTEXT = """[C1] IDENTITY
  name: Priya Patel
  email: priya@example.com
  phone: +91 98765 43210
[L1] LEAD 7 — stage qualifying, source website_chat, opened 2026-10-14 10:00
  capacity_kw: 5.0
  budget: 450000
  monthly_electricity_bill: 3,000
  project_ref: GNE-WB-RES-2026-00125
[M] MEMORIES (most relevant first)
  [M12] (preference, salience 4, 2026-10-14 10:05): Wants installation before 15 Nov 2026
  [M13] (objection, salience 3, 2026-10-14 10:06) — ASSISTANT'S OPINION: worried about 30% shading"""


def check(answer, context=CONTEXT, evidence="Priya Patel <priya@example.com>"):
    return grounding.check_grounding(answer, context, evidence=evidence)


def codes(answer, **kw):
    return [v["code"] for v in check(answer, **kw).violations]


# ── Extraction ─────────────────────────────────────────────────────────────────

def test_citations_are_read_from_brackets():
    assert grounding.extract_citations("Budget [L1], bill [M12] and [M]; not [MOCK] or [m3]") == {"L1", "M12", "M"}


@pytest.mark.parametrize("text, kind, value", [
    ("budget ₹4,50,000", "money", 450_000), ("about 4.5 lakh", "money", 450_000), ("Rs 1.2 crore", "money", 12_000_000),
    ("INR 45,000", "money", 45_000), ("a 5 kW system", "kw", 5.0), ("3.5kWp", "kw", 3.5), ("30% shading", "percent", 30),
    ("३ लाख", "money", 300_000),
])
def test_amounts_sizes_and_percentages(text, kind, value):
    assert value in grounding.extract_facts(text)[kind]


@pytest.mark.parametrize("text, iso", [
    ("on 2026-10-14", "2026-10-14"), ("on 14/10/2026", "2026-10-14"), ("on 14 Oct 2026", "2026-10-14"),
    ("on 14th October, 2026", "2026-10-14"), ("on October 14, 2026", "2026-10-14"), ("by 15 Nov 2026", "2026-11-15"),
])
def test_dates_in_several_formats_become_iso(text, iso):
    assert grounding.extract_facts(text)["dates"] == {iso}


def test_contact_details_and_references_are_not_read_as_money():
    facts = grounding.extract_facts("Call +91 98765 43210 about GNE-WB-RES-2026-00125 or GNR-2026-000042.")
    assert facts["money"] == set()
    assert facts["phones"] == {"9876543210"}
    assert facts["references"] == {"GNE-WB-RES-2026-00125", "GNR-2026-000042"}


# ── Allowed ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("answer", [
    "Their budget is ₹4.5 lakh [L1] for a 5 kW system [L1].",
    "Budget INR 4,50,000 [L1]; they want it installed by 15 Nov 2026 [M12].",
    "The assistant's opinion is that 30% shading worries them [M13] — that is not something they said.",
    "Reach Priya at priya@example.com or +91 98765 43210 [C1]; project GNE-WB-RES-2026-00125 [L1].",
    "The lead opened on 14 Oct [L1].",
    "That is not on record.",
    "Nothing is on record about their roof type.",
])
def test_a_grounded_answer_passes(answer):
    assert check(answer).ok, check(answer).violations


def test_the_mock_answer_is_grounded_by_construction():
    answer = llm_module._mock_memory_answer("q", CONTEXT, "Priya Patel <priya@example.com>")
    assert check(answer).ok
    assert check(llm_module._mock_memory_answer("q", "", "Priya")).ok


def test_the_records_found_answer_is_grounded():
    assert check(grounding.records_found_answer(CONTEXT, "Priya Patel <priya@example.com>")).ok


# ── Violations ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("answer, code", [
    ("Their budget is ₹4.5 lakh [M99].", grounding.UNKNOWN_CITATION),
    ("Their budget is ₹4.5 lakh.", grounding.NO_CITATION),
    ("They are keen and ready to sign.", grounding.NO_CITATION),
    ("Their budget is ₹6 lakh [L1].", grounding.UNSUPPORTED_FACT),
    ("They want a 7 kW system [L1].", grounding.UNSUPPORTED_FACT),
    ("They expect 40% savings [M13].", grounding.UNSUPPORTED_FACT),
    ("Install by 20 Nov 2026 [M12].", grounding.UNSUPPORTED_FACT),
    ("Install by 20 Nov [M12].", grounding.UNSUPPORTED_FACT),
    ("Email priya.p@gmail.com [C1].", grounding.UNSUPPORTED_FACT),
    ("Call +91 91234 56789 [C1].", grounding.UNSUPPORTED_FACT),
    ("Project GNE-BR-RES-2026-00999 [L1].", grounding.UNSUPPORTED_FACT),
    ("That is not on record, but their budget is probably ₹5 lakh.", grounding.NO_CITATION),
])
def test_an_ungrounded_answer_is_caught(answer, code):
    assert code in codes(answer)


def test_the_question_is_not_evidence(memory, db_session, monkeypatch):
    """Repeating the employee's own guess back would otherwise pass as a fact on record."""
    customer = memory.get_or_create_customer(email="q@example.com", name="Quinn", performed_by="test")
    memory.create_lead(customer_id=customer.id, performed_by="test")
    db_session.commit()
    monkeypatch.setattr(llm_module.llm_client, "answer_from_memory", lambda **kw: "Yes, ₹8 lakh [L1].")
    result = memory_agent.answer(memory, "Is their budget ₹8 lakh?", customer_id=customer.id)
    assert result["answer"].startswith("Records found for Quinn")


def test_another_customers_record_is_not_evidence():
    other = "[C1] IDENTITY\n  email: someone.else@example.com\n[L1] LEAD 9\n  budget: 800000"
    assert grounding.UNSUPPORTED_FACT in codes("Budget ₹8 lakh, someone.else@example.com [L1].")
    assert check("Budget ₹8 lakh, someone.else@example.com [L1].", context=other).ok


# ── Wired into the Memory Agent ────────────────────────────────────────────────

@pytest.fixture
def customer(memory, db_session):
    customer = memory.get_or_create_customer(email="priya@example.com", name="Priya Patel", performed_by="test")
    lead = memory.create_lead(customer_id=customer.id, performed_by="test")
    memory.update_lead_fields(lead_id=lead.id, new_fields={"budget": "450000", "capacity_kw": 5},
                              performed_by="test")
    memory.add_customer_memory(customer.id, MemoryKind.preference, "Prefers morning calls",
                               source=MemorySource.reflection, created_by="test")
    db_session.commit()
    return customer


def _ungrounded(memory, customer):
    return [a for a in memory.list_audit_logs(entity_type="customer", entity_id=str(customer.id))
            if a.action == grounding.UNGROUNDED_ANSWER]


def test_a_grounded_answer_goes_out_unchanged(memory, db_session, customer, monkeypatch):
    monkeypatch.setattr(llm_module.llm_client, "answer_from_memory",
                        lambda **kw: "Their budget is ₹4.5 lakh [L1].")
    result = memory_agent.answer(memory, "What is their budget?", customer_id=customer.id)
    assert result["answer"] == "Their budget is ₹4.5 lakh [L1]." and _ungrounded(memory, customer) == []


def test_a_failing_answer_is_retried_with_the_failures(memory, db_session, customer, monkeypatch):
    calls = []

    def answer(**kw):
        calls.append(kw.get("feedback"))
        return "Their budget is ₹9 lakh [L1]." if len(calls) == 1 else "Their budget is ₹4.5 lakh [L1]."

    monkeypatch.setattr(llm_module.llm_client, "answer_from_memory", answer)
    result = memory_agent.answer(memory, "What is their budget?", customer_id=customer.id)
    assert result["answer"] == "Their budget is ₹4.5 lakh [L1]."
    assert calls[0] is None and any("900000" in item for item in calls[1])
    assert _ungrounded(memory, customer) == []


def test_two_failures_send_the_records_and_audit_it(memory, db_session, customer, monkeypatch):
    monkeypatch.setattr(llm_module.llm_client, "answer_from_memory",
                        lambda **kw: "They signed for ₹9 lakh last week and are very happy.")
    result = memory_agent.answer(memory, "What is their budget?", customer_id=customer.id, performed_by="emp-1")
    db_session.commit()
    assert result["answer"].startswith("Records found for Priya Patel")
    assert "₹9 lakh" not in result["answer"] and "budget: 450000" in result["answer"]
    entries = _ungrounded(memory, customer)
    assert len(entries) == 1 and entries[0].performed_by == "emp-1"
    assert len(entries[0].before_value["generated_answers"]) == 2
    assert entries[0].after_value["sent"] == "records_found"
    assert {v["code"] for attempt in entries[0].after_value["violations"] for v in attempt} >= {
        grounding.NO_CITATION, grounding.UNSUPPORTED_FACT}


def test_retry_feedback_reaches_the_prompt(monkeypatch):
    seen = []
    client = llm_module.LLMClient.__new__(llm_module.LLMClient)
    client._mock = False
    monkeypatch.setattr(client, "_chat", lambda messages, lead_id, operation: seen.append(messages) or "ok",
                        raising=False)
    client.answer_from_memory("q", CONTEXT, "Priya", feedback=["states money 900000, which no record holds"])
    assert "FAILED VERIFICATION" in seen[0][1]["content"] and "money 900000" in seen[0][1]["content"]
    assert grounding.NOT_ON_RECORD in seen[0][0]["content"]
