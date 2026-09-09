"""
Public booking endpoints (app/api/routes/public_booking.py) — the backend half
of the frontend's /book-a-call page.

Google's APIs are mocked at the app.scheduling.google_calendar seams, same as
tests/test_scheduling.py — no live credentials are needed for these to pass.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.config import settings
from app.memory.models import LeadSource
from app.scheduling import google_calendar
from app.security.encryption import encrypt, generate_key


@pytest.fixture(autouse=True)
def _fernet_key():
    prior = settings.fernet_encryption_key
    if not prior:
        settings.fernet_encryption_key = generate_key()
    yield
    settings.fernet_encryption_key = prior


def _connect_employee(memory, db_session, emp_id="emp-sales-1", role="sales"):
    memory.get_or_create_employee(emp_id, emp_id, f"{emp_id}@gridnova.in", role)
    db_session.commit()
    memory.update_employee_google_credentials(
        employee_id=emp_id,
        encrypted_refresh_token=encrypt(f"refresh-{emp_id}"),
        calendar_id="primary",
        performed_by="test",
    )
    db_session.commit()
    return emp_id


def _slot():
    return datetime.now(timezone.utc) + timedelta(days=1)


class TestAvailabilityEndpoint:
    def test_no_connected_calendars_degrades_gracefully(self, client, memory, db_session):
        r = client.get("/api/v1/public/booking/availability")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["slots"] == []

    def test_returns_real_slots_when_connected(self, client, memory, db_session, monkeypatch):
        _connect_employee(memory, db_session)
        monkeypatch.setattr(google_calendar, "get_busy_intervals", lambda *a, **k: [])
        r = client.get("/api/v1/public/booking/availability")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["duration_minutes"] == settings.scheduling_default_duration_minutes
        assert body["days_ahead"] == 14
        assert body["slots"]
        first = body["slots"][0]
        assert "scheduled_at" in first and "label" in first

    def test_rate_limited(self, client, memory, db_session, monkeypatch):
        monkeypatch.setattr(google_calendar, "get_busy_intervals", lambda *a, **k: [])
        from app.api.routes import public_booking
        for _ in range(public_booking._AVAILABILITY_RATE_LIMIT):
            r = client.get("/api/v1/public/booking/availability")
            assert r.status_code == 200
        r = client.get("/api/v1/public/booking/availability")
        assert r.status_code == 429


class TestBookingEndpoint:
    def test_successful_booking(self, client, memory, db_session, monkeypatch):
        _connect_employee(memory, db_session)
        monkeypatch.setattr(google_calendar, "get_busy_intervals", lambda *a, **k: [])
        monkeypatch.setattr(
            google_calendar, "insert_event",
            lambda **k: {"google_event_id": "ev-pub-1", "meet_link": "https://meet.google.com/pub1"},
        )
        slot = _slot()
        r = client.post(
            "/api/v1/public/booking",
            json={
                "name": "Tanmoy Halder",
                "email": "tanmoy.booking@example.com",
                "phone": "9876543210",
                "scheduled_at": slot.isoformat(),
                "estimated_total": 450000.0,
                "sizing_method": "bill",
                "sizing_value": 9.0,
                "source": "book_a_call_page",
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["success"] is True
        assert body["meet_link"] == "https://meet.google.com/pub1"
        assert body["duration_minutes"] == settings.scheduling_default_duration_minutes

        lead = memory.get_lead(body["lead_id"])
        assert lead.source == LeadSource.website_booking
        assert lead.collected_fields["estimated_total"] == 450000.0
        assert lead.collected_fields["sizing_method"] == "bill"
        meetings = memory.list_meetings_for_lead(body["lead_id"])
        assert len(meetings) == 1
        assert meetings[0].id == body["meeting_id"]

    def test_slot_taken_race_condition_returns_distinct_code(self, client, memory, db_session, monkeypatch):
        _connect_employee(memory, db_session)
        slot = _slot()
        # Free at the availability-style check inside is_slot_free... but make it
        # busy so is_slot_free itself returns None (nobody free) — the most direct
        # way to exercise the "slot already taken" path through the real endpoint.
        # Only the exact requested slot is busy — every other slot in the window is
        # genuinely free, so real alternatives exist for the endpoint to offer.
        monkeypatch.setattr(
            google_calendar, "get_busy_intervals",
            lambda *a, **k: [(slot, slot + timedelta(minutes=30))],
        )
        r = client.post(
            "/api/v1/public/booking",
            json={
                "name": "Someone Else",
                "email": "race@example.com",
                "scheduled_at": slot.isoformat(),
            },
        )
        assert r.status_code == 409, r.text
        detail = r.json()["detail"]
        assert detail["code"] == "slot_unavailable"
        # The exact requested time is named, not just a generic message.
        assert detail["requested_slot"] == slot.isoformat()
        # Real, currently-free nearby alternatives are offered — the caller
        # chooses, nothing here auto-selects on their behalf.
        assert detail["alternatives"], "expected real alternative slots to be offered"
        alt_times = {a["scheduled_at"] for a in detail["alternatives"]}
        assert slot.isoformat() not in alt_times
        for alt in detail["alternatives"]:
            assert "scheduled_at" in alt and "label" in alt

    def test_true_check_then_act_race_still_caught_by_db_unique_index(
        self, client, memory, db_session, monkeypatch
    ):
        """Two near-simultaneous booking requests for the same slot: the first
        (simulated directly via book_meeting, as if it landed a moment earlier)
        wins. is_slot_free then reports free for the second (so the endpoint
        proceeds), but book_meeting's own re-verification inside the lock finds
        it just got taken — the DB-level partial unique index (migration 0009)
        is the actual backstop. The loser must get a real slot_unavailable
        response naming the exact conflicting time it asked for, with real
        alternative slots attached — nothing here implies a slot was already
        chosen for it."""
        emp = _connect_employee(memory, db_session)
        slot = _slot()
        monkeypatch.setattr(google_calendar, "get_busy_intervals", lambda *a, **k: [])
        monkeypatch.setattr(
            google_calendar, "insert_event",
            lambda **k: {"google_event_id": "ev-pub-2", "meet_link": "https://meet.google.com/pub2"},
        )
        # Pre-book the exact same employee/slot directly, simulating a booking
        # that landed between this request's is_slot_free check and its insert.
        customer = memory.get_or_create_customer(email="other@example.com", name="Other")
        db_session.commit()
        lead = memory.create_lead(customer_id=customer.id)
        db_session.commit()
        from app.scheduling import booking as booking_module
        booking_module.book_meeting(
            memory=memory, lead_id=lead.id, employee_id=emp,
            scheduled_at=slot, duration_minutes=30, performed_by="test",
        )
        db_session.commit()

        r = client.post(
            "/api/v1/public/booking",
            json={"name": "Race Loser", "email": "raceloser@example.com", "scheduled_at": slot.isoformat()},
        )
        assert r.status_code == 409, r.text
        detail = r.json()["detail"]
        assert detail["code"] == "slot_unavailable"
        assert detail["requested_slot"] == slot.isoformat()
        assert "alternatives" in detail
        # The response must never present an already-picked replacement — only a
        # plain list the caller can pick from (possibly empty if none are free).
        assert isinstance(detail["alternatives"], list)
        assert "scheduled_at" not in detail  # no top-level substitute slot
        for alt in detail["alternatives"]:
            assert alt["scheduled_at"] != slot.isoformat()

    def test_malformed_input_rejected(self, client, memory, db_session):
        r = client.post(
            "/api/v1/public/booking",
            json={"name": "", "email": "not-an-email", "scheduled_at": _slot().isoformat()},
        )
        assert r.status_code == 422

    def test_past_slot_rejected(self, client, memory, db_session):
        past = datetime.now(timezone.utc) - timedelta(days=1)
        r = client.post(
            "/api/v1/public/booking",
            json={"name": "Someone", "email": "past@example.com", "scheduled_at": past.isoformat()},
        )
        assert r.status_code == 422

    def test_rate_limited(self, client, memory, db_session, monkeypatch):
        _connect_employee(memory, db_session)
        monkeypatch.setattr(
            google_calendar, "get_busy_intervals",
            lambda *a, **k: [(_slot(), _slot() + timedelta(minutes=30))],
        )
        from app.api.routes import public_booking
        for i in range(public_booking._BOOKING_RATE_LIMIT):
            r = client.post(
                "/api/v1/public/booking",
                json={"name": "N", "email": f"rl{i}@example.com", "scheduled_at": _slot().isoformat()},
            )
            assert r.status_code == 409  # slot unavailable, but not rate-limited yet
        r = client.post(
            "/api/v1/public/booking",
            json={"name": "N", "email": "rl-over@example.com", "scheduled_at": _slot().isoformat()},
        )
        assert r.status_code == 429

    def test_lead_dedup_on_repeat_email(self, client, memory, db_session, monkeypatch):
        _connect_employee(memory, db_session)
        monkeypatch.setattr(google_calendar, "get_busy_intervals", lambda *a, **k: [])
        monkeypatch.setattr(
            google_calendar, "insert_event",
            lambda **k: {"google_event_id": "ev-dedup", "meet_link": "https://meet.google.com/dedup"},
        )
        email = "repeat-booker@example.com"
        r1 = client.post(
            "/api/v1/public/booking",
            json={"name": "Repeat Booker", "email": email, "scheduled_at": _slot().isoformat()},
        )
        assert r1.status_code == 200, r1.text
        customer_id_1 = memory.get_lead(r1.json()["lead_id"]).customer_id

        # A second booking for the same email, past the email cooldown window,
        # should resolve to the SAME customer (dedup by email), not a duplicate.
        from app.api.routes import public_booking
        public_booking._recent_emails.clear()  # simulate the cooldown window elapsing
        later_slot = _slot() + timedelta(days=1)
        r2 = client.post(
            "/api/v1/public/booking",
            json={"name": "Repeat Booker", "email": email, "scheduled_at": later_slot.isoformat()},
        )
        assert r2.status_code == 200, r2.text
        customer_id_2 = memory.get_lead(r2.json()["lead_id"]).customer_id
        assert customer_id_1 == customer_id_2
        # But two distinct leads (one per booking), same as estimate-request's own
        # dedup-by-customer-not-by-lead behaviour.
        assert r1.json()["lead_id"] != r2.json()["lead_id"]

    def test_email_cooldown_rejects_immediate_repeat(self, client, memory, db_session, monkeypatch):
        _connect_employee(memory, db_session)
        monkeypatch.setattr(google_calendar, "get_busy_intervals", lambda *a, **k: [])
        monkeypatch.setattr(
            google_calendar, "insert_event",
            lambda **k: {"google_event_id": "ev-cd", "meet_link": "https://meet.google.com/cd"},
        )
        email = "cooldown@example.com"
        r1 = client.post(
            "/api/v1/public/booking",
            json={"name": "N", "email": email, "scheduled_at": _slot().isoformat()},
        )
        assert r1.status_code == 200, r1.text
        r2 = client.post(
            "/api/v1/public/booking",
            json={"name": "N", "email": email, "scheduled_at": (_slot() + timedelta(days=1)).isoformat()},
        )
        assert r2.status_code == 429
