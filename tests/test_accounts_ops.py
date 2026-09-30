"""Phase 4A Step 7 — CSV exports, work-queue counts, staff email recipients and links,
Sentry scrubbing of payment data."""
import csv
import io

import pytest

from app.memory.models import ProjectStage
from app.payments import exports
from tests.payments_support import (  # noqa: F401 — fixtures
    ACCOUNTS, ADMIN, DASH, ENG, MGMT, OPS, PAY, SALES, accepted_customer, cheque, headers, kyc_ready, neft,
    notifications, onboarding_ready, record, stub_pdf, territories,
)


def _rows(body: bytes) -> list[dict]:
    text = body.decode("utf-8")
    assert text.startswith("﻿")
    return list(csv.DictReader(io.StringIO(text[1:])))


class TestExports:
    @pytest.mark.parametrize("value, safe", [
        ("=HYPERLINK(\"x\")", "'=HYPERLINK(\"x\")"), ("+91 98300", "'+91 98300"), ("-cmd", "'-cmd"),
        ("@SUM(A1)", "'@SUM(A1)"), ("\tx", "'\tx"), ("-20000.00", "-20000.00"), ("20000.00", "20000.00"),
        ("প্রিয়া দাস", "প্রিয়া দাস"), (None, ""),
    ])
    def test_injection_safety(self, value, safe):
        assert exports.safe(value) == safe

    def test_payments_receipts_ledger(self, client, db_session, onboarding_ready, stub_pdf):
        _, oid = accepted_customer(client, db_session, customer_name="প্রিয়া দাস")
        pid = record(client, ACCOUNTS, oid, cheque(payer_name="=cmd|' /C calc'!A0"), staff=True).json()["payment_id"]
        client.post(f"{PAY}/{pid}/verify", headers=ACCOUNTS)
        client.post(f"{PAY}/{pid}/dishonour", json={"reason": "Returned unpaid."}, headers=ACCOUNTS)
        r = client.get(f"{DASH}/exports/payments.csv", headers=ACCOUNTS)
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
        assert "attachment; filename=\"payments-" in r.headers["content-disposition"]
        [row] = _rows(r.content)
        assert row["customer_name"] == "প্রিয়া দাস" and row["amount_inr"] == "20000.00"
        assert row["payer_name"].startswith("'=") and row["status"] == "dishonoured"
        assert row["recorded_at_utc"].endswith("+00:00") and row["recorded_at_ist"].endswith("IST")
        [receipt] = _rows(client.get(f"{DASH}/exports/receipts.csv", headers=ADMIN).content)
        assert receipt["receipt_number"].startswith("GNR-") and receipt["amount_inr"] == "20000.00"
        ledger = _rows(client.get(f"{DASH}/exports/ledger.csv", headers=ACCOUNTS).content)
        assert [(e["kind"], e["amount_inr"]) for e in ledger] == [("credit", "20000.00"), ("reversal", "-20000.00")]
        assert ledger[1]["reverses_entry_id"] == ledger[0]["entry_id"]
        assert _rows(client.get(f"{DASH}/exports/payments.csv", params={"date_from": "2999-01-01"},
                                headers=ACCOUNTS).content) == []

    @pytest.mark.parametrize("who", [MGMT, SALES, OPS, ENG])
    def test_accounts_and_admin_only(self, client, who):
        for name in ("payments", "receipts", "ledger"):
            assert client.get(f"{DASH}/exports/{name}.csv", headers=who).status_code == 403


class TestWorkQueues:
    def test_scoped_by_role(self, client, db_session, onboarding_ready, stub_pdf):
        from app.memory.models import AssociateCapability
        from tests.execution_support import days_ago
        gna, oid = accepted_customer(client, db_session, stage=ProjectStage.agreement_signed,
                                     capability=AssociateCapability.execution)
        client.post(f"/api/v1/associate/onboardings/{oid}/execution/submissions",
                    json={"stage": "feasibility_submitted", "text": {"feasibility_reference": "R"}},
                    headers=headers(gna))
        pid = record(client, ACCOUNTS, oid, cheque(), staff=True).json()["payment_id"]
        client.post(f"{PAY}/{pid}/dishonour", json={"reason": "Returned."}, headers=ACCOUNTS)
        days_ago(db_session, oid, ProjectStage.accepted, 30)
        url = f"{DASH}/work-queues"
        ops = client.get(url, headers=OPS).json()
        assert ops == {"execution": {"pending_stage_verifications": 1, "stuck_projects": 0, "dcr_mismatch": 0}}
        acc = client.get(url, headers=ACCOUNTS).json()
        assert set(acc) == {"payments"}
        assert acc["payments"]["dishonoured"] == 1 and acc["payments"]["overdue_milestones"] == 1
        assert set(client.get(url, headers=ADMIN).json()) == {"execution", "payments"}
        assert set(client.get(url, headers=ENG).json()) == {"execution"}


class TestStaffEmails:
    def _notifier(self, monkeypatch, roles):
        import httpx
        from app.notifications.notifier import EmailNotifier, LogNotifier
        sent = []

        class _Resp:
            status_code = 200
        monkeypatch.setattr(httpx, "post", lambda url, headers, json, timeout: sent.append(json) or _Resp())
        return EmailNotifier("k", "n@x.test", ["staff@x.test"], "https://app.test", LogNotifier(),
                             role_emails=roles), sent

    def test_payment_events_go_to_their_teams(self, monkeypatch):
        n, sent = self._notifier(monkeypatch, {"accounts": ["acc@x.test"], "operations": ["ops@x.test"]})
        base = {"payment_id": 1, "onboarding_id": 7, "amount": "20000.00", "mode": "neft", "customer_name": "Asha",
                "recorded_by": "associate:GNA-WB-26-00001", "reason": "Returned."}
        n.notify("payment_recorded", base)
        n.notify("payment_dishonoured", base)
        n.notify("receipt_blocked", {"receipt_number": "GNR-2026-000001", "payment_id": 1, "onboarding_id": 7,
                                     "reason": "no_customer_email"})
        assert [m["to"] for m in sent] == [["acc@x.test"], ["acc@x.test", "staff@x.test"], ["acc@x.test"]]
        assert sent[0]["subject"] == "Payment to verify: Asha — INR 20000.00 by NEFT"
        assert "has no email address on file" in sent[2]["text"]
        assert all("https://app.test/dashboard/onboardings/7" in m["text"] for m in sent)

    def test_teams_fall_back_to_staff(self, monkeypatch):
        n, sent = self._notifier(monkeypatch, {})
        n.notify("payment_recorded", {"onboarding_id": 7, "amount": "1.00", "mode": "upi"})
        assert sent[0]["to"] == ["staff@x.test"]

    def test_overdue_escalation_links_the_customer_for_staff_only(self, monkeypatch):
        n, sent = self._notifier(monkeypatch, {})
        base = {"onboarding_id": 9, "overdue_hours": 5, "band": "hot", "score": 80, "response_due_at": "x"}
        n.notify("onboarding_overdue", {**base, "escalated_to": "admin"})
        n.notify("onboarding_overdue", {**base, "escalated_to": "district_head",
                                        "recipients": [{"email": "pa@x.test"}]})
        assert "https://app.test/dashboard/onboardings/9" in sent[0]["text"]
        assert "/dashboard/" not in sent[1]["text"]

    def test_settings_parse_team_lists(self, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "staff_notify_emails", "a@x.test")
        monkeypatch.setattr(settings, "accounts_notify_emails", "b@x.test, c@x.test")
        assert settings.accounts_notify_email_list == ["b@x.test", "c@x.test"]
        assert settings.operations_notify_email_list == ["a@x.test"]


class TestSentry:
    def test_payment_failure_event_carries_no_payment_values(self):
        from app.security.sentry_scrub import scrub_event
        event = {
            "request": {"data": {"cheque_number": "000123", "utr": "HDFC0000123456", "payment_reference": "X1",
                                 "payer_account_number": "000011112222", "reference": "000123", "amount": "20000"}},
            "exception": {"values": [{"stacktrace": {"frames": [
                {"module": "app.api.routes.associate_payments", "vars": {"body": "PaymentIn(reference='000123')"}},
                {"module": "app.payments.service", "vars": {"reference": "000123", "data": {"utr": "HDFC01"}}},
                {"module": "app.payments.receipts", "vars": {"receipt": "..."}},
            ]}}]},
        }
        out = scrub_event(event, {})
        data = out["request"]["data"]
        for key in ("cheque_number", "utr", "payment_reference", "payer_account_number", "reference"):
            assert data[key] != event["request"]["data"][key] or data[key] == "[Filtered]"
            assert "000123" not in str(data[key]) and "HDFC" not in str(data[key])
        assert all("vars" not in f for f in out["exception"]["values"][0]["stacktrace"]["frames"])
