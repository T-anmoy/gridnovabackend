"""Months, financial years and working days — all in the business timezone (IST).

A payout period is a calendar month "YYYY-MM". A financial year runs 1 April to 31
March and is written "2026-27". An instant belongs to the IST date it falls on, so a
commission earned at 23:59 IST on 31 March is in the old financial year and one earned
at 00:01 IST on 1 April in the new one.
"""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from app.config import settings

_PERIOD = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")


def _tz() -> ZoneInfo:
    return ZoneInfo(settings.business_timezone)


def today_ist() -> date:
    return datetime.now(_tz()).date()


def ist_date(when: datetime) -> date:
    """The IST date an instant falls on (a naive value is UTC, as everywhere here)."""
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(_tz()).date()


def parse_period(period: str) -> Optional[tuple[int, int]]:
    m = _PERIOD.match(period or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def period_of(day: date) -> str:
    return f"{day.year:04d}-{day.month:02d}"


def first_day(period: str) -> date:
    y, m = parse_period(period)
    return date(y, m, 1)


def next_period(period: str) -> str:
    y, m = parse_period(period)
    return f"{y + (m == 12):04d}-{(m % 12) + 1:02d}"


def previous_period(period: str) -> str:
    y, m = parse_period(period)
    return f"{y - (m == 1):04d}-{((m - 2) % 12) + 1:02d}"


def last_day(period: str) -> date:
    return first_day(next_period(period)) - timedelta(days=1)


def start_utc(day: date) -> datetime:
    """The first instant of an IST date, in UTC."""
    return datetime.combine(day, time.min, tzinfo=_tz()).astimezone(timezone.utc)


def period_end_utc(period: str) -> datetime:
    """The first instant AFTER the period (00:00 IST on the 1st of the next month)."""
    return start_utc(first_day(next_period(period)))


def financial_year(day: date) -> str:
    start = day.year if day.month >= 4 else day.year - 1
    return f"{start}-{(start + 1) % 100:02d}"


def fy_of_period(period: str) -> str:
    return financial_year(first_day(period))


def fy_first_period(fy: str) -> str:
    return f"{int(fy[:4]):04d}-04"


def valid_fy(fy: str) -> bool:
    return bool(re.match(r"^\d{4}-\d{2}$", fy or "")) and (int(fy[:4]) + 1) % 100 == int(fy[5:])


def add_working_days(start: date, days: int, weekdays: list[int]) -> date:
    """The date `days` working days after `start` (start itself not counted)."""
    day, left = start, days
    while left > 0:
        day += timedelta(days=1)
        if day.weekday() in weekdays:
            left -= 1
    return day
