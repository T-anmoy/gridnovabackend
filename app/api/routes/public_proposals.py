"""
Public, unauthenticated proposal accept-link endpoints.

A customer can view and accept a proposal online via a random accept_token (generated
at send time). This is deliberately lightweight — no e-signature vendor. Customer
acceptance is a DISTINCT concept from the internal two-stage approval gate; it never
touches staff approval/permission logic.

Rate limiting mirrors the hand-rolled per-IP limiter already used by the public chat
endpoint (kept consistent with the codebase rather than pulling in a second style).
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.memory.db import get_db
from app.memory.memory_manager import MemoryManager
from app.memory.models import PipelineStage, TimelineEventType
from app.security.client_ip import client_ip

router = APIRouter()

# Proposals are valid for 30 days from send (matches the PDF's stated validity).
PROPOSAL_VALIDITY_DAYS = 30

# Per-IP rate limiting for these public endpoints.
_ACCEPT_RATE_LIMIT = 30
_ACCEPT_RATE_WINDOW_SECONDS = 60
_accept_request_log: defaultdict[str, list[datetime]] = defaultdict(list)


def _check_rate_limit(request: Request) -> None:
    ip = client_ip(request)
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=_ACCEPT_RATE_WINDOW_SECONDS)
    _accept_request_log[ip] = [t for t in _accept_request_log[ip] if t > cutoff]
    if len(_accept_request_log[ip]) >= _ACCEPT_RATE_LIMIT:
        raise HTTPException(status_code=429, detail="Too many requests. Please slow down.")
    _accept_request_log[ip].append(now)


def _validity(proposal) -> tuple[Optional[str], bool]:
    """Return (valid_until_iso, is_expired)."""
    if not proposal.date_sent:
        return None, False
    valid_until = proposal.date_sent + timedelta(days=PROPOSAL_VALIDITY_DAYS)
    expired = datetime.now(timezone.utc) > (
        valid_until if valid_until.tzinfo else valid_until.replace(tzinfo=timezone.utc)
    )
    return valid_until.isoformat(), expired


def _summary(proposal) -> dict:
    content = proposal.content or {}
    scope = [
        {"id": s.get("id"), "body": s.get("body")}
        for s in content.get("sections", [])
        if s.get("category") in ("scope", "note")
    ]
    valid_until, expired = _validity(proposal)
    state = "accepted" if proposal.accepted_at else ("expired" if expired else "acceptable")
    return {
        "proposal_id": proposal.id,
        "version": proposal.version,
        "scope": scope,
        "price": proposal.total_value,
        "valid_until": valid_until,
        "state": state,
        "accepted_at": proposal.accepted_at.isoformat() if proposal.accepted_at else None,
    }


@router.get("/api/v1/proposals/accept/{token}")
def view_proposal(token: str, request: Request, db: Session = Depends(get_db)):
    _check_rate_limit(request)
    memory = MemoryManager(db)
    proposal = memory.get_proposal_by_accept_token(token)
    # 404 for any unknown token — never leak whether a token almost-matched.
    if not proposal:
        raise HTTPException(status_code=404, detail="Proposal not found.")
    return _summary(proposal)


@router.post("/api/v1/proposals/accept/{token}")
def accept_proposal(token: str, request: Request, db: Session = Depends(get_db)):
    _check_rate_limit(request)
    memory = MemoryManager(db)
    proposal = memory.get_proposal_by_accept_token(token)
    if not proposal:
        raise HTTPException(status_code=404, detail="Proposal not found.")

    # Idempotent: accepting again is a no-op success, no double-processing.
    if proposal.accepted_at is not None:
        return {"status": "accepted", "proposal_id": proposal.id,
                "accepted_at": proposal.accepted_at.isoformat(), "already_accepted": True}

    _, expired = _validity(proposal)
    if expired:
        raise HTTPException(status_code=409, detail="This proposal has expired and can no longer be accepted.")

    memory.mark_customer_accepted(proposal.id)
    memory.add_timeline_event(
        lead_id=proposal.lead_id,
        event_type=TimelineEventType.customer_accepted,
        description="Customer accepted the proposal online",
        created_by="customer",
    )
    memory.advance_lead_stage(
        proposal.lead_id, PipelineStage.won, performed_by="customer",
        description=f"Customer accepted proposal v{proposal.version} online",
    )
    # The strongest signal the Proposal Agent gets: a price a real customer said
    # yes to. Every later draft for them is read against this number.
    from app.memory import reflection

    reflection.record_proposal_outcome(
        memory, proposal.id, reflection.OUTCOME_ACCEPTED, performed_by="customer"
    )
    db.commit()

    from app.notifications.notifier import notifier
    customer = proposal.lead.customer if proposal.lead else None
    notifier.customer_accepted(
        proposal_id=proposal.id, lead_id=proposal.lead_id, version=proposal.version,
        customer_name=customer.name if customer else None,
    )
    return {"status": "accepted", "proposal_id": proposal.id,
            "accepted_at": proposal.accepted_at.isoformat(), "already_accepted": False}
