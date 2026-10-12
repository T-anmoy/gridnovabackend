"""
Public-facing chat endpoint. Rate-limited; no authentication required.
Only the Sales Agent speaks to customers — Proposal Agent is internal-only.

A conversation is bound to a secret (`session_token`, app/memory/models.py ChatSession),
never to ids the browser sends: lead and customer ids are sequential, so trusting them
let anyone continue — and read the memory of — someone else's conversation.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app.memory.db import get_db
from app.memory.memory_manager import MemoryManager, is_placeholder_email
from app.memory.models import Channel, LeadSource
from app.orchestrator.orchestrator import orchestrator
from app.rules import sales_policy
from app.security.client_ip import client_ip
from app.security.rate_limit import RateLimitLog, RecentKeys

router = APIRouter()

MAX_MESSAGE_CHARS = 2_000
RATE_LIMIT = 20
RATE_WINDOW_SECONDS = 60

# In-memory, per instance, bounded (app/security/rate_limit.py).
_DEDUP_WINDOW_SECONDS = 10
_request_log = RateLimitLog(RATE_WINDOW_SECONDS)
_recent_messages = RecentKeys(_DEDUP_WINDOW_SECONDS)


def _check_rate_limit(request: Request) -> None:
    if not _request_log.hit(client_ip(request), RATE_LIMIT):
        raise HTTPException(status_code=429, detail="Rate limit exceeded. Please wait before sending more messages.")


_EMAIL_RE = re.compile(r"^[\w.+-]+@[\w-]+\.[\w.-]+$")  # same shape check as public_intake.py


class ChatRequest(BaseModel):
    message: str
    # The secret returned as `session_token` by the previous reply. Absent, unknown
    # or expired → a new conversation (and a new token in the reply).
    session_token: Optional[str] = Field(default=None, max_length=256)
    name: Optional[str] = Field(default=None, max_length=200)
    email: Optional[str] = Field(default=None, max_length=254)
    # Deprecated and IGNORED (kept one release so older widgets still validate): a
    # conversation is identified only by session_token, and its source is always
    # website_chat. Never read.
    customer_id: Optional[int] = Field(default=None, deprecated=True)
    lead_id: Optional[int] = Field(default=None, deprecated=True)
    source: Optional[str] = Field(default=None, deprecated=True)

    @field_validator("email")
    @classmethod
    def _validate_email(cls, value: Optional[str]) -> Optional[str]:
        if value is None or not value.strip():
            return None
        if not _EMAIL_RE.match(value.strip()):
            raise ValueError("must be a valid email address")
        return value.strip()


class ChatResponse(BaseModel):
    reply: str
    # Send this back as session_token with the next message (replaces lead_id and
    # customer_id, which are no longer returned).
    session_token: str
    is_qualified: bool
    missing_fields: list[str]
    meeting_booked: Optional[dict] = None


def _dedup_key(token_hash: str, message: str) -> str:
    raw = f"{token_hash}:{message}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _is_duplicate(key: str) -> bool:
    now = datetime.now(timezone.utc)
    if _recent_messages.get(key, now) is not None:
        return True
    _recent_messages[key] = now
    return False


def _start_conversation(memory: MemoryManager, body: ChatRequest) -> tuple[int, int]:
    """A new conversation: its own customer and a new website_chat lead. An email
    in the first message names a NEW customer only when nobody has it yet; an email
    that already belongs to a customer is a claim, not a sign-in — the conversation
    gets a placeholder customer, the claim is kept on the lead and noted for staff."""
    claimed_by = None
    if body.email and not is_placeholder_email(body.email):
        claimed_by = memory.find_customer(email=body.email)
        if claimed_by is None:
            customer = memory.get_or_create_customer(
                email=body.email, name=body.name or body.email.split("@")[0], performed_by="website",
            )
        else:
            customer = memory.create_placeholder_customer(performed_by="website")
    else:
        customer = memory.create_placeholder_customer(performed_by="website")
    lead = memory.create_lead(customer_id=customer.id, performed_by="website", source=LeadSource.website_chat)
    if claimed_by is not None:
        claim = {"email": body.email, **({"name": body.name} if body.name else {})}
        memory.update_lead_fields(lead_id=lead.id, new_fields=claim, performed_by="website")
        if body.name:
            memory.update_customer(customer.id, performed_by="website", name=body.name)
        memory.record_possible_returning_customer(customer, claimed_by, "email", "website", lead.id)
    else:
        # A brand-new customer from a fresh email: their name/email seed the lead.
        memory.seed_lead_identity_from_history(lead.id, performed_by="website")
    return customer.id, lead.id


@router.post("/api/v1/chat/message", response_model=ChatResponse)
def chat_message(
    request: Request,
    body: ChatRequest,
    db: Session = Depends(get_db),
):
    """One customer message → Nova's reply. A plain function on purpose: the agent
    and the model client are synchronous, so FastAPI runs this in its threadpool and
    a slow reply never holds up any other request."""
    _check_rate_limit(request)
    if len(body.message) > MAX_MESSAGE_CHARS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Message exceeds {MAX_MESSAGE_CHARS} character limit.",
        )

    memory = MemoryManager(db)
    chat_session = memory.resume_chat_session(body.session_token, sales_policy.chat_session_ttl_days())
    if chat_session is not None:
        token = body.session_token
        if _is_duplicate(_dedup_key(chat_session.token_hash, body.message)):
            raise HTTPException(status_code=429, detail="Duplicate message detected. Please wait before resending.")
        customer_id, lead_id = chat_session.customer_id, chat_session.lead_id
    else:
        customer_id, lead_id = _start_conversation(memory, body)
        token = memory.create_chat_session(lead_id=lead_id, customer_id=customer_id)

    # Commit the customer/lead/session now so this request's write transaction
    # releases its lock BEFORE the orchestrator opens its own session to process the
    # turn. Otherwise, on a single-writer backend (SQLite dev), the orchestrator's
    # session deadlocks against this still-open transaction ("database is locked").
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
        session_token=token,
        is_qualified=result.get("is_qualified", False),
        missing_fields=result.get("missing_fields", []),
        meeting_booked=result.get("meeting_booked"),
    )
