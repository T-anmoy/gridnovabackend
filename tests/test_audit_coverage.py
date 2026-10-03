"""Phase 4A Step 8 — every new mutating action leaves an audit row. One flow drives each
action once through the real endpoints, then the audit log must hold every expected
(entity_type, action) pair."""
from datetime import datetime, timezone

from sqlalchemy import select

from app.memory.models import AssociateCapability, AuditLog, PaymentReceipt, ProjectStage
from scripts import seed_dummy_data
from tests.execution_support import (  # noqa: F401 — fixtures
    ADMIN, CAMERA, DASH, OPS, advance_to, exec_url, headers, jpeg, kyc_ready, make_associate, notifications,
    onboarding_ready, staff_exec_url, territories, upload,
)
from tests.payments_support import ACCOUNTS, PAY, accepted_customer, cheque, neft, pay_url, proof, record, stub_pdf  # noqa

EXPECTED = {
    ("placeholder", "resolved"),
    ("project_execution", "started"), ("project_execution", "file_uploaded"),
    ("project_execution", "register_verified"), ("project_execution", "dcr_override"),
    ("stage_submission", "submitted"), ("stage_submission", "verified"), ("stage_submission", "rejected"),
    ("project", "stage_feasibility_submitted"),
    ("installed_unit", "added"), ("installed_unit", "updated"), ("installed_unit", "voided"),
    ("payment_schedule", "created"), ("payment_schedule", "updated"),
    ("payment", "recorded"), ("payment", "proof_attached"), ("payment", "deposited"), ("payment", "verified"),
    ("payment", "dishonoured"), ("payment", "cancelled"),
    ("project_execution", "admin_override"), ("project_execution", "mode_changed"),
    ("payment", "segregation_override"),
    ("payment_receipt", "numbered"), ("payment_receipt", "resend"), ("payment_receipt", "released"),
}


def test_every_new_mutation_is_audited(client, db_session, onboarding_ready, stub_pdf, notifications, monkeypatch):
    client.post(f"{DASH}/placeholders/A1/resolve", json={"note": "Confirmed."}, headers=ADMIN)
    seed_dummy_data.seed(db_session, "development")
    from app.memory.models import Equipment
    catalogue = {e.model_name: e.id for e in db_session.execute(select(Equipment)).scalars()}

    # Execution: submit, reject, resubmit, verify; register add/update/void/verify; DCR override.
    gna, oid = accepted_customer(client, db_session, stage=ProjectStage.agreement_signed,
                                 capability=AssociateCapability.execution)
    assert upload(client, headers(gna), exec_url(oid), photo_slot="earthing", content=jpeg(seed=5),
                  **CAMERA).status_code == 201
    body = {"stage": "feasibility_submitted", "text": {"feasibility_reference": "R1"}}
    sid = client.post(f"{exec_url(oid)}/submissions", json=body, headers=headers(gna)).json()["submission_id"]
    client.post(f"{DASH}/execution/submissions/{sid}/reject", json={"note": "Wrong."}, headers=OPS)
    sid = client.post(f"{exec_url(oid)}/submissions", json=body, headers=headers(gna)).json()["submission_id"]
    assert client.post(f"{DASH}/execution/submissions/{sid}/verify", json={}, headers=OPS).status_code == 200
    unit = {"category": "module", "equipment_id": catalogue["DUMMY-M540-DCR"], "serial_number": "AUD-0001",
            "rating_wp": "540", "is_dcr": True}
    uid = client.post(f"{exec_url(oid)}/units",
                      json=unit, headers=headers(gna)).json()["unit"]["unit_id"]
    client.patch(f"{exec_url(oid)}/units/{uid}", json={"rating_wp": "545"}, headers=headers(gna))
    client.post(f"{exec_url(oid)}/units/{uid}/void", json={"reason": "Typo."}, headers=headers(gna))
    client.post(f"{exec_url(oid)}/units/bulk", headers=headers(gna), json={"units": [
        {**unit, "serial_number": "AUD-0002"},
        {"category": "inverter", "equipment_id": catalogue["DUMMY-INV-5K"], "serial_number": "AUD-INV-1",
         "rating_kw": "5"}]})
    assert client.post(f"{staff_exec_url(oid)}/register/verify", headers=OPS).status_code == 200
    assert client.post(f"{staff_exec_url(oid)}/dcr-override", json={"reason": "Checked."},
                       headers=ADMIN).status_code == 200

    # Payments: schedule edit, record, proof, deposit, verify, dishonour, cancel; receipts.
    gna2, oid2 = accepted_customer(client, db_session)
    client.put(f"{DASH}/onboardings/{oid2}/payment-schedule", headers=ACCOUNTS, json={"milestones": [
        {"label": "A", "percent": "30", "trigger": "accepted"}, {"label": "B", "percent": "50", "trigger": "accepted"},
        {"label": "C", "percent": "20", "trigger": "accepted"}]})
    pid = record(client, headers(gna2), oid2, cheque()).json()["payment"]["payment_id"]
    proof(client, gna2, oid2, pid)
    client.post(f"{PAY}/{pid}/deposit", headers=ACCOUNTS)
    assert client.post(f"{PAY}/{pid}/verify", headers=ACCOUNTS).status_code == 200
    client.post(f"{PAY}/{pid}/dishonour", json={"reason": "Returned."}, headers=ACCOUNTS)
    other = record(client, headers(gna2), oid2, neft(reference="UTR-AUD")).json()["payment"]["payment_id"]
    client.post(f"{pay_url(oid2)}/{other}/cancel", json={"reason": "Duplicate."}, headers=headers(gna2))
    receipt = db_session.execute(select(PaymentReceipt)).scalar_one()
    receipt.emailed_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
    db_session.commit()
    client.post(f"{PAY}/{pid}/receipt/resend", headers=ACCOUNTS)
    from app.execution import config
    receipt_cfg = config.payments()["receipt"]
    monkeypatch.setitem(receipt_cfg, "company", {**receipt_cfg["company"], "gstin": "G", "bank_name": "B",
                                                 "account_number": "1", "ifsc": "I"})
    monkeypatch.setitem(receipt_cfg, "wording", {"heading": "Receipt", "footer": "Not a tax invoice."})
    client.post(f"{DASH}/payment-receipts/release-held", headers=ACCOUNTS)

    # Follow-ups: admin override, mode switch (on a third, associate-mode project), and an
    # admin verifying a payment they recorded.
    gna3, oid3 = accepted_customer(client, db_session, stage=ProjectStage.agreement_signed,
                                   capability=AssociateCapability.execution)
    assert client.post(f"{staff_exec_url(oid3)}/feasibility_submitted", headers=ADMIN, json={
        "text": {"feasibility_reference": "R9"}, "admin_override_reason": "Associate unreachable."}).status_code == 200
    assert client.post(f"{staff_exec_url(oid3)}/mode", headers=ADMIN, json={
        "execution_mode": "grid_nova", "reason": "Taking over."}).status_code == 200
    own = client.post(f"{DASH}/onboardings/{oid2}/payments", headers=ADMIN, json=neft(reference="UTR-OWN"))
    assert client.post(f"{PAY}/{own.json()['payment_id']}/verify", headers=ADMIN,
                       json={"reason": "Paid in person."}).status_code == 200

    seen = {(a.entity_type, a.action) for a in db_session.execute(select(AuditLog)).scalars()}
    assert EXPECTED <= seen, f"not audited: {sorted(EXPECTED - seen)}"
