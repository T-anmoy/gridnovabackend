"""BE-1 A4 — the Accounts payments queue: `q` search (customer name, payment reference,
GNE-… project reference, GNR-… receipt number) inside the caller's scope and before
paging; pages and totals from SQL; milestones paged the same way."""
import pytest
from sqlalchemy import select

from app.memory.models import CustomerOnboarding, Lead, PaymentReceipt, Project
from tests.payments_support import (  # noqa: F401 — fixtures
    ACCOUNTS, DASH, PAY, SALES, accepted_customer, kyc_ready, neft, notifications, onboarding_ready, record,
    stub_pdf, territories,
)


@pytest.fixture
def queue(client, db_session, onboarding_ready, stub_pdf):
    """Three customers, two payments each; the first lead is Sales', the second another
    rep's, the third unassigned."""
    from tests.conftest import seed_employees
    seed_employees(db_session, "emp-sales-1", "emp-sales-2")  # lead assignees (a real FK on PostgreSQL)
    made = []
    for n, name in enumerate(["Asha Roy", "Bikash Sen", "Chitra Das"]):
        _, oid = accepted_customer(client, db_session, customer_name=name, total=500000.0)
        if n < 2:
            onboarding = db_session.get(CustomerOnboarding, oid)
            db_session.get(Lead, onboarding.lead_id).assigned_employee_id = ["emp-sales-1", "emp-sales-2"][n]
            db_session.commit()
        ids = [record(client, ACCOUNTS, oid, neft(reference=f"UTR-{name.split()[0].upper()}-{i}"), staff=True)
               .json()["payment_id"] for i in range(2)]
        made.append((oid, ids))
    return made


def _q(client, q, who=ACCOUNTS, **params):
    r = client.get(PAY, params={"q": q, **params}, headers=who)
    assert r.status_code == 200, r.text
    return r.json()


def test_by_customer_name(client, queue):
    body = _q(client, "bikash")
    assert body["total"] == 2 and {i["customer_name"] for i in body["items"]} == {"Bikash Sen"}


def test_by_payment_reference_normalised(client, queue):
    body = _q(client, " utr-chitra -1 ")   # spaces and case ignored, as at entry
    assert body["total"] == 1 and body["items"][0]["reference"] == "UTR-CHITRA-1"


def test_by_project_reference(client, db_session, queue):
    oid = queue[0][0]
    ref = db_session.get(Project, db_session.get(CustomerOnboarding, oid).project_id).reference_id
    assert ref.startswith("GNE-")
    body = _q(client, ref)
    assert body["total"] == 2 and {i["project_reference_id"] for i in body["items"]} == {ref}


def test_by_receipt_number(client, db_session, queue):
    pid = queue[1][1][0]
    assert client.post(f"{PAY}/{pid}/verify", headers=ACCOUNTS).status_code == 200
    number = db_session.execute(select(PaymentReceipt.receipt_number)).scalar_one()
    assert number.startswith("GNR-")
    body = _q(client, number.lower())
    assert body["total"] == 1 and body["items"][0]["payment_id"] == pid


def test_search_stays_inside_the_sales_scope(client, queue):
    assert _q(client, "bikash", who=SALES)["total"] == 0      # another rep's lead
    assert _q(client, "asha", who=SALES)["total"] == 2        # own lead
    assert _q(client, "chitra", who=SALES)["total"] == 2      # unassigned


def test_paging_totals_come_from_the_filtered_set(client, queue):
    page1 = client.get(PAY, params={"page_size": 4}, headers=ACCOUNTS).json()
    page2 = client.get(PAY, params={"page_size": 4, "page": 2}, headers=ACCOUNTS).json()
    assert page1["total"] == page2["total"] == 6
    assert len(page1["items"]) == 4 and len(page2["items"]) == 2
    assert not {i["payment_id"] for i in page1["items"]} & {i["payment_id"] for i in page2["items"]}
    assert _q(client, "UTR", page_size=5)["total"] == 6


def test_milestones_page_from_sql(client, queue):
    full = client.get(f"{DASH}/payment-milestones", params={"page_size": 100}, headers=ACCOUNTS).json()
    assert full["total"] == len(full["items"]) and full["total"] >= 3
    first = client.get(f"{DASH}/payment-milestones", params={"page_size": 2}, headers=ACCOUNTS).json()
    second = client.get(f"{DASH}/payment-milestones", params={"page_size": 2, "page": 2}, headers=ACCOUNTS).json()
    assert first["total"] == full["total"]
    assert [i["milestone_id"] for i in first["items"] + second["items"]] == \
        [i["milestone_id"] for i in full["items"]][:4]
