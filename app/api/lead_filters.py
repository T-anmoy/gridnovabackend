"""Optional server-side filters shared by GET /dashboard/leads and /leads/export.

They are applied inside the caller's role scope and before pagination (the callers pass
them to MemoryManager), so `total` stays correct. Bad input is a structured 422.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import select

from app.config import settings
from app.memory.models import Associate, Lead, LeadScore, PipelineStage

# Scores are ordinal labels, not numbers: low < medium < high.
SCORE_ORDER = (LeadScore.low, LeadScore.medium, LeadScore.high)


def _bad(code: str, message: str, field: str) -> HTTPException:
    return HTTPException(status_code=422, detail={"code": code, "message": message, "field": field})


@dataclass(frozen=True)
class LeadFilters:
    stages: tuple[PipelineStage, ...] = ()
    scores: tuple[LeadScore, ...] = ()       # empty = no score filter
    created_from: Optional[datetime] = None  # inclusive, UTC
    created_before: Optional[datetime] = None  # exclusive, UTC
    referred_by_associate_id: Optional[str] = None  # an Associate ID, GNA-…  (BE-10)

    def conditions(self) -> list:
        out = []
        if self.stages:
            out.append(Lead.pipeline_stage.in_(self.stages))
        if self.scores:
            out.append(Lead.lead_score.in_(self.scores))  # unscored leads never match
        if self.created_from is not None:
            out.append(Lead.created_at >= self.created_from)
        if self.created_before is not None:
            out.append(Lead.created_at < self.created_before)
        if self.referred_by_associate_id is not None:
            out.append(Lead.referred_by_associate_id.in_(
                select(Associate.id).where(Associate.associate_id == self.referred_by_associate_id)))
        return out


def _ist_day(value: str, field: str) -> date:
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        raise _bad("invalid_date", f"{field} must be a calendar day, YYYY-MM-DD.", field)


def _score(value: Optional[str], field: str) -> Optional[LeadScore]:
    if value is None or not value.strip():
        return None
    try:
        return LeadScore(value.strip().lower())
    except ValueError:
        allowed = ", ".join(s.value for s in SCORE_ORDER)
        raise _bad("unknown_score", f"{field} must be one of: {allowed}.", field)


def parse_lead_filters(
    stage: Optional[list[str]] = None,
    min_score: Optional[str] = None,
    max_score: Optional[str] = None,
    created_from: Optional[str] = None,
    created_to: Optional[str] = None,
    referred_by_associate_id: Optional[str] = None,
) -> LeadFilters:
    stages: list[PipelineStage] = []
    for raw in stage or []:
        for part in raw.split(","):  # ?stage=new&stage=won and ?stage=new,won both work
            part = part.strip()
            if not part:
                continue
            try:
                value = PipelineStage(part)
            except ValueError:
                allowed = ", ".join(s.value for s in PipelineStage)
                raise _bad("unknown_stage", f"Unknown stage {part!r}. Valid stages: {allowed}.", "stage")
            if value not in stages:
                stages.append(value)

    low, high = _score(min_score, "min_score"), _score(max_score, "max_score")
    scores: tuple[LeadScore, ...] = ()
    if low or high:
        lo = SCORE_ORDER.index(low) if low else 0
        hi = SCORE_ORDER.index(high) if high else len(SCORE_ORDER) - 1
        if lo > hi:
            raise _bad("invalid_score_range", "min_score is higher than max_score.", "min_score")
        scores = SCORE_ORDER[lo:hi + 1]

    tz = ZoneInfo(settings.business_timezone)
    start = end = None
    d_from = _ist_day(created_from, "created_from") if created_from and created_from.strip() else None
    d_to = _ist_day(created_to, "created_to") if created_to and created_to.strip() else None
    if d_from and d_to and d_from > d_to:
        raise _bad("invalid_date_range", "created_from is after created_to.", "created_from")
    if d_from:
        start = datetime.combine(d_from, time.min, tzinfo=tz).astimezone(timezone.utc)
    if d_to:
        end = datetime.combine(d_to + timedelta(days=1), time.min, tzinfo=tz).astimezone(timezone.utc)
    referrer = None
    if referred_by_associate_id is not None and referred_by_associate_id.strip():
        from app.associates.referrals import normalise
        referrer = normalise(referred_by_associate_id)
        if referrer is None:
            raise _bad("invalid_associate_id", "referred_by_associate_id must be an Associate ID like "
                       "GNA-WB-26-00001.", "referred_by_associate_id")
    return LeadFilters(tuple(stages), scores, start, end, referrer)
