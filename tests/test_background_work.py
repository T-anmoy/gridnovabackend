"""P5 / D5 (BE-3): work off the request path, sweepers for work that never finished,
one run at a time per scheduled job, and a cap on the stale-lead job's model calls."""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import httpx
import pytest

import app.notifications.notifier as notifier_module
from app.config import settings
from app.jobs import locks, runner, sweeper
from app.memory.models import (
    Associate, ClerkInviteStatus, PaymentReceipt, Proposal, ProposalStatus, ReceiptEmailStatus,
)
from tests.associate_support import (  # noqa: F401 — fixtures
    ASSOCIATE_APPLY, approve_flow, associate_form, kyc_ready, submit, territories,
)
from tests.conftest import admin_headers, seed_employees

DASH = "/api/v1/dashboard"


@pytest.fixture
def threaded(monkeypatch):
    """The runner as production runs it: a real pool, not inline."""
    monkeypatch.setattr(runner, "INLINE", False)
    yield
    runner.shutdown(wait=True)


# ── The runner ───────────────────────────────────────────────────────────────────

def test_work_runs_on_a_background_thread_and_does_not_block(threaded):
    started, release, ran_on = threading.Event(), threading.Event(), []

    def slow():
        started.set()
        ran_on.append(threading.current_thread().name)
        release.wait(5)

    runner.submit(slow, "slow")            # returns at once
    assert started.wait(5)
    release.set()
    runner.shutdown(wait=True)              # drains what is queued
    assert ran_on and ran_on[0].startswith("background") and ran_on[0] != threading.current_thread().name


def test_a_failure_is_logged_never_raised(threaded, caplog):
    runner.submit(lambda: 1 / 0, "divide")
    runner.shutdown(wait=True)
    assert any("Background work 'divide' failed" in r.getMessage() for r in caplog.records)


def test_post_commit_notifications_go_through_the_runner(db_session, monkeypatch):
    from app.notifications.deferred import notify_after_commit
    submitted = []
    monkeypatch.setattr(runner, "submit", lambda fn, description: submitted.append(description))
    notify_after_commit(db_session, lambda: None, "new_lead")
    db_session.commit()
    assert submitted == ["notification new_lead"]


# Captured at import, before tests/conftest.py swaps in its httpx.post seam per test.
_REAL_HTTP_CLIENT = notifier_module.http_client


def test_resend_reuses_one_http_client(monkeypatch):
    monkeypatch.setattr(notifier_module, "_http", None)
    first = _REAL_HTTP_CLIENT()
    assert isinstance(first, httpx.Client) and _REAL_HTTP_CLIENT() is first
    first.close()


# ── Associate approval: invitation and PDFs after the response ───────────────────

def test_approval_answers_before_the_invitation_and_documents(client, kyc_ready, db_session, monkeypatch):
    seed_employees(db_session, "emp-admin-1")
    queued = []
    monkeypatch.setattr(runner, "submit", lambda fn, description: queued.append((fn, description)))
    r = submit(client, ASSOCIATE_APPLY, associate_form(1))
    assert r.status_code == 201, r.text
    with patch("app.associates.documents.render_pdf", return_value=b"%PDF-stub"):
        approved = approve_flow(client, "associate", r.json()["application_id"])
        assert approved.status_code == 200, approved.text
        body = approved.json()["associate"]
        assert body["clerk_invite_status"] is None and body["documents"]["id_card"] is False
        [(work, description)] = [q for q in queued if q[1].startswith("associate issuance")]
        work()                               # what the runner does after the response
    db_session.expire_all()
    associate = db_session.query(Associate).one()
    assert associate.clerk_invite_status == ClerkInviteStatus.skipped   # no CLERK_SECRET_KEY in tests
    assert associate.id_card_storage_key and associate.welcome_letter_storage_key


# ── Sweepers ─────────────────────────────────────────────────────────────────────

def _proposal(db, memory, *, status, approved_minutes_ago, pdf=None) -> int:
    seed_employees(db, "emp-admin-1")
    customer = memory.get_or_create_customer(email=f"s{approved_minutes_ago}{status.value}@example.com", name="S")
    lead = memory.create_lead(customer_id=customer.id)
    p = Proposal(lead_id=lead.id, version=1, status=status, content={}, pdf_url=pdf,
                 approved_by_employee_id="emp-admin-1",
                 approved_at=datetime.now(timezone.utc) - timedelta(minutes=approved_minutes_ago))
    db.add(p)
    db.commit()
    return p.id


def test_proposals_left_without_a_pdf_are_retried(db_session, memory, fake_storage, monkeypatch):
    retried = []
    monkeypatch.setattr("app.api.routes.dashboard._trigger_pdf_generation",
                        lambda pid, proposal, db, memory: retried.append(pid))
    stuck = _proposal(db_session, memory, status=ProposalStatus.approved, approved_minutes_ago=60)
    _proposal(db_session, memory, status=ProposalStatus.sent, approved_minutes_ago=2)            # in flight
    _proposal(db_session, memory, status=ProposalStatus.approved, approved_minutes_ago=60, pdf="x")  # done
    assert sweeper.retry_missing_proposal_pdfs(db_session, datetime.now(timezone.utc)) == 1
    assert retried == [stuck]


def test_nothing_is_retried_without_storage(db_session, memory, monkeypatch):
    monkeypatch.setattr(settings, "storage_bucket", "")
    _proposal(db_session, memory, status=ProposalStatus.approved, approved_minutes_ago=60)
    assert sweeper.retry_missing_proposal_pdfs(db_session, datetime.now(timezone.utc)) == 0


def test_receipts_still_pending_are_retried(client, db_session, territories, fake_storage, monkeypatch):
    from tests.payments_support import accepted_customer
    from app.memory.models import ProjectStage
    rendered = []
    monkeypatch.setattr("app.payments.receipts.generate_and_deliver", lambda rid: rendered.append(rid))
    receipts = db_session.query(PaymentReceipt)
    assert receipts.count() == 0
    # A receipt row as verification leaves it, never rendered (its instance died).
    from app.memory.models import Payment, PaymentMode
    _, oid = accepted_customer(client, db_session, stage=ProjectStage.accepted)
    from app.memory.models import CustomerOnboarding
    project_id = db_session.get(CustomerOnboarding, oid).project_id
    payment = Payment(project_id=project_id, onboarding_id=oid, amount=1000, mode=PaymentMode.neft,
                      reference="UTR-SWEEP", reference_normalized="UTRSWEEP", recorded_by="emp-acc-2")
    db_session.add(payment)
    db_session.flush()
    old = datetime.now(timezone.utc) - timedelta(hours=1)
    db_session.add(PaymentReceipt(payment_id=payment.id, receipt_number="R-SWEEP-1", public_token="t" * 32,
                                  email_status=ReceiptEmailStatus.pending, created_at=old))
    db_session.commit()
    assert sweeper.retry_pending_receipts(db_session, datetime.now(timezone.utc)) == 1
    assert rendered == [receipts.one().id]


# ── One run at a time ────────────────────────────────────────────────────────────

INTERNAL_JOBS = ["check-stale-leads", "check-payment-alerts", "check-execution-alerts",
                 "check-onboarding-escalations", "check-survey-reviews"]


@pytest.mark.parametrize("job", INTERNAL_JOBS)
def test_an_overlapping_run_is_skipped(client, db_session, job):
    with locks.job_lock(db_session, job) as held:
        assert held
        r = client.post(f"{DASH}/internal/{job}", headers=admin_headers())
    assert r.status_code == 200 and r.json() == locks.SKIPPED
    r = client.post(f"{DASH}/internal/{job}", headers=admin_headers())
    assert r.status_code == 200 and r.json() != locks.SKIPPED


def test_lock_keys_are_stable_and_distinct():
    keys = {locks.lock_key(j) for j in INTERNAL_JOBS}
    assert len(keys) == len(INTERNAL_JOBS)
    assert locks.lock_key("check-stale-leads") == locks.lock_key("check-stale-leads")
    assert all(-(2 ** 63) <= k < 2 ** 63 for k in keys)


# ── The stale-lead job's model calls are capped ──────────────────────────────────

def test_quiet_lead_reflections_are_capped_per_run(db_session, memory, monkeypatch):
    from app.jobs import stale_leads
    from tests.test_stale_leads import _seed_lead
    for i in range(3):
        _seed_lead(memory, db_session, email=f"quiet{i}@example.com", last_msg_age_hours=50)
    reflected = []
    monkeypatch.setattr(stale_leads.reflection, "reflect_on_lead",
                        lambda memory, lead_id, trigger, performed_by: reflected.append(lead_id))
    monkeypatch.setattr(settings, "stale_lead_reflections_per_run", 1)
    result = stale_leads.check_stale_leads(db_session)
    assert result["lead_stale_48h"] == 3
    assert len(reflected) == 1 and result["reflections_skipped"] == 2
