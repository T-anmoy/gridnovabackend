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

from datetime import datetime, timezone
from typing import Literal, Optional

import re

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app.api import input_bounds as lim
from app.associates import referrals
from app.memory.db import get_db
from app.memory.lead_fields import apply_lead_fields
from app.memory.memory_manager import MemoryManager
from app.memory.models import LeadSource
from app.security.client_ip import client_ip
from app.security.rate_limit import RateLimitLog, RecentKeys
from app.security.internal_key import forwarded_customer_ip, has_valid_internal_key

router = APIRouter(prefix="/api/v1/public")

# Per-IP rate limiting, matching the hand-rolled limiter already used by the
# public chat endpoint and the public accept-link endpoints. An estimate-form
# submission is a deliberate one-time action, not a chat turn, so the limit is
# tighter than either of those.
_RATE_LIMIT = 10
_RATE_WINDOW_SECONDS = 60
_request_log = RateLimitLog(_RATE_WINDOW_SECONDS)

# Additional per-email guard: a resubmission for the same email address within
# this window is rejected even from a different IP (e.g. a script rotating
# addresses would still be caught by the IP limit; one rotating IPs per email
# would not be, which is exactly the gap this closes).
_EMAIL_COOLDOWN_SECONDS = 60
_recent_emails = RecentKeys(_EMAIL_COOLDOWN_SECONDS)

# The contact form gets the same limits in its own buckets, so an estimate request
# and a contact message from the same visitor don't block each other.
_contact_request_log = RateLimitLog(_RATE_WINDOW_SECONDS)
_recent_contact_emails = RecentKeys(_EMAIL_COOLDOWN_SECONDS)


def _rate_limit_identity(request: Request) -> Optional[str]:
    """Who the per-IP limit counts against, or None to skip it.

    The website mirrors every estimate-form lead here server-to-server, so without
    this every website visitor would share the hosting provider's one IP bucket, and
    a 429 would silently drop the lead. A caller holding INTERNAL_API_KEY is exempt
    from the per-IP limit; if it vouches for the visitor's address via X-Customer-IP,
    that address is limited instead. The per-email cooldown applies either way.
    """
    if has_valid_internal_key(request):
        return forwarded_customer_ip(request)
    return client_ip(request)


def _check_rate_limit(request: Request, log: Optional[RateLimitLog] = None) -> None:
    log = _request_log if log is None else log
    ip = _rate_limit_identity(request)
    if ip is None:
        return
    if not log.hit(ip, _RATE_LIMIT):
        raise HTTPException(status_code=429, detail="Rate limit exceeded. Please wait before submitting again.")


def _check_email_cooldown(email: str, recent: Optional[RecentKeys] = None, what: str = "An estimate request") -> None:
    recent = _recent_emails if recent is None else recent
    now = datetime.now(timezone.utc)
    key = email.strip().lower()
    last = recent.get(key)
    if last is not None and (now - last).total_seconds() < _EMAIL_COOLDOWN_SECONDS:
        raise HTTPException(
            status_code=429,
            detail=f"{what} for this email was just submitted. Please wait before resubmitting.",
        )
    recent[key] = now


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
    average_bill: Optional[float] = lim.bounded(ge=0, le=lim.MAX_MONTHLY_INR)
    roof_type: Optional[str] = Field(default=None, max_length=50)
    roof_area: Optional[float] = lim.bounded(ge=0, le=lim.MAX_AREA_SQFT)
    floors: Optional[int] = Field(default=None, ge=1, le=lim.MAX_FLOORS)
    connection_phase: Optional[str] = Field(default=None, max_length=20)
    # The website Solar Calculator's own fields (the configurator-lead mirror). All
    # optional and bounded; kept on the lead's collected_fields under these names.
    pin_code: Optional[str] = Field(default=None, pattern=r"^\d{6}$")
    discom: Optional[str] = Field(default=None, max_length=100)
    monthly_units: Optional[float] = lim.bounded(ge=0, le=lim.MAX_MONTHLY_KWH)
    sizing_method: Optional[str] = Field(default=None, max_length=50)
    access_difficulty: Optional[str] = Field(default=None, max_length=50)
    shading: Optional[str] = Field(default=None, max_length=50)
    subsidy_interest: Optional[Literal["yes", "no"]] = None
    indicative_total_inr: Optional[float] = lim.bounded(ge=0, le=lim.MAX_PROJECT_INR)
    # BE-10 referral link (/estimate?ref=GNA-…): an Associate ID. Anything that is not an
    # active Associate's ID is ignored silently — never an error (app/associates/referrals.py).
    ref: Optional[str] = None
    _ref = field_validator("ref", mode="before")(referrals.lenient_ref)
    # The consent line "Share my details with the Grid Nova Associate who referred me".
    share_with_referrer: Optional[bool] = None


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
    referrals.record(db, lead, body.ref, body.share_with_referrer, performed_by="website")

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
            "pin_code": body.pin_code,
            "discom": body.discom,
            "monthly_units": body.monthly_units,
            "sizing_method": body.sizing_method,
            "access_difficulty": body.access_difficulty,
            "shading": body.shading,
            "subsidy_interest": body.subsidy_interest,
            "indicative_total_inr": body.indicative_total_inr,
        }.items()
        if v is not None
    }
    if extra_fields:
        # Evaluated exactly as a manual field edit is (score, forward-only stage), so
        # the lead's stage reflects what the form actually collected.
        apply_lead_fields(memory, lead, extra_fields, performed_by="website", via="website estimate form")

    db.commit()
    return EstimateResponse(lead_id=lead.id, success=True)


# ── Contact form ──────────────────────────────────────────────────────────────
# The website's contact form, mirrored here server-to-server exactly like the
# estimate form. No staff email: the website itself emails info@gridnova.in for
# every contact message (see app/notifications/notifier.py).

class ContactRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    email: str = Field(min_length=3, max_length=254)
    phone: Optional[str] = Field(default=None, max_length=30)
    message: str = Field(min_length=1, max_length=2000)
    # The form's optional "what are you interested in" choice.
    interest: Optional[str] = Field(default=None, max_length=100)

    @field_validator("email")
    @classmethod
    def _validate_email(cls, value: str) -> str:
        if not _EMAIL_RE.match(value.strip()):
            raise ValueError("must be a valid email address")
        return value.strip()

    @field_validator("name", "message")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value.strip()


@router.post("/contact", response_model=EstimateResponse)
def submit_contact(
    body: ContactRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    _check_rate_limit(request, _contact_request_log)
    _check_email_cooldown(body.email, _recent_contact_emails, what="A message")

    memory = MemoryManager(db)
    customer = memory.get_or_create_customer(
        email=body.email, name=body.name, performed_by="website", phone=body.phone,
    )
    lead = memory.create_lead(
        customer_id=customer.id, performed_by="website", source=LeadSource.website_contact,
    )
    fields = {
        k: v for k, v in {
            "name": body.name,
            "email": body.email,
            "phone": body.phone,
            "notes": body.message,
            "interest": body.interest,
        }.items() if v is not None
    }
    apply_lead_fields(memory, lead, fields, performed_by="website", via="website contact form")
    db.commit()
    return EstimateResponse(lead_id=lead.id, success=True)
