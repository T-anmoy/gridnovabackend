"""
Public, unauthenticated meeting-booking endpoints — the backend half of the
frontend's /book-a-call page. Anonymous website visitors call these directly, so
this follows the same public-but-guarded pattern already used by
app/api/routes/public_intake.py: no auth, a hand-rolled per-IP rate limit, and a
response that carries nothing sensitive.

Booking reuses app/scheduling/availability.py and app/scheduling/booking.py
completely unmodified — the same race-safety (in-process lock + the DB-level
partial unique index from migration 0009) and the same eligible-employee
selection the conversational (Sales Agent) and staff dashboard paths already
rely on. This module adds no new scheduling logic of its own.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api import input_bounds as lim
from app.config import settings
from app.memory.db import get_db
from app.memory.memory_manager import MemoryManager
from app.memory.models import Customer, Lead, LeadSource
from app.rules import sales_policy
from app.scheduling import availability, booking, google_calendar
from app.security.client_ip import client_ip
from app.security.rate_limit import RateLimitLog, RecentKeys
from app.timezones import format_ist_datetime

router = APIRouter(prefix="/api/v1/public/booking")

# Per-IP rate limiting, matching the hand-rolled limiter already used by the
# public chat and estimate-request endpoints. Availability is read-only and gets
# polled more freely; booking is a real write action, so it's tighter — same
# split in spirit as chat.py's own message-vs-lead-creation distinction.
_AVAILABILITY_RATE_LIMIT = 20
_BOOKING_RATE_LIMIT = 5
_RATE_WINDOW_SECONDS = 60
_availability_request_log = RateLimitLog(_RATE_WINDOW_SECONDS)
_booking_request_log = RateLimitLog(_RATE_WINDOW_SECONDS)

# Same per-email guard as public_intake.py: a repeat booking attempt for the
# same email within this window is rejected even from a different IP.
_EMAIL_COOLDOWN_SECONDS = 60
_recent_emails = RecentKeys(_EMAIL_COOLDOWN_SECONDS)

MAX_DAYS_AHEAD = 30
DEFAULT_DAYS_AHEAD = 14

# How many genuinely-free nearby slots to offer alongside a slot_unavailable
# error, so the caller has real options rather than just a dead end.
NEARBY_ALTERNATIVES_COUNT = 3

# Same lightweight pattern public_intake.py uses — this codebase does not use
# pydantic's EmailStr (would need the email-validator package, which isn't
# installed), so a basic shape check is the existing standard.
_EMAIL_RE = re.compile(r"^[\w.+-]+@[\w-]+\.[\w.-]+$")


def _check_rate_limit(request: Request, log: RateLimitLog, limit: int) -> None:
    if not log.hit(client_ip(request), limit):
        raise HTTPException(status_code=429, detail="Rate limit exceeded. Please wait before trying again.")


def _check_email_cooldown(email: str) -> None:
    now = datetime.now(timezone.utc)
    key = email.strip().lower()
    last = _recent_emails.get(key)
    if last is not None and (now - last).total_seconds() < _EMAIL_COOLDOWN_SECONDS:
        raise HTTPException(
            status_code=429,
            detail="A booking request for this email was just submitted. Please wait before resubmitting.",
        )
    _recent_emails[key] = now


def _duration(requested: Optional[int]) -> int:
    """The meeting length: the default unless asked, never above the configured cap
    (config/sales_policy.yaml booking.max_duration_minutes)."""
    duration = requested or settings.scheduling_default_duration_minutes
    cap = sales_policy.max_booking_duration_minutes()
    if duration > cap:
        raise HTTPException(status_code=422, detail={
            "code": "duration_too_long", "field": "duration_minutes",
            "message": f"A call can be at most {cap} minutes.", "max_duration_minutes": cap})
    return duration


def _check_daily_cap(db: Session, email: str) -> None:
    """At most booking.daily_cap_per_email public bookings per email per IST day."""
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(settings.business_timezone)
    day_start = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    count = db.execute(select(func.count(Lead.id)).join(Customer, Customer.id == Lead.customer_id).where(
        func.lower(Customer.email) == email.strip().lower(), Lead.source == LeadSource.website_booking,
        Lead.created_at >= day_start)).scalar_one()
    cap = sales_policy.booking_daily_cap_per_email()
    if count >= cap:
        raise HTTPException(status_code=429, detail={
            "code": "daily_booking_limit",
            "message": f"This email has already booked {cap} calls today. Please try again tomorrow."})


def _nearby_alternatives(memory: MemoryManager, requested_slot: datetime, duration_minutes: int) -> list[dict]:
    """Genuinely-free slots closest in time to the one that was just refused.

    Calls availability.get_available_slots() fresh, right now — never reuses an
    earlier /availability response — so what's offered here is guaranteed free at
    the moment of this response, not stale. Sorted by closeness to what the caller
    actually asked for; this never invents or auto-picks a slot on the caller's
    behalf, it only surfaces real options for them to choose from.
    """
    try:
        slots = availability.get_available_slots(
            memory, duration_minutes=duration_minutes, days_ahead=DEFAULT_DAYS_AHEAD,
        )
    except availability.NoConnectedCalendarsError:
        return []
    # Exclude the exact requested slot itself — it just proved unavailable.
    slots = [s for s in slots if s != requested_slot]
    slots.sort(key=lambda s: abs((s - requested_slot).total_seconds()))
    nearest = slots[:NEARBY_ALTERNATIVES_COUNT]
    return [{"scheduled_at": s.isoformat(), "label": format_ist_datetime(s)} for s in nearest]


def _raise_slot_unavailable(memory: MemoryManager, requested_slot: datetime, duration_minutes: int) -> None:
    raise HTTPException(
        status_code=409,
        detail={
            "code": "slot_unavailable",
            "message": (
                f"The {format_ist_datetime(requested_slot)} slot is no longer available — "
                "it was just taken. Please pick another time."
            ),
            "requested_slot": requested_slot.isoformat(),
            "alternatives": _nearby_alternatives(memory, requested_slot, duration_minutes),
        },
    )


class AvailabilityResponse(BaseModel):
    duration_minutes: int
    days_ahead: int
    slots: list[dict]


@router.get("/availability", response_model=AvailabilityResponse)
def get_public_availability(
    request: Request,
    duration_minutes: Optional[int] = Query(default=None, gt=0),
    days_ahead: int = Query(default=DEFAULT_DAYS_AHEAD, ge=1, le=MAX_DAYS_AHEAD),
    db: Session = Depends(get_db),
):
    _check_rate_limit(request, _availability_request_log, _AVAILABILITY_RATE_LIMIT)

    memory = MemoryManager(db)
    duration = _duration(duration_minutes)

    try:
        slots = availability.get_available_slots(
            memory, duration_minutes=duration, days_ahead=days_ahead,
        )
    except availability.NoConnectedCalendarsError:
        # Graceful, not an error: the same "degrade gracefully" philosophy the
        # conversational booking path already uses for this exact condition.
        slots = []
    except google_calendar.GoogleCalendarNotConfiguredError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "calendar_not_configured", "message": str(exc)},
        )

    return AvailabilityResponse(
        duration_minutes=duration,
        days_ahead=days_ahead,
        slots=[
            {"scheduled_at": s.isoformat(), "label": format_ist_datetime(s)}
            for s in slots
        ],
    )


class BookingRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    email: str = Field(min_length=3, max_length=254)
    phone: Optional[str] = Field(default=None, max_length=30)
    scheduled_at: datetime
    duration_minutes: Optional[int] = Field(default=None, gt=0)

    # Carried forward from the frontend's SolarCalculator context (the
    # /book-a-call page already shows this to the visitor) — no dedicated
    # columns exist for these, so they land in Lead.collected_fields, the same
    # free-form store public_intake.py's own extra fields use.
    estimated_total: Optional[float] = lim.bounded(ge=0, le=lim.MAX_PROJECT_INR)
    sizing_method: Optional[str] = Field(default=None, max_length=50)
    sizing_value: Optional[float] = lim.bounded(ge=0, le=lim.MAX_MONTHLY_INR)
    source: Optional[str] = Field(default=None, max_length=100)

    @field_validator("email")
    @classmethod
    def _validate_email(cls, value: str) -> str:
        if not _EMAIL_RE.match(value.strip()):
            raise ValueError("must be a valid email address")
        return value.strip()


class BookingResponse(BaseModel):
    success: bool
    lead_id: int
    meeting_id: int
    scheduled_at: str
    duration_minutes: int
    meet_link: Optional[str] = None


@router.post("", response_model=BookingResponse)
def create_public_booking(
    body: BookingRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    _check_rate_limit(request, _booking_request_log, _BOOKING_RATE_LIMIT)

    scheduled_at = body.scheduled_at
    if scheduled_at.tzinfo is None:
        raise HTTPException(status_code=422, detail={
            "code": "timezone_required", "field": "scheduled_at",
            "message": "scheduled_at must carry a timezone offset (e.g. 2026-10-12T04:30:00+00:00)."})
    if scheduled_at <= datetime.now(timezone.utc):
        raise HTTPException(status_code=422, detail="scheduled_at must be in the future.")
    duration = _duration(body.duration_minutes)
    # Only a slot the availability query could have offered: business hours, the
    # booking window, the slot grid. Whether it is still free is checked below.
    if not availability.on_slot_grid(scheduled_at, duration, MAX_DAYS_AHEAD):
        raise HTTPException(status_code=422, detail={
            "code": "slot_not_offered", "field": "scheduled_at",
            "message": "That time is not one of the bookable slots. Pick a time from the availability list."})
    _check_daily_cap(db, body.email)
    _check_email_cooldown(body.email)

    memory = MemoryManager(db)

    # Re-verify the slot is genuinely still free immediately before booking —
    # never trust a potentially-stale earlier availability response. This is the
    # same primitive the conversational auto-book path uses to pick an eligible,
    # actually-free employee without the caller needing to know one in advance.
    try:
        employee_id = availability.is_slot_free(memory, scheduled_at, duration)
    except availability.NoConnectedCalendarsError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "no_calendars_connected", "message": str(exc)},
        )
    if not employee_id:
        _raise_slot_unavailable(memory, scheduled_at, duration)

    customer = memory.get_or_create_customer(
        email=body.email,
        name=body.name,
        performed_by="website",
        phone=body.phone,
    )
    lead = memory.create_lead(
        customer_id=customer.id,
        performed_by="website",
        source=LeadSource.website_booking,
    )

    extra_fields = {
        k: v
        for k, v in {
            "name": body.name,
            "email": body.email,
            "phone": body.phone,
            "estimated_total": body.estimated_total,
            "sizing_method": body.sizing_method,
            "sizing_value": body.sizing_value,
            "source": body.source,
        }.items()
        if v is not None
    }
    if extra_fields:
        memory.update_lead_fields(lead_id=lead.id, new_fields=extra_fields, performed_by="website")

    try:
        meeting = booking.book_meeting(
            memory=memory,
            lead_id=lead.id,
            employee_id=employee_id,
            scheduled_at=scheduled_at,
            duration_minutes=duration,
            performed_by="website",
        )
        from app.notifications.notifier import notifier
        new_lead_id = lead.id
        memory.notify_after_commit(
            lambda: notifier.new_lead(lead_id=new_lead_id, source=LeadSource.website_booking.value,
                                      customer_name=body.name, created_by="website"),
            "new_lead",
        )
        db.commit()
    except booking.SlotUnavailableError:
        db.rollback()
        _raise_slot_unavailable(memory, scheduled_at, duration)
    except google_calendar.GoogleCalendarNotConfiguredError as exc:
        db.rollback()
        raise HTTPException(
            status_code=503,
            detail={"code": "calendar_not_configured", "message": str(exc)},
        )

    return BookingResponse(
        success=True,
        lead_id=lead.id,
        meeting_id=meeting.id,
        scheduled_at=meeting.scheduled_at.isoformat(),
        duration_minutes=meeting.duration_minutes,
        meet_link=meeting.meet_link,
    )
