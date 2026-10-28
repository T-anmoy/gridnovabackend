"""BE-19 — one associate, application to paid commission, through the real routes.

apply → Network Manager approves → Clerk invitation accepted (first sign-in binds) →
onboard a customer → site survey → formal proposal drafted, approved, handed over and
accepted in person → agreement → payments 20/60/20 recorded by the associate and
verified by Accounts → execution (a stage submitted, sent back, resubmitted, verified)
→ handover → commission accrued and approved → payout batch prepared and approved by a
second person → bank file by Accounts → the associate's own statement shows it paid.

The negative scenario: the associate is suspended mid-way — access refused (Clerk banned),
their customer moved to another associate, their commission frozen out of the batch.

Shortcuts, each covered by its own tests: later execution stages are recorded through
app/onboarding/progress.record (their photo/document evidence rules are tested in
test_execution_stages.py); commission rates are TEST values (no real schedule exists,
registry C5-RATES); time is moved to the next month for the payout."""
from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlalchemy import select

import app.notifications.notifier as notifier_module
from app.auth import dependencies as deps
from app.memory.models import (
    Associate, CommissionEntry, CustomerOnboarding, ProjectStage, Proposal, ProposalStatus,
)
from tests.associate_support import ASSOCIATE_APPLY, associate_form, submit
from tests.commission_support import ACCOUNTS_2, approve_all, publish
from tests.execution_support import Recorder, advance_to, exec_url, staff_exec_url
from tests.onboarding_support import (  # noqa: F401 — fixtures
    ADMIN, BASE, DASH, ENG, SALES, complete_survey, confirm_site_location, headers, kyc_ready, onboarding_body,
    onboarding_ready, review_survey, territories, upload_required_documents,
)
from tests.payments_support import stub_pdf  # noqa: F401 — fixture
from tests.proposal_document_support import complete_document

NM = {"X-Employee-Id": "emp-nm-1", "X-Employee-Role": "network_manager"}
OPS = {"X-Employee-Id": "emp-ops-1", "X-Employee-Role": "operations"}
ACCOUNTS = {"X-Employee-Id": "emp-acc-1", "X-Employee-Role": "accounts"}
PDF = b"%PDF-1.4\n" + b"%" * 64
ASSOC = "/api/v1/associate"


@pytest.fixture
def world(client, db_session, onboarding_ready, sendable_proposal_content, stub_pdf, monkeypatch):
    from tests.conftest import seed_employees
    seed_employees(db_session, "emp-admin-1", "emp-sales-1", "emp-eng-1")
    rec = Recorder()
    monkeypatch.setattr(notifier_module, "notifier", rec)
    invites, bans = [], []
    monkeypatch.setattr("app.auth.clerk_client.create_associate_invitation",
                        lambda email, meta, redirect="": invites.append((email, meta)) or (True, "inv_e2e"))
    monkeypatch.setattr("app.auth.clerk_client.ban_user", lambda user: bans.append(user) or True)
    with patch("app.associates.documents.render_pdf", return_value=b"%PDF-stub"):
        yield {"emails": rec, "invites": invites, "bans": bans}


def _ok(r, code=200):
    assert r.status_code == code, f"{r.status_code} {r.text[:400]}"
    return r.json()


def _mails(rec, event):
    return [p for e, p in rec.events if e == "associate_email" and p.get("event_type") == event]


def join_network(client, monkeypatch, world, n=1) -> str:
    """Apply → Network Manager reviews and approves → invitation → first sign-in."""
    app_id = _ok(submit(client, ASSOCIATE_APPLY, associate_form(n, capability="execution")), 201)["application_id"]
    assert _mails(world["emails"], "associate_application_received")
    base = f"{DASH}/associate-applications/associate/{app_id}"
    _ok(client.get(f"{base}/documents/pan_card", headers=NM))           # the KYC file, by signed link
    _ok(client.post(f"{base}/status", json={"status": "under_review"}, headers=NM))
    body = _ok(client.post(f"{base}/approve", json={"verification_call_completed": True,
                                                     "verification_call_note": "Called; identity confirmed."},
                           headers=NM))
    gna = body["associate"]["associate_id"]
    detail = _ok(client.get(f"{DASH}/associates/{gna}", headers=NM))
    assert detail["clerk_invite_status"] == "sent" and detail["documents"]["id_card"]
    assert world["invites"][-1] == (f"applicant{n}@example.test", {"principal": "associate", "associate_id": gna})
    # The invitation is accepted: Clerk copies the metadata onto the new user; the first
    # signed-in request binds that Clerk user to the associate.
    monkeypatch.setattr(deps, "_verify_clerk_jwt", lambda token: {
        "sub": f"user_e2e_{n}", "public_metadata": {"principal": "associate", "associate_id": gna}})
    me = _ok(client.get(f"{ASSOC}/me", headers={"Authorization": "Bearer t"}))
    assert me["associate_id"] == gna and me["status"] == "active"
    return gna


def onboard_to_agreement(client, db_session, gna) -> tuple[int, int]:
    """Customer → survey → formal proposal approved and accepted in person → agreement signed."""
    o = _ok(client.post(BASE, json=onboarding_body(customer_email="cust@example.test",
                                                   connection_phase="single_phase"), headers=headers(gna)), 201)
    oid = o["onboarding_id"]
    reference = db_session.get(CustomerOnboarding, oid).project_id
    assert reference                                      # the client's project (unique reference id)
    _ok(client.post(f"{BASE}/{oid}/estimates", json={}, headers=headers(gna)), 201)   # approximate, unsigned
    upload_required_documents(client, gna, oid)
    confirm_site_location(client, gna, oid)
    complete_survey(client, gna, oid)
    _ok(client.post(f"{BASE}/{oid}/submit", headers=headers(gna)))
    _ok(client.post(f"{DASH}/onboardings/{oid}/verification-call",
                    json={"verification_call_completed": True, "note": "Customer confirmed"}, headers=SALES))
    _ok(client.patch(f"/api/v1/proposal-inputs/{o['site_assessment']['proposal_inputs_id']}/review",
                     json={"status": "approved_for_proposal"}, headers=ENG))
    _ok(review_survey(client, oid))
    _ok(client.post(f"{BASE}/{oid}/request-formal-proposal", headers=headers(gna)))
    pid = _ok(client.post(f"{DASH}/onboardings/{oid}/formal-proposal", headers=SALES))["formal_proposal"]["proposal_id"]
    complete_document(client, pid)
    _ok(client.post(f"{DASH}/proposals/{pid}/approve", headers=ADMIN))
    seen = _ok(client.get(f"{BASE}/{oid}/formal-proposal", headers=headers(gna)))
    assert seen["ready_to_hand_over"] is True
    from app.associates.service import today_ist
    _ok(client.post(f"{BASE}/{oid}/proposal/in-person-acceptance", headers=headers(gna), json={
        "proposal_id": pid, "delivered_on": today_ist().isoformat(), "note": "Explained at the customer's home.",
        "reference": "Printed copy v1 handed over"}))
    assert db_session.get(Proposal, pid).status == ProposalStatus.accepted
    _ok(client.post(f"{DASH}/onboardings/{oid}/agreement", headers=SALES))
    _ok(client.post(f"{BASE}/{oid}/agreement/signed", data={"stamp_paper_number": "WB 123/45",
                                                             "stamp_paper_date": today_ist().isoformat()},
                    files={"file": ("signed.pdf", PDF, "application/pdf")}, headers=headers(gna)))
    return oid, pid


def pay(client, gna, oid, number: int, reference: str) -> str:
    """The associate records milestone `number` with proof; Accounts verifies it."""
    schedule = _ok(client.get(f"{DASH}/onboardings/{oid}/payment-schedule", headers=ACCOUNTS))
    milestone = next(m for m in schedule["milestones"] if m["number"] == number)
    amount = milestone["amount_due"]
    r = _ok(client.post(f"{ASSOC}/onboardings/{oid}/payments", headers=headers(gna),
                        json={"amount": amount, "mode": "neft", "reference": reference,
                              "milestone_id": milestone["milestone_id"]}), 201)
    payment_id = r["payment"]["payment_id"]
    _ok(client.post(f"{ASSOC}/onboardings/{oid}/payments/{payment_id}/proof", headers=headers(gna),
                    files={"file": ("proof.pdf", PDF, "application/pdf")}))
    _ok(client.post(f"{DASH}/payments/{payment_id}/verify", headers=ACCOUNTS))
    return amount


def test_application_to_paid_commission(client, db_session, world, monkeypatch):
    from app.commissions import periods
    from tests.commission_support import after_month_end, this_period

    gna = join_network(client, monkeypatch, world)
    publish(db_session, overrides={"personal_sale.associate.execution": "10"})
    oid, pid = onboard_to_agreement(client, db_session, gna)
    assert _mails(world["emails"], "associate_proposal_ready")

    schedule = _ok(client.get(f"{DASH}/onboardings/{oid}/payment-schedule", headers=ACCOUNTS))
    assert [Decimal(m["percent"]) for m in schedule["milestones"]] == [20, 60, 20]
    paid = [pay(client, gna, oid, 1, "UTR-E2E-1")]

    # Execution, run by the associate: a submission sent back, resubmitted, verified.
    sub = _ok(client.post(f"{exec_url(oid)}/submissions", headers=headers(gna),
                          json={"stage": "feasibility_submitted", "text": {"feasibility_reference": "WRONG"}}), 201)
    _ok(client.post(f"{DASH}/execution/submissions/{sub['submission_id']}/reject",
                    json={"note": "Reference does not match the DISCOM receipt"}, headers=OPS))
    assert _mails(world["emails"], "associate_stage_rejected")
    sub = _ok(client.post(f"{exec_url(oid)}/submissions", headers=headers(gna),
                          json={"stage": "feasibility_submitted", "text": {"feasibility_reference": "WBSEDCL/1"}}), 201)
    _ok(client.post(f"{DASH}/execution/submissions/{sub['submission_id']}/verify", json={}, headers=OPS))
    advance_to(db_session, oid, ProjectStage.material_dispatched, by="emp-ops-1")
    paid.append(pay(client, gna, oid, 2, "UTR-E2E-2"))
    advance_to(db_session, oid, ProjectStage.commissioned, by="emp-ops-1")
    paid.append(pay(client, gna, oid, 3, "UTR-E2E-3"))
    advance_to(db_session, oid, ProjectStage.handover_complete, by="emp-ops-1")
    assert sum(Decimal(a) for a in paid) == Decimal(str(db_session.get(Proposal, pid).total_value)).quantize(
        Decimal("0.01"))
    assert len(_mails(world["emails"], "associate_payment_verified")) == 3
    progress = _ok(client.get(f"{BASE}/{oid}/progress", headers=headers(gna)))
    assert progress["current_stage"] == "handover_complete"

    # Commission: accrued on every verified payment, approved by Accounts.
    me = db_session.execute(select(Associate).where(Associate.associate_id == gna)).scalar_one()
    earned = [e for e in db_session.execute(select(CommissionEntry).where(
        CommissionEntry.associate_id == me.id)).scalars()]
    assert len(earned) == 3 and all(e.amount is not None and e.amount > 0 for e in earned), [
        (e.status, e.pending_reason, e.basis_amount, e.rate_percent if hasattr(e, "rate_percent") else None)
        for e in earned]
    assert approve_all(client, db_session)
    expected = sum(e.amount for e in earned)

    # Payout: next month, prepared by Accounts, approved by someone else, bank file, paid.
    period, day = this_period(), after_month_end()
    monkeypatch.setattr(periods, "today_ist", lambda: day)
    batch = _ok(client.post(f"{DASH}/payout-batches", json={"period": period}, headers=ACCOUNTS), 201)
    [line] = [ln for ln in batch["lines"] if ln["associate"]["associate_id"] == gna]
    assert Decimal(line["gross"]) == expected
    refused = client.post(f"{DASH}/payout-batches/{batch['batch_id']}/approve", json={}, headers=ACCOUNTS)
    assert refused.status_code in (403, 409)                       # the preparer never approves
    _ok(client.post(f"{DASH}/payout-batches/{batch['batch_id']}/approve", json={}, headers=ACCOUNTS_2))
    assert client.get(f"{DASH}/exports/payout-bank-{batch['batch_id']}.csv", headers=ADMIN).status_code == 403
    bank = client.get(f"{DASH}/exports/payout-bank-{batch['batch_id']}.csv", headers=ACCOUNTS)
    assert bank.status_code == 200 and line["net"] in bank.text
    _ok(client.post(f"{DASH}/payout-batches/{batch['batch_id']}/mark-paid", headers=ACCOUNTS,
                    json={"lines": [{"associate_id": gna, "bank_reference": "NEFT-E2E-1"}]}))

    # The associate's own statement.
    mine = _ok(client.get(f"{ASSOC}/commissions", headers=headers(gna)))
    assert mine["summary"]["paid"] == str(expected) and mine["total"] == 3
    payouts = _ok(client.get(f"{ASSOC}/payouts", headers=headers(gna)))["items"]
    assert [p["status"] for p in payouts] == ["paid"] and payouts[0]["net"] == line["net"]


def test_suspended_mid_way(client, db_session, world, monkeypatch):
    from app.commissions import periods
    from tests.commission_support import after_month_end, this_period

    gna = join_network(client, monkeypatch, world, n=1)
    other = join_network(client, monkeypatch, world, n=2)
    publish(db_session, overrides={"personal_sale.associate.execution": "10"})
    oid, _ = onboard_to_agreement(client, db_session, gna)
    pay(client, gna, oid, 1, "UTR-SUS-1")
    assert approve_all(client, db_session)

    # Suspended: access refused at once, the Clerk user banned.
    body = _ok(client.post(f"{DASH}/associates/{gna}/status", json={"status": "suspended", "reason": "Complaint"},
                           headers=NM))
    assert body["access"]["clerk_banned"] is True and world["bans"] == ["user_e2e_1"]
    assert body["open_customers"] == 1
    assert client.get(f"{ASSOC}/me", headers=headers(gna)).status_code == 403
    assert client.get(f"{BASE}/{oid}", headers=headers(gna)).status_code == 403

    # The customer is moved to an associate who can run its execution.
    moved = _ok(client.post(f"{DASH}/associates/{gna}/reassign-customers", headers=NM,
                            json={"to_associate_id": other, "reason": "Owner suspended"}))
    assert moved["onboarding_ids"] == [oid]
    assert _ok(client.get(f"{BASE}/{oid}", headers=headers(other)))["onboarding_id"] == oid
    assert _ok(client.get(staff_exec_url(oid), headers=OPS))["execution_mode"] == "associate"
    _ok(client.post(f"{exec_url(oid)}/submissions", headers=headers(other),
                    json={"stage": "feasibility_submitted", "text": {"feasibility_reference": "WBSEDCL/2"}}), 201)

    # Their commission is frozen: the month's batch has nothing for them.
    period, day = this_period(), after_month_end()
    monkeypatch.setattr(periods, "today_ist", lambda: day)
    r = client.post(f"{DASH}/payout-batches", json={"period": period}, headers=ACCOUNTS)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "no_approved_entries"
    assert r.json()["detail"]["frozen_entries"] == 1
