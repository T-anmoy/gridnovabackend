"""
Internal dashboard API — all endpoints require Clerk authentication.
The two-stage approval gate (standard vs. manager sign-off) is enforced here.
"""
from __future__ import annotations

import csv
import io
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.agents.sales_agent import QUALIFICATION_FIELDS, _score_from_fields
from app.auth.clerk_client import ClerkSyncError, update_user_role
from app.auth.dependencies import AuthenticatedEmployee, bearer_scheme, get_current_employee
from app.config import settings
from app.governance import policy as gov_policy, registry as gov_registry
from app.memory import reflection
from app.memory.db import get_db
from app.memory.memory_manager import MemoryManager
from app.memory.models import (
    EmployeeRole, LeadSource, MemoryKind, PipelineStage, ProposalStatus, TimelineEventType,
)
from app.rules import approval_engine
from app.rules.business_rules import evaluate_lead
from app.scheduling import availability, booking, google_calendar
from app.security.internal_key import has_valid_internal_key
from app.notifications.notifier import notifier
from app.orchestrator.orchestrator import orchestrator
from app.pdf.generator import generate_and_upload_pdf

router = APIRouter(prefix="/api/v1/dashboard")
logger = logging.getLogger(__name__)

def _get_yaml_config() -> dict:
    # Single source of truth — the content engine owns loading + validation + caching.
    from app.proposals.content_engine import load_content_config
    return load_content_config()


def _accept_path(accept_token: Optional[str]) -> Optional[str]:
    """The backend's own public accept-link path for a token. Deliberately not a
    full URL — this API doesn't know the frontend's public domain, only its own
    fixed route shape — so callers prefix their own origin to build the real link.
    """
    if not accept_token:
        return None
    return f"/api/v1/proposals/accept/{accept_token}"


# Technical fields an Engineer is allowed to see (no pricing / customer PII).
_ENGINEER_TECHNICAL_FIELDS = ("project_type", "capacity_kw", "location", "roof_type")


def _parse_date(value: Optional[str], field_name: str) -> Optional[datetime]:
    """Parse an ISO date/datetime query param into a tz-aware datetime, or None."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Invalid {field_name}: expected ISO date, got {value!r}")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _proposal_summary(p, employee: AuthenticatedEmployee) -> dict:
    """List-row projection for a proposal, with its lead/customer summary joined."""
    lead = p.lead
    if employee.is_engineer:
        return {
            "id": p.id,
            "lead_id": p.lead_id,
            "version": p.version,
            "status": p.status,
            "technical_fields": {
                k: v for k, v in (lead.collected_fields or {}).items()
                if k in _ENGINEER_TECHNICAL_FIELDS
            } if lead else {},
        }
    customer = lead.customer if lead else None
    return {
        "id": p.id,
        "lead_id": p.lead_id,
        "version": p.version,
        "status": p.status,
        "total_value": p.total_value,
        "requires_manager_signoff": p.requires_manager_signoff,
        "used_fallback_template": p.used_fallback_template,
        "pdf_url": p.pdf_url,
        "date_created": p.date_created.isoformat() if p.date_created else None,
        "date_sent": p.date_sent.isoformat() if p.date_sent else None,
        # Not shown to Engineers — the accept link is a bearer credential onto the
        # public accept endpoint, which returns pricing to anyone holding it.
        "accept_token": p.accept_token,
        "accept_path": _accept_path(p.accept_token),
        "lead": {
            "id": lead.id,
            "pipeline_stage": lead.pipeline_stage,
            "lead_score": lead.lead_score,
        } if lead else None,
        "customer": {
            "id": customer.id,
            "name": customer.name,
            "company": customer.company,
            "email": customer.email,
        } if customer else None,
    }


# ── Leads ─────────────────────────────────────────────────────────────────────

@router.get("/leads")
def list_leads(
    source: Optional[str] = Query(default=None),
    db: Session = Depends(get_db),
    employee: AuthenticatedEmployee = Depends(get_current_employee),
):
    source_enum: Optional[LeadSource] = None
    if source:
        try:
            source_enum = LeadSource(source)
        except ValueError:
            raise HTTPException(status_code=422, detail=f"Unknown lead source: {source!r}")

    memory = MemoryManager(db)
    # Sales sees only their own leads; others see all (with role-appropriate filtering below)
    assigned_filter = employee.employee_id if employee.is_sales else None
    leads = memory.list_leads(assigned_employee_id=assigned_filter, source=source_enum)
    return [
        {
            "id": l.id,
            "customer_id": l.customer_id,
            "pipeline_stage": l.pipeline_stage,
            "lead_score": l.lead_score,
            "source": l.source,
            "created_at": l.created_at.isoformat(),
        }
        for l in leads
    ]


# ── CSV exports (Admin / Management only) ─────────────────────────────────────
# NOTE: these static /…/export routes MUST be declared before the parametrized
# /leads/{lead_id} and /proposals/{proposal_id} routes, or "export" would be matched
# as an {id} path segment and fail int-parsing.

def _csv_response(rows: list[list], header: list[str], filename: str) -> StreamingResponse:
    def _generate():
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(header)
        yield buffer.getvalue()
        buffer.seek(0); buffer.truncate(0)
        for row in rows:
            writer.writerow(row)
            yield buffer.getvalue()
            buffer.seek(0); buffer.truncate(0)

    return StreamingResponse(
        _generate(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/leads/export")
def export_leads(
    source: Optional[str] = Query(default=None),
    db: Session = Depends(get_db),
    employee: AuthenticatedEmployee = Depends(get_current_employee),
):
    if not employee.can_view_full_analytics():
        raise HTTPException(status_code=403, detail="Only Admin or Management can export leads.")

    source_enum: Optional[LeadSource] = None
    if source:
        try:
            source_enum = LeadSource(source)
        except ValueError:
            raise HTTPException(status_code=422, detail=f"Unknown lead source: {source!r}")

    leads = memory_of(db).list_leads(source=source_enum)
    header = ["lead_id", "customer_name", "customer_email", "source",
              "pipeline_stage", "lead_score", "assigned_employee_id", "created_at"]
    rows = [
        [
            l.id,
            l.customer.name if l.customer else "",
            l.customer.email if l.customer else "",
            l.source.value if l.source else "",
            l.pipeline_stage.value if l.pipeline_stage else "",
            l.lead_score.value if l.lead_score else "",
            l.assigned_employee_id or "",
            l.created_at.isoformat() if l.created_at else "",
        ]
        for l in leads
    ]
    filename = f"leads-export-{datetime.now(timezone.utc).date().isoformat()}.csv"
    return _csv_response(rows, header, filename)


@router.get("/proposals/export")
def export_proposals(
    filter_status: Optional[str] = Query(default=None, alias="status"),
    date_from: Optional[str] = Query(default=None),
    date_to: Optional[str] = Query(default=None),
    lead_id: Optional[int] = Query(default=None),
    db: Session = Depends(get_db),
    employee: AuthenticatedEmployee = Depends(get_current_employee),
):
    if not employee.can_view_full_analytics():
        raise HTTPException(status_code=403, detail="Only Admin or Management can export proposals.")

    status_enum: Optional[ProposalStatus] = None
    if filter_status:
        try:
            status_enum = ProposalStatus(filter_status)
        except ValueError:
            raise HTTPException(status_code=422, detail=f"Unknown proposal status: {filter_status!r}")

    proposals = memory_of(db).list_proposals_for_export(
        status=status_enum,
        date_from=_parse_date(date_from, "date_from"),
        date_to=_parse_date(date_to, "date_to"),
        lead_id=lead_id,
    )
    header = ["proposal_id", "lead_id", "customer_name", "version", "status",
              "total_value", "requires_manager_signoff", "used_fallback_template",
              "date_created", "date_sent", "approved_at", "accepted_at"]
    rows = [
        [
            p.id,
            p.lead_id,
            p.lead.customer.name if (p.lead and p.lead.customer) else "",
            p.version,
            p.status.value if p.status else "",
            p.total_value if p.total_value is not None else "",
            p.requires_manager_signoff,
            p.used_fallback_template,
            p.date_created.isoformat() if p.date_created else "",
            p.date_sent.isoformat() if p.date_sent else "",
            p.approved_at.isoformat() if p.approved_at else "",
            p.accepted_at.isoformat() if p.accepted_at else "",
        ]
        for p in proposals
    ]
    filename = f"proposals-export-{datetime.now(timezone.utc).date().isoformat()}.csv"
    return _csv_response(rows, header, filename)


@router.get("/leads/{lead_id}")
def get_lead(
    lead_id: int,
    db: Session = Depends(get_db),
    employee: AuthenticatedEmployee = Depends(get_current_employee),
):
    memory = MemoryManager(db)
    ctx = memory.get_lead_context(lead_id)
    if not ctx or not ctx.get("lead"):
        raise HTTPException(status_code=404, detail="Lead not found")

    lead = ctx["lead"]
    customer = ctx["customer"]

    # Engineers see only technical fields
    if employee.is_engineer:
        return {
            "lead_id": lead.id,
            "pipeline_stage": lead.pipeline_stage,
            "collected_fields": {
                k: v for k, v in (lead.collected_fields or {}).items()
                if k in ("project_type", "capacity_kw", "location", "roof_type")
            },
        }

    return {
        "lead_id": lead.id,
        "customer": {"id": customer.id, "name": customer.name, "email": customer.email} if customer else None,
        "pipeline_stage": lead.pipeline_stage,
        "lead_score": lead.lead_score,
        "source": lead.source,
        "collected_fields": lead.collected_fields,
        "proposals": [
            {"id": p.id, "version": p.version, "status": p.status, "total_value": p.total_value}
            for p in ctx["proposals"]
        ],
        "timeline": [
            {"event_type": e.event_type, "description": e.description, "created_at": e.created_at.isoformat()}
            for e in ctx["timeline"]
        ],
        "messages": [
            {"role": m.role, "content": m.content, "created_at": m.created_at.isoformat()}
            for m in ctx["messages"]
        ] if not employee.is_engineer else [],
    }


class LeadFieldsUpdateRequest(BaseModel):
    fields: dict[str, Any]


@router.patch("/leads/{lead_id}/fields")
def update_lead_fields_manual(
    lead_id: int,
    body: LeadFieldsUpdateRequest,
    db: Session = Depends(get_db),
    employee: AuthenticatedEmployee = Depends(get_current_employee),
):
    """Manual correction/completion of a lead's collected fields — the employee-side
    escape hatch for when AI extraction (chat, or its mock-mode heuristic) missed
    something or got it wrong. Re-evaluates qualification exactly the way a chat
    turn does, so a manually-completed lead becomes just as "qualified" as one
    qualified conversationally.
    """
    if not employee.can_edit_lead():
        raise HTTPException(status_code=403, detail="Your role cannot edit lead fields.")

    if not body.fields:
        raise HTTPException(status_code=422, detail="fields must not be empty.")

    unknown = [k for k in body.fields if k not in QUALIFICATION_FIELDS]
    if unknown:
        raise HTTPException(status_code=422, detail=f"Unknown field(s): {', '.join(unknown)}")

    memory = MemoryManager(db)
    lead = memory.get_lead(lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")

    # Same scoping as the rest of the Sales-role surface: an unassigned lead is fair
    # game, but one already owned by a different Sales rep is not.
    if employee.is_sales and lead.assigned_employee_id and lead.assigned_employee_id != employee.employee_id:
        raise HTTPException(status_code=403, detail="This lead is not assigned to you.")

    merged = {**dict(lead.collected_fields or {}), **body.fields}
    score = _score_from_fields(merged)
    eval_result = evaluate_lead(merged)

    stage = None
    if eval_result.is_ready_for_proposal:
        stage = PipelineStage.qualified
    elif merged:
        stage = PipelineStage.qualifying

    updated = memory.update_lead_fields(
        lead_id=lead_id,
        new_fields=body.fields,
        performed_by=employee.employee_id,
        stage=stage,
        score=score,
    )
    if eval_result.is_ready_for_proposal:
        memory.add_timeline_event(
            lead_id=lead_id,
            event_type=TimelineEventType.qualification_completed,
            description=f"All required fields collected via manual edit by {employee.employee_id}",
            created_by=employee.employee_id,
        )
    db.commit()

    return {
        "lead_id": lead_id,
        "collected_fields": updated.collected_fields,
        "pipeline_stage": updated.pipeline_stage,
        "lead_score": updated.lead_score,
        "is_qualified": eval_result.is_ready_for_proposal,
        "missing_fields": eval_result.missing_fields,
    }


# ── Proposals ─────────────────────────────────────────────────────────────────

class DraftRequest(BaseModel):
    lead_id: int
    customer_id: int


@router.post("/proposals/draft")
def create_proposal_draft(
    body: DraftRequest,
    db: Session = Depends(get_db),
    employee: AuthenticatedEmployee = Depends(get_current_employee),
):
    if not employee.can_trigger_proposal_draft():
        raise HTTPException(status_code=403, detail="Your role cannot trigger proposal drafts.")

    result = orchestrator.request_proposal_draft(
        lead_id=body.lead_id,
        customer_id=body.customer_id,
        performed_by=employee.employee_id,
    )
    db.commit()

    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])

    return {
        "proposal_id": result.get("proposal_id"),
        "lead_id": result.get("lead_id"),
        "version": result.get("proposal_version"),
        "status": result.get("proposal_status"),
        "requires_manager_signoff": result.get("requires_manager_signoff"),
        "total_value": result.get("total_value"),
        "missing_fields": result.get("missing_fields", []),
    }


@router.post("/proposals/{proposal_id}/approve")
def approve_proposal(
    proposal_id: int,
    db: Session = Depends(get_db),
    employee: AuthenticatedEmployee = Depends(get_current_employee),
):
    """Standard approval — only for proposals where requires_manager_signoff is False."""
    if not employee.can_approve_standard():
        raise HTTPException(status_code=403, detail="Your role cannot approve proposals.")

    memory = MemoryManager(db)
    proposal = memory.get_proposal(proposal_id)
    if not proposal:
        raise HTTPException(status_code=404, detail="Proposal not found")

    if proposal.requires_manager_signoff:
        raise HTTPException(
            status_code=403,
            detail="This proposal exceeds the standard threshold and requires manager sign-off. Use /manager-sign-off instead.",
        )

    if proposal.status not in (ProposalStatus.pending_approval, ProposalStatus.pending_pricing_validation, ProposalStatus.draft):
        raise HTTPException(status_code=400, detail=f"Proposal is in status '{proposal.status}' — cannot approve.")

    lead_id = proposal.lead_id
    memory.transition_proposal_status(
        proposal_id=proposal_id,
        new_status=ProposalStatus.approved,
        performed_by=employee.employee_id,
        approved_by_employee_id=employee.employee_id,
    )
    memory.add_timeline_event(
        lead_id=lead_id,
        event_type=TimelineEventType.proposal_approved,
        description=f"Approved by {employee.employee_id}",
        created_by=employee.employee_id,
    )
    db.commit()

    # Captured lead_id above, before commit expires the ORM object's attributes —
    # _trigger_pdf_generation starts a background thread against its own session
    # right below, so re-touching `proposal` on this (the request's) session past
    # this point would mean two threads issuing queries concurrently against it.
    _trigger_pdf_generation(proposal_id, proposal, db, memory)

    reflection.record_proposal_outcome(
        memory, proposal_id, reflection.OUTCOME_APPROVED, employee.employee_id
    )
    db.commit()

    notifier.proposal_approved(proposal_id=proposal_id, lead_id=lead_id)
    return {"status": "approved", "proposal_id": proposal_id}


@router.post("/proposals/{proposal_id}/manager-sign-off")
def manager_sign_off(
    proposal_id: int,
    db: Session = Depends(get_db),
    employee: AuthenticatedEmployee = Depends(get_current_employee),
):
    """Manager sign-off — only for proposals where requires_manager_signoff is True."""
    if not employee.can_approve_manager_signoff():
        raise HTTPException(status_code=403, detail="Only Management or Admin can perform manager sign-off.")

    memory = MemoryManager(db)
    proposal = memory.get_proposal(proposal_id)
    if not proposal:
        raise HTTPException(status_code=404, detail="Proposal not found")

    if not proposal.requires_manager_signoff:
        raise HTTPException(
            status_code=400,
            detail="This proposal does not require manager sign-off. Use /approve instead.",
        )

    if proposal.status not in (ProposalStatus.pending_approval, ProposalStatus.pending_pricing_validation, ProposalStatus.draft):
        raise HTTPException(status_code=400, detail=f"Proposal is in status '{proposal.status}' — cannot sign off.")

    lead_id = proposal.lead_id
    memory.transition_proposal_status(
        proposal_id=proposal_id,
        new_status=ProposalStatus.approved,
        performed_by=employee.employee_id,
        approved_by_employee_id=employee.employee_id,
    )
    memory.add_timeline_event(
        lead_id=lead_id,
        event_type=TimelineEventType.manager_signoff_completed,
        description=f"Manager sign-off by {employee.employee_id}",
        created_by=employee.employee_id,
    )
    db.commit()

    # See the matching comment in approve_proposal above — capture lead_id before
    # the background PDF thread starts, don't re-touch `proposal` after.
    _trigger_pdf_generation(proposal_id, proposal, db, memory)

    reflection.record_proposal_outcome(
        memory, proposal_id, reflection.OUTCOME_SIGNED_OFF, employee.employee_id
    )
    db.commit()

    notifier.proposal_approved(proposal_id=proposal_id, lead_id=lead_id)
    return {"status": "approved", "proposal_id": proposal_id, "signoff_by": employee.employee_id}


class RevisionRequest(BaseModel):
    reason: str = Field(min_length=1)


@router.post("/proposals/{proposal_id}/request-revision")
def request_revision(
    proposal_id: int,
    body: RevisionRequest,
    db: Session = Depends(get_db),
    employee: AuthenticatedEmployee = Depends(get_current_employee),
):
    if not employee.can_approve_standard():
        raise HTTPException(status_code=403, detail="Your role cannot request revisions.")

    memory = MemoryManager(db)
    proposal = memory.get_proposal(proposal_id)
    if not proposal:
        raise HTTPException(status_code=404, detail="Proposal not found")

    memory.transition_proposal_status(
        proposal_id=proposal_id,
        new_status=ProposalStatus.revision_requested,
        performed_by=employee.employee_id,
        revision_reason=body.reason,
    )
    memory.add_timeline_event(
        lead_id=proposal.lead_id,
        event_type=TimelineEventType.revision_requested,
        description=f"Revision requested: {body.reason}",
        created_by=employee.employee_id,
    )
    # The written reason is the best feedback the Proposal Agent ever gets — a human
    # saying exactly what was wrong with the draft. Kept verbatim, never paraphrased.
    reflection.record_proposal_outcome(
        memory, proposal_id, reflection.OUTCOME_REVISION_REQUESTED,
        employee.employee_id, reason=body.reason,
    )
    db.commit()
    return {"status": "revision_requested", "proposal_id": proposal_id, "reason": body.reason}


class AdjustPricingRequest(BaseModel):
    new_value: float = Field(ge=0)
    reason: str = Field(min_length=1)


@router.patch("/proposals/{proposal_id}/adjust-pricing")
def adjust_pricing(
    proposal_id: int,
    body: AdjustPricingRequest,
    db: Session = Depends(get_db),
    employee: AuthenticatedEmployee = Depends(get_current_employee),
):
    """Adjust a proposal's price BEFORE approval and re-derive the manager-signoff flag.

    The flag is recomputed from the Business Rules Engine on every adjustment, with no
    way to bypass it — so a price nudged over the threshold always requires manager
    sign-off, and one dropped back under correctly reverts to standard approval. Only
    permitted while the proposal is still in draft / pending_pricing_validation; a price
    change on an approved or sent proposal must go through request-revision instead.
    """
    if not employee.can_adjust_pricing():
        raise HTTPException(status_code=403, detail="Your role cannot adjust proposal pricing.")

    memory = MemoryManager(db)
    proposal = memory.get_proposal(proposal_id)
    if not proposal:
        raise HTTPException(status_code=404, detail="Proposal not found")

    # Sales may only adjust proposals on their own leads.
    lead = memory.get_lead(proposal.lead_id)
    if employee.is_sales and lead and lead.assigned_employee_id != employee.employee_id:
        raise HTTPException(status_code=403, detail="This proposal is not assigned to you.")

    if proposal.status not in (ProposalStatus.draft, ProposalStatus.pending_pricing_validation):
        raise HTTPException(
            status_code=409,
            detail=(
                f"Pricing can only be adjusted while a proposal is in 'draft' or "
                f"'pending_pricing_validation' (this one is '{proposal.status.value}'). "
                "Use request-revision to change an approved or sent proposal."
            ),
        )

    # Recompute the signoff requirement from the SAME rule used at draft time — this
    # also preserves a capacity-driven signoff requirement, never just the value one.
    collected = dict(lead.collected_fields or {}) if lead else {}
    new_flag = evaluate_lead(collected, total_value=body.new_value).requires_manager_signoff
    # Captured before the write — the size and direction of the correction is the
    # lesson, and it is unrecoverable once the row is overwritten.
    previous_value = proposal.total_value

    updated = memory.adjust_proposal_pricing(
        proposal_id=proposal_id,
        new_value=body.new_value,
        requires_manager_signoff=new_flag,
        reason=body.reason,
        performed_by=employee.employee_id,
    )
    memory.add_timeline_event(
        lead_id=proposal.lead_id,
        event_type=TimelineEventType.pricing_adjusted,
        description=f"Pricing adjusted to {body.new_value:,.0f}: {body.reason}",
        created_by=employee.employee_id,
    )
    reflection.record_proposal_outcome(
        memory, proposal_id, reflection.OUTCOME_PRICING_ADJUSTED,
        employee.employee_id, reason=body.reason, previous_value=previous_value,
    )
    db.commit()

    return {
        "status": "pricing_adjusted",
        "proposal_id": proposal_id,
        "total_value": updated.total_value,
        "requires_manager_signoff": updated.requires_manager_signoff,
        "reason": body.reason,
    }


@router.post("/proposals/{proposal_id}/send")
def send_proposal(
    proposal_id: int,
    db: Session = Depends(get_db),
    employee: AuthenticatedEmployee = Depends(get_current_employee),
):
    if not employee.can_send_proposal():
        raise HTTPException(status_code=403, detail="Your role cannot send proposals.")

    memory = MemoryManager(db)
    proposal = memory.get_proposal(proposal_id)
    if not proposal:
        raise HTTPException(status_code=404, detail="Proposal not found")

    if proposal.status != ProposalStatus.approved:
        raise HTTPException(status_code=400, detail="Only approved proposals can be sent.")

    memory.transition_proposal_status(
        proposal_id=proposal_id,
        new_status=ProposalStatus.sent,
        performed_by=employee.employee_id,
        date_sent=datetime.now(timezone.utc),
    )
    # Generate the customer accept-link token at send time (idempotent).
    accept_token = memory.generate_accept_token(proposal_id, performed_by=employee.employee_id)
    accept_path = _accept_path(accept_token)
    memory.add_timeline_event(
        lead_id=proposal.lead_id,
        event_type=TimelineEventType.proposal_sent,
        description=f"Sent by {employee.employee_id}",
        created_by=employee.employee_id,
    )
    db.commit()

    ctx = memory.get_lead_context(proposal.lead_id)
    customer = ctx.get("customer")
    reflection.record_proposal_outcome(
        memory, proposal_id, reflection.OUTCOME_SENT, employee.employee_id
    )
    db.commit()

    if customer:
        notifier.proposal_sent(proposal_id=proposal_id, customer_email=customer.email, accept_path=accept_path)

    return {
        "status": "sent",
        "proposal_id": proposal_id,
        "accept_token": accept_token,
        "accept_path": accept_path,
    }


@router.get("/proposals/{proposal_id}/pdf")
def get_proposal_pdf(
    proposal_id: int,
    db: Session = Depends(get_db),
    employee: AuthenticatedEmployee = Depends(get_current_employee),
):
    memory = MemoryManager(db)
    proposal = memory.get_proposal(proposal_id)
    if not proposal:
        raise HTTPException(status_code=404, detail="Proposal not found")

    # The PDF contains pricing + customer contact — same access rules as the
    # proposal detail endpoint: Engineers are blocked; Sales is scoped to own leads.
    if employee.is_engineer:
        raise HTTPException(status_code=403, detail="Engineers cannot access proposal PDFs.")
    if employee.is_sales:
        lead = memory.get_lead(proposal.lead_id)
        if lead and lead.assigned_employee_id != employee.employee_id:
            raise HTTPException(status_code=403, detail="This proposal is not assigned to you.")

    if not proposal.pdf_url:
        return {"status": "pdf_pending", "proposal_id": proposal_id}

    return {"pdf_url": proposal.pdf_url, "proposal_id": proposal_id}


# ── Proposals (read) ────────────────────────────────────────────────────────

@router.get("/proposals")
def list_proposals(
    filter_status: Optional[str] = Query(default=None, alias="status"),
    date_from: Optional[str] = Query(default=None),
    date_to: Optional[str] = Query(default=None),
    lead_id: Optional[int] = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    employee: AuthenticatedEmployee = Depends(get_current_employee),
):
    status_enum: Optional[ProposalStatus] = None
    if filter_status:
        try:
            status_enum = ProposalStatus(filter_status)
        except ValueError:
            raise HTTPException(status_code=422, detail=f"Unknown proposal status: {filter_status!r}")

    # Sales is scoped to its own leads at the query level; others see all.
    assigned_filter = employee.employee_id if employee.is_sales else None

    proposals, total = memory_of(db).list_proposals(
        status=status_enum,
        date_from=_parse_date(date_from, "date_from"),
        date_to=_parse_date(date_to, "date_to"),
        lead_id=lead_id,
        assigned_employee_id=assigned_filter,
        page=page,
        page_size=page_size,
    )
    return {
        "items": [_proposal_summary(p, employee) for p in proposals],
        "page": page,
        "page_size": page_size,
        "total": total,
    }


@router.get("/proposals/{proposal_id}")
def get_proposal_detail(
    proposal_id: int,
    db: Session = Depends(get_db),
    employee: AuthenticatedEmployee = Depends(get_current_employee),
):
    memory = memory_of(db)
    detail = memory.get_proposal_detail(proposal_id)
    if not detail:
        raise HTTPException(status_code=404, detail="Proposal not found")

    proposal = detail["proposal"]
    lead = detail["lead"]
    customer = detail["customer"]

    # Sales may only open proposals on its own leads.
    if employee.is_sales and lead and lead.assigned_employee_id != employee.employee_id:
        raise HTTPException(status_code=403, detail="This proposal is not assigned to you.")

    if employee.is_engineer:
        return {
            "id": proposal.id,
            "lead_id": proposal.lead_id,
            "version": proposal.version,
            "status": proposal.status,
            "requires_manager_signoff": proposal.requires_manager_signoff,
            "used_fallback_template": proposal.used_fallback_template,
            "technical_fields": {
                k: v for k, v in (lead.collected_fields or {}).items()
                if k in _ENGINEER_TECHNICAL_FIELDS
            } if lead else {},
            "versions": [
                {"id": v.id, "version": v.version, "status": v.status}
                for v in detail["versions"]
            ],
        }

    return {
        "id": proposal.id,
        "lead_id": proposal.lead_id,
        "version": proposal.version,
        "status": proposal.status,
        "content": proposal.content,
        "total_value": proposal.total_value,
        "requires_manager_signoff": proposal.requires_manager_signoff,
        "used_fallback_template": proposal.used_fallback_template,
        "revision_reason": proposal.revision_reason,
        "pdf_url": proposal.pdf_url,
        "date_created": proposal.date_created.isoformat() if proposal.date_created else None,
        "date_sent": proposal.date_sent.isoformat() if proposal.date_sent else None,
        # Not shown to Engineers (see the Engineer branch above) — the accept link
        # is a bearer credential onto the public accept endpoint, which returns
        # pricing to anyone holding it.
        "accept_token": proposal.accept_token,
        "accept_path": _accept_path(proposal.accept_token),
        "customer": {
            "id": customer.id, "name": customer.name,
            "company": customer.company, "email": customer.email,
        } if customer else None,
        "lead": {
            "id": lead.id,
            "pipeline_stage": lead.pipeline_stage,
            "lead_score": lead.lead_score,
        } if lead else None,
        "versions": [
            {
                "id": v.id,
                "version": v.version,
                "status": v.status,
                "total_value": v.total_value,
                "date_created": v.date_created.isoformat() if v.date_created else None,
            }
            for v in detail["versions"]
        ],
    }


@router.get("/proposals/{proposal_id}/required-approval-tier")
def get_required_approval_tier(
    proposal_id: int,
    discount_percent: float = Query(default=0.0, ge=0),
    margin_percent: Optional[float] = Query(default=None),
    is_flagged_risk: bool = Query(default=False),
    db: Session = Depends(get_db),
    employee: AuthenticatedEmployee = Depends(get_current_employee),
):
    """The 3-tier Approval Engine's determination for a real proposal —
    additive and read-only. Does NOT change what actually gates approval
    today: /proposals/{id}/approve and /manager-sign-off still run on
    Proposal.requires_manager_signoff exactly as before. This exists so the
    richer, multi-factor determination is consultable (e.g. by a future
    frontend) without touching the 2-tier flow that currently works.

    discount_percent, margin_percent and is_flagged_risk are query params,
    not read off the Proposal, because none of those three are tracked on it
    yet — only total_value is. Pass real figures here if you have them from
    elsewhere; omit what you don't.
    """
    if employee.is_engineer:
        raise HTTPException(status_code=403, detail="Your role cannot view approval-tier information.")

    memory = memory_of(db)
    proposal = memory.get_proposal(proposal_id)
    if not proposal:
        raise HTTPException(status_code=404, detail="Proposal not found")

    tier = approval_engine.determine_required_approval_tier(
        proposal_value=proposal.total_value,
        discount_percent=discount_percent,
        margin_percent=margin_percent,
        is_flagged_risk=is_flagged_risk,
    )
    return {
        "proposal_id": proposal_id,
        "required_tier": tier.value,
        "based_on": {
            "proposal_value": proposal.total_value,
            "discount_percent": discount_percent,
            "margin_percent": margin_percent,
            "is_flagged_risk": is_flagged_risk,
        },
        # The existing 2-tier gate, unchanged — shown alongside for comparison,
        # not replaced by the value above.
        "current_2tier_requires_manager_signoff": proposal.requires_manager_signoff,
    }


# ── Analytics ─────────────────────────────────────────────────────────────────

@router.get("/analytics")
def get_analytics(
    date_from: Optional[str] = Query(default=None),
    date_to: Optional[str] = Query(default=None),
    employee_id: Optional[str] = Query(default=None),
    db: Session = Depends(get_db),
    employee: AuthenticatedEmployee = Depends(get_current_employee),
):
    # Engineers have no analytics view.
    if employee.is_engineer:
        raise HTTPException(status_code=403, detail="Your role cannot view analytics.")

    # Full analytics (any employee, or the whole org) is Admin/Management only.
    # Sales is hard-scoped to its own metrics regardless of the query param.
    if employee.can_view_full_analytics():
        scope_employee_id = employee_id
    else:
        if employee_id and employee_id != employee.employee_id:
            raise HTTPException(
                status_code=403,
                detail="Your role can only view your own metrics.",
            )
        scope_employee_id = employee.employee_id

    return memory_of(db).compute_analytics(
        date_from=_parse_date(date_from, "date_from"),
        date_to=_parse_date(date_to, "date_to"),
        employee_id=scope_employee_id,
    )


# ── Reporting analytics (Admin / Management only) ─────────────────────────────

def _require_full_analytics(employee: AuthenticatedEmployee) -> None:
    if not employee.can_view_full_analytics():
        raise HTTPException(status_code=403, detail="Your role cannot view reporting analytics.")


@router.get("/analytics/conversion-by-source")
def analytics_conversion_by_source(
    db: Session = Depends(get_db),
    employee: AuthenticatedEmployee = Depends(get_current_employee),
):
    _require_full_analytics(employee)
    return {"by_source": memory_of(db).analytics_conversion_by_source()}


@router.get("/analytics/deal-size-by-tier")
def analytics_deal_size_by_tier(
    db: Session = Depends(get_db),
    employee: AuthenticatedEmployee = Depends(get_current_employee),
):
    _require_full_analytics(employee)
    return {"by_tier": memory_of(db).analytics_deal_size_by_tier(_get_yaml_config())}


@router.get("/analytics/rep-performance")
def analytics_rep_performance(
    db: Session = Depends(get_db),
    employee: AuthenticatedEmployee = Depends(get_current_employee),
):
    _require_full_analytics(employee)
    return {"reps": memory_of(db).analytics_rep_performance()}


@router.get("/analytics/pipeline-trend")
def analytics_pipeline_trend(
    db: Session = Depends(get_db),
    employee: AuthenticatedEmployee = Depends(get_current_employee),
):
    _require_full_analytics(employee)
    return {"weeks": memory_of(db).analytics_pipeline_trend()}


# ── Employees (Admin only) ────────────────────────────────────────────────────

@router.get("/employees")
def list_employees(
    db: Session = Depends(get_db),
    employee: AuthenticatedEmployee = Depends(get_current_employee),
):
    if not employee.can_manage_employees():
        raise HTTPException(status_code=403, detail="Only Admin can view employees.")
    return [
        {
            "id": e.id,
            "name": e.name,
            "email": e.email,
            "role": e.role,
            "is_active": e.is_active,
            # Connection state only — the refresh token itself never leaves the database.
            "google_calendar_connected": bool(e.google_calendar_connected),
            "created_at": e.created_at.isoformat() if e.created_at else None,
        }
        for e in memory_of(db).list_employees()
    ]


@router.get("/employees/me")
def get_my_employee(
    db: Session = Depends(get_db),
    employee: AuthenticatedEmployee = Depends(get_current_employee),
):
    """Any authenticated employee's own record. `role` is the role this request was
    authorised with (from the Clerk token), which is what every permission check uses."""
    me = memory_of(db).get_employee(employee.employee_id)
    if me is None:
        raise HTTPException(status_code=404, detail="Employee not found")
    return {
        "id": me.id,
        "role": employee.role.value,
        "is_active": me.is_active,
        "google_calendar_connected": bool(me.google_calendar_connected),
    }


class RoleUpdateRequest(BaseModel):
    role: EmployeeRole


@router.post("/employees/{employee_id}/role")
def update_employee_role(
    employee_id: str,
    body: RoleUpdateRequest,
    db: Session = Depends(get_db),
    employee: AuthenticatedEmployee = Depends(get_current_employee),
):
    if not employee.can_manage_employees():
        raise HTTPException(status_code=403, detail="Only Admin can change roles.")

    # Clerk is the source of truth the JWT reads from — sync it first so we never
    # leave the local mirror ahead of Clerk on a failure.
    try:
        synced = update_user_role(employee_id, body.role.value)
    except ClerkSyncError:
        raise HTTPException(status_code=502, detail="Failed to update role in Clerk. No change applied.")

    updated = memory_of(db).update_employee_role(
        employee_id=employee_id,
        role=body.role,
        performed_by=employee.employee_id,
    )
    db.commit()

    if updated is None:
        # Clerk updated (or skipped in dev) but this employee isn't mirrored locally yet.
        return {
            "employee_id": employee_id,
            "role": body.role.value,
            "clerk_synced": synced,
            "local_mirror": False,
        }

    return {
        "employee_id": employee_id,
        "role": body.role.value,
        "clerk_synced": synced,
        "local_mirror": True,
    }


@router.patch("/employees/{employee_id}/deactivate")
def deactivate_employee(
    employee_id: str,
    db: Session = Depends(get_db),
    employee: AuthenticatedEmployee = Depends(get_current_employee),
):
    """Deactivate an employee. Never deletes the row — every historical lead,
    proposal, meeting and audit-log reference keeps pointing at a real employee.
    A deactivated employee is also blocked at sign-in (see
    app/auth/dependencies.py) so this actually stops them, not just labels them.
    """
    if not employee.can_manage_employees():
        raise HTTPException(status_code=403, detail="Only Admin can deactivate employees.")

    memory = memory_of(db)
    target = memory.get_employee(employee_id)
    if not target:
        raise HTTPException(status_code=404, detail="Employee not found")

    if not target.is_active:
        return {"employee_id": employee_id, "is_active": False}

    # A system must never be able to deactivate its way into having zero admins.
    if target.role == EmployeeRole.admin and memory.count_active_admins(exclude_employee_id=employee_id) == 0:
        raise HTTPException(
            status_code=409,
            detail="Cannot deactivate the last remaining active Admin.",
        )

    memory.set_employee_active(employee_id, active=False, performed_by=employee.employee_id)
    db.commit()
    return {"employee_id": employee_id, "is_active": False}


@router.patch("/employees/{employee_id}/reactivate")
def reactivate_employee(
    employee_id: str,
    db: Session = Depends(get_db),
    employee: AuthenticatedEmployee = Depends(get_current_employee),
):
    """Reactivate a previously deactivated employee. Audit-logged like every
    other change to an employee's standing."""
    if not employee.can_manage_employees():
        raise HTTPException(status_code=403, detail="Only Admin can reactivate employees.")

    memory = memory_of(db)
    target = memory.get_employee(employee_id)
    if not target:
        raise HTTPException(status_code=404, detail="Employee not found")

    if target.is_active:
        return {"employee_id": employee_id, "is_active": True}

    memory.set_employee_active(employee_id, active=True, performed_by=employee.employee_id)
    db.commit()
    return {"employee_id": employee_id, "is_active": True}


# ── Audit log ─────────────────────────────────────────────────────────────────

@router.get("/audit-log")
def get_audit_log(
    entity_type: Optional[str] = Query(default=None),
    employee_id: Optional[str] = Query(default=None),
    date_from: Optional[str] = Query(default=None),
    date_to: Optional[str] = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    employee: AuthenticatedEmployee = Depends(get_current_employee),
):
    # Admin full, Management read-only. Sales/Engineer blocked.
    if not employee.can_view_audit_logs():
        raise HTTPException(status_code=403, detail="Your role cannot view the audit log.")

    logs, total = memory_of(db).paginate_audit_logs(
        entity_type=entity_type,
        employee_id=employee_id,
        date_from=_parse_date(date_from, "date_from"),
        date_to=_parse_date(date_to, "date_to"),
        page=page,
        page_size=page_size,
    )
    return {
        "items": [
            {
                "id": log.id,
                "entity_type": log.entity_type,
                "entity_id": log.entity_id,
                "action": log.action,
                "performed_by": log.performed_by,
                "before_value": log.before_value,
                "after_value": log.after_value,
                "created_at": log.created_at.isoformat() if log.created_at else None,
            }
            for log in logs
        ],
        "page": page,
        "page_size": page_size,
        "total": total,
    }


# ── Recent activity feed ──────────────────────────────────────────────────────

# Audit actions that expose pricing — hidden from Engineers, matching the pricing
# visibility restriction already enforced on proposal/PDF endpoints.
_PRICING_AUDIT_ACTIONS = ("pricing_adjusted",)

_ACTIVITY_VERB = {
    "created": "created",
    "created_placeholder": "started",
    "draft_created": "drafted",
    "updated": "updated",
    "details_updated": "details updated",
    "fields_updated": "updated",
    "pricing_adjusted": "pricing adjusted",
    "customer_accepted": "accepted by customer",
    "accept_token_generated": "accept-link generated",
    "role_changed": "role changed",
    "stale_notified": "flagged as stale",
    "merged_into": "merged",
    "absorbed_customer": "absorbed a duplicate",
    "google_calendar_connection_changed": "calendar connection changed",
}


def _describe_activity(entry, actor_name: str) -> str:
    entity = (entry.entity_type or "").capitalize()
    ref = f"#{entry.entity_id}"
    action = entry.action or ""
    if action.startswith("status_changed_to_"):
        verb = action.replace("status_changed_to_", "").split(".")[-1]
    else:
        verb = _ACTIVITY_VERB.get(action, action.replace("_", " "))
    return f"{entity} {ref} {verb} by {actor_name}"


@router.get("/activity/recent")
def recent_activity(
    db: Session = Depends(get_db),
    employee: AuthenticatedEmployee = Depends(get_current_employee),
):
    """Last 15 audit entries as a human-readable feed (all roles). Engineers do not
    see pricing-related entries, matching the dashboard's pricing-visibility rule."""
    memory = memory_of(db)
    exclude = _PRICING_AUDIT_ACTIONS if employee.is_engineer else None
    entries = memory.list_recent_activity(limit=15, exclude_actions=exclude)

    actor_names: dict[str, str] = {}

    def _actor(performed_by: str) -> str:
        if performed_by not in actor_names:
            emp = memory.get_employee(performed_by)
            actor_names[performed_by] = emp.name if emp and emp.name else performed_by
        return actor_names[performed_by]

    return {
        "items": [
            {
                "description": _describe_activity(e, _actor(e.performed_by)),
                "entity_type": e.entity_type,
                "entity_id": e.entity_id,
                "action": e.action,
                "actor": _actor(e.performed_by),
                "created_at": e.created_at.isoformat() if e.created_at else None,
            }
            for e in entries
        ]
    }


# ── Overview stats (with prior-day trend) ─────────────────────────────────────

@router.get("/overview-stats")
def overview_stats(
    db: Session = Depends(get_db),
    employee: AuthenticatedEmployee = Depends(get_current_employee),
):
    """The Overview page's four headline counts, each with a prior-day `trend` value."""
    return memory_of(db).compute_overview_stats()


# ── Calendar connection + meeting scheduling ──────────────────────────────────

def _sign_state(employee_id: str) -> str:
    """Sign the OAuth state so the callback can trust which employee it belongs to
    (the browser redirect from Google carries no auth header)."""
    import base64
    import hashlib
    import hmac

    from app.config import settings as _settings

    secret = (_settings.clerk_secret_key or _settings.fernet_encryption_key or "dev-insecure").encode()
    sig = hmac.new(secret, employee_id.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{employee_id}:{sig}".encode()).decode()


def _verify_state(state: str) -> Optional[str]:
    import base64
    import hashlib
    import hmac

    from app.config import settings as _settings

    try:
        raw = base64.urlsafe_b64decode(state.encode()).decode()
        employee_id, sig = raw.rsplit(":", 1)
    except Exception:
        return None
    secret = (_settings.clerk_secret_key or _settings.fernet_encryption_key or "dev-insecure").encode()
    expected = hmac.new(secret, employee_id.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    return employee_id


@router.get("/employees/me/calendar/connect")
def calendar_connect(
    employee: AuthenticatedEmployee = Depends(get_current_employee),
):
    """Start the Google OAuth consent flow for the authenticated employee."""
    try:
        auth_url = google_calendar.build_authorization_url(state=_sign_state(employee.employee_id))
    except google_calendar.GoogleCalendarNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return {"authorization_url": auth_url}


def _calendar_redirect(error_reason: Optional[str] = None) -> RedirectResponse:
    """Send the employee's browser back to the dashboard. The query string carries only
    `calendar=connected` or `calendar=error&reason=<fixed short code>`, never a token,
    an authorization code, or text echoed from the request."""
    from urllib.parse import urlencode

    query = {"calendar": "connected"} if error_reason is None else {"calendar": "error", "reason": error_reason}
    base = settings.frontend_base_url.rstrip("/")
    return RedirectResponse(f"{base}/dashboard/meetings?{urlencode(query)}", status_code=303)


@router.get("/employees/me/calendar/callback")
def calendar_callback(
    code: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    error: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    """OAuth callback — identifies the employee from the signed `state` (no bearer on
    a browser redirect), exchanges the code, and stores the refresh token encrypted.
    Always ends in a 303 back to the dashboard (see _calendar_redirect)."""
    employee_id = _verify_state(state) if state else None
    if not employee_id:
        return _calendar_redirect("invalid_state")
    if error:
        # Google redirects here with ?error=... when consent is declined or fails.
        return _calendar_redirect("access_denied" if error == "access_denied" else "consent_failed")
    if not code:
        return _calendar_redirect("missing_code")

    try:
        refresh_token = google_calendar.exchange_code(code)
    except google_calendar.GoogleCalendarNotConfiguredError:
        return _calendar_redirect("not_configured")
    except Exception:
        logger.warning("Google authorization-code exchange failed for employee %s", employee_id)
        return _calendar_redirect("exchange_failed")

    from app.security.encryption import encrypt

    updated = memory_of(db).update_employee_google_credentials(
        employee_id=employee_id,
        encrypted_refresh_token=encrypt(refresh_token),
        calendar_id="primary",
        performed_by=employee_id,
    )
    db.commit()
    if updated is None:
        return _calendar_redirect("employee_not_found")
    return _calendar_redirect()


class ScheduleMeetingRequest(BaseModel):
    scheduled_at: datetime
    duration_minutes: Optional[int] = None


@router.post("/leads/{lead_id}/schedule-meeting")
def schedule_meeting(
    lead_id: int,
    body: ScheduleMeetingRequest,
    db: Session = Depends(get_db),
    employee: AuthenticatedEmployee = Depends(get_current_employee),
):
    """Employee-initiated booking — books on a connected calendar after re-verifying
    the slot is free (shares book_meeting with the conversational path)."""
    if not employee.can_schedule_meeting():
        raise HTTPException(status_code=403, detail="Your role cannot schedule meetings.")

    memory = memory_of(db)
    lead = memory.get_lead(lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")

    from app.config import settings as _settings
    duration = body.duration_minutes or _settings.scheduling_default_duration_minutes
    scheduled_at = body.scheduled_at
    if scheduled_at.tzinfo is None:
        scheduled_at = scheduled_at.replace(tzinfo=timezone.utc)

    # Resolve which connected employee to book on: the authenticated employee if they
    # have a calendar connected, else the lead's assigned employee if they do.
    target_id = None
    me = memory.get_employee(employee.employee_id)
    if me and me.google_calendar_connected:
        target_id = me.id
    elif lead.assigned_employee_id:
        assigned = memory.get_employee(lead.assigned_employee_id)
        if assigned and assigned.google_calendar_connected:
            target_id = assigned.id
    if not target_id:
        raise HTTPException(
            status_code=409,
            detail="No connected Google Calendar available to book this meeting on.",
        )

    try:
        meeting = booking.book_meeting(
            memory=memory,
            lead_id=lead_id,
            employee_id=target_id,
            scheduled_at=scheduled_at,
            duration_minutes=duration,
            performed_by=employee.employee_id,
        )
        db.commit()
    except booking.SlotUnavailableError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except availability.NoConnectedCalendarsError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except google_calendar.GoogleCalendarNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    return {
        "meeting_id": meeting.id,
        "lead_id": lead_id,
        "employee_id": target_id,
        "scheduled_at": meeting.scheduled_at.isoformat(),
        "duration_minutes": meeting.duration_minutes,
        "meet_link": meeting.meet_link,
        "status": meeting.status,
    }


@router.get("/meetings")
def list_meetings(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    employee: AuthenticatedEmployee = Depends(get_current_employee),
):
    """All meetings across every lead, newest-first — the Meetings dashboard page's
    data source. Same {items, page, page_size, total} shape as GET /audit-log.
    Sales sees only meetings on their own connected calendar (matching
    can_schedule_meeting()'s own admin/sales split); Admin/Management see every
    meeting. Engineers have no reason to see customer-identifying meeting data
    (same "nothing to access yet" restriction already applied to their lead view)."""
    if employee.is_engineer:
        raise HTTPException(status_code=403, detail="Your role cannot view meetings.")

    memory = memory_of(db)
    assigned_filter = employee.employee_id if employee.is_sales else None
    meetings, total = memory.paginate_meetings(employee_id=assigned_filter, page=page, page_size=page_size)

    return {
        "items": [
            {
                "id": m.id,
                "lead_id": m.lead_id,
                "employee_id": m.employee_id,
                "employee_name": m.employee.name if m.employee else None,
                "customer": (
                    {
                        "id": m.lead.customer.id,
                        "name": m.lead.customer.name,
                        "email": m.lead.customer.email,
                        "phone": m.lead.customer.phone,
                    }
                    if m.lead and m.lead.customer else None
                ),
                "scheduled_at": m.scheduled_at.isoformat(),
                "duration_minutes": m.duration_minutes,
                "status": m.status,
                "meet_link": m.meet_link,
            }
            for m in meetings
        ],
        "page": page,
        "page_size": page_size,
        "total": total,
    }


# ── Internal jobs ─────────────────────────────────────────────────────────────

async def _require_admin_or_internal_key(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> None:
    """A valid X-Internal-Key (Cloud Scheduler — it has no Clerk session), otherwise
    exactly the ordinary path: authenticate, then require Admin. A wrong key is the
    same as no key, so it falls through to that path (401 without a token)."""
    if has_valid_internal_key(request):
        return
    employee = await get_current_employee(request, credentials, db)
    if not employee.is_admin:
        raise HTTPException(status_code=403, detail="Only Admin can run the stale-lead check.")


@router.post("/internal/check-stale-leads")
def run_check_stale_leads(
    db: Session = Depends(get_db),
    _: None = Depends(_require_admin_or_internal_key),
):
    """Admin or INTERNAL_API_KEY. Scans for quiet leads and fires internal follow-up
    notifications. Idempotent — safe to call repeatedly (Cloud Scheduler calls it)."""
    from app.jobs.stale_leads import check_stale_leads
    return check_stale_leads(db)


# ── Helpers ───────────────────────────────────────────────────────────────────

def memory_of(db: Session) -> MemoryManager:
    return MemoryManager(db)


_PDF_GENERATION_TIMEOUT_SECONDS = 20.0


# ── Customer memory ────────────────────────────────────────────────────────────

class MemoryQuestionRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=500)
    # Optional: when the question is asked from an open customer record, pass the id
    # and skip name resolution entirely.
    customer_id: Optional[int] = None


def _assert_may_read_customer_memory(
    memory: MemoryManager, customer_id: int, employee: AuthenticatedEmployee
) -> None:
    """Same scoping as the rest of the Sales-role surface: unassigned leads are fair
    game, a customer whose every lead belongs to another rep is not. Admin and
    Management see everything, as they do elsewhere.
    """
    if not employee.is_sales:
        return
    dossier = memory.get_customer_dossier(customer_id)
    leads = dossier.get("leads") or []
    if not leads:
        return
    if all(
        lead.assigned_employee_id and lead.assigned_employee_id != employee.employee_id
        for lead in leads
    ):
        raise HTTPException(
            status_code=403, detail="This customer's leads are assigned to another rep."
        )


@router.post("/memory/ask")
def ask_customer_memory(
    body: MemoryQuestionRequest,
    db: Session = Depends(get_db),
    employee: AuthenticatedEmployee = Depends(get_current_employee),
):
    """Ask a question about a customer and get an answer from stored memory.

    Read-only. The answer is grounded in retrieved records only — whatever is not
    on file comes back as not on file, never as a guess.
    """
    if not employee.can_query_customer_memory():
        raise HTTPException(status_code=403, detail="Your role cannot query customer memory.")

    memory = MemoryManager(db)
    if body.customer_id is not None:
        if not memory.get_customer_dossier(body.customer_id):
            raise HTTPException(status_code=404, detail="Customer not found")
        _assert_may_read_customer_memory(memory, body.customer_id, employee)

    result = orchestrator.answer_customer_question(
        question=body.question,
        performed_by=employee.employee_id,
        customer_id=body.customer_id,
    )

    # A question that resolved by name still has to pass the same scoping check —
    # otherwise naming a customer would sidestep the id-based check above.
    resolved_id = result.get("customer_id")
    if result.get("resolved") and resolved_id and body.customer_id is None:
        _assert_may_read_customer_memory(memory, resolved_id, employee)

    if result.get("error"):
        raise HTTPException(status_code=503, detail=result["error"])

    return {
        "answer": result.get("answer"),
        "resolved": result.get("resolved", False),
        "customer_id": resolved_id,
        "candidates": result.get("candidates", []),
        "citations": result.get("citations", []),
        "retrieval_mode": result.get("recall_mode"),
    }


@router.get("/customers/{customer_id}/memory")
def get_customer_memory(
    customer_id: int,
    kind: Optional[str] = Query(None, description="Filter to one memory kind"),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
    employee: AuthenticatedEmployee = Depends(get_current_employee),
):
    """Everything remembered about one customer — the profile, then the raw
    memories. This is the audit view: what the agent believes and why."""
    if not employee.can_query_customer_memory():
        raise HTTPException(status_code=403, detail="Your role cannot query customer memory.")

    memory = MemoryManager(db)
    dossier = memory.get_customer_dossier(customer_id)
    if not dossier:
        raise HTTPException(status_code=404, detail="Customer not found")
    _assert_may_read_customer_memory(memory, customer_id, employee)

    kinds = None
    if kind:
        try:
            kinds = (MemoryKind(kind),)
        except ValueError:
            raise HTTPException(status_code=422, detail=f"Unknown memory kind: {kind!r}")

    memories = memory.list_customer_memories(customer_id, kinds=kinds, limit=limit)
    profile = dossier.get("profile")
    customer = dossier["customer"]

    return {
        "customer": {
            "customer_id": customer.id,
            "name": customer.name,
            "email": customer.email,
            "company": customer.company,
        },
        "profile": {
            "summary": profile.summary,
            "traits": profile.traits or [],
            "open_questions": profile.open_questions or [],
            "agent_notes": profile.agent_notes,
            "next_best_action": profile.next_best_action,
            "last_reflected_at": profile.last_reflected_at.isoformat() if profile.last_reflected_at else None,
            "reflection_count": profile.reflection_count,
        } if profile else None,
        "memory_count": memory.count_customer_memories(customer_id),
        "memories": [
            {
                "id": m.id,
                "kind": m.kind.value,
                "source": m.source.value,
                "summary": m.summary,
                "detail": m.detail,
                "tags": m.tags or [],
                "salience": m.salience,
                "lead_id": m.lead_id,
                "occurred_at": m.occurred_at.isoformat() if m.occurred_at else None,
                "created_by": m.created_by,
            }
            for m in memories
        ],
    }


@router.post("/leads/{lead_id}/reflect")
def reflect_on_lead_now(
    lead_id: int,
    db: Session = Depends(get_db),
    employee: AuthenticatedEmployee = Depends(get_current_employee),
):
    """Force a reflection on a lead now, instead of waiting for a milestone.

    Synchronous on purpose — the caller asked for it and wants the result, unlike
    the automatic path where a customer is waiting on a reply.
    """
    if not employee.can_query_customer_memory():
        raise HTTPException(status_code=403, detail="Your role cannot trigger reflection.")

    from app.memory import reflection

    memory = MemoryManager(db)
    lead = memory.get_lead(lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    _assert_may_read_customer_memory(memory, lead.customer_id, employee)

    result = reflection.reflect_on_lead(
        memory, lead_id, trigger=reflection.TRIGGER_MANUAL, performed_by=employee.employee_id
    )
    db.commit()
    return result


# ── Agent governance ───────────────────────────────────────────────────────────

@router.get("/governance/agents")
def list_governed_agents(
    db: Session = Depends(get_db),
    employee: AuthenticatedEmployee = Depends(get_current_employee),
):
    """What every AI agent in this system is permitted and forbidden to do.

    Served from the registry the enforcement layer actually reads, so this can never
    drift from what the agents are really allowed to do — the failure mode of a
    governance page maintained by hand.
    """
    if not employee.can_view_audit_logs():
        raise HTTPException(status_code=403, detail="Your role cannot view agent governance.")

    memory = memory_of(db)
    return {
        "agents": [
            {
                "id": spec.id,
                "name": spec.name,
                "purpose": spec.purpose,
                "owner": spec.owner,
                "autonomy": spec.autonomy,
                "speaks_to": spec.audience,
                "uses_llm": spec.uses_llm,
                "enabled": not gov_policy._disabled(spec.id, memory=memory),
                "capabilities": list(spec.capabilities),
                "prohibitions": list(spec.prohibitions),
                "human_gates": list(spec.human_gates),
                "guardrails": list(spec.guardrails),
                "module": spec.module,
            }
            for spec in gov_registry.all_specs()
        ],
        "limits": {
            "replies_per_lead_per_24h": settings.agent_daily_action_limit_per_lead,
            "disabled_agents": list(settings.disabled_agents),
        },
    }


@router.get("/governance/runs")
def governance_runs(
    days: int = Query(7, ge=1, le=90),
    db: Session = Depends(get_db),
    employee: AuthenticatedEmployee = Depends(get_current_employee),
):
    """How the agents are performing: volume, failures, latency and model spend.

    Every number is derived from the run records the harness writes, so the report
    and the audit trail cannot disagree — the one thing an operations view of an
    autonomous system must never do.
    """
    if not employee.can_view_audit_logs():
        raise HTTPException(status_code=403, detail="Your role cannot view agent runs.")

    memory = MemoryManager(db)
    since = datetime.now(timezone.utc) - timedelta(days=days)
    return memory.agent_run_stats(since)


@router.get("/governance/activity")
def governance_activity(
    days: int = Query(7, ge=1, le=90),
    db: Session = Depends(get_db),
    employee: AuthenticatedEmployee = Depends(get_current_employee),
):
    """What the agents have actually been doing, against what they are allowed to do.

    Two counts that matter more than the rest: `denials` (an agent reached for
    something governance refused) and `guardrail_blocks` (an agent said something
    that had to be withheld). Both should be near zero; a rising number is the
    signal to look at, which is the whole reason they are counted separately.
    """
    if not employee.can_view_audit_logs():
        raise HTTPException(status_code=403, detail="Your role cannot view agent governance.")

    memory = MemoryManager(db)
    since = datetime.now(timezone.utc) - timedelta(days=days)
    return memory.governance_activity(since)


def _start_pdf_generation(generate_fn, proposal_id: int) -> None:
    """Run `generate_fn` in a background thread — split out from
    _trigger_pdf_generation as its own seam so tests can substitute a synchronous
    call instead (see tests/conftest.py). generate_fn opens its own DB session,
    which under the test harness's single shared SQLite connection (StaticPool)
    is not safe to run concurrently with the request's own session no matter how
    carefully the call sites avoid touching shared ORM state — real deployments
    don't share a connection this way, so this only matters for tests.
    """
    import threading

    threading.Thread(
        target=generate_fn, daemon=True, name=f"pdf-generation-proposal-{proposal_id}"
    ).start()


def _trigger_pdf_generation(proposal_id: int, proposal, db: Session, memory: MemoryManager) -> None:
    """
    Attempt PDF generation in a background thread so the approval response
    is not blocked by R2 upload time. If it fails, pdf_url stays None (pending).

    A real thread can't be safely force-killed from outside in Python, so
    "timeout" here means the retry/back-off loop inside generate_and_upload_pdf
    is bounded to _PDF_GENERATION_TIMEOUT_SECONDS total — it can't run away
    indefinitely if storage is down. Every step from here on is wrapped so a
    failure (including the final session close) is logged, never left as an
    unhandled exception in the thread — that's what previously surfaced as
    "Cannot operate on a closed database" whenever this thread's session outlived
    the engine it was bound to.
    """

    def _generate():
        from app.memory.db import SessionLocal
        inner_db = SessionLocal()
        try:
            inner_memory = MemoryManager(inner_db)
            inner_proposal = inner_memory.get_proposal(proposal_id)
            if not inner_proposal:
                return
            content_config = _get_yaml_config()
            proposal_data = {
                **dict(inner_proposal.content or {}),
                "proposal_id": proposal_id,
                "version": inner_proposal.version,
                "requires_manager_signoff": inner_proposal.requires_manager_signoff,
                "total_value": inner_proposal.total_value,
            }
            pdf_url = generate_and_upload_pdf(
                proposal_data=proposal_data,
                content_config=content_config,
                proposal_id=proposal_id,
                timeout_seconds=_PDF_GENERATION_TIMEOUT_SECONDS,
            )
            if pdf_url:
                inner_memory.transition_proposal_status(
                    proposal_id=proposal_id,
                    new_status=ProposalStatus.approved,
                    performed_by="system",
                    pdf_url=pdf_url,
                )
                inner_db.commit()
        except Exception:
            logger.exception("Background PDF generation failed for proposal %s", proposal_id)
            try:
                inner_db.rollback()
            except Exception:
                logger.exception(
                    "Rollback also failed after background PDF generation error for proposal %s", proposal_id
                )
        finally:
            try:
                inner_db.close()
            except Exception:
                logger.warning(
                    "Closing the background PDF generation session failed for proposal %s "
                    "(connection likely already invalid) — nothing left to clean up.",
                    proposal_id,
                )

    _start_pdf_generation(_generate, proposal_id)
