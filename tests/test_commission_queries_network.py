"""BE-4 Phase 3 — commission queries (policy 4.8), the overdue-query job, the
associate home feed items, and the network summary (SQL aggregates under a fixed query
budget)."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.commissions import periods
from app.commissions import queries as commission_queries
from app.commissions import service as commissions
from app.memory.models import CommissionQuery, ProjectStage
from tests.commission_support import (  # noqa: F401 — fixtures
    ACCOUNTS, ACCOUNTS_2, ADMIN, DASH, MGMT, OPS, SALES, accepted, approve_all, entries, headers, kyc_ready,
    make_associate, network, onboarding_ready, publish, record_and_verify, territories,
)
from tests.test_query_budgets import count_queries

UTC = timezone.utc
QUEUE = f"{DASH}/commission-queries"
JOB = f"{DASH}/internal/check-commission-queries"


def q_url(entry_id: int) -> str:
    return f"/api/v1/associate/commissions/{entry_id}/queries"


@pytest.fixture
def earned(client, db_session, onboarding_ready):
    publish(db_session)
    owner = make_associate(db_session)
    oid = accepted(client, db_session, owner, stage=ProjectStage.agreement_signed)
    record_and_verify(client, oid, "21780.00", "UTR-Q1")
    (entry,) = entries(db_session)
    return owner, entry


@pytest.fixture
def sent(monkeypatch):
    import app.notifications.notifier as notifier_module
    from app.notifications.notifier import LogNotifier
    out = []

    class Capture(LogNotifier):
        def notify(self, event_type, payload):
            out.append((event_type, payload))
    monkeypatch.setattr(notifier_module, "notifier", Capture())
    return out


class TestQueries:
    def test_due_in_working_days(self):
        raised = datetime(2026, 10, 16, 4, 30, tzinfo=UTC)          # Friday 10:00 IST
        assert periods.add_working_days(date(2026, 10, 16), 15, [0, 1, 2, 3, 4]) == date(2026, 11, 6)
        assert commission_queries.due_at(raised) == datetime(2026, 11, 6, 18, 30, tzinfo=UTC)   # end of 6 Nov IST

    def test_raise_answer_and_see_it(self, client, db_session, earned):
        owner, entry = earned
        stranger = make_associate(db_session)
        assert client.post(q_url(entry.id), json={"message": "Why 5%?"}, headers=headers(stranger)).status_code == 404
        r = client.post(q_url(entry.id), json={"message": "  Why 5%?  "}, headers=headers(owner))
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["message"] == "Why 5%?" and body["status"] == "open" and "responded_by" not in body
        again = client.post(q_url(entry.id), json={"message": "Again"}, headers=headers(owner))
        assert again.status_code == 409 and again.json()["detail"]["code"] == "query_already_open"
        listed = client.get(f"/api/v1/associate/commissions", headers=headers(owner)).json()["items"][0]
        assert listed["query"]["open"] is True

        queue = client.get(QUEUE, params={"status": "open"}, headers=MGMT).json()
        assert queue["total"] == 1 and queue["items"][0]["associate"]["associate_id"] == owner
        assert queue["items"][0]["entry"]["amount"] == "1000.00" and queue["items"][0]["overdue"] is False
        for who in (SALES, OPS):
            assert client.get(QUEUE, headers=who).status_code == 403
        url = f"{QUEUE}/{body['query_id']}/respond"
        assert client.post(url, json={"response": "x"}, headers=MGMT).status_code == 403
        r = client.post(url, json={"response": "Rate schedule v1, 5 % personal sale."}, headers=ACCOUNTS)
        assert r.status_code == 200 and r.json()["status"] == "answered" and r.json()["responded_by"] == "emp-acc-1"
        assert client.post(url, json={"response": "y"}, headers=ACCOUNTS).json()["detail"]["code"] == "already_answered"
        seen = client.get(q_url(entry.id), headers=headers(owner)).json()["items"]
        assert seen[0]["response"].startswith("Rate schedule v1")
        assert client.get(q_url(entry.id), headers=headers(stranger)).status_code == 404
        home = client.get("/api/v1/associate/home", headers=headers(owner)).json()["items"]
        answered = [i for i in home if i["kind"] == "commission_query_answered"]
        assert answered == [{"kind": "commission_query_answered", "message_key": "todo.commission_query_answered",
                             "onboarding_id": entry.onboarding_id, "customer_name": None, "priority": 3,
                             "params": {"query_id": body["query_id"], "entry_id": entry.id}}]
        # A second query once the first is answered.
        assert client.post(q_url(entry.id), json={"message": "Thanks"}, headers=headers(owner)).status_code == 201

    def test_the_window_closes(self, client, db_session, onboarding_ready, monkeypatch):
        publish(db_session)
        owner = make_associate(db_session)
        oid = accepted(client, db_session, owner, stage=ProjectStage.agreement_signed)
        monkeypatch.setattr(commissions, "_now", lambda: datetime.now(UTC) - timedelta(days=31))
        record_and_verify(client, oid, "21780.00", "UTR-OLD")
        (entry,) = entries(db_session)
        r = client.post(q_url(entry.id), json={"message": "Late"}, headers=headers(owner))
        assert r.status_code == 409 and r.json()["detail"]["code"] == "query_window_closed"


class TestOverdueJob:
    def test_alerts_once_and_skips_when_overlapping(self, client, db_session, earned, sent):
        owner, entry = earned
        qid = client.post(q_url(entry.id), json={"message": "?"}, headers=headers(owner)).json()["query_id"]
        assert client.post(JOB, headers=ADMIN).json() == {"overdue_queries": 0}
        row = db_session.get(CommissionQuery, qid)
        row.due_at = datetime.now(UTC) - timedelta(minutes=1)
        db_session.commit()
        assert client.get(QUEUE, params={"overdue": True}, headers=ACCOUNTS).json()["total"] == 1
        assert client.post(JOB, headers=ADMIN).json() == {"overdue_queries": 1}
        assert [e for e, _ in sent if e == "commission_queries_overdue"] == ["commission_queries_overdue"]
        payload = next(p for e, p in sent if e == "commission_queries_overdue")
        assert payload["queries"][0]["associate_id"] == owner
        assert client.post(JOB, headers=ADMIN).json() == {"overdue_queries": 0}       # once per query
        from app.jobs.locks import job_lock
        with job_lock(db_session, "check-commission-queries") as held:
            assert held
            assert client.post(JOB, headers=ADMIN).json() == {"status": "skipped", "reason": "already_running"}
        assert client.post(JOB, headers=ACCOUNTS).status_code == 403

    def test_the_email(self):
        from app.notifications.notifier import _render_commission_queries_overdue
        subject, html, text = _render_commission_queries_overdue(
            {"queries": [{"query_id": 1, "entry_id": 2, "associate_name": "<b>A</b>", "due_at": "2026-11-06"}]},
            "https://app.gridnova.test")
        assert subject == "Commission queries past their response date: 1"
        assert "&lt;b&gt;A&lt;/b&gt;" in html and "<b>A</b>" not in html and "/dashboard/commission-queries" in text


class TestPayoutOnTheHomeFeed:
    def test_a_paid_payout_appears(self, client, db_session, earned, monkeypatch):
        owner, entry = earned
        approve_all(client, db_session)
        period = periods.period_of(periods.ist_date(entry.created_at))
        after = periods.first_day(periods.next_period(period)).replace(day=5)
        monkeypatch.setattr(periods, "today_ist", lambda: after)
        b = client.post(f"{DASH}/payout-batches", json={"period": period}, headers=ACCOUNTS).json()
        client.post(f"{DASH}/payout-batches/{b['batch_id']}/approve", json={}, headers=ACCOUNTS_2)
        client.post(f"{DASH}/payout-batches/{b['batch_id']}/mark-paid",
                    json={"lines": [{"associate_id": owner, "bank_reference": "UTR-OUT"}]}, headers=ACCOUNTS)
        items = [i for i in client.get("/api/v1/associate/home", headers=headers(owner)).json()["items"]
                 if i["kind"] == "commission_payout_paid"]
        assert len(items) == 1 and items[0]["message_key"] == "todo.commission_payout_paid"
        assert items[0]["params"]["net"] == "1000.00" and items[0]["params"]["bank_reference"] == "UTR-OUT"
        assert items[0]["onboarding_id"] is None


# ── Network summary ──────────────────────────────────────────────────────────────

SUMMARY = f"{DASH}/network/summary"


class TestNetworkSummary:
    def test_figures_and_roles(self, client, db_session, onboarding_ready):
        net = network(db_session)
        publish(db_session)
        owner = make_associate(db_session)
        oid = accepted(client, db_session, owner, stage=ProjectStage.agreement_signed)
        record_and_verify(client, oid, "21780.00", "UTR-N1")
        for who in (ACCOUNTS, SALES, OPS):
            assert client.get(SUMMARY, headers=who).status_code == 403
        body = client.get(SUMMARY, headers=MGMT).json()
        today = periods.today_ist()
        assert body["period"] == {"date_from": today.replace(day=1).isoformat(), "date_to": today.isoformat()}
        counts = {(r["associate_type"], r["level"] or r["capability"], r["status"]): r["count"]
                  for r in body["associates"]}
        assert counts[("preferred_associate", "state_head", "active")] == 1
        assert counts[("associate", "sales", "active")] == 1
        wb = next(s for s in body["seats"] if s["state_lgd_code"] == 19)
        assert wb["state_heads"] == {"held": 1, "max": 2}
        assert wb["districts"]["with_head"] == 1 and wb["blocks"]["with_associate"] == 1
        assert body["onboardings"]["by_status"]["submitted"] + body["onboardings"]["by_status"]["verified"] >= 0
        assert body["onboardings"]["by_current_stage"] == {"agreement_signed": 1}
        assert body["verified_revenue"] == {"credits": "21780.00", "reversals": "0.00", "net": "21780.00"}
        assert body["commissions"]["earned"] == "1600.00" and body["commissions"]["pending_count"] == 0
        assert set(body["overdue"]) == {"verification_calls", "survey_reviews", "payments_awaiting_verification",
                                        "commission_queries"}
        empty = client.get(SUMMARY, params={"date_from": "2020-01-01", "date_to": "2020-01-31"}, headers=ADMIN).json()
        assert empty["verified_revenue"]["net"] == "0.00" and empty["commissions"]["earned"] == "0.00"
        bad = client.get(SUMMARY, params={"date_from": "2026-02-01", "date_to": "2026-01-01"}, headers=ADMIN)
        assert bad.status_code == 422 and bad.json()["detail"]["code"] == "invalid_period"
        assert net  # the three seat holders exist

    def _grow(self, client, db, n: int) -> None:
        from tests.associate_support import ALIPURDUAR_II
        from tests.commission_support import seat
        for i in range(n):
            seat(db, make_associate(db, level="block", territory=ALIPURDUAR_II))
            owner = make_associate(db)
            oid = accepted(client, db, owner, stage=ProjectStage.agreement_signed)
            record_and_verify(client, oid, "21780.00", f"UTR-G-{n}-{i}")

    def test_query_count_does_not_grow_with_the_network(self, client, db_session, onboarding_ready):
        publish(db_session)
        network(db_session)
        self._grow(client, db_session, 1)
        client.get(SUMMARY, headers=ADMIN)                       # provision the caller first
        with count_queries() as few:
            assert client.get(SUMMARY, headers=ADMIN).status_code == 200
        self._grow(client, db_session, 4)
        with count_queries() as many:
            assert client.get(SUMMARY, headers=ADMIN).status_code == 200
        assert len(many) == len(few), (len(few), len(many))
        assert len(many) <= 25, len(many)
