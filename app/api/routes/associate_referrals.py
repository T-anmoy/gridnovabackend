"""Referral links for associates — associate-authenticated (BE-10; docs/api/associate_ease.md).

Both routes serve Associates and Preferred Associates alike (get_current_associate
covers both types, as every /api/v1/associate route does). An associate sees only the
enquiries that came through their OWN link — never another associate's.
"""
from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.associates import referrals
from app.auth.dependencies import AuthenticatedAssociate, get_current_associate
from app.memory.db import get_db
from app.memory.models import Associate

router = APIRouter(prefix="/api/v1/associate")


class ReferralLinkResponse(BaseModel):
    associate_id: str
    # Relative to the public website; the frontend prefixes its own origin.
    share_path: str


class ReferralItem(BaseModel):
    lead_id: int
    created_at: str
    customer_name: Optional[str] = None
    # As the customer typed it on the form (city, state) — the public forms do not ask
    # for a district.
    location: Optional[str] = None
    pin_code: Optional[str] = None
    enquiry_kind: Literal["estimate", "call_booked", "other"]
    status: Literal["received", "in_progress", "proposal_sent", "won", "closed"]
    share_with_referrer: bool
    # Present ONLY when the customer agreed to share their details with the referrer.
    customer_mobile: Optional[str] = None


class ReferralListResponse(BaseModel):
    items: list[ReferralItem]
    page: int
    page_size: int
    total: int


@router.get("/referral-link", response_model=ReferralLinkResponse)
def referral_link(db: Session = Depends(get_db), current: AuthenticatedAssociate = Depends(get_current_associate)):
    """The caller's Associate ID and the share path, so the frontend never builds it."""
    return referrals.link(db.get(Associate, current.associate_pk))


@router.get("/referrals", response_model=ReferralListResponse, response_model_exclude_unset=True)
def my_referrals(page: int = Query(default=1, ge=1), page_size: int = Query(default=25, ge=1, le=100),
                 db: Session = Depends(get_db), current: AuthenticatedAssociate = Depends(get_current_associate)):
    """The caller's own referred enquiries, newest first."""
    data = referrals.page(db, db.get(Associate, current.associate_pk), page, page_size)
    return ReferralListResponse(items=[ReferralItem(**i) for i in data["items"]], page=data["page"],
                                page_size=data["page_size"], total=data["total"])
