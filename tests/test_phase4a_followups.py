"""Phase 4A follow-ups: payments keyed by project, admin execution override and mode
switch, voided-serial flag, segregation of duties on verification, re-presenting a
bounced cheque."""
import csv
import io

import pytest
from sqlalchemy import select

from app.memory.models import (
    AssociateCapability, AuditLog, CustomerOnboarding, Equipment, InstalledUnit, Payment, PaymentMilestone,
    PaymentLedgerEntry, PaymentReceipt, ProjectStage, ProjectStageEvent, StageSubmission,
)
from app.payments import receipts
from scripts import seed_dummy_data
from tests.execution_support import QUEUE, associate_in_execution, exec_url, staff_exec_url
from tests.payments_support import (  # noqa: F401 — fixtures
    ACCOUNTS, ADMIN, DASH, OPS, PAY, RECORDER, SALES, accepted_customer, cheque, headers, kyc_ready, make_associate,
    neft, new_customer, notifications, onboarding_ready, pay_url, proof, record, stub_pdf, territories,
)


# ── 1. Payments are keyed by project ─────────────────────────────────────────────

class TestProjectKey:
    def test_columns(self):
        for model in (Payment, PaymentMilestone):
            assert model.__table__.c.project_id.nullable is False
            assert model.__table__.c.onboarding_id.nullable is True
        ledger = PaymentLedgerEntry
        assert ledger.__table__.c.project_id.nullable is False and "onboarding_id" not in ledger.__table__.c

    def test_a_payment_with_no_onboarding_works_end_to_end(self, client, db_session, onboarding_ready, stub_pdf):
        gna, oid = accepted_customer(client, db_session)
        onboarding = db_session.get(CustomerOnboarding, oid)
        payment = Payment(project_id=onboarding.project_id, onboarding_id=None, amount=1500, mode="neft",
                          reference="UTR-NOOB", reference_normalized="UTR-NOOB", bank_name_normalized="",
                          status="recorded", recorded_by="emp-acc-2")
        db_session.add(payment)
        db_session.commit()
        item = client.get(f"{PAY}/{payment.id}", headers=ACCOUNTS).json()
        assert item["onboarding_id"] is None and item["project"]["project_reference_id"]
        assert item["project"]["accepted_total"] is None and item["project"]["paid_verified"] == "0.00"
        assert client.get(f"{PAY}/{payment.id}", headers=SALES).status_code == 403   # nothing to scope Sales by
        r = client.post(f"{PAY}/{payment.id}/verify", headers=ACCOUNTS)
        assert r.status_code == 200, r.text
        assert r.json()["project"]["paid_verified"] == "1500.00"
        db_session.expire_all()   # the API verified it in another session
        receipt = db_session.execute(select(PaymentReceipt)).scalar_one()
        html = receipts.receipt_html(db_session, receipt)
        assert "Rs 1,500.00" in html
        rows = list(csv.DictReader(io.StringIO(client.get(f"{DASH}/exports/payments.csv",
                                                          headers=ACCOUNTS).content.decode()[1:])))
        assert [r["amount_inr"] for r in rows] == ["1500.00"] and rows[0]["customer_name"]
        assert client.post(f"{DASH}/internal/check-payment-alerts", headers=ADMIN).status_code == 200


# ── 2. Admin override and mode switch ────────────────────────────────────────────

BODY = {"text": {"feasibility_reference": "WB/ADM/1"}}


class TestAdminOverride:
    def test_only_an_admin_with_a_reason(self, client, db_session, onboarding_ready):
        gna, oid = associate_in_execution(client, db_session)
        url = f"{staff_exec_url(oid)}/feasibility_submitted"
        r = client.post(url, json={**BODY, "admin_override_reason": "Associate unreachable."}, headers=OPS)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "execution_mode_associate"
        for reason in (None, "   "):
            r = client.post(url, json={**BODY, "admin_override_reason": reason}, headers=ADMIN)
            assert r.status_code == 422 and r.json()["detail"] == {
                "code": "reason_required", "field": "admin_override_reason", "message": r.json()["detail"]["message"]}
        assert db_session.execute(select(ProjectStageEvent).where(
            ProjectStageEvent.stage == ProjectStage.feasibility_submitted)).first() is None

    def test_records_audits_and_supersedes_a_pending_submission(self, client, db_session, onboarding_ready):
        gna, oid = associate_in_execution(client, db_session)
        sid = client.post(f"{exec_url(oid)}/submissions", headers=headers(gna),
                          json={"stage": "feasibility_submitted", **BODY}).json()["submission_id"]
        r = client.post(f"{staff_exec_url(oid)}/feasibility_submitted", headers=ADMIN,
                        json={**BODY, "admin_override_reason": "Associate unreachable; filed by phone."})
        assert r.status_code == 200, r.text
        stage = next(s for s in r.json()["stages"] if s["stage"] == "feasibility_submitted")
        assert stage["recorded"] and stage["via"] == "admin_override" and r.json()["pending_submission"] is None
        event = db_session.execute(select(ProjectStageEvent).where(
            ProjectStageEvent.stage == ProjectStage.feasibility_submitted)).scalar_one()
        assert event.evidence["admin_override_reason"] == "Associate unreachable; filed by phone."
        audit = db_session.execute(select(AuditLog).where(AuditLog.action == "admin_override")).scalar_one()
        assert (audit.entity_type, audit.performed_by) == ("project_execution", "emp-admin-1")
        assert audit.after_value["stage"] == "feasibility_submitted" and "unreachable" in audit.after_value["reason"]
        sub = db_session.get(StageSubmission, sid)
        assert sub.status.value == "rejected" and sub.review_note.startswith("Superseded")
        # The associate sees how it was recorded.
        mine = client.get(exec_url(oid), headers=headers(gna)).json()
        assert next(s for s in mine["stages"] if s["stage"] == "feasibility_submitted")["via"] == "admin_override"

    def test_still_needs_the_stage_to_be_in_order_with_evidence(self, client, db_session, onboarding_ready):
        gna, oid = associate_in_execution(client, db_session)
        url = f"{staff_exec_url(oid)}/material_dispatched"
        r = client.post(url, json={"text": {"dispatch_reference": "D"}, "admin_override_reason": "x"}, headers=ADMIN)
        assert r.json()["detail"]["code"] == "stage_out_of_order"
        r = client.post(f"{staff_exec_url(oid)}/feasibility_submitted", json={"admin_override_reason": "x"},
                        headers=ADMIN)
        assert r.json()["detail"]["code"] == "evidence_missing"


class TestModeSwitch:
    URL = lambda self, oid: f"{staff_exec_url(oid)}/mode"   # noqa: E731

    def test_admin_only_with_a_reason(self, client, db_session, onboarding_ready):
        gna, oid = associate_in_execution(client, db_session)
        body = {"execution_mode": "grid_nova", "reason": "Associate left the programme."}
        for who in (OPS, SALES):
            assert client.post(self.URL(oid), json=body, headers=who).status_code == 403
        assert client.post(self.URL(oid), json={**body, "reason": " "}, headers=ADMIN).json()["detail"]["code"] == \
            "reason_required"
        assert client.post(self.URL(oid), json={**body, "execution_mode": "associate"},
                           headers=ADMIN).status_code == 422
        r = client.post(self.URL(oid), json=body, headers=ADMIN)
        assert r.status_code == 200 and r.json()["execution_mode"] == "grid_nova"
        assert r.json()["can_record"] is True
        audit = db_session.execute(select(AuditLog).where(AuditLog.action == "mode_changed")).scalar_one()
        assert audit.before_value == {"execution_mode": "associate"}
        assert audit.after_value == {"execution_mode": "grid_nova", "reason": "Associate left the programme."}
        again = client.post(self.URL(oid), json=body, headers=ADMIN)
        assert again.status_code == 409 and again.json()["detail"]["code"] == "already_grid_nova"

    def test_after_the_switch_roles_follow_the_new_mode(self, client, db_session, onboarding_ready):
        gna, oid = associate_in_execution(client, db_session)
        client.post(self.URL(oid), json={"execution_mode": "grid_nova", "reason": "Taking over."}, headers=ADMIN)
        r = client.post(f"{exec_url(oid)}/submissions", headers=headers(gna),
                        json={"stage": "feasibility_submitted", **BODY})
        assert r.status_code == 409 and r.json()["detail"]["code"] == "execution_mode_grid_nova"
        r = client.post(f"{staff_exec_url(oid)}/feasibility_submitted", json=BODY, headers=OPS)
        assert r.status_code == 200 and client.get(exec_url(oid), headers=headers(gna)).json()["can_submit"] is False
        event = db_session.execute(select(ProjectStageEvent).where(
            ProjectStageEvent.stage == ProjectStage.feasibility_submitted)).scalar_one()
        assert event.evidence["via"] == "direct"

    def test_mode_is_not_mistaken_for_a_stage(self, client, db_session, onboarding_ready):
        _, oid = associate_in_execution(client, db_session)
        r = client.post(self.URL(oid), json={"text": {}}, headers=ADMIN)   # a stage body is wrong for THIS route
        assert r.status_code == 422 and isinstance(r.json()["detail"], list)   # request validation, not stage handling


# ── 3. Voided serials ────────────────────────────────────────────────────────────

@pytest.fixture
def catalogue(db_session):
    seed_dummy_data.seed(db_session, "development")
    return {e.model_name: e.id for e in db_session.execute(select(Equipment)).scalars()}


def _module(catalogue, serial):
    return {"category": "module", "equipment_id": catalogue["DUMMY-M540-DCR"], "serial_number": serial,
            "rating_wp": "540", "is_dcr": True}


class TestVoidedSerials:
    def test_raw_text_is_kept_and_the_serial_is_reusable(self, client, db_session, onboarding_ready, catalogue):
        gna, oid = associate_in_execution(client, db_session)
        unit = client.post(f"{exec_url(oid)}/units", json=_module(catalogue, " sn-77 a "),
                           headers=headers(gna)).json()["unit"]
        client.post(f"{exec_url(oid)}/units/{unit['unit_id']}/void", json={"reason": "Typo."}, headers=headers(gna))
        row = db_session.get(InstalledUnit, unit["unit_id"])
        assert (row.serial_number, row.serial_normalized, row.voided_serial_normalized) == ("sn-77 a", None, "SN-77A")
        # The same project re-entering it is not "previously voided elsewhere".
        again = client.post(f"{staff_exec_url(oid)}/units", json=_module(catalogue, "SN-77A"), headers=OPS)
        assert again.status_code == 201 and again.json()["unit"]["previously_voided"] is False
        assert "previously_voided" not in again.json()["register"]["flags"]

    def test_a_different_project_raises_a_staff_only_flag(self, client, db_session, onboarding_ready, catalogue):
        first, first_oid = associate_in_execution(client, db_session)
        unit = client.post(f"{exec_url(first_oid)}/units", json=_module(catalogue, "DUP-1000"),
                           headers=headers(first)).json()["unit"]
        client.post(f"{exec_url(first_oid)}/units/{unit['unit_id']}/void", json={"reason": "Wrong."},
                    headers=headers(first))
        second, second_oid = associate_in_execution(client, db_session)
        r = client.post(f"{exec_url(second_oid)}/units", json=_module(catalogue, " dup-1000"), headers=headers(second))
        assert r.status_code == 201                                   # never blocking
        # The associate sees nothing of it.
        assert "previously_voided" not in r.json()["unit"] and "previously_voided" not in r.json()["register"]["flags"]
        assert "previously_voided" not in client.get(exec_url(second_oid), headers=headers(second)).json()["flags"]
        # Staff do, on the unit, the register, the project flags and the queue.
        for who in (OPS, ADMIN):
            detail = client.get(staff_exec_url(second_oid), headers=who).json()
            assert "previously_voided" in detail["flags"] and "previously_voided" in detail["register"]["flags"]
            assert detail["register"]["units"][0]["previously_voided"] is True
        item = next(i for i in client.get(QUEUE, headers=OPS).json()["items"] if i["onboarding_id"] == second_oid)
        assert "previously_voided" in item["flags"] and item["needs_attention"] is True
        # The first project's own (voided) unit is not itself flagged.
        assert client.get(staff_exec_url(first_oid), headers=OPS).json()["flags"] == []


# ── 6. Segregation of duties ─────────────────────────────────────────────────────

class TestSegregationOfDuties:
    def _recorded_by(self, client, who, oid, **body):
        return client.post(f"{DASH}/onboardings/{oid}/payments", json=neft(**body), headers=who).json()["payment_id"]

    def test_the_recorder_cannot_verify(self, client, db_session, onboarding_ready, stub_pdf):
        _, oid = accepted_customer(client, db_session)
        pid = self._recorded_by(client, ACCOUNTS, oid)            # recorded and verified by the same person
        r = client.post(f"{PAY}/{pid}/verify", headers=ACCOUNTS)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "self_verification"
        r = client.post(f"{PAY}/{pid}/verify", json={"reason": "I am the only one here."}, headers=ACCOUNTS)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "self_verification"   # a reason doesn't help
        assert client.post(f"{PAY}/{pid}/verify", headers=RECORDER).status_code == 200      # a colleague can

    def test_sales_records_accounts_verifies_and_so_do_associate_payments(self, client, db_session, onboarding_ready,
                                                                           stub_pdf):
        gna, oid = accepted_customer(client, db_session)
        by_sales = self._recorded_by(client, SALES, oid, reference="UTR-S1")
        assert client.post(f"{PAY}/{by_sales}/verify", headers=ACCOUNTS).status_code == 200
        mine = client.post(pay_url(oid), json=neft(reference="UTR-A1"), headers=headers(gna)).json()["payment"]
        proof(client, gna, oid, mine["payment_id"])
        assert client.post(f"{PAY}/{mine['payment_id']}/verify", headers=ACCOUNTS).status_code == 200

    def test_an_admin_may_with_a_reason_and_it_is_audited(self, client, db_session, onboarding_ready, stub_pdf):
        _, oid = accepted_customer(client, db_session)
        pid = self._recorded_by(client, ADMIN, oid)
        r = client.post(f"{PAY}/{pid}/verify", headers=ADMIN)
        assert r.status_code == 422 and r.json()["detail"]["code"] == "reason_required"
        assert client.post(f"{PAY}/{pid}/verify", json={"reason": " "}, headers=ADMIN).status_code == 422
        r = client.post(f"{PAY}/{pid}/verify", json={"reason": "Customer paid in person at the office."},
                        headers=ADMIN)
        assert r.status_code == 200 and r.json()["status"] == "verified"
        rows = db_session.execute(select(AuditLog).where(AuditLog.entity_type == "payment",
                                                         AuditLog.entity_id == str(pid))).scalars().all()
        override = next(a for a in rows if a.action == "segregation_override")
        assert override.performed_by == "emp-admin-1" and override.after_value == {
            "recorded_by": "emp-admin-1", "reason": "Customer paid in person at the office."}
        verified = next(a for a in rows if a.action == "verified")
        assert verified.after_value["segregation_override"] == "Customer paid in person at the office."

    def test_an_ordinary_verification_carries_no_override(self, client, db_session, onboarding_ready, stub_pdf):
        _, oid = accepted_customer(client, db_session)
        pid = self._recorded_by(client, SALES, oid)
        client.post(f"{PAY}/{pid}/verify", headers=ADMIN)
        actions = {a.action for a in db_session.execute(select(AuditLog).where(
            AuditLog.entity_id == str(pid))).scalars()}
        assert "segregation_override" not in actions


# ── 7. Re-presenting a bounced cheque ────────────────────────────────────────────

class TestRepresentedCheque:
    def _bounce(self, client, gna, oid, reference="000777"):
        pid = client.post(pay_url(oid), json=cheque(reference=reference), headers=headers(gna)).json()["payment"][
            "payment_id"]
        client.post(f"{PAY}/{pid}/dishonour", json={"reason": "Returned: insufficient funds."}, headers=ACCOUNTS)
        return pid

    def test_same_cheque_again_is_linked_to_the_bounce(self, client, db_session, onboarding_ready):
        gna, oid = accepted_customer(client, db_session)
        first = self._bounce(client, gna, oid)
        r = client.post(pay_url(oid), json=cheque(reference=" 000 777 "), headers=headers(gna))
        assert r.status_code == 201, r.text
        second = r.json()["payment"]
        assert second["previous_payment_id"] == first and second["status"] == "recorded"
        assert db_session.get(Payment, first).previous_payment_id is None
        audit = db_session.execute(select(AuditLog).where(AuditLog.entity_id == str(second["payment_id"]),
                                                          AuditLog.action == "recorded")).scalar_one()
        assert audit.after_value["previous_payment_id"] == first

    def test_active_and_verified_payments_still_block_duplicates(self, client, db_session, onboarding_ready,
                                                                  stub_pdf):
        gna, oid = accepted_customer(client, db_session)
        self._bounce(client, gna, oid)
        second = client.post(pay_url(oid), json=cheque(reference="000777"), headers=headers(gna)).json()["payment"]
        dup = client.post(pay_url(oid), json=cheque(reference="000777"), headers=headers(gna))
        assert dup.status_code == 409 and dup.json()["detail"]["code"] == "duplicate_payment_reference"
        proof(client, gna, oid, second["payment_id"])
        client.post(f"{PAY}/{second['payment_id']}/verify", headers=ACCOUNTS)
        dup = client.post(pay_url(oid), json=cheque(reference="000777"), headers=headers(gna))
        assert dup.status_code == 409 and dup.json()["detail"]["code"] == "duplicate_payment_reference"

    def test_not_on_another_project(self, client, db_session, onboarding_ready):
        gna, oid = accepted_customer(client, db_session)
        self._bounce(client, gna, oid)
        gna2, oid2 = accepted_customer(client, db_session)
        r = client.post(pay_url(oid2), json=cheque(reference="000777"), headers=headers(gna2))
        assert r.status_code == 409 and r.json()["detail"]["code"] == "duplicate_payment_reference"

    def test_a_cancelled_one_is_reusable_but_unlinked(self, client, db_session, onboarding_ready):
        gna, oid = accepted_customer(client, db_session)
        pid = client.post(pay_url(oid), json=cheque(reference="000888"), headers=headers(gna)).json()["payment"][
            "payment_id"]
        client.post(f"{pay_url(oid)}/{pid}/cancel", json={"reason": "Wrong amount."}, headers=headers(gna))
        r = client.post(pay_url(oid), json=cheque(reference="000888"), headers=headers(gna))
        assert r.status_code == 201 and r.json()["payment"]["previous_payment_id"] is None

    def test_a_bounce_does_not_count_toward_the_total(self, client, db_session, onboarding_ready):
        gna, oid = accepted_customer(client, db_session, total=30000.0)
        self._bounce(client, gna, oid)                               # 20,000 bounced
        assert client.post(pay_url(oid), json=cheque(amount="30000.00", reference="000777"),
                           headers=headers(gna)).status_code == 201
