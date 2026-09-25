"""
Site assessment intake — receives raw customer-provided data from the
/proposal/new frontend form and lets staff review it before a Proposal is
drafted. Kept separate from app/api/routes/dashboard.py's Proposal endpoints:
ProposalInputs is the customer's raw input, Proposal is the AI-generated output.
"""
from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, field_validator

from app.auth.dependencies import AuthenticatedEmployee, get_current_employee
from app.memory.db import get_db
from app.memory.lead_fields import fill_lead_from_inputs
from app.memory.memory_manager import MemoryManager
from app.memory.models import (
    INSTALLATION_PHOTO_SLOTS,
    ConnectionPhase,
    ConnectionType,
    ConsumerPriorityClass,
    CustomerType,
    DiscomConsumerCategory,
    EquipmentTier,
    GovernmentScheme,
    InputStatus,
    ProjectFamily,
    ShadingCondition,
    SiteType,
    TimelineEventType,
)
from app.orchestrator.orchestrator import orchestrator
from sqlalchemy.orm import Session

router = APIRouter(prefix="/api/v1/proposal-inputs")


class DocumentRef(BaseModel):
    type: str
    filename: str
    # Cloudflare R2 object key — null until upload is wired (see TODO on the model).
    storage_key: Optional[str] = None


class ConsumptionMonth(BaseModel):
    month: str  # "YYYY-MM"
    units_kwh: Optional[float] = None
    bill_inr: Optional[float] = None


class InstallationPhotoRef(BaseModel):
    filename: str
    # Same R2-upload TODO as DocumentRef above.
    storage_key: Optional[str] = None


class SignatureRef(BaseModel):
    filename: str
    storage_key: Optional[str] = None
    # ISO string, not a datetime — this is stored in a plain JSON column, and a
    # datetime object doesn't serialize through that without extra handling
    # nothing else here needs.
    signed_at: Optional[str] = None


class ProposalInputsCreate(BaseModel):
    lead_id: int

    # Customer & Project
    customer_type: CustomerType
    organisation_name: Optional[str] = None
    gst_number: Optional[str] = None
    project_family: ProjectFamily
    scheme_linked: bool = False
    scheme_name: Optional[GovernmentScheme] = GovernmentScheme.none

    # Site Location
    state: str
    district: str
    pin_code: str
    full_address: Optional[str] = None
    discom: Optional[str] = None
    consumer_number: Optional[str] = None
    site_type: SiteType
    roof_area_sqft: Optional[float] = None
    floors: Optional[int] = None
    shading_condition: Optional[ShadingCondition] = None
    structural_notes: Optional[str] = None

    # Roof detail
    roof_ownership_confirmed: Optional[bool] = None
    roofs_available_count: Optional[int] = None
    roofs_considered_count: Optional[int] = None
    future_roof_expansion_planned: Optional[bool] = None
    roof_age_years: Optional[float] = None

    # Load & Output
    avg_monthly_bill_inr: Optional[float] = None
    avg_monthly_units_kwh: Optional[float] = None
    consumption_history: Optional[list[ConsumptionMonth]] = None
    peak_load_kw: Optional[float] = None
    sanctioned_load_kw: Optional[float] = None
    connection_phase: Optional[ConnectionPhase] = None
    desired_output_kw: Optional[float] = None
    load_notes: Optional[str] = None

    # DISCOM consumer classification
    discom_consumer_category: Optional[DiscomConsumerCategory] = None
    consumer_priority_class: ConsumerPriorityClass = ConsumerPriorityClass.normal

    # System Configuration
    connection_type: ConnectionType
    net_metering_required: Optional[bool] = None
    dcr_panels_required: bool = False
    ev_charging: bool = False
    battery_storage: bool = False
    equipment_tier: Optional[EquipmentTier] = None
    additional_requirements: Optional[str] = None

    # Documents
    document_refs: Optional[list[DocumentRef]] = None
    installation_photo_refs: Optional[dict[str, InstallationPhotoRef]] = None
    staff_signature_ref: Optional[SignatureRef] = None
    customer_signature_ref: Optional[SignatureRef] = None

    @field_validator("installation_photo_refs")
    @classmethod
    def _known_photo_slots_only(cls, value):
        if value is None:
            return value
        unknown = set(value) - set(INSTALLATION_PHOTO_SLOTS)
        if unknown:
            raise ValueError(
                f"Unknown installation photo slot(s): {sorted(unknown)}. "
                f"Expected one of {INSTALLATION_PHOTO_SLOTS}."
            )
        return value


class ProposalInputsReview(BaseModel):
    status: InputStatus
    internal_notes: Optional[str] = None
    rejection_reason: Optional[str] = None


def _serialize(inputs) -> dict:
    return {
        "id": inputs.id,
        "lead_id": inputs.lead_id,
        "status": inputs.status,
        "submitted_at": inputs.submitted_at.isoformat() if inputs.submitted_at else None,
        "reviewed_at": inputs.reviewed_at.isoformat() if inputs.reviewed_at else None,
        "reviewed_by_id": inputs.reviewed_by_id,
        "rejection_reason": inputs.rejection_reason,
        "customer_type": inputs.customer_type,
        "organisation_name": inputs.organisation_name,
        "gst_number": inputs.gst_number,
        "project_family": inputs.project_family,
        "scheme_linked": inputs.scheme_linked,
        "scheme_name": inputs.scheme_name,
        "state": inputs.state,
        "district": inputs.district,
        "pin_code": inputs.pin_code,
        "full_address": inputs.full_address,
        "discom": inputs.discom,
        "consumer_number": inputs.consumer_number,
        "site_type": inputs.site_type,
        "roof_area_sqft": inputs.roof_area_sqft,
        "floors": inputs.floors,
        "shading_condition": inputs.shading_condition,
        "structural_notes": inputs.structural_notes,
        "roof_ownership_confirmed": inputs.roof_ownership_confirmed,
        "roofs_available_count": inputs.roofs_available_count,
        "roofs_considered_count": inputs.roofs_considered_count,
        "future_roof_expansion_planned": inputs.future_roof_expansion_planned,
        "roof_age_years": inputs.roof_age_years,
        "avg_monthly_bill_inr": inputs.avg_monthly_bill_inr,
        "avg_monthly_units_kwh": inputs.avg_monthly_units_kwh,
        "consumption_history": inputs.consumption_history,
        "peak_load_kw": inputs.peak_load_kw,
        "sanctioned_load_kw": inputs.sanctioned_load_kw,
        "connection_phase": inputs.connection_phase,
        "desired_output_kw": inputs.desired_output_kw,
        "load_notes": inputs.load_notes,
        "discom_consumer_category": inputs.discom_consumer_category,
        "consumer_priority_class": inputs.consumer_priority_class,
        "connection_type": inputs.connection_type,
        "net_metering_required": inputs.net_metering_required,
        "dcr_panels_required": inputs.dcr_panels_required,
        "ev_charging": inputs.ev_charging,
        "battery_storage": inputs.battery_storage,
        "equipment_tier": inputs.equipment_tier,
        "additional_requirements": inputs.additional_requirements,
        "document_refs": inputs.document_refs,
        "installation_photo_refs": inputs.installation_photo_refs,
        "staff_signature_ref": inputs.staff_signature_ref,
        "customer_signature_ref": inputs.customer_signature_ref,
        "internal_notes": inputs.internal_notes,
    }


@router.post("/")
def submit_proposal_inputs(
    body: ProposalInputsCreate,
    db: Session = Depends(get_db),
    employee: AuthenticatedEmployee = Depends(get_current_employee),
):
    if not employee.can_manage_proposal_inputs():
        raise HTTPException(status_code=403, detail="Your role cannot submit proposal inputs.")

    memory = MemoryManager(db)
    lead = memory.get_lead(body.lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")

    fields = body.model_dump(exclude={"lead_id", "document_refs"})
    fields["document_refs"] = (
        [d.model_dump() for d in body.document_refs] if body.document_refs is not None else None
    )

    # DCR (Domestic Content Requirement) panels are mandatory under government
    # schemes (e.g. PM Surya Ghar, PM-KUSUM) regardless of what was submitted.
    if body.scheme_linked and body.scheme_name is not None and body.scheme_name != GovernmentScheme.none:
        fields["dcr_panels_required"] = True

    inputs = memory.create_proposal_inputs(
        lead_id=body.lead_id,
        performed_by=employee.employee_id,
        **fields,
    )
    filled = fill_lead_from_inputs(memory, inputs, performed_by=employee.employee_id)
    db.commit()

    return {
        "id": inputs.id,
        "status": inputs.status,
        "submitted_at": inputs.submitted_at.isoformat() if inputs.submitted_at else None,
        "lead_fields_filled": filled,
    }


@router.get("/{inputs_id}")
def get_proposal_inputs(
    inputs_id: int,
    db: Session = Depends(get_db),
    employee: AuthenticatedEmployee = Depends(get_current_employee),
):
    memory = MemoryManager(db)
    inputs = memory.get_proposal_inputs(inputs_id)
    if not inputs:
        raise HTTPException(status_code=404, detail="Proposal inputs not found")
    return _serialize(inputs)


@router.get("/")
def list_proposal_inputs(
    status: Optional[str] = Query(default=None),
    lead_id: Optional[int] = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    employee: AuthenticatedEmployee = Depends(get_current_employee),
):
    if status is not None:
        try:
            status_enum = InputStatus(status)
        except ValueError:
            raise HTTPException(status_code=422, detail=f"Unknown status: {status!r}")
    else:
        status_enum = InputStatus.submitted

    memory = MemoryManager(db)
    items, total = memory.list_proposal_inputs(
        status=status_enum,
        lead_id=lead_id,
        page=page,
        page_size=page_size,
    )
    return {
        "items": [_serialize(i) for i in items],
        "page": page,
        "page_size": page_size,
        "total": total,
    }


@router.patch("/{inputs_id}/review")
def review_proposal_inputs(
    inputs_id: int,
    body: ProposalInputsReview,
    db: Session = Depends(get_db),
    employee: AuthenticatedEmployee = Depends(get_current_employee),
):
    if not employee.can_manage_proposal_inputs():
        raise HTTPException(status_code=403, detail="Your role cannot review proposal inputs.")

    if body.status == InputStatus.submitted:
        raise HTTPException(status_code=422, detail="Cannot set status back to 'submitted' via review.")

    memory = MemoryManager(db)
    inputs = memory.get_proposal_inputs(inputs_id)
    if not inputs:
        raise HTTPException(status_code=404, detail="Proposal inputs not found")

    updated = memory.review_proposal_inputs(
        inputs_id=inputs_id,
        new_status=body.status,
        performed_by=employee.employee_id,
        internal_notes=body.internal_notes,
        rejection_reason=body.rejection_reason,
    )
    # An approved site assessment fills the lead's still-blank required fields
    # before the auto-draft below checks readiness.
    filled = {}
    if body.status == InputStatus.approved_for_proposal:
        filled = fill_lead_from_inputs(memory, updated, performed_by=employee.employee_id)
    db.commit()

    result = _serialize(updated)
    result["lead_fields_filled"] = filled

    if body.status == InputStatus.approved_for_proposal:
        result["proposal_draft"] = _trigger_auto_draft(
            memory=memory,
            db=db,
            lead_id=updated.lead_id,
            inputs_id=inputs_id,
            performed_by=employee.employee_id,
        )

    return result


def _trigger_auto_draft(
    memory: MemoryManager, db: Session, lead_id: int, inputs_id: int, performed_by: str,
) -> dict:
    """Call the Proposal Agent's real drafting entry point — the same
    orchestrator.request_proposal_draft() the dashboard's manual "Draft
    proposal" button uses — now that a reviewer has approved this lead's site
    assessment for one.

    This only succeeds when the Lead itself satisfies the same readiness gate
    every other draft trigger does (app.rules.business_rules.REQUIRED_FIELDS on
    Lead.collected_fields). The review has already filled whatever blanks the
    site assessment can honestly supply (app/memory/lead_fields.py); anything
    still missing is reported back as missing_fields. Approving
    a site assessment for a lead nobody has qualified yet is a valid action in
    its own right, so a failure here never blocks or reverts the review that
    already committed above — it is reported back for the reviewer to see, not
    raised as an error.
    """
    lead = memory.get_lead(lead_id)
    if not lead:
        return {"triggered": False, "reason": "Lead not found"}

    outcome = orchestrator.request_proposal_draft(
        lead_id=lead_id, customer_id=lead.customer_id, performed_by=performed_by,
    )
    proposal_id = outcome.get("proposal_id")
    if proposal_id is None:
        return {
            "triggered": False,
            "reason": outcome.get("error") or "Lead is not yet ready for a proposal draft.",
            "missing_fields": outcome.get("missing_fields", []),
        }

    version = outcome.get("proposal_version")
    memory.add_timeline_event(
        lead_id=lead_id,
        event_type=TimelineEventType.proposal_auto_drafted_from_inputs,
        description=(
            f"Proposal v{version} auto-drafted from approved "
            f"site assessment (proposal_inputs #{inputs_id})"
        ),
        created_by=performed_by,
    )
    db.commit()
    return {"triggered": True, "proposal_id": proposal_id, "version": version}


# ── File upload ──────────────────────────────────────────────────────────────
# Real bytes in, stored via the same R2 client app/pdf/generator.py uses for
# proposal PDFs (see app/storage/r2.py) — this is the backend half that was
# missing: DocumentRef/InstallationPhotoRef/SignatureRef previously had no
# real endpoint filling them in, so a file picked in the UI was validated,
# previewed, and then silently discarded.

UPLOAD_TARGETS = ("document", "installation_photo", "staff_signature", "customer_signature")

# The real limits already enforced by the frontend's FileUploadSlot component
# (see app/proposal/new/steps/Step5Documents.tsx): PDF/JPG/JPEG/PNG/WEBP across
# every slot, sizes varying 5-10MB per slot. This endpoint doesn't know which
# UI slot a given upload came from, so it uses the more permissive 10MB most
# slots already use as one uniform server-side ceiling — a judgment call, not
# a third, independently-invented number.
MAX_UPLOAD_SIZE_BYTES = 10 * 1024 * 1024
ALLOWED_CONTENT_TYPES = {"application/pdf", "image/jpeg", "image/png", "image/webp"}


@router.post("/{inputs_id}/upload")
async def upload_proposal_input_file(
    inputs_id: int,
    db: Session = Depends(get_db),
    employee: AuthenticatedEmployee = Depends(get_current_employee),
    file: UploadFile = File(...),
    target: str = Form(...),
    document_type: Optional[str] = Form(default=None),
    photo_slot: Optional[str] = Form(default=None),
):
    if not employee.can_manage_proposal_inputs():
        raise HTTPException(status_code=403, detail="Your role cannot upload site-assessment files.")

    memory = MemoryManager(db)
    inputs = memory.get_proposal_inputs(inputs_id)
    if not inputs:
        raise HTTPException(status_code=404, detail="Proposal inputs not found")

    if target not in UPLOAD_TARGETS:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown upload target: {target!r}. Expected one of {UPLOAD_TARGETS}.",
        )
    if target == "document" and not document_type:
        raise HTTPException(status_code=422, detail="document_type is required when target='document'.")
    if target == "installation_photo":
        if not photo_slot:
            raise HTTPException(status_code=422, detail="photo_slot is required when target='installation_photo'.")
        if photo_slot not in INSTALLATION_PHOTO_SLOTS:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown installation photo slot: {photo_slot!r}. Expected one of {INSTALLATION_PHOTO_SLOTS}.",
            )

    if file.content_type not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Unsupported file type: {file.content_type!r}. "
                f"Expected one of {sorted(ALLOWED_CONTENT_TYPES)}."
            ),
        )

    content = await file.read()
    if len(content) > MAX_UPLOAD_SIZE_BYTES:
        raise HTTPException(
            status_code=422,
            detail=f"File exceeds the {MAX_UPLOAD_SIZE_BYTES // (1024 * 1024)}MB limit.",
        )
    if not content:
        raise HTTPException(status_code=422, detail="Uploaded file is empty.")

    from app.storage import r2

    key = f"proposal-inputs/{inputs_id}/{target}/{uuid.uuid4().hex}_{file.filename}"
    try:
        r2.upload_bytes(key, content, file.content_type)
    except r2.StorageNotConfiguredError as exc:
        # Never a fake success — this is the whole point of this endpoint.
        raise HTTPException(
            status_code=503,
            detail={"code": "storage_not_configured", "message": str(exc)},
        )

    updated = memory.attach_proposal_input_upload(
        inputs_id=inputs_id,
        target=target,
        filename=file.filename,
        storage_key=key,
        performed_by=employee.employee_id,
        document_type=document_type,
        photo_slot=photo_slot,
    )
    db.commit()
    return _serialize(updated)
