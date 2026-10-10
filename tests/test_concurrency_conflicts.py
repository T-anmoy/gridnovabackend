"""BE-1 C4 / C6 — money and review paths under simultaneous requests.

C4: two payments recorded at once could both pass the overpayment check (the sum was
read without a lock). C6: every IntegrityError on approval was reported as seat_full
(and a racing duplicate approval escaped as a 500); execution verify/reject decided on
unlocked rows. Thread tests need real concurrent connections: PostgreSQL only.
"""
import threading
from unittest.mock import patch

import pytest
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError

import app.notifications.notifier as notifier_module
from app.associates import service
from app.memory.models import ApplicationStatus, AssociateApplication, CustomerOnboarding, Payment
from tests.associate_support import (  # noqa: F401 — fixtures
    ADMIN, ASSOCIATE_APPLY, DASH, associate_form, kyc_ready, submit, territories,
)
from tests.conftest import TEST_DATABASE_URL, seed_employees
from tests.payments_support import accepted_customer, neft, onboarding_ready  # noqa: F401

PG_ONLY = pytest.mark.skipif(not TEST_DATABASE_URL, reason="needs real concurrent connections (PostgreSQL)")


def _race(n, fn):
    barrier, results, errors = threading.Barrier(n), [], []

    def run(i):
        try:
            barrier.wait()
            results.append(fn(i))
        except Exception as exc:  # collected, asserted by the caller
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results, errors


# ── C4 ───────────────────────────────────────────────────────────────────────────

@PG_ONLY
def test_two_simultaneous_payments_cannot_overpay(client, db_session, onboarding_ready, _isolated_db, monkeypatch):
    from app.payments import service as payments
    monkeypatch.setattr(notifier_module.notifier, "notify", lambda *a, **k: None)
    _, oid = accepted_customer(client, db_session, total=100000.0)

    def record(i):
        session = _isolated_db()
        try:
            onboarding = session.get(CustomerOnboarding, oid)
            payments.record(session, onboarding, neft(amount="100000.00", reference=f"UTR-RACE-{i}"),
                            "emp-acc-1", by_associate=False)
            session.commit()
            return "recorded"
        except service.AssociateError as exc:
            session.rollback()
            return exc.code
        finally:
            session.close()

    results, errors = _race(2, record)
    assert errors == []
    assert sorted(results) == ["exceeds_project_total", "recorded"]
    db_session.expire_all()
    assert db_session.query(Payment).count() == 1


# ── C6: associate approval ───────────────────────────────────────────────────────

@pytest.fixture
def under_review(client, kyc_ready, db_session, monkeypatch):
    seed_employees(db_session, "emp-admin-1")
    monkeypatch.setattr(notifier_module, "notifier", notifier_module.LogNotifier())
    with patch("app.associates.documents.render_pdf", return_value=b"%PDF-stub"):
        r = submit(client, ASSOCIATE_APPLY, associate_form(1))
        assert r.status_code == 201, r.text
        app_id = r.json()["application_id"]
        url = f"{DASH}/associate-applications/associate/{app_id}"
        assert client.post(f"{url}/status", json={"status": "under_review"}, headers=ADMIN).status_code == 200
        yield app_id, url


APPROVE = {"verification_call_completed": True, "verification_call_note": "called, confirmed"}


def test_a_racing_duplicate_approval_is_invalid_transition(client, db_session, under_review, _isolated_db):
    app_id, url = under_review

    def lost_the_race(db, application, *a, **k):
        # The other approval's commit becomes visible once this request rolls back (and
        # releases its row lock — writing it from here, under the lock, would wait on
        # ourselves on PostgreSQL).
        def other_approval_committed(session):
            other = _isolated_db()
            row = other.get(AssociateApplication, app_id)
            row.status, row.verification_call_completed = ApplicationStatus.approved, True
            row.verification_call_note, row.decided_by_employee_id = "x", "emp-admin-1"
            other.commit()
            other.close()
        event.listen(db, "after_rollback", other_approval_committed, once=True)
        raise IntegrityError("INSERT INTO associates", {}, Exception("duplicate key"))

    with patch.object(service, "approve", lost_the_race):
        r = client.post(f"{url}/approve", json=APPROVE, headers=ADMIN)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "invalid_transition"


def test_any_other_refused_insert_is_seat_full(client, under_review):
    _, url = under_review
    with patch.object(service, "approve", side_effect=IntegrityError("x", {}, Exception("seat index"))):
        r = client.post(f"{url}/approve", json=APPROVE, headers=ADMIN)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "seat_full"


# ── C6: execution verify / reject ────────────────────────────────────────────────

def test_a_stage_recorded_meanwhile_is_submission_not_pending(client, db_session, monkeypatch):
    """execution.verify loses the insert race (the unique stage row) → 409, not 500."""
    from app.api.routes import execution_admin
    from app.execution import service as execution
    class _Sub:
        id, onboarding_id = 1, 1
    monkeypatch.setattr(execution_admin, "_submission", lambda *a, **k: _Sub())

    def raced(*a, **k):
        raise IntegrityError("INSERT INTO project_stage_events", {}, Exception("uq_project_stage_once"))
    monkeypatch.setattr(execution, "verify", raced)
    monkeypatch.setattr(execution, "reject", raced)
    for action in ("verify", "reject"):
        r = client.post(f"{DASH}/execution/submissions/1/{action}", json={"note": "ok"},
                        headers={"X-Employee-Id": "emp-ops-1", "X-Employee-Role": "operations"})
        assert r.status_code == 409 and r.json()["detail"]["code"] == "submission_not_pending", action
