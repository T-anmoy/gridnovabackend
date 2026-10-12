"""S1 Step 8 — the Engineer's survey review queue, its counts on /work-queues, and the
overdue alert job (idempotent, also when two runs overlap)."""
from datetime import datetime, timedelta, timezone

import pytest

from app.memory.models import CustomerOnboarding, Lead, ProposalInputs
from tests.conftest import TEST_DATABASE_URL, seed_employees
from tests.execution_support import notifications  # noqa: F401 — fixture
from tests.onboarding_support import (  # noqa: F401 — fixtures
    ADMIN, BASE, DASH, ENG, MGMT, SALES, complete_survey, headers, kyc_ready, make_associate, onboarding_ready,
    review_survey, sign_survey, territories,
)
from tests.test_survey_review import _submitted, _verified

OPS = {"X-Employee-Id": "emp-ops-1", "X-Employee-Role": "operations"}
QUEUE = f"{DASH}/survey-reviews"
JOB = f"{DASH}/internal/check-survey-reviews"
PG_ONLY = pytest.mark.skipif(not TEST_DATABASE_URL, reason="needs real concurrent connections (PostgreSQL)")


def _age(db_session, oid, hours):
    """Move the submission and both signatures `hours` into the past."""
    db_session.expire_all()
    onboarding = db_session.get(CustomerOnboarding, oid)
    inputs = db_session.get(ProposalInputs, onboarding.proposal_inputs_id)
    when = datetime.now(timezone.utc) - timedelta(hours=hours)
    onboarding.submitted_at = when
    for attr in ("customer_signature_ref", "associate_signature_ref"):
        setattr(inputs, attr, {**getattr(inputs, attr), "signed_at": when.isoformat()})
    db_session.commit()


def _ids(client, state, who=ENG):
    r = client.get(QUEUE, params={"state": state}, headers=who)
    assert r.status_code == 200, r.text
    return [i["onboarding_id"] for i in r.json()["items"]]


@pytest.fixture
def surveys(client, db_session, onboarding_ready):
    """fresh: submitted, not reviewed · old: verified, not reviewed, 60 h · done: reviewed ·
    changed: reviewed, then corrected · draft: never submitted (not in the queue)."""
    _, fresh = _submitted(client, db_session, n=1)
    _, old = _verified(client, db_session, n=2)
    _age(db_session, old, 60)
    _, done = _submitted(client, db_session, n=3)
    assert review_survey(client, done).status_code == 200
    gna, changed = _verified(client, db_session, n=4)
    assert review_survey(client, changed).status_code == 200
    cid = client.post(f"{BASE}/{changed}/survey/corrections", json={"changes": {"floors": 3}, "reason": "r"},
                      headers=headers(gna)).json()["correction_id"]
    assert client.post(f"{DASH}/survey-corrections/{cid}/apply", json={}, headers=ADMIN).status_code == 200
    for party in ("customer", "associate"):   # signed again after the correction
        assert sign_survey(client, gna, changed, party).status_code == 201
    other = make_associate(db_session)
    from tests.onboarding_support import onboarding_body
    draft = client.post(BASE, json=onboarding_body(n=5), headers=headers(other)).json()["onboarding_id"]
    complete_survey(client, other, draft)
    return {"fresh": fresh, "old": old, "done": done, "changed": changed, "draft": draft}


class TestQueue:
    def test_states(self, client, surveys):
        s = surveys
        assert _ids(client, "awaiting") == [s["changed"], s["fresh"], s["old"]]   # newest wait first
        assert _ids(client, "overdue") == [s["old"]]
        assert _ids(client, "reviewed") == [s["done"]]
        assert _ids(client, "revised") == [s["changed"]]
        assert s["draft"] not in _ids(client, "awaiting")

    def test_item_shape(self, client, db_session, surveys):
        body = client.get(QUEUE, params={"state": "overdue"}, headers=ENG).json()
        assert body["review_target_hours"] == 48 and body["total"] == 1 and body["state"] == "overdue"
        item = body["items"][0]
        assert item["client_code"].startswith("GNE-") and item["customer_name"]
        assert item["associate"]["associate_id"].startswith("GNA-") and item["associate"]["name"]
        assert item["onboarding_status"] == "verified" and item["overdue"] is True
        assert 59 <= item["hours_waiting"] <= 60
        assert item["waiting_since"] == item["submitted_at"] == item["signed_at"]
        assert item["review"] == {"outcome": None, "reviewed_at": None, "reviewed_by": None}
        reviewed = client.get(QUEUE, params={"state": "reviewed"}, headers=ENG).json()["items"][0]
        assert reviewed["hours_waiting"] is None and reviewed["review"]["outcome"] == "feasible"
        revised = client.get(QUEUE, params={"state": "revised"}, headers=ENG).json()["items"][0]
        assert revised["revised_after_review"] is True and revised["hours_waiting"] == 0

    def test_who_reads(self, client, db_session, surveys):
        for who in (ENG, ADMIN, MGMT, SALES):
            assert client.get(QUEUE, headers=who).status_code == 200
        assert client.get(QUEUE, headers=OPS).status_code == 403
        assert client.get(QUEUE, params={"state": "late"}, headers=ENG).status_code == 422
        # Sales: own or unassigned leads only.
        seed_employees(db_session, "emp-sales-2")
        db_session.get(Lead, db_session.get(CustomerOnboarding, surveys["old"]).lead_id).assigned_employee_id = \
            "emp-sales-2"
        db_session.commit()
        assert surveys["old"] not in _ids(client, "awaiting", who=SALES)
        assert surveys["old"] in _ids(client, "awaiting", who=MGMT)

    def test_paging(self, client, surveys):
        page = client.get(QUEUE, params={"state": "awaiting", "page": 2, "page_size": 2}, headers=ENG).json()
        assert page["total"] == 3 and [i["onboarding_id"] for i in page["items"]] == [surveys["old"]]


class TestWorkQueues:
    def test_counts_for_the_roles_that_act(self, client, surveys):
        for who in (ENG, ADMIN):
            body = client.get(f"{DASH}/work-queues", headers=who).json()
            assert body["survey_reviews"] == {"awaiting": 3, "overdue": 1}
            assert body["survey_corrections"] == {"pending": 0}
        for who in (MGMT, SALES, OPS):
            body = client.get(f"{DASH}/work-queues", headers=who).json()
            assert "survey_reviews" not in body and "survey_corrections" not in body

    def test_pending_corrections_are_counted(self, client, db_session, onboarding_ready):
        gna, oid = _verified(client, db_session)
        client.post(f"{BASE}/{oid}/survey/corrections", json={"changes": {"floors": 3}, "reason": "r"},
                    headers=headers(gna))
        assert client.get(f"{DASH}/work-queues", headers=ENG).json()["survey_corrections"] == {"pending": 1}


class TestOverdueJob:
    def _emails(self, notifications):
        return notifications.of("survey_review_overdue")

    def test_alerts_once_per_wait(self, client, db_session, surveys, notifications):
        r = client.post(JOB, headers=ADMIN)
        assert r.status_code == 200 and r.json() == {"overdue": 1, "newly_alerted": 1}
        sent = self._emails(notifications)
        assert len(sent) == 1 and sent[0]["surveys"][0]["onboarding_id"] == surveys["old"]
        assert sent[0]["review_target_hours"] == 48
        assert client.post(JOB, headers=ADMIN).json() == {"overdue": 1, "newly_alerted": 0}
        assert len(self._emails(notifications)) == 1
        # The survey is reviewed, then changes and waits again: a new wait is alerted again.
        assert review_survey(client, surveys["old"]).status_code == 200
        assert client.post(JOB, headers=ADMIN).json() == {"overdue": 0, "newly_alerted": 0}
        db_session.expire_all()
        inputs = db_session.get(ProposalInputs,
                                db_session.get(CustomerOnboarding, surveys["old"]).proposal_inputs_id)
        inputs.survey_revised_after_review = True
        inputs.feasibility_reviewed_at = datetime.now(timezone.utc) - timedelta(hours=50)
        db_session.commit()
        assert client.post(JOB, headers=ADMIN).json() == {"overdue": 1, "newly_alerted": 1}
        assert len(self._emails(notifications)) == 2

    def test_auth(self, client, surveys, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "internal_api_key", "k" * 32)
        assert client.post(JOB, headers=ENG).status_code == 403
        assert client.post(JOB).status_code == 401
        assert client.post(JOB, headers={"X-Internal-Key": "k" * 32}).status_code == 200

    def test_the_staff_email(self, monkeypatch):
        import httpx
        from app.notifications.notifier import EmailNotifier, LogNotifier
        sent = []

        class _Resp:
            status_code = 200
        monkeypatch.setattr(httpx, "post", lambda url, headers, json, timeout: sent.append(json) or _Resp())
        n = EmailNotifier("k", "n@x.test", ["staff@x.test"], "https://app.test", LogNotifier())
        n.notify("survey_review_overdue", {"review_target_hours": 48, "surveys": [
            {"onboarding_id": 7, "client_code": "GNE-WB-RES-2026-00007", "customer_name": "<b>Asha</b>",
             "hours_waiting": 60, "revised_after_review": True}]})
        assert sent[0]["to"] == ["staff@x.test"]
        assert sent[0]["subject"] == "1 site survey waiting for an Engineer's review"
        assert "longer than 48 hours" in sent[0]["text"] and "changed after its last review" in sent[0]["text"]
        assert "https://app.test/dashboard/onboardings/7" in sent[0]["text"]
        assert "<b>Asha</b>" not in sent[0]["html"] and "&lt;b&gt;Asha" in sent[0]["html"]


@PG_ONLY
def test_overlapping_runs_alert_once(client, db_session, surveys, _isolated_db, monkeypatch):
    import app.notifications.notifier as notifier_module
    from app.jobs.survey_reviews import check_survey_reviews
    from tests.test_concurrency_conflicts import _race
    sent = []
    monkeypatch.setattr(notifier_module.notifier, "notify",
                        lambda event, payload: sent.append(payload) if event == "survey_review_overdue" else None)

    def run(i):
        session = _isolated_db()
        try:
            return check_survey_reviews(session, datetime.now(timezone.utc))["newly_alerted"]
        finally:
            session.close()

    results, errors = _race(2, run)
    assert errors == []
    assert sorted(results) == [0, 1] and len(sent) == 1
