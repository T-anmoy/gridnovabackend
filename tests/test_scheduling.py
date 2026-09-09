"""
Meeting scheduling — availability engine, booking (with race re-verification),
zero-connected handling, the dashboard endpoint, and the conversational auto-book path.

Google's APIs are mocked at the `app.scheduling.google_calendar` seams — no live
credentials are needed for these tests to pass.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from app.config import settings
from app.llm.client import llm_client
from app.memory.models import MeetingStatus, PipelineStage, TimelineEventType
from app.scheduling import availability, booking, google_calendar
from app.security.encryption import encrypt, generate_key
from tests.conftest import admin_headers, sales_headers, management_headers


@pytest.fixture(autouse=True)
def _fernet_key():
    # Enable at-rest encryption for stored refresh tokens.
    prior = settings.fernet_encryption_key
    if not prior:
        settings.fernet_encryption_key = generate_key()
    yield
    settings.fernet_encryption_key = prior


def _connect_employee(memory, db_session, emp_id="emp-sales-1", role="sales", email=None):
    email = email or f"{emp_id}@gridnova.in"
    memory.get_or_create_employee(emp_id, emp_id, email, role)
    db_session.commit()
    memory.update_employee_google_credentials(
        employee_id=emp_id,
        encrypted_refresh_token=encrypt(f"refresh-{emp_id}"),
        calendar_id="primary",
        performed_by="test",
    )
    db_session.commit()
    return emp_id


def _seed_lead(memory, db_session, email="cust@example.com"):
    customer = memory.get_or_create_customer(email=email, name="Cust")
    db_session.commit()
    lead = memory.create_lead(customer_id=customer.id)
    db_session.commit()
    return customer.id, lead.id


# ── Availability engine ──────────────────────────────────────────────────────────

class TestAvailability:
    def test_zero_connected_raises(self, memory, db_session):
        with pytest.raises(availability.NoConnectedCalendarsError):
            availability.get_available_slots(memory, duration_minutes=30, days_ahead=5)

    def test_business_hours_only_when_all_free(self, memory, db_session, monkeypatch):
        _connect_employee(memory, db_session)
        monkeypatch.setattr(google_calendar, "get_busy_intervals", lambda *a, **k: [])
        slots = availability.get_available_slots(memory, duration_minutes=30, days_ahead=5)
        assert slots  # at least one business day in a 5-day window
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(settings.business_timezone)
        now = datetime.now(timezone.utc)
        for s in slots:
            local = s.astimezone(tz)
            assert settings.business_hours_start_ist <= local.hour < settings.business_hours_end_ist
            assert local.weekday() in settings.business_days
            assert s > now

    def test_excludes_busy_slot(self, memory, db_session, monkeypatch):
        _connect_employee(memory, db_session)
        monkeypatch.setattr(google_calendar, "get_busy_intervals", lambda *a, **k: [])
        free_slots = availability.get_available_slots(memory, duration_minutes=30, days_ahead=5)
        target = free_slots[0]

        # Now mark exactly that slot busy and confirm it drops out.
        busy = [(target, target + timedelta(minutes=30))]
        monkeypatch.setattr(google_calendar, "get_busy_intervals", lambda *a, **k: busy)
        remaining = availability.get_available_slots(memory, duration_minutes=30, days_ahead=5)
        assert target not in remaining


# ── Booking ──────────────────────────────────────────────────────────────────────

class TestBooking:
    def test_book_creates_meeting_and_timeline(self, memory, db_session, monkeypatch):
        emp = _connect_employee(memory, db_session)
        _, lead_id = _seed_lead(memory, db_session)
        monkeypatch.setattr(google_calendar, "get_busy_intervals", lambda *a, **k: [])
        monkeypatch.setattr(
            google_calendar, "insert_event",
            lambda **k: {"google_event_id": "ev-1", "meet_link": "https://meet.google.com/xyz"},
        )
        slot = datetime.now(timezone.utc) + timedelta(days=1)
        meeting = booking.book_meeting(
            memory=memory, lead_id=lead_id, employee_id=emp,
            scheduled_at=slot, duration_minutes=30, performed_by="test",
        )
        db_session.commit()

        assert meeting.id is not None
        assert meeting.meet_link == "https://meet.google.com/xyz"
        assert meeting.google_event_id == "ev-1"
        assert meeting.status == MeetingStatus.scheduled
        ctx = memory.get_lead_context(lead_id)
        assert any(e.event_type == TimelineEventType.meeting_scheduled for e in ctx["timeline"])

    def test_timeline_description_is_ist_formatted_not_raw_iso(self, memory, db_session, monkeypatch):
        """Regression test for the real bug: a staff user saw "Meeting scheduled
        for 2026-09-09T06:30:00+00:00" — a raw UTC ISO string — on a lead's
        timeline instead of a real, human-readable IST time."""
        from app.timezones import format_ist_datetime

        emp = _connect_employee(memory, db_session)
        _, lead_id = _seed_lead(memory, db_session)
        monkeypatch.setattr(google_calendar, "get_busy_intervals", lambda *a, **k: [])
        monkeypatch.setattr(
            google_calendar, "insert_event",
            lambda **k: {"google_event_id": "ev-ist", "meet_link": "https://meet.google.com/ist"},
        )
        slot = datetime.now(timezone.utc) + timedelta(days=1)
        booking.book_meeting(
            memory=memory, lead_id=lead_id, employee_id=emp,
            scheduled_at=slot, duration_minutes=30, performed_by="test",
        )
        db_session.commit()

        ctx = memory.get_lead_context(lead_id)
        event = next(e for e in ctx["timeline"] if e.event_type == TimelineEventType.meeting_scheduled)
        assert format_ist_datetime(slot) in event.description
        assert "IST" in event.description
        assert slot.isoformat() not in event.description

    def test_meeting_booked_notification_is_ist_formatted_not_raw_iso(self, memory, db_session, monkeypatch):
        """The Slack/log notification fired on booking carries the same
        scheduled_at value — it must not leak a raw ISO string into human-read
        notification text either."""
        from app.notifications.notifier import LogNotifier
        from app.timezones import format_ist_datetime

        events = []

        class _CaptureNotifier(LogNotifier):
            def notify(self, event_type, payload):
                events.append((event_type, payload))

        monkeypatch.setattr("app.notifications.notifier.notifier", _CaptureNotifier())

        emp = _connect_employee(memory, db_session)
        _, lead_id = _seed_lead(memory, db_session)
        monkeypatch.setattr(google_calendar, "get_busy_intervals", lambda *a, **k: [])
        monkeypatch.setattr(
            google_calendar, "insert_event",
            lambda **k: {"google_event_id": "ev-notif", "meet_link": "https://meet.google.com/notif"},
        )
        slot = datetime.now(timezone.utc) + timedelta(days=1)
        booking.book_meeting(
            memory=memory, lead_id=lead_id, employee_id=emp,
            scheduled_at=slot, duration_minutes=30, performed_by="test",
        )

        assert events and events[0][0] == "meeting_booked"
        assert events[0][1]["scheduled_at"] == format_ist_datetime(slot)
        assert events[0][1]["scheduled_at"] != slot.isoformat()

    def test_race_condition_prevents_double_book(self, memory, db_session, monkeypatch):
        emp = _connect_employee(memory, db_session)
        _, lead_id = _seed_lead(memory, db_session)
        slot = datetime.now(timezone.utc) + timedelta(days=1)
        # Slot became busy between offer and confirm.
        monkeypatch.setattr(
            google_calendar, "get_busy_intervals",
            lambda *a, **k: [(slot, slot + timedelta(minutes=30))],
        )
        insert_called = MagicMock()
        monkeypatch.setattr(google_calendar, "insert_event", insert_called)

        with pytest.raises(booking.SlotUnavailableError):
            booking.book_meeting(
                memory=memory, lead_id=lead_id, employee_id=emp,
                scheduled_at=slot, duration_minutes=30, performed_by="test",
            )
        insert_called.assert_not_called()
        assert memory.list_meetings_for_lead(lead_id) == []

    def test_insert_event_sets_send_updates_and_conference(self, monkeypatch):
        # Directly verify the Google call carries sendUpdates="all" + conferenceDataVersion=1.
        monkeypatch.setattr(settings, "google_oauth_client_id", "cid")
        monkeypatch.setattr(settings, "google_oauth_client_secret", "secret")
        monkeypatch.setattr(settings, "google_oauth_redirect_uri", "https://app/callback")

        service = MagicMock()
        insert = service.events.return_value.insert
        insert.return_value.execute.return_value = {"id": "ev-9", "hangoutLink": "https://meet/abc"}
        monkeypatch.setattr(google_calendar, "_service", lambda rt: service)

        start = datetime.now(timezone.utc) + timedelta(days=1)
        out = google_calendar.insert_event(
            refresh_token="rt", calendar_id="primary", summary="s",
            start=start, end=start + timedelta(minutes=30),
            attendee_emails=["a@x.com", "b@y.com"],
        )
        assert out == {"google_event_id": "ev-9", "meet_link": "https://meet/abc"}
        kwargs = insert.call_args.kwargs
        assert kwargs["sendUpdates"] == "all"
        assert kwargs["conferenceDataVersion"] == 1
        assert kwargs["body"]["conferenceData"]["createRequest"]["conferenceSolutionKey"]["type"] == "hangoutsMeet"


# ── Dashboard endpoint ───────────────────────────────────────────────────────────

class TestDashboardSchedule:
    def test_role_gated(self, client, memory, db_session):
        _, lead_id = _seed_lead(memory, db_session, email="mgr@example.com")
        r = client.post(
            f"/api/v1/dashboard/leads/{lead_id}/schedule-meeting",
            json={"scheduled_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()},
            headers=management_headers(),
        )
        assert r.status_code == 403

    def test_happy_path(self, client, memory, db_session, monkeypatch):
        _connect_employee(memory, db_session, emp_id="emp-admin-1", role="admin")
        _, lead_id = _seed_lead(memory, db_session, email="book@example.com")
        monkeypatch.setattr(google_calendar, "get_busy_intervals", lambda *a, **k: [])
        monkeypatch.setattr(
            google_calendar, "insert_event",
            lambda **k: {"google_event_id": "ev-2", "meet_link": "https://meet.google.com/aaa"},
        )
        slot = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        r = client.post(
            f"/api/v1/dashboard/leads/{lead_id}/schedule-meeting",
            json={"scheduled_at": slot, "duration_minutes": 30},
            headers=admin_headers("emp-admin-1"),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["meet_link"] == "https://meet.google.com/aaa"
        assert memory.list_meetings_for_lead(lead_id)

    def test_no_connected_calendar_conflict(self, client, memory, db_session):
        _, lead_id = _seed_lead(memory, db_session, email="nocal@example.com")
        r = client.post(
            f"/api/v1/dashboard/leads/{lead_id}/schedule-meeting",
            json={"scheduled_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()},
            headers=admin_headers("emp-admin-nocal"),
        )
        assert r.status_code == 409


# ── Conversational auto-book path ────────────────────────────────────────────────

class TestConversationalBooking:
    def test_offer_then_autobook(self, memory, db_session, monkeypatch):
        from app.agents.sales_agent import sales_agent

        emp = _connect_employee(memory, db_session)
        cid, lead_id = _seed_lead(memory, db_session, email="chat@example.com")
        monkeypatch.setattr(google_calendar, "get_busy_intervals", lambda *a, **k: [])
        monkeypatch.setattr(
            google_calendar, "insert_event",
            lambda **k: {"google_event_id": "ev-3", "meet_link": "https://meet.google.com/conv"},
        )

        # Turn 1 — intent, no confirmation yet → offers real slots.
        monkeypatch.setattr(
            llm_client, "extract_meeting_signal",
            lambda **k: {"intent": True, "confirmed_slot_iso": None},
        )
        r1 = sales_agent.handle_message(memory, cid, lead_id, "Can we set up a call?")
        db_session.commit()
        assert r1["offered_slots"], "should have offered real slots"
        lead = memory.get_lead(lead_id)
        assert lead.pending_meeting_slots["slots"] == r1["offered_slots"]

        # Turn 2 — customer confirms one of the offered slots → auto-book.
        chosen = r1["offered_slots"][0]
        monkeypatch.setattr(
            llm_client, "extract_meeting_signal",
            lambda **k: {"intent": True, "confirmed_slot_iso": chosen},
        )
        r2 = sales_agent.handle_message(memory, cid, lead_id, f"Yes, {chosen} works")
        db_session.commit()
        assert r2["meeting_booked"] is not None
        assert r2["meeting_booked"]["meet_link"] == "https://meet.google.com/conv"
        meetings = memory.list_meetings_for_lead(lead_id)
        assert len(meetings) == 1
        # Pending slots cleared after booking.
        assert memory.get_lead(lead_id).pending_meeting_slots is None

    def test_no_calendars_degrades_gracefully(self, memory, db_session, monkeypatch):
        from app.agents.sales_agent import sales_agent

        cid, lead_id = _seed_lead(memory, db_session, email="nocal2@example.com")
        # No connected employees. Intent detected but scheduling must not raise.
        monkeypatch.setattr(
            llm_client, "extract_meeting_signal",
            lambda **k: {"intent": True, "confirmed_slot_iso": None},
        )
        result = sales_agent.handle_message(memory, cid, lead_id, "Can we meet?")
        db_session.commit()
        # Graceful: no crash, no booking, no offered slots.
        assert result["meeting_booked"] is None
        assert result["offered_slots"] == []
        assert result["reply_text"]  # normal conversational reply still produced


# ── Encryption ───────────────────────────────────────────────────────────────────

class TestEncryption:
    def test_roundtrip(self):
        assert __import__("app.security.encryption", fromlist=["decrypt"]).decrypt(encrypt("tok")) == "tok"

    def test_missing_key_raises(self, monkeypatch):
        from app.security import encryption
        monkeypatch.setattr(settings, "fernet_encryption_key", "")
        with pytest.raises(encryption.EncryptionKeyMissingError):
            encryption.encrypt("x")
