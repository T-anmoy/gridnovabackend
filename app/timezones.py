"""
Shared IST (Asia/Kolkata) formatting for any timestamp turned into
human-readable text — timeline descriptions, notification text, and PDF
dates, plus the meeting-slot labels the Sales Agent and public booking API
already showed customers before this module existed (previously duplicated
in both places; centralized here so they can't drift apart).

Never use this for an API response field that hands a raw timestamp to a
frontend to format itself — those stay untouched (UTC, ISO 8601). This module
is only for a string a human reads directly.
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.config import settings


def _as_ist(when: datetime) -> datetime:
    """Naive datetimes are treated as UTC (SQLite can hand those back) before
    converting to the business timezone."""
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(ZoneInfo(settings.business_timezone))


def format_ist_datetime(when: datetime) -> str:
    """e.g. 'Tue 09 Sep, 06:30 IST' — the existing slot-label format."""
    return _as_ist(when).strftime("%a %d %b, %H:%M IST")


def format_ist_date(when: datetime) -> str:
    """e.g. '09 September 2026' — date only, no time-of-day."""
    return _as_ist(when).strftime("%d %B %Y")
