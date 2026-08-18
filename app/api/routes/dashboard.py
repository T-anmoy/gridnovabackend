"""
Internal dashboard API — all endpoints require Clerk authentication.
The two-stage approval gate (standard vs. manager sign-off) is enforced here.
"""
from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth.clerk_client import ClerkSyncError, update_user_role
from app.auth.dependencies import AuthenticatedEmployee, get_current_employee
from app.memory.db import get_db
from app.memory.memory_manager import MemoryManager
from app.memory.models import EmployeeRole, LeadSource, ProposalStatus, TimelineEventType
from app.rules.business_rules import evaluate_lead
from app.scheduling import availability, booking, google_calendar
from app.notifications.notifier import notifier
from app.orchestrator.orchestrator import orchestrator
from app.pdf.generator import generate_and_upload_pdf

router = APIRouter(prefix="/api/v1/dashboard")

def _get_yaml_config() -> dict:
    # Single source of truth — the content engine owns loading + validation + caching.
    from app.proposals.content_engine import load_content_config
    return load_content_config()


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
    status: Optional[str] = Query(default=None),
    date_from: Optional[str] = Query(default=None),
    date_to: Optional[str] = Query(default=None),
    lead_id: Optional[int] = Query(default=None),
    db: Session = Depends(get_db),
    employee: AuthenticatedEmployee = Depends(get_current_employee),
):
    if not employee.can_view_full_analytics():
        raise HTTPException(status_code=403, detail="Only Admin or Management can export proposals.")

    status_enum: Optional[ProposalStatus] = None
    if status:
        try:
            status_enum = ProposalStatus(status)
        except ValueError:
            raise HTTPException(status_code=422, detail=f"Unknown proposal status: {status!r}")

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

    memory.transition_proposal_status(
        proposal_id=proposal_id,
        new_status=ProposalStatus.approved,
        performed_by=employee.employee_id,
        approved_by_employee_id=employee.employee_id,
    )
    memory.add_timeline_event(
        lead_id=proposal.lead_id,
        event_type=TimelineEventType.proposal_approved,
        description=f"Approved by {employee.employee_id}",
        created_by=employee.employee_id,
    )
    db.commit()

    _trigger_pdf_generation(proposal_id, proposal, db, memory)

    notifier.proposal_approved(proposal_id=proposal_id, lead_id=proposal.lead_id)
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

    memory.transition_proposal_status(
        proposal_id=proposal_id,
        new_status=ProposalStatus.approved,
        performed_by=employee.employee_id,
        approved_by_employee_id=employee.employee_id,
    )
    memory.add_timeline_event(
        lead_id=proposal.lead_id,
        event_type=TimelineEventType.manager_signoff_completed,
        description=f"Manager sign-off by {employee.employee_id}",
        created_by=employee.employee_id,
    )
    db.commit()

    _trigger_pdf_generation(proposal_id, proposal, db, memory)

    notifier.proposal_approved(proposal_id=proposal_id, lead_id=proposal.lead_id)
    return {"status": "approved", "proposal_id": proposal_id, "signoff_by": employee.employee_id}


class RevisionRequest(BaseModel):
    reason: str


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
    memory.generate_accept_token(proposal_id, performed_by=employee.employee_id)
    memory.add_timeline_event(
        lead_id=proposal.lead_id,
        event_type=TimelineEventType.proposal_sent,
        description=f"Sent by {employee.employee_id}",
        created_by=employee.employee_id,
    )
    db.commit()

    ctx = memory.get_lead_context(proposal.lead_id)
    customer = ctx.get("customer")
    if customer:
        notifier.proposal_sent(proposal_id=proposal_id, customer_email=customer.email)

    return {"status": "sent", "proposal_id": proposal_id}


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
    status: Optional[str] = Query(default=None),
    date_from: Optional[str] = Query(default=None),
    date_to: Optional[str] = Query(default=None),
    lead_id: Optional[int] = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    employee: AuthenticatedEmployee = Depends(get_current_employee),
):
    status_enum: Optional[ProposalStatus] = None
    if status:
        try:
            status_enum = ProposalStatus(status)
        except ValueError:
            raise HTTPException(status_code=422, detail=f"Unknown proposal status: {status!r}")

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
            "created_at": e.created_at.isoformat() if e.created_at else None,
        }
        for e in memory_of(db).list_employees()
    ]


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


@router.get("/employees/me/calendar/callback")
def calendar_callback(
    code: str = Query(...),
    state: str = Query(...),
    db: Session = Depends(get_db),
):
    """OAuth callback — identifies the employee from the signed `state` (no bearer on
    a browser redirect), exchanges the code, and stores the refresh token encrypted."""
    employee_id = _verify_state(state)
    if not employee_id:
        raise HTTPException(status_code=400, detail="Invalid OAuth state.")

    try:
        refresh_token = google_calendar.exchange_code(code)
    except google_calendar.GoogleCalendarNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception:
        raise HTTPException(status_code=400, detail="Failed to exchange authorization code.")

    from app.security.encryption import encrypt

    updated = memory_of(db).update_employee_google_credentials(
        employee_id=employee_id,
        encrypted_refresh_token=encrypt(refresh_token),
        calendar_id="primary",
        performed_by=employee_id,
    )
    db.commit()
    if updated is None:
        raise HTTPException(status_code=404, detail="Employee not found for this connection.")
    return {"status": "connected", "employee_id": employee_id, "calendar_id": "primary"}


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


# ── Internal jobs ─────────────────────────────────────────────────────────────

@router.post("/internal/check-stale-leads")
def run_check_stale_leads(
    db: Session = Depends(get_db),
    employee: AuthenticatedEmployee = Depends(get_current_employee),
):
    """Admin-only. Scans for quiet leads and fires internal follow-up notifications.
    Idempotent — safe to call repeatedly (e.g. from a cron job once deployed)."""
    if not employee.is_admin:
        raise HTTPException(status_code=403, detail="Only Admin can run the stale-lead check.")
    from app.jobs.stale_leads import check_stale_leads
    return check_stale_leads(db)


# ── Helpers ───────────────────────────────────────────────────────────────────

def memory_of(db: Session) -> MemoryManager:
    return MemoryManager(db)


def _trigger_pdf_generation(proposal_id: int, proposal, db: Session, memory: MemoryManager) -> None:
    """
    Attempt PDF generation in a background thread so the approval response
    is not blocked by R2 upload time. If it fails, pdf_url stays None (pending).
    """
    import threading

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
            inner_db.rollback()
        finally:
            inner_db.close()

    threading.Thread(target=_generate, daemon=True).start()
