"""
Meeting booking — the single shared entry point for both the conversational
(Sales Agent) path and the employee-initiated dashboard path.

Booking re-verifies the slot against the live calendar immediately before creating the
event, so a slot that was free when offered but taken since cannot be double-booked.
The Google Calendar event carries a real Meet link and emails both parties directly
(sendUpdates="all") — we never build a separate email system for invites.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy.exc import IntegrityError

from app.memory.models import Meeting, TimelineEventType
from app.scheduling import availability, google_calendar

if TYPE_CHECKING:
    from app.memory.memory_manager import MemoryManager

logger = logging.getLogger(__name__)

# In-process lock serializing the check-then-insert around a single (employee,
# slot) pair — narrows the race window down to just cross-process/cross-worker
# concurrency, which the DB-level partial unique index on meetings(employee_id,
# scheduled_at) below then guarantees can never actually double-book.
_booking_lock = threading.Lock()


class SlotUnavailableError(RuntimeError):
    """Raised when the requested slot is no longer free at booking time."""


def book_meeting(
    memory: "MemoryManager",
    lead_id: int,
    employee_id: str,
    scheduled_at: datetime,
    duration_minutes: int,
    performed_by: str,
) -> Meeting:
    ctx = memory.get_lead_context(lead_id)
    lead = ctx.get("lead")
    customer = ctx.get("customer")
    if not lead:
        raise ValueError(f"Lead {lead_id} not found")

    employee = memory.get_employee(employee_id)
    if not employee or not employee.google_calendar_connected or not employee.google_refresh_token:
        raise SlotUnavailableError(
            f"Employee {employee_id} has no connected Google Calendar to book on."
        )

    # Re-verify immediately before booking — someone may have taken the slot since it
    # was offered. This narrows the race but, under real concurrency, two requests can
    # both pass this check before either has committed (confirmed live). The
    # process-local lock below serializes same-process attempts for this exact
    # (employee, slot) window; the partial unique index on meetings(employee_id,
    # scheduled_at) (migration 0009) is the actual, database-level guarantee —
    # caught as an IntegrityError just below and turned into the same
    # SlotUnavailableError every caller already handles.
    with _booking_lock:
        if not availability.is_employee_free(memory, employee_id, scheduled_at, duration_minutes):
            raise SlotUnavailableError(
                "That slot is no longer available — it was taken since it was offered."
            )

        from app.security.encryption import decrypt

        refresh_token = decrypt(employee.google_refresh_token)
        calendar_id = employee.calendar_id or "primary"
        end = scheduled_at + timedelta(minutes=duration_minutes)
        attendees = [e for e in [customer.email if customer else None, employee.email] if e]

        event = google_calendar.insert_event(
            refresh_token=refresh_token,
            calendar_id=calendar_id,
            summary=f"Grid Nova Energy — consultation with {customer.name if customer else 'customer'}",
            start=scheduled_at,
            end=end,
            attendee_emails=attendees,
            description="Solar consultation booked via Grid Nova.",
        )

        try:
            meeting = memory.create_meeting(
                lead_id=lead_id,
                employee_id=employee_id,
                scheduled_at=scheduled_at,
                duration_minutes=duration_minutes,
                performed_by=performed_by,
                google_event_id=event.get("google_event_id"),
                meet_link=event.get("meet_link"),
            )
        except IntegrityError:
            memory.rollback()
            raise SlotUnavailableError(
                "That slot is no longer available — it was just booked by someone else."
            )

        memory.add_timeline_event(
            lead_id=lead_id,
            event_type=TimelineEventType.meeting_scheduled,
            description=f"Meeting scheduled for {scheduled_at.isoformat()} with {employee_id}",
            created_by=performed_by,
        )

    from app.notifications.notifier import notifier
    notifier.meeting_booked(
        meeting_id=meeting.id,
        lead_id=lead_id,
        employee_id=employee_id,
        scheduled_at=scheduled_at.isoformat(),
    )

    return meeting
