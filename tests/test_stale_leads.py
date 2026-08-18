"""
Phase 2 — stale-lead follow-up + pluggable notifications.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.jobs.stale_leads import check_stale_leads
from app.memory.models import PipelineStage
from app.notifications.notifier import LogNotifier, SlackWebhookNotifier, get_notifier
from tests.conftest import admin_headers, sales_headers


def _seed_lead(memory, db_session, *, email, last_msg_age_hours=None, stage=PipelineStage.qualifying):
    customer = memory.get_or_create_customer(email=email, name="C")
    db_session.commit()
    lead = memory.create_lead(customer_id=customer.id)
    memory.update_lead_fields(lead_id=lead.id, new_fields={"name": "C"}, performed_by="t", stage=stage)
    if last_msg_age_hours is not None:
        when = datetime.now(timezone.utc) - timedelta(hours=last_msg_age_hours)
        memory.record_customer_message(lead.id, when=when)
    db_session.commit()
    return lead.id


class TestStaleDetection:
    def test_49h_quiet_triggers_once(self, memory, db_session, monkeypatch):
        events = []
        monkeypatch.setattr("app.notifications.notifier.notifier",
                            _CaptureNotifier(events))
        lead_id = _seed_lead(memory, db_session, email="q49@example.com", last_msg_age_hours=49)

        c1 = check_stale_leads(db_session)
        assert c1["lead_stale_48h"] == 1
        assert [e[0] for e in events] == ["lead_stale_48h"]
        assert memory.get_lead(lead_id).stale_notified_at is not None

        # Second run — must NOT double-notify.
        c2 = check_stale_leads(db_session)
        assert c2["lead_stale_48h"] == 0
        assert len(events) == 1

    def test_recent_lead_triggers_nothing(self, memory, db_session, monkeypatch):
        events = []
        monkeypatch.setattr("app.notifications.notifier.notifier", _CaptureNotifier(events))
        _seed_lead(memory, db_session, email="recent@example.com", last_msg_age_hours=2)
        c = check_stale_leads(db_session)
        assert c["lead_stale_48h"] == 0 and c["lead_stale_5d"] == 0
        assert events == []

    def test_5d_quiet_fires_5d_not_48h(self, memory, db_session, monkeypatch):
        events = []
        monkeypatch.setattr("app.notifications.notifier.notifier", _CaptureNotifier(events))
        _seed_lead(memory, db_session, email="q6d@example.com", last_msg_age_hours=6 * 24)
        c = check_stale_leads(db_session)
        assert c["lead_stale_5d"] == 1 and c["lead_stale_48h"] == 0
        assert [e[0] for e in events] == ["lead_stale_5d"]

    def test_escalates_48h_then_5d_across_runs(self, memory, db_session, monkeypatch):
        events = []
        monkeypatch.setattr("app.notifications.notifier.notifier", _CaptureNotifier(events))
        # Fixed last-message time; drive the clock forward via the job's `now` param.
        base = datetime(2026, 8, 1, 9, 0, tzinfo=timezone.utc)
        customer = memory.get_or_create_customer(email="esc@example.com", name="C")
        db_session.commit()
        lead = memory.create_lead(customer_id=customer.id)
        memory.update_lead_fields(lead_id=lead.id, new_fields={"name": "C"}, performed_by="t",
                                  stage=PipelineStage.qualifying)
        memory.record_customer_message(lead.id, when=base)
        db_session.commit()

        check_stale_leads(db_session, now=base + timedelta(hours=49))   # fires 48h
        check_stale_leads(db_session, now=base + timedelta(days=6))     # escalates to 5d
        kinds = [e[0] for e in events]
        assert kinds == ["lead_stale_48h", "lead_stale_5d"]

    def test_qualified_lead_excluded(self, memory, db_session, monkeypatch):
        events = []
        monkeypatch.setattr("app.notifications.notifier.notifier", _CaptureNotifier(events))
        _seed_lead(memory, db_session, email="qual@example.com", last_msg_age_hours=100,
                   stage=PipelineStage.qualified)
        c = check_stale_leads(db_session)
        assert c["checked"] == 0 and events == []


class TestEndpoint:
    def test_admin_only(self, client, memory, db_session):
        assert client.post("/api/v1/dashboard/internal/check-stale-leads", headers=sales_headers()).status_code == 403
        r = client.post("/api/v1/dashboard/internal/check-stale-leads", headers=admin_headers())
        assert r.status_code == 200
        assert set(r.json()) >= {"lead_stale_48h", "lead_stale_5d", "checked"}

    def test_requires_auth(self, client):
        assert client.post("/api/v1/dashboard/internal/check-stale-leads").status_code == 401


class TestNotifierSelection:
    def test_log_notifier_when_no_webhook(self, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "slack_webhook_url", "")
        assert isinstance(get_notifier(), LogNotifier)

    def test_slack_notifier_when_webhook_set(self, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "slack_webhook_url", "https://hooks.slack.com/x")
        assert isinstance(get_notifier(), SlackWebhookNotifier)

    def test_slack_never_raises_on_failure(self, monkeypatch):
        n = SlackWebhookNotifier("https://hooks.slack.com/x")

        def _boom(*a, **k):
            raise RuntimeError("network down")

        import httpx
        monkeypatch.setattr(httpx, "post", _boom)
        # Must not raise despite the POST failing.
        n.notify("lead_stale_48h", {"lead_id": 1})

    def test_named_methods_route_through_notify(self):
        events = []
        n = _CaptureNotifier(events)
        n.proposal_approved(proposal_id=5, lead_id=9)
        assert events[0][0] == "proposal_approved"
        assert events[0][1]["proposal_id"] == 5


class _CaptureNotifier(LogNotifier):
    """Test notifier that records (event_type, payload) instead of logging."""

    def __init__(self, sink: list):
        self._sink = sink

    def notify(self, event_type: str, payload: dict) -> None:
        self._sink.append((event_type, payload))
