"""A bookable slot for public-booking tests: one the availability grid really offers
(BE-1 S6 refuses anything else), an hour or more ahead."""
from datetime import datetime, timedelta, timezone

from app.config import settings
from app.scheduling.availability import _candidate_slots


def grid_slot(index: int = 0, duration_minutes: int = None) -> datetime:
    now = datetime.now(timezone.utc)
    slots = [s for s in _candidate_slots(duration_minutes or settings.scheduling_default_duration_minutes, 14, now)
             if s > now + timedelta(hours=1)]
    return slots[index]
