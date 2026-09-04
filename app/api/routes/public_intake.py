"""
Public, unauthenticated lead-intake endpoint — the backend half of the
frontend's /estimate form. Anonymous website visitors call this directly, so
it follows the same public-but-guarded pattern already used by
app/api/routes/chat.py and app/api/routes/public_proposals.py: no auth, a
hand-rolled per-IP rate limit, and a response that carries nothing sensitive.

This is deliberately a lighter-weight intake than app/api/routes/proposal_inputs.py
(the full staff-reviewed site assessment) — an anonymous estimate request, not a
qualified lead. It creates a real Lead + Project (see MemoryManager.create_lead,
Phase 1 of this change) so Sales has something real to open in the dashboard,
tagged with a distinct source so it's never confused with a chat-qualified lead.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

import re

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app.memory.db import get_db
from app.memory.memory_manager import MemoryManager
from app.memory.models import LeadSource

router = APIRouter(prefix="/api/v1/public")

# Per-IP rate limiting, matching the hand-rolled limiter already used by the
# public chat endpoint and the public accept-link endpoints. An estimate-form
# submission is a deliberate one-time action, not a chat turn, so the limit is
# tighter than either of those.
_RATE_LIMIT = 10
_RATE_WINDOW_SECONDS = 60
_request_log: defaultdict[str, list[datetime]] = defaultdict(list)

# Additional per-email guard: a resubmission for the same email address within
# this window is rejected even from a different IP (e.g. a script rotating
# addresses would still be caught by the IP limit; one rotating IPs per email
# would not be, which is exactly the gap this closes).
_EMAIL_COOLDOWN_SECONDS = 60
_recent_emails: dict[str, datetime] = {}


def _client_ip(request: Request) -> str:
    if request.client and request.client.host:
        return request.client.host
    return "127.0.0.1"


def _check_rate_limit(request: Request) -> None:
    ip = _client_ip(request)
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=_RATE_WINDOW_SECONDS)
    _request_log[ip] = [t for t in _request_log[ip] if t > cutoff]
    if len(_request_log[ip]) >= _RATE_LIMIT:
        raise HTTPException(status_code=429, detail="Rate limit exceeded. Please wait before submitting again.")
    _request_log[ip].append(now)


def _check_email_cooldown(email: str) -> None:
    now = datetime.now(timezone.utc)
    key = email.strip().lower()
    last = _recent_emails.get(key)
    if last is not None and (now - last).total_seconds() < _EMAIL_COOLDOWN_SECONDS:
        raise HTTPException(
            status_code=429,
            detail="An estimate request for this email was just submitted. Please wait before resubmitting.",
        )
    _recent_emails[key] = now


# Same lightweight pattern app/llm/client.py's mock field extractor uses — this
# codebase does not use pydantic's EmailStr (would need the email-validator
# package, which isn't installed), so a basic shape check is the existing standard.
_EMAIL_RE = re.compile(r"^[\w.+-]+@[\w-]+\.[\w.-]+$")


class EstimateRequest(BaseModel):
    # Required — the minimum needed to create a real Customer + Lead.
    name: str = Field(min_length=1, max_length=200)
    email: str = Field(min_length=3, max_length=254)

    @field_validator("email")
    @classmethod
    def _validate_email(cls, value: str) -> str:
        if not _EMAIL_RE.match(value.strip()):
            raise ValueError("must be a valid email address")
        return value.strip()

    # Everything below is optional: this endpoint has no access to the actual
    # frontend form to confirm its required/optional split, so nothing here is
    # forced beyond identity — see the accompanying report.
    phone: Optional[str] = Field(default=None, max_length=30)
    customer_type: Optional[str] = Field(default=None, max_length=50)
    state: Optional[str] = Field(default=None, max_length=100)
    city: Optional[str] = Field(default=None, max_length=100)
    average_bill: Optional[float] = Field(default=None, ge=0)
    roof_type: Optional[str] = Field(default=None, max_length=50)
    roof_area: Optional[float] = Field(default=None, ge=0)
    floors: Optional[int] = Field(default=None, ge=1)
    connection_phase: Optional[str] = Field(default=None, max_length=20)


class EstimateResponse(BaseModel):
    lead_id: int
    success: bool


def _location(state: Optional[str], city: Optional[str]) -> Optional[str]:
    if city and state:
        return f"{city}, {state}"
    return city or state or None


@router.post("/estimate-request", response_model=EstimateResponse)
def submit_estimate_request(
    body: EstimateRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    _check_rate_limit(request)
    _check_email_cooldown(body.email)

    memory = MemoryManager(db)
    customer = memory.get_or_create_customer(
        email=body.email,
        name=body.name,
        performed_by="website",
        phone=body.phone,
    )

    # Real project details go straight onto the Project row (Phase 1 wiring) —
    # create_lead() only creates an empty one when the caller has nothing to give it.
    project = memory.create_project(
        customer_id=customer.id,
        performed_by="website",
        state=body.state,
        location=_location(body.state, body.city),
        roof_type=body.roof_type,
        monthly_electricity_bill=body.average_bill,
    )

    lead = memory.create_lead(
        customer_id=customer.id,
        project_id=project.id,
        performed_by="website",
        source=LeadSource.website_estimate_form,
    )

    # Everything the form captured that has no dedicated Project column, plus
    # the identity fields — collected_fields is the same free-form store the
    # chat-qualification path writes to, so this lead reads the same way in
    # the dashboard as any other.
    extra_fields = {
        k: v
        for k, v in {
            "name": body.name,
            "email": body.email,
            "phone": body.phone,
            "location": _location(body.state, body.city),
            "monthly_electricity_bill": body.average_bill,
            "customer_type": body.customer_type,
            "roof_area_sqft": body.roof_area,
            "floors": body.floors,
            "connection_phase": body.connection_phase,
        }.items()
        if v is not None
    }
    if extra_fields:
        memory.update_lead_fields(lead_id=lead.id, new_fields=extra_fields, performed_by="website")

    db.commit()
    return EstimateResponse(lead_id=lead.id, success=True)
