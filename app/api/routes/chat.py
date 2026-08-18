"""
Public-facing chat endpoint. Rate-limited; no authentication required.
Only the Sales Agent speaks to customers — Proposal Agent is internal-only.
"""
from __future__ import annotations

import hashlib
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.memory.db import get_db
from app.memory.memory_manager import MemoryManager, is_placeholder_email
from app.memory.models import Channel, LeadSource
from app.orchestrator.orchestrator import orchestrator

router = APIRouter()

MAX_MESSAGE_CHARS = 2_000
RATE_LIMIT = 20
RATE_WINDOW_SECONDS = 60

# In-memory stores — suitable for single-process Phase 1 deployment.
_request_log: defaultdict[str, list[datetime]] = defaultdict(list)
_recent_messages: dict[str, datetime] = {}
_DEDUP_WINDOW_SECONDS = 10


def _get_client_ip(request: Request) -> str:
    if request.client and request.client.host:
        return request.client.host
    return "127.0.0.1"


def _check_rate_limit(request: Request) -> None:
    ip = _get_client_ip(request)
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=RATE_WINDOW_SECONDS)
    _request_log[ip] = [t for t in _request_log[ip] if t > cutoff]
    if len(_request_log[ip]) >= RATE_LIMIT:
        raise HTTPException(status_code=429, detail="Rate limit exceeded. Please wait before sending more messages.")
    _request_log[ip].append(now)


class ChatRequest(BaseModel):
    message: str
    customer_id: Optional[int] = None
    lead_id: Optional[int] = None
    email: Optional[str] = None
    name: Optional[str] = None
    # Which channel this conversation came through; the public Ask Nova widget
    # defaults to website_chat.
    source: Optional[str] = None


class ChatResponse(BaseModel):
    reply: str
    lead_id: Optional[int]
    customer_id: Optional[int]
    is_qualified: bool
    missing_fields: list[str]
    meeting_booked: Optional[dict] = None


def _dedup_key(customer_id: Optional[int], message: str) -> str:
    raw = f"{customer_id}:{message}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _is_duplicate(key: str) -> bool:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=_DEDUP_WINDOW_SECONDS)
    expired = [k for k, ts in _recent_messages.items() if ts < cutoff]
    for k in expired:
        _recent_messages.pop(k, None)
    if key in _recent_messages:
        return True
    _recent_messages[key] = now
    return False


@router.post("/api/v1/chat/message", response_model=ChatResponse)
async def chat_message(
    request: Request,
    body: ChatRequest,
    db: Session = Depends(get_db),
):
    _check_rate_limit(request)
    if len(body.message) > MAX_MESSAGE_CHARS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Message exceeds {MAX_MESSAGE_CHARS} character limit.",
        )

    memory = MemoryManager(db)

    # Resolve or create customer
    customer_id = body.customer_id
    lead_id = body.lead_id

    # A client-supplied lead_id must actually exist — validated before any write
    # happens. A stale or foreign lead_id used to fall through silently, creating an
    # orphan customer and then crashing deep inside the orchestrator (KeyError) once
    # it tried to load a lead that was never there.
    if lead_id is not None:
        existing_lead = memory.get_lead(lead_id)
        if not existing_lead:
            raise HTTPException(status_code=404, detail=f"Lead {lead_id} not found.")
        # An ongoing conversation is identified by its lead — the lead already knows
        # its customer, so never create a second customer for it.
        if not customer_id:
            customer_id = existing_lead.customer_id

    if not customer_id:
        if body.email and not is_placeholder_email(body.email):
            # Real email → the match key going forward.
            customer = memory.get_or_create_customer(
                email=body.email,
                name=body.name or body.email.split("@")[0],
                performed_by="website",
            )
        else:
            # No identity yet — start with a placeholder customer the conversation
            # fills in later (name/email get extracted mid-chat, updated in place).
            customer = memory.create_placeholder_customer(performed_by="website")
        customer_id = customer.id

    if not lead_id:
        # Fold rapid/duplicate first-contact messages into the customer's existing
        # open lead (if any) rather than minting a new one per request — this is
        # what makes several near-simultaneous "first messages" from the same real
        # customer (double-tapped send, multiple tabs) resolve to one lead instead
        # of one each. A customer whose only prior lead already reached won/lost
        # still gets a fresh lead for a new inquiry.
        existing_open_lead = memory.get_open_lead_for_customer(customer_id)
        if existing_open_lead:
            lead_id = existing_open_lead.id
        else:
            try:
                source = LeadSource(body.source) if body.source else LeadSource.website_chat
            except ValueError:
                raise HTTPException(status_code=422, detail=f"Unknown lead source: {body.source!r}")
            lead = memory.create_lead(customer_id=customer_id, performed_by="website", source=source)
            lead_id = lead.id

    dedup_key = _dedup_key(customer_id, body.message)
    if _is_duplicate(dedup_key):
        raise HTTPException(status_code=429, detail="Duplicate message detected. Please wait before resending.")

    # Commit the customer/lead now so this request's write transaction releases its
    # lock BEFORE the orchestrator opens its own session to process the turn.
    # Otherwise, on a single-writer backend (SQLite dev), the orchestrator's session
    # deadlocks against this still-open transaction ("database is locked").
    db.commit()

    result = orchestrator.handle_customer_message(
        customer_id=customer_id,
        lead_id=lead_id,
        message=body.message,
        channel=Channel.website.value,
    )
    db.commit()

    return ChatResponse(
        reply=result.get("reply_text", ""),
        lead_id=result.get("lead_id"),
        customer_id=result.get("customer_id"),
        is_qualified=result.get("is_qualified", False),
        missing_fields=result.get("missing_fields", []),
        meeting_booked=result.get("meeting_booked"),
    )
