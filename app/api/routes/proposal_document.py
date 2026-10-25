"""BE-13 — the proposal's structured customer document: read it, change one section, and
preview the PDF. Contract: docs/api/proposal_document.md.

    GET   /api/v1/dashboard/proposals/{id}/document     staff who can open the proposal
                                                         (Engineers: the technical part)
    PATCH /api/v1/dashboard/proposals/{id}/document     one section, by the roles
                                                         config/proposal_document.yaml allows
    GET   /api/v1/dashboard/proposals/{id}/preview.pdf  the current draft as the customer
                                                         would get it, stamped DRAFT until
                                                         approved; never stored

The header, footer, watermark, letterhead, signatory and fixed sentences are config only;
the price changes only through adjust-pricing; every figure is computed.
"""
from __future__ import annotations

from typing import Any, Literal, Optional, Union

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.auth.dependencies import AuthenticatedEmployee, get_current_employee
from app.auth.scoping import require_lead_access
from app.memory.db import get_db
from app.memory.memory_manager import MemoryManager
from app.memory.models import ProposalStatus
from app.proposals import document as d
from app.proposals import document_service
from app.security.rate_limit import RateLimitLog

router = APIRouter(prefix="/api/v1/dashboard")

PREVIEW_LIMIT = 20                      # renders per employee per window
PREVIEW_WINDOW_SECONDS = 60
_preview_log = RateLimitLog(PREVIEW_WINDOW_SECONDS)
_READERS = ("admin", "management", "sales", "engineer")


# ── Response models ──────────────────────────────────────────────────────────────

class MissingField(BaseModel):
    field: str
    message: str


class PaymentFigure(BaseModel):
    percent: str
    label: str
    short_label: Optional[str] = None
    amount: str


class SubsidySlab(BaseModel):
    kw: str
    rate: str
    amount: str


class SubsidyStateLine(BaseModel):
    state: str
    amount: str


class SubsidyFigures(BaseModel):
    applies: bool
    slabs: list[SubsidySlab]
    # A confirmed state top-up, printed only when config/subsidies.yaml says so (BE-14).
    state_lines: list[SubsidyStateLine] = []
    total: Optional[str] = None
    effective_cost: Optional[str] = None


class GenerationFigures(BaseModel):
    units_per_kwp_per_month: int
    monthly_units: Optional[int] = None
    annual_units: Optional[int] = None


class SpaceFigures(BaseModel):
    low: int
    high: int
    single: int


class AmountWords(BaseModel):
    basic: Optional[str] = None
    gst: Optional[str] = None
    total: Optional[str] = None


class DocumentFigures(BaseModel):
    """Computed by app/proposals/document.figures — never typed."""
    layout: str
    capacity_kwp: Optional[str] = None
    price_mode: str
    basic: Optional[str] = None
    gst: Optional[str] = None
    total: Optional[str] = None
    gst_rate: str
    words: AmountWords
    payments: list[PaymentFigure]
    whole_rupees: bool
    subsidy: SubsidyFigures
    generation: GenerationFigures
    space_required_sqft: Optional[SpaceFigures] = None
    validity_days: int


class Signatory(BaseModel):
    name: str
    title: str
    for_line: str


class TechnicalPlant(BaseModel):
    model_config = ConfigDict(extra="forbid")
    capacity_kwp: Optional[str] = None
    dcr: bool


class TechnicalDocument(BaseModel):
    """What an Engineer sees: no customer, contact or price."""
    model_config = ConfigDict(extra="forbid")
    view: Literal["technical"]
    revision: int
    plant: TechnicalPlant
    system: d.System
    boq: list[d.BoqRow]


class DocumentView(BaseModel):
    proposal_id: int
    has_document: bool
    version: Optional[int] = None
    status: Optional[str] = None
    revision: Optional[int] = None
    layout: Optional[str] = None
    locked: Optional[bool] = None
    editable_sections: list[str] = Field(default_factory=list)
    document: Optional[Union[TechnicalDocument, d.ProposalDocument]] = None
    figures: Optional[DocumentFigures] = None
    missing_required: Optional[list[MissingField]] = None
    document_reference: Optional[str] = None
    document_date: Optional[str] = None
    signatory: Optional[Signatory] = None


class FieldChange(BaseModel):
    field: str
    before: Any = None
    after: Any = None


class DocumentEditResponse(DocumentView):
    changes: list[FieldChange]
    regenerated_sections: list[str]
    total_value: Optional[float] = None                  # null for Engineers (no pricing)
    requires_manager_signoff: Optional[bool] = None      # null for Engineers


class DocumentEditRequest(BaseModel):
    """Replace one section. `value` is the section's object (addressee, connection, site,
    pm_surya_ghar, plant, system) or list (boq, customer_scope, payment_terms, notes)."""
    base_revision: int = Field(ge=0)
    section: str = Field(min_length=1, max_length=40)
    value: Union[dict[str, Any], list[Any]]


# ── Routes ───────────────────────────────────────────────────────────────────────

def _proposal_for(db: Session, proposal_id: int, employee: AuthenticatedEmployee):
    if employee.role.value not in _READERS:
        raise HTTPException(status_code=403, detail="Your role cannot open proposals.")
    memory = MemoryManager(db)
    proposal = memory.get_proposal(proposal_id)
    if not proposal:
        raise HTTPException(status_code=404, detail="Proposal not found")
    require_lead_access(employee, memory.get_lead(proposal.lead_id), "This proposal is not assigned to you.")
    return proposal


@router.get("/proposals/{proposal_id}/document", response_model=DocumentView)
def get_proposal_document(
    proposal_id: int,
    db: Session = Depends(get_db),
    employee: AuthenticatedEmployee = Depends(get_current_employee),
):
    """The structured document, its computed figures, what still blocks approval and the
    sections this viewer may change (none once approved)."""
    proposal = _proposal_for(db, proposal_id, employee)
    return document_service.view(proposal, employee.role.value)


@router.patch("/proposals/{proposal_id}/document", response_model=DocumentEditResponse)
def edit_proposal_document(
    proposal_id: int,
    body: DocumentEditRequest,
    db: Session = Depends(get_db),
    employee: AuthenticatedEmployee = Depends(get_current_employee),
):
    """Change one section of a draft's document. 403 field_not_editable (role / fixed
    section), 409 proposal_locked / stale_version / no_document, 422 invalid_value /
    payment_terms_total / make_not_in_catalogue."""
    _proposal_for(db, proposal_id, employee)
    if not document_service.editable_sections(employee.role.value):
        raise HTTPException(status_code=403, detail={"code": "field_not_editable",
                                                     "message": "Your role cannot change proposal documents."})
    proposal = document_service.lock_proposal(db, proposal_id)
    try:
        result = document_service.edit(db, proposal, role=employee.role.value, employee_id=employee.employee_id,
                                       section=body.section, value=body.value, base_revision=body.base_revision)
    except d.DocumentError as exc:
        db.rollback()
        raise HTTPException(status_code=exc.status_code, detail=exc.body())
    db.commit()
    proposal = MemoryManager(db).get_proposal(proposal_id)
    priced = not employee.is_engineer
    return {**document_service.view(proposal, employee.role.value), **result,
            "total_value": proposal.total_value if priced else None,
            "requires_manager_signoff": proposal.requires_manager_signoff if priced else None}


@router.get("/proposals/{proposal_id}/preview.pdf", response_class=Response,
            responses={200: {"content": {"application/pdf": {}}, "description": "The PDF"}})
def preview_proposal_pdf(
    proposal_id: int,
    db: Session = Depends(get_db),
    employee: AuthenticatedEmployee = Depends(get_current_employee),
):
    """The document as the customer would receive it, rendered now and never stored —
    stamped "DRAFT — NOT FOR CUSTOMER" until the proposal is approved. It carries
    pricing and contact details, so Engineers are refused, as for /pdf."""
    if employee.is_engineer:
        raise HTTPException(status_code=403, detail="Engineers cannot access proposal PDFs.")
    proposal = _proposal_for(db, proposal_id, employee)
    doc = d.stored(proposal.content)
    if doc is None:
        raise HTTPException(status_code=409, detail={"code": "no_document",
                                                     "message": "This proposal was drafted before structured "
                                                                "documents; use its stored PDF."})
    if not _preview_log.hit(employee.employee_id, PREVIEW_LIMIT):
        raise HTTPException(status_code=429, detail={"code": "rate_limited",
                                                     "message": "Too many previews. Wait a minute and try again."})
    if d.figures(doc)["total"] is None:
        raise HTTPException(status_code=409, detail={"code": "missing_required",
                                                     "message": "Set a price before previewing the PDF.",
                                                     "missing": [{"field": "price", "message": "a price"}]})
    from app.proposals import document_render
    from app.proposals.reference import today_ist
    approved = ProposalStatus(proposal.status) in (ProposalStatus.approved, ProposalStatus.sent,
                                                   ProposalStatus.accepted)
    try:
        pdf = document_render.render(doc, reference=proposal.document_reference,
                                     doc_date=proposal.document_date or today_ist(), draft=not approved)
    except document_render.DocumentRenderError as exc:
        raise HTTPException(status_code=409, detail={"code": "render_failed", "message": str(exc)})
    filename = f"Grid-Nova-Proposal-{proposal.id}-v{proposal.version}{'' if approved else '-DRAFT'}.pdf"
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{filename}"', "Cache-Control": "no-store"})
