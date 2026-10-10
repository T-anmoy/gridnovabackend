"""BE-1 C8 — GET /dashboard/leads is a paged envelope {items, page, page_size, total}
with filters and a `q` search applied in SQL; the CSV export loads customers with the
leads (one query, not one per lead)."""
import pytest
from sqlalchemy import event

from app.memory.models import LeadSource
from tests.conftest import admin_headers, engineer_headers, sales_headers

LEADS = "/api/v1/dashboard/leads"


@pytest.fixture
def leads(memory, db_session):
    memory.get_or_create_employee("emp-sales-1", "S1", "s1@gridnova.in", "sales")
    memory.get_or_create_employee("emp-sales-2", "S2", "s2@gridnova.in", "sales")
    made = []
    for i, (name, email, phone, owner) in enumerate([
        ("Asha Roy", "asha@example.com", "+91 98300 11111", "emp-sales-1"),
        ("Bikash Sen", "bikash@example.com", "9830022222", "emp-sales-2"),
        ("Chitra Das", "chitra@example.com", None, None),
    ] + [(f"Bulk {n}", f"bulk{n}@example.com", None, None) for n in range(7)]):
        customer = memory.get_or_create_customer(email=email, name=name, phone=phone)
        lead = memory.create_lead(customer_id=customer.id, source=LeadSource.referral)
        lead.assigned_employee_id = owner
        made.append(lead)
    db_session.commit()
    return made


def test_the_envelope_and_paging(client, leads):
    r = client.get(LEADS, params={"page_size": 4}, headers=admin_headers())
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"items", "page", "page_size", "total"}
    assert body["total"] == 10 and len(body["items"]) == 4 and body["page"] == 1
    page3 = client.get(LEADS, params={"page_size": 4, "page": 3}, headers=admin_headers()).json()
    assert len(page3["items"]) == 2 and page3["total"] == 10
    all_ids = [i["id"] for p in (1, 2, 3)
               for i in client.get(LEADS, params={"page_size": 4, "page": p}, headers=admin_headers()).json()["items"]]
    assert all_ids == sorted(all_ids, reverse=True) and len(set(all_ids)) == 10


def test_page_size_is_bounded(client, leads):
    assert client.get(LEADS, params={"page_size": 201}, headers=admin_headers()).status_code == 422
    assert client.get(LEADS, params={"page_size": 200}, headers=admin_headers()).status_code == 200


@pytest.mark.parametrize("q, expected", [("asha", {"Asha Roy"}), ("BIKASH@EXAMPLE", {"Bikash Sen"}),
                                         ("98300 22222", {"Bikash Sen"}), ("11111", {"Asha Roy"})])
def test_q_matches_name_email_or_phone(client, memory, leads, q, expected):
    items = client.get(LEADS, params={"q": q}, headers=admin_headers()).json()["items"]
    assert {memory.get_lead(i["id"]).customer.name for i in items} == expected


def test_q_matches_a_lead_id(client, leads):
    target = leads[5].id
    items = client.get(LEADS, params={"q": str(target)}, headers=admin_headers()).json()["items"]
    assert target in [i["id"] for i in items]


def test_q_stays_inside_the_sales_scope(client, leads):
    body = client.get(LEADS, params={"q": "bikash"}, headers=sales_headers("emp-sales-1")).json()
    assert body == {"items": [], "page": 1, "page_size": 25, "total": 0}


def test_an_engineer_can_search_only_by_lead_id(client, leads):
    assert client.get(LEADS, params={"q": "asha"}, headers=engineer_headers()).json()["total"] == 0
    assert client.get(LEADS, params={"q": str(leads[0].id)}, headers=engineer_headers()).json()["total"] >= 1


def test_the_export_loads_customers_with_the_leads(client, leads, _isolated_db):
    from app.memory import db as db_module
    engine = db_module.SessionLocal.kw["bind"]
    statements = []

    def count(conn, cursor, statement, *a):
        if "FROM customers" in statement and "leads" not in statement:
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", count)
    try:
        r = client.get(f"{LEADS}/export", headers=admin_headers())
    finally:
        event.remove(engine, "before_cursor_execute", count)
    assert r.status_code == 200 and r.text.count("\n") == 11
    assert statements == []  # no per-lead customer lookups
