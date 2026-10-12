"""Phase 4A Step 5 — the payments ledger: exact decimals, the schedule, recording rules,
every transition, the append-only ledger, milestone due status, permissions."""
import threading
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DatabaseError

from app.associates.service import AssociateError
from app.execution import config
from app.memory.models import (
    AssociateCapability, AuditLog, CustomerOnboarding, LedgerEntryKind, OnboardingStatus, Payment,
    PaymentLedgerEntry, PaymentMilestone, ProjectStage,
)
from app.payments import service as payments
from tests.conftest import TEST_DATABASE_URL
from tests.payments_support import (  # noqa: F401 — fixtures
    ACCOUNTS, ADMIN, DASH, ENG, MGMT, OPS, PAY, SALES, accepted_customer, advance_to, cheque, headers, kyc_ready,
    make_associate, neft, new_customer, notifications, onboarding_ready, pay_url, proof, record, staff_pay_url,
    stub_pdf, territories,
)


class TestDecimals:
    @pytest.mark.parametrize("total, percents, parts", [
        ("100000.00", ["20", "60", "20"], ["20000.00", "60000.00", "20000.00"]),
        ("123456.78", ["20", "60", "20"], ["24691.36", "74074.07", "24691.35"]),
        ("0.10", ["20", "60", "20"], ["0.02", "0.06", "0.02"]),
        ("1000.01", ["33.33", "33.33", "33.34"], ["333.30", "333.30", "333.41"]),
        ("100.00", ["33.33", "33.33", "33.34"], ["33.33", "33.33", "33.34"]),
        ("99999.99", ["20", "60", "20"], ["20000.00", "59999.99", "20000.00"]),
    ])
    def test_split_sums_exactly(self, total, percents, parts):
        got = payments.split(Decimal(total), [Decimal(p) for p in percents])
        assert [str(g) for g in got] == parts and sum(got) == Decimal(total)

    def test_float_totals_go_through_str(self):
        assert payments.money(0.1 + 0.2) == Decimal("0.30")
        assert payments.money(123456.785) == Decimal("123456.79")

    @pytest.mark.parametrize("raw", ["25000.505", "-1", "0", "abc", "1e3.5", "NaN", "Infinity", "10000000000"])
    def test_invalid_amounts(self, raw):
        with pytest.raises(AssociateError) as exc:
            payments.parse_amount(raw)
        assert exc.value.code == "invalid_amount"

    def test_valid_amounts(self):
        assert payments.parse_amount("25000.5") == Decimal("25000.50")
        assert payments.parse_amount(25000) == Decimal("25000.00")


class TestSchedule:
    def test_created_on_acceptance_from_the_template(self, client, db_session, onboarding_ready):
        gna, oid = accepted_customer(client, db_session, total=123456.78)
        body = client.get(pay_url(oid), headers=headers(gna)).json()
        assert [(m["number"], m["amount_due"], m["due_status"]) for m in body["milestones"]] == [
            (1, "24691.36", "due"), (2, "74074.07", "not_due"), (3, "24691.35", "not_due")]
        assert body["totals"] == {"accepted_total": "123456.78", "paid_verified": "0.00",
                                  "awaiting_verification": "0.00", "balance": "123456.78"}
        assert db_session.execute(select(AuditLog).where(AuditLog.entity_type == "payment_schedule")).scalar_one()

    def test_no_schedule_without_an_accepted_total(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        oid = new_customer(client, gna)
        advance_to(db_session, oid, ProjectStage.accepted)
        assert client.get(pay_url(oid), headers=headers(gna)).json()["milestones"] == []
        r = record(client, headers(gna), oid, neft())
        assert r.status_code == 409 and r.json()["detail"]["code"] == "no_accepted_total"

    def test_accounts_edit_until_a_payment_is_recorded(self, client, db_session, onboarding_ready):
        gna, oid = accepted_customer(client, db_session)
        url = f"{DASH}/onboardings/{oid}/payment-schedule"
        new = {"milestones": [{"label": "Booking", "percent": "10", "trigger": "accepted"},
                              {"label": "Before dispatch", "percent": "70", "trigger": "agreement_signed"},
                              {"label": "After commissioning", "percent": "20", "trigger": "commissioned"}]}
        assert client.put(url, json=new, headers=SALES).status_code == 403
        assert client.put(url, json=new, headers=MGMT).status_code == 403
        bad = {"milestones": [{**m, "percent": "30"} for m in new["milestones"]]}
        assert client.put(url, json=bad, headers=ACCOUNTS).json()["detail"]["code"] == "invalid_schedule"
        assert client.put(url, json={"milestones": new["milestones"][:2]},
                          headers=ACCOUNTS).json()["detail"]["code"] == "invalid_schedule"
        r = client.put(url, json=new, headers=ACCOUNTS)
        assert r.status_code == 200 and [m["amount_due"] for m in r.json()["milestones"]] == \
            ["10000.00", "70000.00", "20000.00"]
        audit = db_session.execute(select(AuditLog).where(AuditLog.entity_type == "payment_schedule",
                                                          AuditLog.action == "updated")).scalar_one()
        assert audit.before_value["milestones"][0]["percent"] == "20.00"
        record(client, headers(gna), oid, neft())
        assert client.put(url, json=new, headers=ACCOUNTS).json()["detail"]["code"] == "schedule_locked"


class TestRecording:
    def test_associate_records_and_accounts_is_told(self, client, db_session, onboarding_ready, notifications):
        gna, oid = accepted_customer(client, db_session)
        r = record(client, headers(gna), oid, neft(payer_name="Asha Roy"))
        assert r.status_code == 201, r.text
        p = r.json()["payment"]
        assert (p["amount"], p["status"], p["has_proof"]) == ("20000.00", "recorded", False)
        assert r.json()["totals"]["awaiting_verification"] == "20000.00"
        [event] = notifications.of("payment_recorded")
        assert event["amount"] == "20000.00" and event["recorded_by"] == f"associate:{gna}"

    @pytest.mark.parametrize("body, code", [
        (lambda: {**neft(), "mode": "cash"}, "mode_not_enabled"),
        (lambda: {**neft(), "amount": "0"}, "invalid_amount"),
        (lambda: {**neft(), "reference": "  "}, "reference_required"),
        (lambda: cheque(bank=None), "instrument_details_required"),
        (lambda: {**cheque(), "instrument_date": None}, "instrument_details_required"),
        (lambda: {**neft(), "milestone_id": 999}, "unknown_milestone"),
    ])
    def test_rules(self, client, db_session, onboarding_ready, body, code):
        gna, oid = accepted_customer(client, db_session)
        r = record(client, headers(gna), oid, body())
        assert r.status_code == 422 and r.json()["detail"]["code"] == code

    def test_mode_list_comes_from_config(self, client, db_session, onboarding_ready, monkeypatch):
        monkeypatch.setitem(config.payments(), "enabled_modes", ["neft"])
        gna, oid = accepted_customer(client, db_session)
        assert record(client, headers(gna), oid, {**neft(), "mode": "upi"}).json()["detail"]["code"] == \
            "mode_not_enabled"

    def test_duplicate_reference(self, client, db_session, onboarding_ready):
        gna, oid = accepted_customer(client, db_session)
        first = record(client, headers(gna), oid, cheque(reference="000123", bank="State Bank of India"))
        assert first.status_code == 201
        dup = record(client, ADMIN, oid, cheque(reference=" 000 123 ", bank="STATE BANK OF INDIA."), staff=True)
        assert dup.status_code == 409 and dup.json()["detail"]["code"] == "duplicate_payment_reference"
        # Another bank's cheque with the same number is a different instrument.
        assert record(client, headers(gna), oid, cheque(reference="000123", bank="HDFC Bank")).status_code == 201
        # Cancelling the first frees its reference.
        pid = first.json()["payment"]["payment_id"]
        client.post(f"{pay_url(oid)}/{pid}/cancel", json={"reason": "Typed the wrong amount."}, headers=headers(gna))
        assert record(client, headers(gna), oid, cheque(reference="000123")).status_code == 201

    def test_cannot_exceed_the_accepted_total(self, client, db_session, onboarding_ready):
        gna, oid = accepted_customer(client, db_session, total=50000.0)
        assert record(client, headers(gna), oid, neft("40000.00")).status_code == 201
        r = record(client, headers(gna), oid, neft("10000.01", reference="UTR-2"))
        assert r.status_code == 409 and r.json()["detail"]["code"] == "exceeds_project_total"
        assert r.json()["detail"]["accepted_total"] == "50000.00"
        assert record(client, headers(gna), oid, neft("10000.00", reference="UTR-3")).status_code == 201

    def test_not_before_acceptance_and_not_when_closed(self, client, db_session, onboarding_ready):
        gna, oid = accepted_customer(client, db_session, stage=ProjectStage.proposal_issued)
        assert record(client, headers(gna), oid, neft()).json()["detail"]["code"] == "not_accepted"
        advance_to(db_session, oid, ProjectStage.accepted)
        db_session.get(CustomerOnboarding, oid).status = OnboardingStatus.closed
        db_session.commit()
        assert record(client, headers(gna), oid, neft()).json()["detail"]["code"] == "not_editable"

    def test_only_the_owner_sees_payments(self, client, db_session, onboarding_ready):
        from tests.execution_support import ALIPURDUAR
        head = make_associate(db_session, level="district_head", territory=ALIPURDUAR)
        gna, oid = accepted_customer(client, db_session)
        # The territory Preferred Associate can see the customer, but never its money.
        assert client.get(f"/api/v1/associate/onboardings/{oid}", headers=headers(head)).status_code == 200
        r = client.get(pay_url(oid), headers=headers(head))
        assert r.status_code == 403
        assert record(client, headers(head), oid, neft()).status_code == 403


class TestTransitions:
    def _paid(self, client, gna, oid, body=None):
        pid = record(client, headers(gna), oid, body or neft()).json()["payment"]["payment_id"]
        assert proof(client, gna, oid, pid).status_code == 200
        return pid

    def test_verify_writes_one_credit(self, client, db_session, onboarding_ready, stub_pdf):
        gna, oid = accepted_customer(client, db_session)
        pid = self._paid(client, gna, oid)
        assert client.post(f"{PAY}/{pid}/verify", headers=MGMT).status_code == 403
        assert client.post(f"{PAY}/{pid}/verify", headers=SALES).status_code == 403
        r = client.post(f"{PAY}/{pid}/verify", headers=ACCOUNTS)
        assert r.status_code == 200 and r.json()["status"] == "verified"
        assert r.json()["project"]["paid_verified"] == "20000.00" and r.json()["project"]["balance"] == "80000.00"
        assert client.post(f"{PAY}/{pid}/verify", headers=ACCOUNTS).json()["detail"]["code"] == "invalid_transition"
        [credit] = db_session.execute(select(PaymentLedgerEntry)).scalars().all()
        assert (credit.kind, credit.amount, credit.reverses_entry_id) == (LedgerEntryKind.credit,
                                                                          Decimal("20000.00"), None)
        body = client.get(pay_url(oid), headers=headers(gna)).json()
        assert body["milestones"][0]["due_status"] == "paid"
        assert body["payments"][0]["receipt"]["receipt_number"].startswith("GNR-")

    def test_proof_is_required_from_associates(self, client, db_session, onboarding_ready, stub_pdf):
        gna, oid = accepted_customer(client, db_session)
        pid = record(client, headers(gna), oid, neft()).json()["payment"]["payment_id"]
        item = client.get(f"{PAY}/{pid}", headers=ACCOUNTS).json()
        assert "proof_missing" in item["attention_reasons"]
        assert client.post(f"{PAY}/{pid}/verify", headers=ACCOUNTS).json()["detail"]["code"] == "proof_required"
        # Staff-recorded payments need no associate proof.
        spid = record(client, ACCOUNTS, oid, neft(reference="UTR-STAFF"), staff=True).json()["payment_id"]
        assert client.post(f"{PAY}/{spid}/verify", headers=ACCOUNTS).status_code == 200

    def test_cheque_deposit_then_verify(self, client, db_session, onboarding_ready, stub_pdf):
        gna, oid = accepted_customer(client, db_session)
        pid = self._paid(client, gna, oid, cheque())
        npid = self._paid(client, gna, oid, neft(reference="UTR-9", amount="1000.00"))
        assert client.post(f"{PAY}/{npid}/deposit", headers=ACCOUNTS).json()["detail"]["code"] == "not_an_instrument"
        r = client.post(f"{PAY}/{pid}/deposit", headers=ACCOUNTS)
        assert r.status_code == 200 and r.json()["status"] == "deposited"
        assert client.post(f"{PAY}/{pid}/deposit", headers=ACCOUNTS).json()["detail"]["code"] == "invalid_transition"
        assert client.post(f"{PAY}/{pid}/verify", headers=ACCOUNTS).json()["status"] == "verified"

    def test_dishonour_after_verification_reverses(self, client, db_session, onboarding_ready, stub_pdf,
                                                    notifications):
        gna, oid = accepted_customer(client, db_session)
        pid = self._paid(client, gna, oid, cheque())
        client.post(f"{PAY}/{pid}/verify", headers=ACCOUNTS)
        url = f"{PAY}/{pid}/dishonour"
        assert client.post(url, json={"reason": " "}, headers=ACCOUNTS).json()["detail"]["code"] == "reason_required"
        r = client.post(url, json={"reason": "Cheque returned: insufficient funds."}, headers=ACCOUNTS)
        assert r.status_code == 200 and r.json()["status"] == "dishonoured"
        assert r.json()["project"]["paid_verified"] == "0.00"
        credit, reversal = db_session.execute(select(PaymentLedgerEntry).order_by(PaymentLedgerEntry.id)).scalars()
        assert (reversal.kind, reversal.amount, reversal.reverses_entry_id) == (
            LedgerEntryKind.reversal, Decimal("-20000.00"), credit.id)
        assert client.post(url, json={"reason": "again"}, headers=ACCOUNTS).json()["detail"]["code"] == \
            "invalid_transition"
        assert notifications.of("payment_dishonoured")[0]["reason"] == "Cheque returned: insufficient funds."
        assert client.get(pay_url(oid), headers=headers(gna)).json()["milestones"][0]["due_status"] == "due"

    def test_dishonour_before_verification_writes_no_ledger(self, client, db_session, onboarding_ready):
        gna, oid = accepted_customer(client, db_session)
        pid = self._paid(client, gna, oid, cheque())
        client.post(f"{PAY}/{pid}/deposit", headers=ACCOUNTS)
        assert client.post(f"{PAY}/{pid}/dishonour", json={"reason": "Returned."}, headers=ACCOUNTS).status_code == 200
        assert db_session.execute(select(PaymentLedgerEntry)).first() is None

    def test_cancelling(self, client, db_session, onboarding_ready, stub_pdf):
        gna, oid = accepted_customer(client, db_session)
        mine = self._paid(client, gna, oid)
        theirs = record(client, ACCOUNTS, oid, neft(reference="UTR-ACC"), staff=True).json()["payment_id"]
        assert client.post(f"{pay_url(oid)}/{theirs}/cancel", json={"reason": "x"},
                           headers=headers(gna)).json()["detail"]["code"] == "not_yours"
        assert client.post(f"{pay_url(oid)}/{mine}/cancel", json={"reason": ""},
                           headers=headers(gna)).json()["detail"]["code"] == "reason_required"
        r = client.post(f"{pay_url(oid)}/{mine}/cancel", json={"reason": "Duplicate entry."}, headers=headers(gna))
        assert r.status_code == 200 and r.json()["status"] == "cancelled"
        client.post(f"{PAY}/{theirs}/verify", headers=ACCOUNTS)
        assert client.post(f"{PAY}/{theirs}/cancel", json={"reason": "x"},
                           headers=ACCOUNTS).json()["detail"]["code"] == "invalid_transition"   # dishonour instead
        actions = [a.action for a in db_session.execute(select(AuditLog).where(
            AuditLog.entity_type == "payment").order_by(AuditLog.id)).scalars()]
        assert actions.count("recorded") == 2 and "cancelled" in actions and "verified" in actions

    def test_the_ledger_is_append_only(self, client, db_session, onboarding_ready, stub_pdf):
        gna, oid = accepted_customer(client, db_session)
        pid = self._paid(client, gna, oid)
        client.post(f"{PAY}/{pid}/verify", headers=ACCOUNTS)
        with pytest.raises(DatabaseError):
            db_session.execute(text("UPDATE payment_ledger_entries SET amount = 1"))
        db_session.rollback()
        with pytest.raises(DatabaseError):
            db_session.execute(text("DELETE FROM payment_ledger_entries"))
        db_session.rollback()
        assert db_session.execute(select(PaymentLedgerEntry)).scalar_one().amount == Decimal("20000.00")


class TestMilestoneStatus:
    def test_due_partial_paid_and_overdue(self, client, db_session, onboarding_ready, stub_pdf, notifications):
        gna, oid = accepted_customer(client, db_session)
        pid = record(client, ACCOUNTS, oid, neft("5000.00"), staff=True).json()["payment_id"]
        client.post(f"{PAY}/{pid}/verify", headers=ACCOUNTS)
        m1 = client.get(pay_url(oid), headers=headers(gna)).json()["milestones"][0]
        assert (m1["due_status"], m1["paid_toward"], m1["outstanding"], m1["overdue"]) == (
            "partially_paid", "5000.00", "15000.00", False)
        from tests.execution_support import days_ago
        days_ago(db_session, oid, ProjectStage.accepted, 8)
        m1 = client.get(pay_url(oid), headers=headers(gna)).json()["milestones"][0]
        assert m1["overdue"] is True
        overdue = client.get(f"{DASH}/payment-milestones", params={"needs_attention": True}, headers=ACCOUNTS).json()
        assert [(i["onboarding_id"], i["number"]) for i in overdue["items"]] == [(oid, 1)]
        url = f"{DASH}/internal/check-payment-alerts"
        assert client.post(url, headers=ADMIN).json() == {"overdue_milestones": 1, "awaiting_verification": 0,
                                                          "receipts_retried": 0}
        assert client.post(url, headers=ADMIN).json() == {"overdue_milestones": 0, "awaiting_verification": 0,
                                                          "receipts_retried": 0}
        assert len(notifications.of("payments_attention")) == 1

    def test_second_milestone_falls_due_at_its_trigger(self, client, db_session, onboarding_ready):
        # Grid Nova's terms (price list): On Order 20 · Against Material Bill 60 ·
        # Before Commissioning 20; M2 falls due at material_dispatched (P9).
        gna, oid = accepted_customer(client, db_session, stage=ProjectStage.agreement_signed)
        body = client.get(pay_url(oid), headers=headers(gna)).json()["milestones"]
        assert [(m["label"], m["percent"], m["trigger"]) for m in body] == [
            ("On Order", "20.00", "accepted"), ("Against Material Bill", "60.00", "material_dispatched"),
            ("Before Commissioning", "20.00", "inspection_net_meter")]
        assert [m["due_status"] for m in body] == ["due", "not_due", "not_due"]
        advance_to(db_session, oid, ProjectStage.material_dispatched)
        statuses = [m["due_status"] for m in client.get(pay_url(oid), headers=headers(gna)).json()["milestones"]]
        assert statuses == ["due", "due", "not_due"]

    def test_third_milestone_is_due_at_the_stage_before_commissioning(self, client, db_session, onboarding_ready):
        # Off-grid: no net-meter inspection, so "before commissioning" is `installed`.
        gna, oid = accepted_customer(client, db_session, connection_type="off_grid")
        triggers = [m["trigger"] for m in client.get(pay_url(oid), headers=headers(gna)).json()["milestones"]]
        assert triggers == ["accepted", "material_dispatched", "installed"]

    def test_commissioning_gate(self, client, db_session, onboarding_ready, stub_pdf, monkeypatch):
        from app.execution import service as execution
        from app.memory.models import CustomerOnboarding
        gna, oid = accepted_customer(client, db_session, stage=ProjectStage.inspection_net_meter)
        client.get(pay_url(oid), headers=headers(gna))          # creates the schedule
        onboarding = db_session.get(CustomerOnboarding, oid)
        assert execution.stage_blockers(db_session, onboarding, ProjectStage.commissioned) == []   # off by default
        monkeypatch.setitem(config.execution()["gates"]["commissioned"], "enabled", True)
        [blocker] = execution.stage_blockers(db_session, onboarding, ProjectStage.commissioned)
        assert blocker["code"] == "commissioning_payment_gate"
        pid = record(client, ACCOUNTS, oid, neft("100000.00"), staff=True).json()["payment_id"]
        client.post(f"{PAY}/{pid}/verify", headers=ACCOUNTS)
        assert execution.stage_blockers(db_session, onboarding, ProjectStage.commissioned) == []

    def test_dispatch_gate_counts_verified_money(self, client, db_session, onboarding_ready, stub_pdf, monkeypatch):
        monkeypatch.setitem(config.execution()["gates"]["material_dispatched"], "enabled", True)
        gna, oid = accepted_customer(client, db_session, stage=ProjectStage.feasibility_submitted,
                                     capability=AssociateCapability.sales)
        from tests.execution_support import staff_exec_url, upload
        url = f"{staff_exec_url(oid)}/material_dispatched"
        body = {"text": {"dispatch_reference": "D-1"}}
        assert client.post(url, json=body, headers=OPS).json()["detail"]["code"] == "payment_gate"
        pid = record(client, ACCOUNTS, oid, neft("79999.99"), staff=True).json()["payment_id"]
        client.post(f"{PAY}/{pid}/verify", headers=ACCOUNTS)
        assert client.post(url, json=body, headers=OPS).json()["detail"]["code"] == "payment_gate"   # 1 paisa short
        pid = record(client, ACCOUNTS, oid, neft("0.01", reference="UTR-P"), staff=True).json()["payment_id"]
        client.post(f"{PAY}/{pid}/verify", headers=ACCOUNTS)
        assert client.post(url, json=body, headers=OPS).status_code == 200


class TestStaffViews:
    def test_roles(self, client, db_session, onboarding_ready):
        gna, oid = accepted_customer(client, db_session)
        record(client, headers(gna), oid, neft())
        for who in (OPS, ENG):
            assert client.get(PAY, headers=who).status_code == 403
            assert client.get(staff_pay_url(oid), headers=who).status_code == 403
        for who in (ADMIN, MGMT, ACCOUNTS, SALES):
            assert client.get(PAY, headers=who).json()["total"] == 1
        assert record(client, MGMT, oid, neft(reference="UTR-M"), staff=True).status_code == 403
        assert record(client, SALES, oid, neft(reference="UTR-S"), staff=True).status_code == 201
        from app.memory.models import Lead
        from tests.conftest import seed_employees
        seed_employees(db_session, "emp-sales-9")
        db_session.get(Lead, db_session.get(CustomerOnboarding, oid).lead_id).assigned_employee_id = "emp-sales-9"
        db_session.commit()
        assert client.get(PAY, headers=SALES).json()["total"] == 0
        assert client.get(staff_pay_url(oid), headers=SALES).status_code == 403

    def test_filters_and_project_summary(self, client, db_session, onboarding_ready):
        gna, oid = accepted_customer(client, db_session)
        record(client, headers(gna), oid, neft())
        record(client, headers(gna), oid, cheque())
        assert client.get(PAY, params={"mode": "cheque"}, headers=ACCOUNTS).json()["total"] == 1
        assert client.get(PAY, params={"status": "verified"}, headers=ACCOUNTS).json()["total"] == 0
        assert client.get(PAY, params={"needs_attention": True}, headers=ACCOUNTS).json()["total"] == 2  # no proof
        assert client.get(PAY, params={"date_from": "2999-01-01"}, headers=ACCOUNTS).json()["total"] == 0
        body = client.get(staff_pay_url(oid), headers=ACCOUNTS).json()
        assert body["project"]["accepted_total"] == "100000.00" and body["project"]["current_stage"] == "accepted"
        assert all(isinstance(p["amount"], str) for p in body["payments"])

    def test_proof_upload_limit(self, client, db_session, onboarding_ready, monkeypatch):
        from app.api.routes import associate_payments
        monkeypatch.setattr(associate_payments, "MAX_PROOF_BYTES", 1024)
        gna, oid = accepted_customer(client, db_session)
        pid = record(client, headers(gna), oid, neft()).json()["payment"]["payment_id"]
        big = b"%PDF-1.4\n" + b"x" * (200 * 1024)
        r = client.post(f"{pay_url(oid)}/{pid}/proof", files={"file": ("p.pdf", big, "application/pdf")},
                        headers=headers(gna))
        assert r.status_code == 413
        r = client.post(f"{PAY}/{pid}/proof", files={"file": ("p.pdf", big, "application/pdf")}, headers=ACCOUNTS)
        assert r.status_code == 413


@pytest.mark.skipif(not TEST_DATABASE_URL, reason="needs real concurrent connections (PostgreSQL)")
def test_two_simultaneous_verifies_make_one_credit(client, db_session, onboarding_ready, stub_pdf, _isolated_db):
    gna, oid = accepted_customer(client, db_session)
    pid = record(client, ACCOUNTS, oid, neft(), staff=True).json()["payment_id"]
    SessionLocal = _isolated_db
    barrier = threading.Barrier(2)
    results = []

    def attempt():
        db = SessionLocal()
        try:
            barrier.wait()
            payments.verify(db, pid, "emp-acc-1")
            db.commit()
            results.append("ok")
        except AssociateError as exc:
            db.rollback()
            results.append(exc.code)
        finally:
            db.close()

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    [t.start() for t in threads]
    [t.join(timeout=30) for t in threads]
    assert sorted(results) == ["invalid_transition", "ok"]
    assert len(db_session.execute(select(PaymentLedgerEntry)).scalars().all()) == 1
    assert db_session.execute(select(PaymentMilestone)).first() is not None
    assert db_session.get(Payment, pid) is not None
