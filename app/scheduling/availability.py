"""
Deterministic availability engine.

This is a real calendar query, never an LLM guess — consistent with the platform
principle that the AI never invents facts. It enumerates business-hours slots, queries
Google free/busy across every eligible connected employee, and returns only slots where
at least one of them is genuinely free for the full duration.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Optional
from zoneinfo import ZoneInfo

from app.config import settings
from app.scheduling import google_calendar

if TYPE_CHECKING:
    from app.memory.memory_manager import MemoryManager

logger = logging.getLogger(__name__)

# Customer bookings go to active employees holding `meetings.host` (Admin, Sales —
# config/access_control.yaml).
BOOKING_PERMISSION = "meetings.host"


class NoConnectedCalendarsError(RuntimeError):
    """Raised when scheduling is attempted but no eligible employee has connected a calendar."""


def _decrypt_token(encrypted: str) -> str:
    from app.security.encryption import decrypt

    return decrypt(encrypted)


def _connected_calendars(memory: "MemoryManager") -> list[tuple[str, str, str]]:
    """Return (employee_id, refresh_token_plaintext, calendar_id) for eligible,
    calendar-connected employees. Raises NoConnectedCalendarsError if there are none."""
    from app.auth import access

    employees = [e for e in memory.list_calendar_connected_employees()
                 if e.is_active and BOOKING_PERMISSION in access.permissions_for(memory.employee_roles(e.id))]
    connected = [e for e in employees if e.google_refresh_token]
    if not connected:
        raise NoConnectedCalendarsError(
            "No Sales/Admin employee has connected a Google Calendar — cannot offer "
            "meeting slots. Ask an eligible employee to connect their calendar first."
        )
    return [
        (e.id, _decrypt_token(e.google_refresh_token), e.calendar_id or "primary")
        for e in connected
    ]


def _candidate_slots(duration_minutes: int, days_ahead: int, now_utc: datetime) -> list[datetime]:
    """Generate future business-hours slot start times (UTC) over the window."""
    tz = ZoneInfo(settings.business_timezone)
    start_h = settings.business_hours_start_ist
    end_h = settings.business_hours_end_ist
    now_local = now_utc.astimezone(tz)
    slots: list[datetime] = []

    for day_offset in range(0, days_ahead + 1):
        day = (now_local + timedelta(days=day_offset)).date()
        if day.weekday() not in settings.business_days:
            continue
        cursor = datetime(day.year, day.month, day.day, start_h, 0, tzinfo=tz)
        day_end = datetime(day.year, day.month, day.day, end_h, 0, tzinfo=tz)
        while cursor + timedelta(minutes=duration_minutes) <= day_end:
            slot_utc = cursor.astimezone(timezone.utc)
            if slot_utc > now_utc:
                slots.append(slot_utc)
            cursor += timedelta(minutes=duration_minutes)
    return slots


def on_slot_grid(slot: datetime, duration_minutes: int, days_ahead: int, now: Optional[datetime] = None) -> bool:
    """Whether `slot` (timezone-aware) is one of the business-hours slot starts
    get_available_slots() would consider — same grid, same window — free or not."""
    now_utc = now or datetime.now(timezone.utc)
    return slot.astimezone(timezone.utc) in set(_candidate_slots(duration_minutes, days_ahead, now_utc))


def _overlaps(slot_start: datetime, slot_end: datetime, busy: list[tuple[datetime, datetime]]) -> bool:
    for b_start, b_end in busy:
        if slot_start < b_end and b_start < slot_end:
            return True
    return False


def get_available_slots(
    memory: "MemoryManager",
    duration_minutes: Optional[int] = None,
    days_ahead: Optional[int] = None,
    now: Optional[datetime] = None,
) -> list[datetime]:
    """Return open, business-hours-only slot start times (UTC).

    A slot is available if at least one eligible connected employee is free for the
    full duration. Zero connected employees raises NoConnectedCalendarsError.
    """
    duration_minutes = duration_minutes or settings.scheduling_default_duration_minutes
    days_ahead = days_ahead if days_ahead is not None else settings.scheduling_days_ahead
    now_utc = now or datetime.now(timezone.utc)

    calendars = _connected_calendars(memory)  # raises if none
    candidates = _candidate_slots(duration_minutes, days_ahead, now_utc)
    if not candidates:
        return []

    window_min = candidates[0]
    window_max = candidates[-1] + timedelta(minutes=duration_minutes)

    # One free/busy query per employee for the whole window, then reuse in memory.
    busy_by_employee: dict[str, list[tuple[datetime, datetime]]] = {}
    for employee_id, refresh_token, calendar_id in calendars:
        try:
            busy_by_employee[employee_id] = google_calendar.get_busy_intervals(
                refresh_token, calendar_id, window_min, window_max
            )
        except Exception:
            logger.exception("free/busy query failed for employee %s", employee_id)
            busy_by_employee[employee_id] = None  # treat as unknown → not offered

    available: list[datetime] = []
    for slot in candidates:
        slot_end = slot + timedelta(minutes=duration_minutes)
        for employee_id, _rt, _cal in calendars:
            busy = busy_by_employee.get(employee_id)
            if busy is None:
                continue  # query failed for this employee — don't offer their time
            if not _overlaps(slot, slot_end, busy):
                available.append(slot)
                break
    return available


def is_slot_free(
    memory: "MemoryManager",
    slot: datetime,
    duration_minutes: int,
) -> Optional[str]:
    """Return the employee_id of an eligible employee free for [slot, slot+duration],
    or None if nobody is free. Used to re-verify immediately before booking."""
    slot_end = slot + timedelta(minutes=duration_minutes)
    for employee_id, refresh_token, calendar_id in _connected_calendars(memory):
        try:
            busy = google_calendar.get_busy_intervals(refresh_token, calendar_id, slot, slot_end)
        except Exception:
            logger.exception("re-verify free/busy failed for employee %s", employee_id)
            continue
        if not _overlaps(slot, slot_end, busy):
            return employee_id
    return None


def is_employee_free(
    memory: "MemoryManager",
    employee_id: str,
    slot: datetime,
    duration_minutes: int,
) -> bool:
    """Re-verify one specific employee is free for the slot (dashboard booking path)."""
    slot_end = slot + timedelta(minutes=duration_minutes)
    for eid, refresh_token, calendar_id in _connected_calendars(memory):
        if eid != employee_id:
            continue
        try:
            busy = google_calendar.get_busy_intervals(refresh_token, calendar_id, slot, slot_end)
        except Exception:
            logger.exception("re-verify free/busy failed for employee %s", employee_id)
            return False
        return not _overlaps(slot, slot_end, busy)
    return False
