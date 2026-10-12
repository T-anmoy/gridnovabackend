"""
Public, unauthenticated proposal accept-link endpoints.

A customer can view and accept a proposal online via a random accept_token (generated
at send time). This is deliberately lightweight — no e-signature vendor. Customer
acceptance is a DISTINCT concept from the internal two-stage approval gate; it never
touches staff approval/permission logic.

Rate-limited per IP through the shared bounded store (app/security/rate_limit.py).
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.memory.db import get_db
from app.memory.memory_manager import MemoryManager
from app.memory.models import PipelineStage, TimelineEventType
from app.proposals import acceptance
from app.security.client_ip import client_ip
from app.security.rate_limit import RateLimitLog

router = APIRouter()

# Per-IP rate limiting for these public endpoints.
_ACCEPT_RATE_LIMIT = 30
_ACCEPT_RATE_WINDOW_SECONDS = 60
_accept_request_log = RateLimitLog(_ACCEPT_RATE_WINDOW_SECONDS)


def _check_rate_limit(request: Request, log: Optional[RateLimitLog] = None) -> None:
    log = _accept_request_log if log is None else log
    if not log.hit(client_ip(request), _ACCEPT_RATE_LIMIT):
        raise HTTPException(status_code=429, detail="Too many requests. Please slow down.")


def _validity(proposal) -> tuple[Optional[str], bool]:
    """Return (valid_until_iso, is_expired) — the period is config/sales_policy.yaml
    proposal_validity_days (app/proposals/acceptance.py)."""
    until = acceptance.valid_until(proposal)
    return (until.isoformat() if until else None), acceptance.is_expired(proposal)


def _refuse(code: str, message: str) -> HTTPException:
    return HTTPException(status_code=409, detail={"code": code, "message": message})


def _on_offer(db: Session, proposal) -> None:
    """409 unless the proposal is the one on offer (sent and not superseded) or the
    customer's accepted one."""
    refused = acceptance.refusal(db, proposal)
    if refused:
        raise _refuse(*refused)


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
        "pdf_available": bool(proposal.pdf_url or proposal.pdf_storage_key),
    }


@router.get("/api/v1/proposals/accept/{token}")
def view_proposal(token: str, request: Request, db: Session = Depends(get_db)):
    _check_rate_limit(request)
    memory = MemoryManager(db)
    proposal = memory.get_proposal_by_accept_token(token)
    # 404 for any unknown token — never leak whether a token almost-matched.
    if not proposal:
        raise HTTPException(status_code=404, detail="Proposal not found.")
    _on_offer(db, proposal)
    return _summary(proposal)


@router.post("/api/v1/proposals/accept/{token}")
def accept_proposal(token: str, request: Request, db: Session = Depends(get_db)):
    _check_rate_limit(request)
    memory = MemoryManager(db)
    proposal = memory.get_proposal_by_accept_token(token)
    if not proposal:
        raise HTTPException(status_code=404, detail="Proposal not found.")
    # Lock the lead and the proposal, then decide on what is true under the lock: a
    # simultaneous second accept waits here and then sees accepted_at set.
    proposal = acceptance.lock(db, proposal.id)

    # Idempotent: accepting again is a no-op success, no double-processing.
    if proposal.accepted_at is not None:
        return {"status": "accepted", "proposal_id": proposal.id,
                "accepted_at": proposal.accepted_at.isoformat(), "already_accepted": True}

    _on_offer(db, proposal)
    if acceptance.is_expired(proposal):
        raise _refuse(acceptance.EXPIRED, "This proposal has expired and can no longer be accepted.")

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
    from app.notifications.notifier import notifier
    customer = proposal.lead.customer if proposal.lead else None
    accepted = {"proposal_id": proposal.id, "lead_id": proposal.lead_id, "version": proposal.version,
                "customer_name": customer.name if customer else None}
    memory.notify_after_commit(lambda: notifier.customer_accepted(**accepted), "customer_accepted")
    db.commit()
    return {"status": "accepted", "proposal_id": proposal.id,
            "accepted_at": proposal.accepted_at.isoformat(), "already_accepted": False}


_pdf_request_log = RateLimitLog(_ACCEPT_RATE_WINDOW_SECONDS)


@router.get("/api/v1/public/proposals/{token}/pdf")
def download_proposal_pdf(token: str, request: Request, db: Session = Depends(get_db)):
    """The customer's PDF download. Gated by the accept token exactly as the public
    view is (any valid token); 404 for every unknown token, never saying why. Answers
    with a redirect to a short-lived signed link — never a stored storage URL."""
    _check_rate_limit(request, _pdf_request_log)
    memory = MemoryManager(db)
    proposal = memory.get_proposal_by_accept_token(token)
    if not proposal:
        raise HTTPException(status_code=404, detail="Proposal not found.")
    _on_offer(db, proposal)
    if not proposal.pdf_url and not proposal.pdf_storage_key:
        raise HTTPException(status_code=409, detail="The proposal PDF is still being prepared.")

    key = MemoryManager.proposal_pdf_key(proposal)
    if key is None:
        raise HTTPException(status_code=409, detail="The proposal PDF is not available right now.")
    from app.storage import r2
    try:
        url, _ = r2.signed_get_url(key, filename=MemoryManager.proposal_pdf_filename(proposal), as_attachment=True)
    except r2.StorageNotConfiguredError:
        raise HTTPException(status_code=503, detail="The proposal PDF is not available right now.")
    return RedirectResponse(url, status_code=302)
