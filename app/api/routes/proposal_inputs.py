"""
Site assessment intake — receives raw customer-provided data from the
/proposal/new frontend form and lets staff review it before a Proposal is
drafted. Kept separate from app/api/routes/dashboard.py's Proposal endpoints:
ProposalInputs is the customer's raw input, Proposal is the AI-generated output.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.auth.dependencies import AuthenticatedEmployee, get_current_employee
from app.memory.db import get_db
from app.memory.memory_manager import MemoryManager
from app.memory.models import (
    ConnectionPhase,
    ConnectionType,
    CustomerType,
    EquipmentTier,
    GovernmentScheme,
    InputStatus,
    ProjectFamily,
    ShadingCondition,
    SiteType,
)
from sqlalchemy.orm import Session

router = APIRouter(prefix="/api/v1/proposal-inputs")


class DocumentRef(BaseModel):
    type: str
    filename: str
    # Cloudflare R2 object key — null until upload is wired (see TODO on the model).
    storage_key: Optional[str] = None


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

    # Load & Output
    avg_monthly_bill_inr: Optional[float] = None
    avg_monthly_units_kwh: Optional[float] = None
    peak_load_kw: Optional[float] = None
    connection_phase: Optional[ConnectionPhase] = None
    desired_output_kw: Optional[float] = None
    load_notes: Optional[str] = None

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
        "avg_monthly_bill_inr": inputs.avg_monthly_bill_inr,
        "avg_monthly_units_kwh": inputs.avg_monthly_units_kwh,
        "peak_load_kw": inputs.peak_load_kw,
        "connection_phase": inputs.connection_phase,
        "desired_output_kw": inputs.desired_output_kw,
        "load_notes": inputs.load_notes,
        "connection_type": inputs.connection_type,
        "net_metering_required": inputs.net_metering_required,
        "dcr_panels_required": inputs.dcr_panels_required,
        "ev_charging": inputs.ev_charging,
        "battery_storage": inputs.battery_storage,
        "equipment_tier": inputs.equipment_tier,
        "additional_requirements": inputs.additional_requirements,
        "document_refs": inputs.document_refs,
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
    db.commit()

    return {
        "id": inputs.id,
        "status": inputs.status,
        "submitted_at": inputs.submitted_at.isoformat() if inputs.submitted_at else None,
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
    db.commit()

    return _serialize(updated)
