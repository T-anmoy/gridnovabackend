"""BE-1 S8 — /dashboard/memory/ask never shows a Sales user another rep's customer.

An ambiguous name used to come back with every matching customer in `candidates`
(name, email, company), whoever's leads they were; and a customer with no lead at all
was readable by any Sales user. Matches are now filtered by the caller's lead scope
before anything is listed, resolved or handed to the model.
"""
import pytest

from app.llm import client as llm_module
from app.memory.models import MemoryKind, MemorySource
from tests.conftest import admin_headers, sales_headers

ASK = "/api/v1/dashboard/memory/ask"


@pytest.fixture
def customers(memory, db_session):
    def make(name, email, assigned):
        customer = memory.get_or_create_customer(email=email, name=name)
        if assigned is not False:
            lead = memory.create_lead(customer_id=customer.id)
            lead.assigned_employee_id = assigned
        memory.add_customer_memory(customer.id, MemoryKind.preference, f"{name} prefers mornings",
                                   source=MemorySource.reflection, created_by="test")
        return customer
    for emp in ("emp-sales-1", "emp-sales-2"):
        memory.get_or_create_employee(emp, emp, f"{emp}@gridnova.in", "sales")
    mine = make("Priya Patel", "patel@example.com", "emp-sales-1")
    theirs = make("Priya Sharma", "sharma@example.com", "emp-sales-2")
    leadless = make("Priya Noone", "noone@example.com", False)
    db_session.commit()
    return mine, theirs, leadless


@pytest.fixture
def labels(monkeypatch):
    seen = []
    real = llm_module.llm_client.answer_from_memory

    def capture(**kwargs):
        seen.append(kwargs["customer_label"] + "\n" + kwargs["context_block"])
        return real(**kwargs)

    monkeypatch.setattr(llm_module.llm_client, "answer_from_memory", capture)
    return seen


def test_an_ambiguous_name_resolves_inside_the_callers_scope(client, customers, labels):
    mine, theirs, leadless = customers
    r = client.post(ASK, json={"question": "What does Priya prefer?"}, headers=sales_headers("emp-sales-1"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["resolved"] is True and body["customer_id"] == mine.id
    assert "sharma" not in r.text.lower() and "noone" not in r.text.lower()
    assert labels and all("Sharma" not in text and "Noone" not in text for text in labels)


def test_only_other_reps_customers_match_nothing_leaks(client, customers, labels):
    r = client.post(ASK, json={"question": "Tell me about Priya Sharma"}, headers=sales_headers("emp-sales-1"))
    assert r.status_code == 200
    assert r.json()["resolved"] is False and r.json()["candidates"] == []
    assert "sharma@example.com" not in r.text
    assert labels == []


def test_admin_still_sees_every_candidate(client, customers):
    r = client.post(ASK, json={"question": "What does Priya prefer?"}, headers=admin_headers())
    assert r.status_code == 200
    assert {c["email"] for c in r.json()["candidates"]} == {
        "patel@example.com", "sharma@example.com", "noone@example.com"}


@pytest.mark.parametrize("which", [1, 2])  # another rep's customer; a customer with no lead
def test_asking_by_id_outside_the_scope_is_refused(client, customers, which):
    target = customers[which]
    r = client.post(ASK, json={"question": "anything?", "customer_id": target.id},
                    headers=sales_headers("emp-sales-1"))
    assert r.status_code == 403
    r = client.get(f"/api/v1/dashboard/customers/{target.id}/memory", headers=sales_headers("emp-sales-1"))
    assert r.status_code == 403


def test_an_unassigned_lead_is_in_scope(client, memory, db_session, customers):
    _, theirs, _ = customers
    memory.create_lead(customer_id=theirs.id)  # a second, unassigned lead
    db_session.commit()
    r = client.post(ASK, json={"question": "anything?", "customer_id": theirs.id},
                    headers=sales_headers("emp-sales-1"))
    assert r.status_code == 200
