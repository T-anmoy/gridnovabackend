"""BE-1 S6 — the public booking endpoint books only what the availability grid offers,
for a bounded length, a bounded number of times per email per day."""
from datetime import timedelta

import pytest

from app.api.routes import public_booking
from app.config import settings
from app.rules import sales_policy
from app.scheduling import google_calendar
from app.security.encryption import encrypt, generate_key
from tests.booking_support import grid_slot

URL = "/api/v1/public/booking"


@pytest.fixture(autouse=True)
def calendar(memory, db_session, monkeypatch):
    monkeypatch.setattr(settings, "fernet_encryption_key", generate_key())
    memory.get_or_create_employee("emp-sales-1", "S", "s@gridnova.in", "sales")
    db_session.commit()
    memory.update_employee_google_credentials(employee_id="emp-sales-1", encrypted_refresh_token=encrypt("r"),
                                              calendar_id="primary", performed_by="t")
    db_session.commit()
    monkeypatch.setattr(google_calendar, "get_busy_intervals", lambda *a, **k: [])
    monkeypatch.setattr(google_calendar, "insert_event",
                        lambda **k: {"google_event_id": "ev", "meet_link": "https://meet.google.com/x"})
    public_booking._booking_request_log.clear()
    public_booking._recent_emails.clear()
    yield
    public_booking._booking_request_log.clear()
    public_booking._recent_emails.clear()


def _book(client, slot, email="limit@example.com", **extra):
    return client.post(URL, json={"name": "N", "email": email, "scheduled_at": slot if isinstance(slot, str)
                                  else slot.isoformat(), **extra})


def test_a_grid_slot_books(client):
    assert _book(client, grid_slot()).status_code == 200


def test_a_naive_datetime_is_refused(client):
    naive = grid_slot().replace(tzinfo=None).isoformat()
    r = _book(client, naive)
    assert r.status_code == 422 and r.json()["detail"]["code"] == "timezone_required"


def _three_am_tomorrow_ist():
    from zoneinfo import ZoneInfo
    ist = grid_slot().astimezone(ZoneInfo("Asia/Kolkata"))
    return (ist + timedelta(days=1)).replace(hour=3, minute=0)


@pytest.mark.parametrize("make", [lambda: grid_slot() + timedelta(minutes=7), _three_am_tomorrow_ist,
                                  lambda: grid_slot() + timedelta(days=40)],
                         ids=["off_the_grid", "outside_hours", "past_the_window"])
def test_a_time_off_the_grid_is_refused(client, make):
    r = _book(client, make())
    assert r.status_code == 422 and r.json()["detail"]["code"] == "slot_not_offered"


def test_duration_is_capped(client):
    cap = sales_policy.max_booking_duration_minutes()
    assert cap == settings.scheduling_default_duration_minutes  # null in config = the default length
    r = _book(client, grid_slot(), duration_minutes=cap * 8)
    assert r.status_code == 422 and r.json()["detail"]["code"] == "duration_too_long"
    r = client.get(f"{URL}/availability", params={"duration_minutes": 24 * 60})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "duration_too_long"


def test_a_daily_cap_per_email(client, monkeypatch):
    monkeypatch.setattr(sales_policy, "booking_daily_cap_per_email", lambda: 2)
    for i in range(2):
        public_booking._recent_emails.clear()
        assert _book(client, grid_slot(i * 3), email="Cap@Example.com").status_code == 200
    public_booking._recent_emails.clear()
    r = _book(client, grid_slot(10), email="cap@example.com")
    assert r.status_code == 429 and r.json()["detail"]["code"] == "daily_booking_limit"
    public_booking._recent_emails.clear()
    assert _book(client, grid_slot(10), email="someone-else@example.com").status_code == 200
