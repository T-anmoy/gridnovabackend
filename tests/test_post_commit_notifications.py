"""Notifications about a database change are sent only after that change commits
(app/notifications/deferred.py): commit → sent, rollback/close → nothing sent, and a
failing send never affects the request."""
from datetime import datetime, timedelta, timezone

import pytest

import app.notifications.notifier as notifier_module
from app.config import settings
from app.memory.memory_manager import MemoryManager
from app.notifications.deferred import notify_after_commit, pending_count
from app.notifications.notifier import LogNotifier
from app.scheduling import booking, google_calendar
from app.security.encryption import encrypt, generate_key
from tests.conftest import admin_headers, backdate_lead


class _Capture(LogNotifier):
    def __init__(self, fail=False):
        self.events = []
        self.fail = fail

    def notify(self, event_type, payload):
        self.events.append((event_type, payload))
        if self.fail:
            raise RuntimeError("delivery failed")


@pytest.fixture
def capture(monkeypatch):
    def _install(fail=False):
        c = _Capture(fail)
        monkeypatch.setattr(notifier_module, "notifier", c)
        monkeypatch.setattr("app.api.routes.dashboard.notifier", c)
        return c
    return _install


class TestQueue:
    def test_commit_sends(self, db_session):
        sent = []
        notify_after_commit(db_session, lambda: sent.append(1), "t")
        assert sent == [] and pending_count(db_session) == 1
        db_session.commit()
        assert sent == [1] and pending_count(db_session) == 0

    def test_rollback_discards(self, db_session, memory):
        sent = []
        memory.get_or_create_customer(email="rb@example.com", name="R")  # start a real transaction
        notify_after_commit(db_session, lambda: sent.append(1), "t")
        db_session.rollback()
        db_session.commit()
        assert sent == [] and pending_count(db_session) == 0

    def test_close_without_commit_discards(self, _isolated_db):
        session = _isolated_db()
        sent = []
        MemoryManager(session).get_or_create_customer(email="cl@example.com", name="C")
        notify_after_commit(session, lambda: sent.append(1), "t")
        session.close()
        assert sent == []

    def test_a_failing_send_does_not_stop_the_others_or_the_commit(self, db_session, memory, caplog):
        sent = []

        def _boom():
            raise RuntimeError("smtp down")
        notify_after_commit(db_session, _boom, "first")
        notify_after_commit(db_session, lambda: sent.append("second"), "second")
        customer = memory.get_or_create_customer(email="ok@example.com", name="O")
        db_session.commit()  # must not raise
        assert sent == ["second"]
        assert memory.find_customer(email="ok@example.com").id == customer.id
        assert any("failed after commit" in r.getMessage() for r in caplog.records)


class TestRoutes:
    def test_manual_lead_notifies_after_commit(self, client, capture):
        c = capture()
        r = client.post("/api/v1/dashboard/leads", json={"name": "N", "phone": "9000000002", "source": "other"},
                        headers=admin_headers())
        assert r.status_code == 201
        assert [e for e, _ in c.events] == ["new_lead"]

    def test_notifier_failure_after_commit_does_not_affect_the_response(self, client, capture, memory, db_session):
        capture(fail=True)
        r = client.post("/api/v1/dashboard/leads", json={"name": "F", "phone": "9000000003", "source": "other"},
                        headers=admin_headers())
        assert r.status_code == 201
        db_session.expire_all()
        assert memory.get_lead(r.json()["lead_id"]) is not None

    def _calendar(self, memory, db_session, monkeypatch):
        monkeypatch.setattr(settings, "fernet_encryption_key", generate_key())
        memory.get_or_create_employee("emp-cal", "Cal", "cal@gridnova.in", "sales")
        db_session.commit()
        memory.update_employee_google_credentials(employee_id="emp-cal", encrypted_refresh_token=encrypt("r"),
                                                  calendar_id="primary", performed_by="t")
        db_session.commit()
        monkeypatch.setattr(google_calendar, "get_busy_intervals", lambda *a, **k: [])

    def test_booking_commit_sends_both(self, client, capture, memory, db_session, monkeypatch):
        c = capture()
        self._calendar(memory, db_session, monkeypatch)
        monkeypatch.setattr(google_calendar, "insert_event",
                            lambda **k: {"google_event_id": "ev", "meet_link": "https://meet.google.com/a"})
        slot = datetime.now(timezone.utc) + timedelta(days=1)
        r = client.post("/api/v1/public/booking", json={"name": "B", "email": "b@example.com",
                                                        "scheduled_at": slot.isoformat()})
        assert r.status_code == 200
        assert sorted(e for e, _ in c.events) == ["meeting_booked", "new_lead"]

    def test_booking_rollback_sends_nothing(self, client, capture, memory, db_session, monkeypatch):
        c = capture()
        self._calendar(memory, db_session, monkeypatch)

        def _taken(**kwargs):
            raise booking.SlotUnavailableError("gone")
        monkeypatch.setattr(booking, "book_meeting", _taken)
        slot = datetime.now(timezone.utc) + timedelta(days=1)
        r = client.post("/api/v1/public/booking", json={"name": "B", "email": "rb@example.com",
                                                        "scheduled_at": slot.isoformat()})
        assert r.status_code == 409
        assert c.events == []

    def test_meeting_booked_is_discarded_when_the_caller_rolls_back(self, capture, memory, db_session, monkeypatch):
        c = capture()
        self._calendar(memory, db_session, monkeypatch)
        monkeypatch.setattr(google_calendar, "insert_event",
                            lambda **k: {"google_event_id": "ev2", "meet_link": "https://meet.google.com/b"})
        customer = memory.get_or_create_customer(email="m@example.com", name="M")
        lead = memory.create_lead(customer_id=customer.id)
        db_session.commit()
        booking.book_meeting(memory=memory, lead_id=lead.id, employee_id="emp-cal",
                             scheduled_at=datetime.now(timezone.utc) + timedelta(days=2),
                             duration_minutes=30, performed_by="t")
        db_session.rollback()
        db_session.commit()
        assert c.events == []

    def test_stale_lead_alert_waits_for_the_commit(self, capture, memory, db_session):
        from app.jobs.stale_leads import check_stale_leads
        c = capture()
        customer = memory.get_or_create_customer(email="q@example.com", name="Q")
        lead = memory.create_lead(customer_id=customer.id)
        lead.last_customer_message_at = datetime.now(timezone.utc) - timedelta(hours=60)
        db_session.commit()
        backdate_lead(db_session, lead.id, datetime.now(timezone.utc) - timedelta(hours=60))
        check_stale_leads(db_session)
        assert [e for e, _ in c.events] == ["lead_stale_48h", "stale_lead_digest"]
