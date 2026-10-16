"""Phase 4A Step 6 — payment receipts: numbering, figures and words, the placeholder
hold, missing email, resend, the public token route."""
import threading
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.memory.models import AuditLog, PaymentReceipt, ReceiptEmailStatus
from app.payments import receipts
from tests.conftest import TEST_DATABASE_URL
from tests.payments_support import (  # noqa: F401 — fixtures
    ACCOUNTS, ADMIN, PAY, accepted_customer, headers, kyc_ready, neft, notifications, onboarding_ready, record,
    real_receipt_text, stub_pdf, territories,
)


class TestWords:
    @pytest.mark.parametrize("n, words", [
        (0, "Zero"), (5, "Five"), (15, "Fifteen"), (40, "Forty"), (99, "Ninety-Nine"), (105, "One Hundred Five"),
        (1000, "One Thousand"), (1001, "One Thousand One"), (99999, "Ninety-Nine Thousand Nine Hundred Ninety-Nine"),
        (100000, "One Lakh"), (123456, "One Lakh Twenty-Three Thousand Four Hundred Fifty-Six"),
        (1000000, "Ten Lakh"), (10000000, "One Crore"), (25000000, "Two Crore Fifty Lakh"),
        (1234567890, "One Hundred Twenty-Three Crore Forty-Five Lakh Sixty-Seven Thousand Eight Hundred Ninety"),
    ])
    def test_indian_grouping(self, n, words):
        assert receipts.indian_words(n) == words

    def test_with_paise(self):
        assert receipts.amount_in_words(Decimal("123456.78")) == \
            "Rupees One Lakh Twenty-Three Thousand Four Hundred Fifty-Six and Seventy-Eight Paise Only"
        assert receipts.amount_in_words(Decimal("20000.00")) == "Rupees Twenty Thousand Only"
        assert receipts.amount_in_words(Decimal("0.05")) == "Rupees Zero and Five Paise Only"

    @pytest.mark.parametrize("amount, figures", [
        ("999", "Rs 999.00"), ("1000", "Rs 1,000.00"), ("100000", "Rs 1,00,000.00"), ("1234567.89", "Rs 12,34,567.89"),
        ("123456789", "Rs 12,34,56,789.00"),
    ])
    def test_figures(self, amount, figures):
        assert receipts.indian_figures(Decimal(amount)) == figures

    def test_number_format(self):
        assert receipts.format_number("GNR-{YYYY}-{SEQ:06}", 2026, 7) == "GNR-2026-000007"
        assert receipts.format_number("R/{YYYY}/{SEQ}", 2026, 12) == "R/2026/12"


def _verified(client, db_session, reference="UTR-0001", **customer):
    gna, oid = accepted_customer(client, db_session, **customer)
    pid = record(client, ACCOUNTS, oid, neft(reference=reference), staff=True).json()["payment_id"]
    r = client.post(f"{PAY}/{pid}/verify", headers=ACCOUNTS)
    assert r.status_code == 200, r.text
    return gna, oid, pid


class TestIssue:
    def test_numbered_and_held_unrendered_while_dummy(self, client, db_session, onboarding_ready, stub_pdf,
                                                      notifications):
        _, _, first = _verified(client, db_session, customer_email="asha@example.test")
        _verified(client, db_session, reference="UTR-0002", customer_email="ravi@example.test")
        rows = db_session.execute(select(PaymentReceipt).order_by(PaymentReceipt.id)).scalars().all()
        year = rows[0].receipt_number.split("-")[1]
        assert [r.receipt_number for r in rows] == [f"GNR-{year}-000001", f"GNR-{year}-000002"]
        # BE-1 C3: decided before rendering — no placeholder-content PDF is ever stored.
        assert all(r.pdf_key is None and r.email_status == ReceiptEmailStatus.held for r in rows)
        assert rows[0].held_reason == "placeholder_content"
        assert not notifications.of("receipt_customer_email")          # never a DUMMY receipt to a customer
        assert [b["reason"] for b in notifications.of("receipt_blocked")] == ["held", "held"]
        item = client.get(f"{PAY}/{first}", headers=ACCOUNTS).json()
        assert item["receipt"]["held"] is True and "receipt_held" in item["attention_reasons"]

    def test_sent_with_real_content(self, client, db_session, onboarding_ready, stub_pdf, real_receipt_text,
                                    notifications):
        gna, oid, pid = _verified(client, db_session, customer_email="asha@example.test")
        [sent] = notifications.of("receipt_customer_email")
        receipt = db_session.execute(select(PaymentReceipt)).scalar_one()
        assert sent["customer_email"] == "asha@example.test" and sent["receipt_number"] == receipt.receipt_number
        assert sent["receipt_url"].endswith(f"/api/v1/public/receipts/{receipt.public_token}/pdf")
        db_session.refresh(receipt)
        assert receipt.email_status == ReceiptEmailStatus.sent and receipt.emailed_at
        mine = client.get(f"/api/v1/associate/onboardings/{oid}/payments", headers=headers(gna)).json()
        assert mine["payments"][0]["receipt"]["url"] == sent["receipt_url"]
        assert "held_reason" not in mine["payments"][0]["receipt"]   # staff-only detail

    def test_no_customer_email(self, client, db_session, onboarding_ready, stub_pdf, real_receipt_text,
                               notifications):
        _, _, pid = _verified(client, db_session)                  # onboarded without an email
        receipt = db_session.execute(select(PaymentReceipt)).scalar_one()
        db_session.refresh(receipt)
        assert (receipt.email_status, receipt.email_skipped_reason) == (ReceiptEmailStatus.skipped,
                                                                        "no_customer_email")
        item = client.get(f"{PAY}/{pid}", headers=ACCOUNTS).json()
        assert item["receipt"]["receipt_pending_email"] is True and "receipt_pending_email" in item["attention_reasons"]
        assert [b["reason"] for b in notifications.of("receipt_blocked")] == ["no_customer_email"]
        assert client.get(PAY, params={"needs_attention": True}, headers=ACCOUNTS).json()["total"] == 1

    def test_production_holds_while_the_registry_is_unresolved(self, db_session, real_receipt_text, monkeypatch):
        """Even with real-looking text in place, production holds while P5/P6 are
        still open in the registry (they probe config/execution_payments.yaml)."""
        from app.config import settings
        assert receipts.hold_reason(db_session) is None
        monkeypatch.setattr(settings, "environment", "production")
        assert receipts.hold_reason(db_session) == "placeholder_content"

    def test_release_held_once_resolved(self, client, db_session, onboarding_ready, stub_pdf, notifications,
                                        monkeypatch):
        _verified(client, db_session, customer_email="asha@example.test")
        url = "/api/v1/dashboard/payment-receipts/release-held"
        assert client.post(url, headers=ACCOUNTS).json() == {"released": 0, "failed": 0, "still_held": 1,
                                                              "reason": "placeholder_content"}
        from app.execution import config
        receipt_cfg = config.payments()["receipt"]
        monkeypatch.setitem(receipt_cfg, "company", {**receipt_cfg["company"], "gstin": "19ABCDE1234F1Z5",
                                                     "bank_name": "SBI", "account_number": "1", "ifsc": "SBIN0000001"})
        monkeypatch.setitem(receipt_cfg, "wording", {"heading": "Receipt", "footer": "Not a GST tax invoice."})
        assert client.post(url, headers=ADMIN).json() == {"released": 1, "failed": 0, "still_held": 0,
                                                          "reason": None}
        assert len(notifications.of("receipt_customer_email")) == 1
        receipt = db_session.execute(select(PaymentReceipt)).scalar_one()
        db_session.refresh(receipt)
        assert receipt.pdf_key  # rendered on release, with the real content

    def test_render_failure_is_visible(self, client, db_session, onboarding_ready, notifications, monkeypatch,
                                       real_receipt_text):
        from app.associates import documents

        def boom(html):
            raise RuntimeError("pango missing")
        monkeypatch.setattr(documents, "render_pdf", boom)
        _, _, pid = _verified(client, db_session, customer_email="asha@example.test")
        item = client.get(f"{PAY}/{pid}", headers=ACCOUNTS).json()
        assert item["status"] == "verified" and item["receipt"]["email_status"] == "failed"
        assert "pango missing" in item["receipt"]["render_error"] and "receipt_failed" in item["attention_reasons"]
        assert [b["reason"] for b in notifications.of("receipt_blocked")] == ["render_failed"]

    def test_receipt_content(self, client, db_session, onboarding_ready, stub_pdf, real_receipt_text):
        _verified(client, db_session, customer_email="asha@example.test")
        html = receipts.receipt_html(db_session, db_session.execute(select(PaymentReceipt)).scalar_one())
        for expected in ("Rs 20,000.00", "Rupees Twenty Thousand Only", "NEFT", "UTR-0001", "Rs 80,000.00",
                         "19ABCDE1234F1Z5", "not a GST tax invoice"):
            assert expected in html

    def test_letterhead_prints_the_patna_branch_under_the_head_office(self, client, db_session, onboarding_ready,
                                                                      stub_pdf, real_receipt_text):
        """BE-7 N2: config/proposal_content.yaml company.branch_offices."""
        _verified(client, db_session, customer_email="asha@example.test")
        html = receipts.receipt_html(db_session, db_session.execute(select(PaymentReceipt)).scalar_one())
        head = html.index("Kolkata – 700107")
        assert html.index("Patna Branch: C/O Parbati Bhawan") > head
        assert "Patna – 800027, Bihar" in html


class TestResendAndPublic:
    def test_resend(self, client, db_session, onboarding_ready, stub_pdf, real_receipt_text, notifications):
        _, _, pid = _verified(client, db_session, customer_email="asha@example.test")
        r = client.post(f"{PAY}/{pid}/receipt/resend", headers=ACCOUNTS)
        assert r.status_code == 429 and r.json()["detail"]["code"] == "resend_too_soon"
        receipt = db_session.execute(select(PaymentReceipt)).scalar_one()
        receipt.emailed_at = receipt.emailed_at.replace(year=2020)
        db_session.commit()
        assert client.post(f"{PAY}/{pid}/receipt/resend", headers=ACCOUNTS).status_code == 200
        assert len(notifications.of("receipt_customer_email")) == 2
        assert db_session.execute(select(AuditLog).where(AuditLog.action == "resend")).scalar_one()
        assert client.post(f"{PAY}/{pid}/receipt/resend", headers=headers("GNA-X")).status_code == 401

    def test_public_token_route(self, client, db_session, onboarding_ready, stub_pdf, real_receipt_text):
        _verified(client, db_session)
        receipt = db_session.execute(select(PaymentReceipt)).scalar_one()
        r = client.get(f"/api/v1/public/receipts/{receipt.public_token}/pdf", follow_redirects=False)
        assert r.status_code == 302 and "receipts/" in r.headers["location"]
        assert client.get("/api/v1/public/receipts/not-a-real-token-at-all/pdf").status_code == 404
        assert client.get("/api/v1/public/receipts/x/pdf").status_code == 404

    def test_no_receipt_before_verification(self, client, db_session, onboarding_ready):
        _, oid = accepted_customer(client, db_session)
        pid = record(client, ACCOUNTS, oid, neft(), staff=True).json()["payment_id"]
        assert db_session.execute(select(PaymentReceipt)).first() is None
        assert client.post(f"{PAY}/{pid}/receipt/resend", headers=ACCOUNTS).json()["detail"]["code"] == "no_receipt"


@pytest.mark.skipif(not TEST_DATABASE_URL, reason="needs real concurrent connections (PostgreSQL)")
def test_numbering_under_concurrency(_isolated_db):
    SessionLocal = _isolated_db
    barrier = threading.Barrier(8)
    numbers = []

    def allocate():
        db = SessionLocal()
        try:
            barrier.wait()
            numbers.append(receipts.next_number(db, 2026))
            db.commit()
        finally:
            db.close()

    threads = [threading.Thread(target=allocate) for _ in range(8)]
    [t.start() for t in threads]
    [t.join(timeout=30) for t in threads]
    assert sorted(numbers) == [f"GNR-2026-{i:06d}" for i in range(1, 9)]


class TestHeldVoidAndRelease:
    """BE-1 C3."""

    def test_a_held_receipt_is_404_on_the_public_link(self, client, db_session, onboarding_ready, stub_pdf):
        _verified(client, db_session)                                 # shipped DUMMY text → held
        receipt = db_session.execute(select(PaymentReceipt)).scalar_one()
        receipt.pdf_key = "receipts/legacy-dummy.pdf"                 # as an older build left it
        db_session.commit()
        assert client.get(f"/api/v1/public/receipts/{receipt.public_token}/pdf").status_code == 404

    def test_dishonour_voids_the_receipt(self, client, db_session, onboarding_ready, stub_pdf, real_receipt_text,
                                         notifications):
        _, _, pid = _verified(client, db_session, customer_email="asha@example.test")
        r = client.post(f"{PAY}/{pid}/dishonour", json={"reason": "cheque bounced"}, headers=ACCOUNTS)
        assert r.status_code == 200, r.text
        receipt = db_session.execute(select(PaymentReceipt)).scalar_one()
        db_session.refresh(receipt)
        assert receipt.voided_at is not None and receipt.void_reason == "cheque bounced"
        assert db_session.execute(select(AuditLog).where(AuditLog.action == "voided")).scalar_one()
        assert client.get(f"/api/v1/public/receipts/{receipt.public_token}/pdf").status_code == 404
        receipt.emailed_at = receipt.emailed_at.replace(year=2020)
        db_session.commit()
        r = client.post(f"{PAY}/{pid}/receipt/resend", headers=ACCOUNTS)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "receipt_voided"
        body = client.get(f"{PAY}/{pid}", headers=ACCOUNTS).json()["receipt"]
        assert body["voided"] is True and body["url"] is None and body["voided_at"]
        assert "VOID" in receipts.receipt_html(db_session, receipt)

    def test_one_failed_delivery_does_not_strand_the_rest(self, client, db_session, onboarding_ready, stub_pdf,
                                                          notifications, monkeypatch):
        _verified(client, db_session, customer_email="a@example.test")
        _verified(client, db_session, reference="UTR-0002", customer_email="b@example.test")
        from app.execution import config
        receipt_cfg = config.payments()["receipt"]
        monkeypatch.setitem(receipt_cfg, "company", {**receipt_cfg["company"], "gstin": "19ABCDE1234F1Z5",
                                                     "bank_name": "SBI", "account_number": "1", "ifsc": "SBIN0000001"})
        monkeypatch.setitem(receipt_cfg, "wording", {"heading": "Receipt", "footer": "Not a GST tax invoice."})
        real_deliver, calls = receipts.deliver, []

        def flaky(db, receipt):
            calls.append(receipt.id)
            if len(calls) == 1:
                raise RuntimeError("mail provider down")
            return real_deliver(db, receipt)

        monkeypatch.setattr(receipts, "deliver", flaky)
        body = client.post("/api/v1/dashboard/payment-receipts/release-held", headers=ADMIN).json()
        assert body == {"released": 2, "failed": 1, "still_held": 0, "reason": None}
        db_session.expire_all()
        statuses = sorted(r.email_status.value for r in db_session.execute(select(PaymentReceipt)).scalars())
        assert statuses == ["failed", "sent"]
        failed = next(r for r in db_session.execute(select(PaymentReceipt)).scalars()
                      if r.email_status == ReceiptEmailStatus.failed)
        assert failed.email_skipped_reason == "release_failed: RuntimeError"

    def test_a_stale_pending_receipt_needs_attention(self, client, db_session, onboarding_ready, stub_pdf,
                                                     real_receipt_text):
        from datetime import datetime, timedelta, timezone
        _verified(client, db_session, customer_email="asha@example.test")
        receipt = db_session.execute(select(PaymentReceipt)).scalar_one()
        receipt.email_status = ReceiptEmailStatus.pending
        receipt.created_at = datetime.now(timezone.utc) - timedelta(minutes=61)
        db_session.commit()
        assert receipts.attention(receipt) == ["receipt_pending_stale"]
        receipt.created_at = datetime.now(timezone.utc) - timedelta(minutes=5)
        assert receipts.attention(receipt) == []
