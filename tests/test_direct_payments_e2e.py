"""BE-20 — payments for direct customers (no associate), end to end, on the real routes.

Scenario 1: a website enquiry → Sales drafts the proposal → approved and sent → the customer
accepts online → the schedule is the proposal's own terms (≤ 10 kWp: 20/60/20) → Accounts
records the advance by NEFT → a different Accounts person verifies → the receipt is numbered,
rendered and reachable → the material payment by cheque bounces after verification and the
same cheque is presented again → the final payment → nothing outstanding, every milestone
paid, the money exactly the proposal total.

Scenario 2: a Proposal Studio proposal above 10 kWp (25/60/15) accepted online, then converted.

Negative cases: wrong role, disabled mode, over-payment, an unaccepted proposal, a future
date, double and concurrent verification, an associate's lead. Test customers are invented.
"""
from __future__ import annotations

import threading
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.memory.models import (
    AuditLog, CommissionEntry, Lead, LedgerEntryKind, Payment, PaymentLedgerEntry, PaymentMilestone, PaymentReceipt,
    PaymentStatus, Proposal, ProposalStatus, ReceiptEmailStatus, TimelineEvent, TimelineEventType,
)
from tests.conftest import TEST_DATABASE_URL
from tests.execution_support import ACCOUNTS, Recorder
from tests.payments_support import kyc_ready, onboarding_ready, territories  # noqa: F401 — fixtures
from tests.proposal_document_support import complete_document, require_weasyprint

D = "/api/v1/dashboard"
PAY = f"{D}/payments"
ADMIN = {"X-Employee-Id": "emp-admin-1", "X-Employee-Role": "admin"}
SALES = {"X-Employee-Id": "emp-sales-1", "X-Employee-Role": "sales"}
OTHER_SALES = {"X-Employee-Id": "emp-sales-2", "X-Employee-Role": "sales"}
MGMT = {"X-Employee-Id": "emp-mgmt-1", "X-Employee-Role": "management"}
RECORDER = {"X-Employee-Id": "emp-acc-2", "X-Employee-Role": "accounts"}   # records; ACCOUNTS verifies
DESK = {"X-Employee-Id": "emp-desk-1", "X-Employee-Role": "proposal_desk"}
PUBLIC_STUDIO = "/api/v1/public/studio"
ACCEPTANCE = {"full_name": "Test Kumar Das", "mobile_last4": "0001", "accept_terms": True}


@pytest.fixture(autouse=True)
def _env(monkeypatch, fake_storage):
    """Storage, a Fernet key (Studio links), real-looking receipt text (the shipped DUMMY
    wording holds every receipt — tested below), a PDF stub for receipts, fresh public
    rate-limit windows, a base URL for links and a recording notifier."""
    from app.api.routes import public_intake, public_proposals, public_receipts, public_studio, studio
    from app.associates import documents
    from app.config import settings
    from app.execution import config
    from app.notifications import notifier as notifier_module
    from app.security.encryption import generate_key
    from app.security.rate_limit import RateLimitLog

    monkeypatch.setattr(settings, "fernet_encryption_key", generate_key())
    monkeypatch.setattr(settings, "public_api_base_url", "https://api.example.test")
    for module in (public_intake, public_proposals, public_receipts, public_studio):
        for name in [n for n in vars(module) if n.endswith("_log") and isinstance(getattr(module, n), RateLimitLog)]:
            monkeypatch.setattr(module, name, RateLimitLog(60))
    monkeypatch.setattr(studio, "_preview_log", RateLimitLog(60))
    receipt = config.payments()["receipt"]
    monkeypatch.setitem(receipt, "wording", {"heading": "Payment Receipt",
                                             "footer": "This is a payment receipt, not a GST tax invoice."})
    monkeypatch.setattr(documents, "render_pdf", lambda html: b"%PDF-1.4 receipt " + html.encode()[:64])
    rec = Recorder()
    monkeypatch.setattr(notifier_module, "notifier", rec)
    return rec


def ok(r, status=200):
    assert r.status_code == status, r.text
    return r.json()


# ── Builders ─────────────────────────────────────────────────────────────────────

def website_lead(client, email="web.customer@example.test") -> int:
    body = {"name": "Test Web Customer", "email": email, "phone": "9830000011", "customer_type": "residential",
            "state": "West Bengal", "city": "Kolkata", "average_bill": 2500}
    return ok(client.post("/api/v1/public/estimate-request", json=body))["lead_id"]


def sent_proposal(client, db_session, lead_id: int, basic="250000") -> tuple[int, str]:
    """Sales completes the enquiry and drafts, staff complete the document (3 kWp), Admin
    approves, then it is sent."""
    ok(client.patch(f"{D}/leads/{lead_id}/fields", headers=SALES,
                    json={"fields": {"project_type": "rooftop", "segment": "residential", "capacity_kw": 3}}))
    lead = db_session.get(Lead, lead_id)
    pid = ok(client.post(f"{D}/proposals/draft", json={"lead_id": lead_id, "customer_id": lead.customer_id},
                         headers=SALES))["proposal_id"]
    complete_document(client, pid, basic_price=basic)
    ok(client.post(f"{D}/proposals/{pid}/approve", headers=ADMIN))
    ok(client.post(f"{D}/proposals/{pid}/send", headers=ADMIN))
    db_session.expire_all()
    token = db_session.get(Proposal, pid).accept_token
    assert token
    return pid, token


def accepted_website_customer(client, db_session, email="web.customer@example.test") -> tuple[int, int]:
    lead_id = website_lead(client, email)
    pid, token = sent_proposal(client, db_session, lead_id)
    ok(client.post(f"/api/v1/proposals/accept/{token}"))
    return lead_id, pid


def lead_pay(lead_id: int) -> str:
    return f"{D}/leads/{lead_id}/payments"


def neft(amount, reference, **extra) -> dict:
    return {"amount": amount, "mode": "neft", "reference": reference, **extra}


def cheque(amount, number="004521", bank="State Bank of India", on=None, **extra) -> dict:
    return {"amount": amount, "mode": "cheque", "reference": number, "bank_name": bank,
            "instrument_date": (on or date.today() - timedelta(days=1)).isoformat(), **extra}


def record(client, lead_id, body, who=RECORDER):
    return client.post(lead_pay(lead_id), json=body, headers=who)


def verify(client, payment_id, who=ACCOUNTS):
    return client.post(f"{PAY}/{payment_id}/verify", headers=who)


# ── Scenario 1: website lead, ≤ 10 kWp, 20/60/20 ─────────────────────────────────

class TestWebsiteCustomerEndToEnd:
    def test_from_enquiry_to_fully_paid(self, client, db_session, _env, fake_storage):
        lead_id, proposal_id = accepted_website_customer(client, db_session)

        # The schedule exists on acceptance, from the accepted document's own terms.
        rows = db_session.execute(select(PaymentMilestone).where(PaymentMilestone.lead_id == lead_id)
                                  .order_by(PaymentMilestone.number)).scalars().all()
        assert [(m.number, str(m.percent), str(m.amount_due)) for m in rows] == [
            (1, "20.00", "54450.00"), (2, "60.00", "163350.00"), (3, "20.00", "54450.00")]
        assert all(m.onboarding_id is None and m.proposal_id == proposal_id for m in rows)
        audit = db_session.execute(select(AuditLog).where(AuditLog.entity_type == "payment_schedule")).scalar_one()
        assert audit.after_value["source"] == "document" and audit.after_value["total"] == "272250.00"

        view = ok(client.get(lead_pay(lead_id), headers=ACCOUNTS))
        assert view["lead_id"] == lead_id and view["onboarding_id"] is None
        assert view["totals"] == {"accepted_total": "272250.00", "paid_verified": "0.00",
                                  "awaiting_verification": "0.00", "balance": "272250.00"}
        assert [m["due_status"] for m in view["milestones"]] == ["due", "not_due", "not_due"]
        assert view["milestones"][0]["label"] == "On Order"
        assert view["enabled_modes"] == ["cheque", "neft", "rtgs", "imps"]
        assert view["project"]["project_reference_id"].startswith("GNE-")

        # Advance by NEFT, recorded by one Accounts person …
        advance = ok(record(client, lead_id, neft("54450.00", "UTR-ADV-0001",
                                                  milestone_id=view["milestones"][0]["milestone_id"])), 201)
        assert advance["status"] == "recorded" and advance["lead_id"] == lead_id
        # … who may not verify it themselves.
        r = verify(client, advance["payment_id"], who=RECORDER)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "self_verification"
        verified = ok(verify(client, advance["payment_id"]))
        assert verified["status"] == "verified" and verified["verified_by"] == "emp-acc-1"

        # The receipt: numbered, rendered, emailed, reachable on the customer's link.
        receipt = db_session.execute(select(PaymentReceipt)).scalar_one()
        db_session.refresh(receipt)
        assert receipt.receipt_number.startswith("GNR-") and receipt.email_status == ReceiptEmailStatus.sent
        [mail] = _env.of("receipt_customer_email")
        assert mail["customer_email"] == "web.customer@example.test"
        assert verified["receipt"]["url"] == mail["receipt_url"]
        public = client.get(f"/api/v1/public/receipts/{receipt.public_token}/pdf", follow_redirects=False)
        assert public.status_code == 302
        staff = ok(client.get(f"{lead_pay(lead_id)}/{advance['payment_id']}/receipt", headers=ACCOUNTS))
        assert staff["receipt_number"] == receipt.receipt_number and staff["pdf_url"]
        from app.payments import receipts
        html = receipts.receipt_html(db_session, receipt)
        assert "Rs 54,450.00" in html and "Rs 2,17,800.00" in html     # paid to date, and the balance

        # Material payment by cheque: verified, then it bounces (reversal, receipt void) …
        bounced = ok(record(client, lead_id, cheque("163350.00")), 201)
        ok(client.post(f"{PAY}/{bounced['payment_id']}/deposit", headers=ACCOUNTS))
        ok(verify(client, bounced["payment_id"]))
        dishonoured = ok(client.post(f"{PAY}/{bounced['payment_id']}/dishonour", headers=ACCOUNTS,
                                     json={"reason": "Returned unpaid: insufficient funds"}))
        assert dishonoured["status"] == "dishonoured" and dishonoured["receipt"]["voided"] is True
        assert ok(client.get(lead_pay(lead_id), headers=ACCOUNTS))["totals"]["paid_verified"] == "54450.00"
        # … and the same cheque, presented again, is linked to the bounce.
        again = ok(record(client, lead_id, cheque("163350.00")), 201)
        assert again["previous_payment_id"] == bounced["payment_id"]
        ok(verify(client, again["payment_id"]))

        # Final payment by RTGS.
        final = ok(record(client, lead_id, {"amount": "54450.00", "mode": "rtgs", "reference": "RTGS-FIN-01"}), 201)
        ok(verify(client, final["payment_id"]))

        view = ok(client.get(lead_pay(lead_id), headers=MGMT))
        assert view["totals"] == {"accepted_total": "272250.00", "paid_verified": "272250.00",
                                  "awaiting_verification": "0.00", "balance": "0.00"}
        assert [m["due_status"] for m in view["milestones"]] == ["paid", "paid", "paid"]
        assert [m["outstanding"] for m in view["milestones"]] == ["0.00", "0.00", "0.00"]
        ledger = db_session.execute(select(PaymentLedgerEntry)).scalars().all()
        assert sum((e.amount for e in ledger), Decimal(0)) == Decimal("272250.00")
        assert sorted(e.kind.value for e in ledger) == ["credit", "credit", "credit", "credit", "reversal"]
        assert sum((m.amount_due for m in rows), Decimal(0)) == Decimal("272250.00")

        # Nothing more can be recorded: the order is fully paid.
        r = record(client, lead_id, neft("0.01", "UTR-EXTRA-1"))
        assert r.status_code == 409 and r.json()["detail"]["code"] == "exceeds_outstanding"
        assert r.json()["detail"]["outstanding"] == "0.00"

        # Every state change is audited and on the lead's timeline; no commission anywhere.
        actions = [a.action for a in db_session.execute(select(AuditLog).where(
            AuditLog.entity_type == "payment").order_by(AuditLog.id)).scalars()]
        assert actions.count("recorded") == 4 and actions.count("verified") == 4
        assert actions.count("dishonoured") == 1 and actions.count("deposited") == 1
        events = [e.event_type for e in db_session.execute(select(TimelineEvent).where(
            TimelineEvent.lead_id == lead_id)).scalars()]
        assert events.count(TimelineEventType.payment_recorded) == 4
        assert events.count(TimelineEventType.payment_verified) == 4
        assert events.count(TimelineEventType.payment_dishonoured) == 1
        assert db_session.execute(select(CommissionEntry)).scalars().all() == []

        # Alerts name the lead, so the lead's owner is reached too (alerts.assigned_employee).
        recorded = _env.of("payment_recorded")
        assert len(recorded) == 4 and all(p["lead_id"] == lead_id and p["onboarding_id"] is None for p in recorded)
        assert len(_env.of("payment_verified")) == 4 and len(_env.of("payment_dishonoured")) == 1

    def test_the_dummy_wording_holds_the_receipt(self, client, db_session, monkeypatch):
        from app.execution import config
        monkeypatch.setitem(config.payments()["receipt"], "wording",
                            {"heading": "DUMMY — heading", "footer": "DUMMY — footer"})
        lead_id, _ = accepted_website_customer(client, db_session)
        p = ok(record(client, lead_id, neft("54450.00", "UTR-HELD-1")), 201)
        assert ok(verify(client, p["payment_id"]))["receipt"]["held"] is True
        receipt = db_session.execute(select(PaymentReceipt)).scalar_one()
        assert receipt.pdf_key is None
        assert client.get(f"/api/v1/public/receipts/{receipt.public_token}/pdf").status_code == 404

    def test_partial_payments_are_cumulative(self, client, db_session):
        lead_id, _ = accepted_website_customer(client, db_session)
        for n, amount in enumerate(("30000.00", "24450.00", "1.00"), 1):
            ok(verify(client, ok(record(client, lead_id, neft(amount, f"UTR-PART-{n}")), 201)["payment_id"]))
        statuses = ok(client.get(f"{D}/leads/{lead_id}/payment-schedule", headers=ACCOUNTS))["milestones"]
        assert [(m["due_status"], m["paid_toward"]) for m in statuses] == [
            ("paid", "54450.00"), ("not_due", "1.00"), ("not_due", "0.00")]


# ── Scenario 2: Proposal Studio, > 10 kWp, 25/60/15 ──────────────────────────────

COMMERCIAL_52 = [
    ("site", {"address_lines": ["1, Example Road", "Kolkata – 700006, West Bengal"], "town": "Kolkata",
              "state": "West Bengal"}),
    ("connection", {"category": "Commercial", "phase": "three", "sanctioned_load_kw": "60"}),
    ("plant", {"capacity_kwp": "52"}),
    ("price", {"mode": "staff_basic", "basic": "2860000"}),
]


def issued_commercial(client) -> tuple[int, str]:
    from tests.studio_support import acknowledge_all, fill, new_proposal
    require_weasyprint()
    pid = new_proposal(client, "commercial_rooftop", headers=DESK)["proposal"]["id"]
    fill(client, pid, COMMERCIAL_52, headers=DESK)
    acknowledge_all(client, pid, headers=DESK)
    ok(client.post(f"{D}/studio/proposals/{pid}/submit", headers=DESK))
    ok(client.post(f"{D}/studio/proposals/{pid}/approve", headers=MGMT, json={}))
    out = ok(client.post(f"{D}/studio/proposals/{pid}/issue", headers=DESK))
    return pid, out["public_url"].rsplit("/", 1)[1]


STUDIO_52_SCHEDULE = [(1, "25.00", "778635.00"), (2, "60.00", "1868724.00"), (3, "15.00", "467181.00")]


class TestStudioCustomerEndToEnd:
    def test_accepted_online_then_converted(self, client, db_session):
        pid, token = issued_commercial(client)
        ok(client.post(f"{PUBLIC_STUDIO}/{token}/accept", json=ACCEPTANCE))
        conv = ok(client.post(f"{D}/studio/proposals/{pid}/convert", headers=DESK))
        lead_id = conv["lead_id"]
        rows = db_session.execute(select(PaymentMilestone).where(PaymentMilestone.lead_id == lead_id)
                                  .order_by(PaymentMilestone.number)).scalars().all()
        assert [(m.number, str(m.percent), str(m.amount_due)) for m in rows] == STUDIO_52_SCHEDULE
        assert rows[0].label == "Advance along with the order"
        # Converting again changes nothing.
        ok(client.post(f"{D}/studio/proposals/{pid}/convert", headers=DESK))
        assert db_session.execute(select(PaymentMilestone)).scalars().all().__len__() == 3

        ids = []
        for n, (_, _, amount) in enumerate(STUDIO_52_SCHEDULE, 1):
            mode = {1: "neft", 2: "rtgs", 3: "imps"}[n]
            p = ok(record(client, lead_id, {"amount": amount, "mode": mode, "reference": f"S52-{n:03d}"}), 201)
            ids.append(ok(verify(client, p["payment_id"]))["payment_id"])
        view = ok(client.get(lead_pay(lead_id), headers=ADMIN))
        assert view["totals"]["paid_verified"] == view["totals"]["accepted_total"] == "3114540.00"
        assert view["totals"]["balance"] == "0.00"
        assert [m["due_status"] for m in view["milestones"]] == ["paid", "paid", "paid"]
        assert len(db_session.execute(select(PaymentReceipt)).scalars().all()) == 3

    def test_converted_first_then_accepted_online(self, client, db_session):
        pid, token = issued_commercial(client)
        conv = ok(client.post(f"{D}/studio/proposals/{pid}/convert", headers=DESK))
        lead_id = conv["lead_id"]
        assert ok(client.get(lead_pay(lead_id), headers=ACCOUNTS))["milestones"] == []   # sent, not accepted
        r = record(client, lead_id, neft("1000.00", "UTR-EARLY-1"))
        assert r.status_code == 409 and r.json()["detail"]["code"] == "not_accepted"
        ok(client.post(f"{PUBLIC_STUDIO}/{token}/accept", json=ACCEPTANCE))
        db_session.expire_all()
        rows = db_session.execute(select(PaymentMilestone).where(PaymentMilestone.lead_id == lead_id)
                                  .order_by(PaymentMilestone.number)).scalars().all()
        assert [(m.number, str(m.percent), str(m.amount_due)) for m in rows] == STUDIO_52_SCHEDULE


# ── Negative cases ───────────────────────────────────────────────────────────────

class TestRefusals:
    def test_roles(self, client, db_session):
        lead_id, _ = accepted_website_customer(client, db_session)
        body = neft("1000.00", "UTR-ROLE-1")
        for who in (SALES, MGMT, {"X-Employee-Id": "emp-ops-1", "X-Employee-Role": "operations"},
                    {"X-Employee-Id": "emp-eng-1", "X-Employee-Role": "engineer"}, DESK,
                    {"X-Employee-Id": "emp-nm-1", "X-Employee-Role": "network_manager"}):
            assert record(client, lead_id, body, who=who).status_code == 403, who
        p = ok(record(client, lead_id, body, who=ADMIN), 201)
        for who in (SALES, MGMT):
            assert verify(client, p["payment_id"], who=who).status_code == 403

    def test_disabled_mode_and_missing_details(self, client, db_session):
        lead_id, _ = accepted_website_customer(client, db_session)
        for mode in ("cash", "upi", "demand_draft"):
            r = record(client, lead_id, {"amount": "1000.00", "mode": mode, "reference": "REF-001"})
            assert r.status_code == 422 and r.json()["detail"]["code"] == "mode_not_enabled", mode
        r = record(client, lead_id, {"amount": "1000.00", "mode": "imps", "reference": ""})
        assert r.json()["detail"]["code"] == "reference_required"
        r = record(client, lead_id, {"amount": "1000.00", "mode": "cheque", "reference": "000777"})
        assert r.json()["detail"]["code"] == "instrument_details_required"
        r = record(client, lead_id, {"amount": "0", "mode": "neft", "reference": "UTR-ZERO"})
        assert r.json()["detail"]["code"] == "invalid_amount"

    def test_date_in_the_future(self, client, db_session):
        lead_id, _ = accepted_website_customer(client, db_session)
        r = record(client, lead_id, cheque("1000.00", on=date.today() + timedelta(days=3)))
        assert r.status_code == 422 and r.json()["detail"]["code"] == "date_in_future"

    def test_over_payment(self, client, db_session):
        lead_id, _ = accepted_website_customer(client, db_session)
        ok(record(client, lead_id, neft("272000.00", "UTR-BIG-1")), 201)   # recorded, not yet verified: counts
        r = record(client, lead_id, neft("250.01", "UTR-BIG-2"))
        assert r.status_code == 409 and r.json()["detail"] == {
            "code": "exceeds_outstanding", "message": r.json()["detail"]["message"], "accepted_total": "272250.00",
            "already_recorded": "272000.00", "outstanding": "250.00"}
        ok(record(client, lead_id, neft("250.00", "UTR-BIG-3")), 201)

    def test_unaccepted_proposal(self, client, db_session):
        lead_id = website_lead(client)
        sent_proposal(client, db_session, lead_id)
        r = record(client, lead_id, neft("1000.00", "UTR-NOPE-1"))
        assert r.status_code == 409 and r.json()["detail"]["code"] == "not_accepted"
        assert ok(client.get(f"{D}/leads/{lead_id}/payment-schedule", headers=ACCOUNTS))["milestones"] == []

    def test_double_verification(self, client, db_session):
        lead_id, _ = accepted_website_customer(client, db_session)
        p = ok(record(client, lead_id, neft("1000.00", "UTR-TWICE-1")), 201)
        ok(verify(client, p["payment_id"]))
        r = verify(client, p["payment_id"], who=ADMIN)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "invalid_transition"
        credits = db_session.execute(select(PaymentLedgerEntry).where(
            PaymentLedgerEntry.kind == LedgerEntryKind.credit)).scalars().all()
        assert len(credits) == 1

    def test_an_associates_lead_keeps_its_own_routes(self, client, db_session, onboarding_ready):
        from tests.payments_support import accepted_customer
        _, oid = accepted_customer(client, db_session)
        from app.memory.models import CustomerOnboarding
        lead_id = db_session.get(CustomerOnboarding, oid).lead_id
        r = client.get(lead_pay(lead_id), headers=ACCOUNTS)
        assert r.status_code == 409 and r.json()["detail"] == {
            "code": "associate_customer", "message": r.json()["detail"]["message"], "onboarding_id": oid}
        assert record(client, lead_id, neft("1000.00", "UTR-ASSOC-1")).status_code == 409

    def test_sales_reads_only_its_own_or_unassigned_lead(self, client, db_session):
        lead_id, _ = accepted_website_customer(client, db_session)
        assert client.get(lead_pay(lead_id), headers=SALES).status_code == 200      # unassigned
        lead = db_session.get(Lead, lead_id)
        lead.assigned_employee_id = "emp-sales-1"
        db_session.commit()
        p = ok(record(client, lead_id, neft("1000.00", "UTR-OWN-1")), 201)
        assert client.get(lead_pay(lead_id), headers=SALES).status_code == 200
        assert client.get(f"{PAY}/{p['payment_id']}", headers=SALES).status_code == 200
        assert [i["payment_id"] for i in ok(client.get(PAY, headers=SALES))["items"]] == [p["payment_id"]]
        assert client.get(lead_pay(lead_id), headers=OTHER_SALES).status_code == 403
        assert client.get(f"{PAY}/{p['payment_id']}", headers=OTHER_SALES).status_code == 403
        assert ok(client.get(PAY, headers=OTHER_SALES))["items"] == []
        # Sales never records, uploads proof, verifies or cancels on a direct customer.
        from tests.execution_support import PDF
        r = client.post(f"{PAY}/{p['payment_id']}/proof", headers=SALES,
                        files={"file": ("proof.pdf", PDF, "application/pdf")})
        assert r.status_code == 403

    def test_milestone_queue_and_work_queues_include_direct_customers(self, client, db_session):
        lead_id, _ = accepted_website_customer(client, db_session)
        items = ok(client.get(f"{D}/payment-milestones", headers=ACCOUNTS))["items"]
        assert [(i["lead_id"], i["onboarding_id"], i["number"]) for i in items] == [
            (lead_id, None, 1), (lead_id, None, 2), (lead_id, None, 3)]
        assert ok(client.get(f"{D}/work-queues", headers=ACCOUNTS))["payments"]["overdue_milestones"] == 0
        assert ok(client.get(PAY, params={"lead_id": lead_id}, headers=ACCOUNTS))["total"] == 0


class TestSchedule:
    def test_a_proposal_without_a_document_falls_back_to_the_price_list(self, db_session):
        from app.memory.memory_manager import MemoryManager
        from app.payments import direct
        memory = MemoryManager(db_session)
        customer = memory.get_or_create_customer("old.format@example.test", "Old Format")
        lead = memory.create_lead(customer.id)
        db_session.add(Proposal(lead_id=lead.id, version=1, status=ProposalStatus.accepted, content={},
                                total_value=100000.33))
        db_session.commit()
        rows = direct.ensure_schedule(db_session, lead, "test")
        assert [(m.label, str(m.amount_due), m.trigger) for m in rows] == [
            ("On Order", "20000.07", "accepted"), ("Against Material Bill", "60000.20", "material_dispatched"),
            ("Before Commissioning", "20000.06", "inspection_net_meter")]
        assert direct.ensure_schedule(db_session, lead, "test") == rows       # idempotent

    def test_the_advance_is_overdue_after_the_threshold(self, client, db_session, monkeypatch):
        from datetime import datetime, timezone

        from app.payments import direct
        from app.payments import service as payments
        lead_id, pid = accepted_website_customer(client, db_session)
        lead = db_session.get(Lead, lead_id)
        later = datetime.now(timezone.utc) + timedelta(days=8)
        statuses = direct.milestone_status(db_session, lead, now=later)
        assert statuses[0]["overdue"] is True and statuses[1]["overdue"] is False
        monkeypatch.setattr(payments, "_now", lambda: later)
        overdue = ok(client.get(f"{D}/payment-milestones", params={"needs_attention": True}, headers=ACCOUNTS))
        assert [(i["lead_id"], i["number"]) for i in overdue["items"]] == [(lead_id, 1)]


@pytest.mark.skipif(not TEST_DATABASE_URL, reason="needs real concurrent connections (PostgreSQL)")
class TestConcurrency:
    def test_two_simultaneous_verifications_credit_once(self, client, db_session, _isolated_db):
        from app.associates.service import AssociateError
        from app.payments import service as payments
        lead_id, _ = accepted_website_customer(client, db_session)
        pid = ok(record(client, lead_id, neft("1000.00", "UTR-RACE-1")), 201)["payment_id"]
        barrier, results = threading.Barrier(2), []

        def run(who):
            session = _isolated_db()
            try:
                barrier.wait()
                payments.verify(session, pid, who)
                session.commit()
                results.append("verified")
            except AssociateError as exc:
                session.rollback()
                results.append(exc.code)
            finally:
                session.close()

        threads = [threading.Thread(target=run, args=(who,)) for who in ("emp-acc-1", "emp-acc-3")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sorted(results) == ["invalid_transition", "verified"]
        db_session.expire_all()
        assert db_session.get(Payment, pid).status == PaymentStatus.verified
        assert len(db_session.execute(select(PaymentLedgerEntry)).scalars().all()) == 1
        assert len(db_session.execute(select(PaymentReceipt)).scalars().all()) == 1

    def test_two_simultaneous_payments_cannot_overpay(self, client, db_session, _isolated_db):
        from app.associates.service import AssociateError
        from app.payments import direct
        lead_id, _ = accepted_website_customer(client, db_session)
        barrier, results = threading.Barrier(2), []

        def run(i):
            session = _isolated_db()
            try:
                barrier.wait()
                direct.record(session, session.get(Lead, lead_id), neft("272250.00", f"UTR-RACE-{i}"), "emp-acc-2")
                session.commit()
                results.append("recorded")
            except AssociateError as exc:
                session.rollback()
                results.append(exc.code)
            finally:
                session.close()

        threads = [threading.Thread(target=run, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sorted(results) == ["exceeds_outstanding", "recorded"]


class TestAlertsJob:
    def test_overdue_advance_and_unverified_payment_link_to_the_lead(self, client, db_session, _env):
        from datetime import datetime, timezone

        from app.jobs.payment_alerts import check_payment_alerts
        from app.notifications.notifier import _render_payments_attention
        lead_id, _ = accepted_website_customer(client, db_session)
        p = ok(record(client, lead_id, neft("1000.00", "UTR-LATE-1")), 201)
        later = datetime.now(timezone.utc) + timedelta(days=8)
        assert check_payment_alerts(db_session, later) == {"overdue_milestones": 1, "awaiting_verification": 1}
        [payload] = _env.of("payments_attention")
        assert payload["overdue_milestones"][0]["lead_id"] == lead_id
        assert payload["awaiting_verification"][0] == {"onboarding_id": None, "payment_id": p["payment_id"],
                                                       "amount": "1000.00", "mode": "neft", "lead_id": lead_id}
        _, html, text = _render_payments_attention(payload, "https://app.example.test")
        assert text.count(f"/dashboard/leads/{lead_id}") == 2 and "onboardings/None" not in text
        # Once per milestone and per payment.
        assert check_payment_alerts(db_session, later) == {"overdue_milestones": 0, "awaiting_verification": 0}
