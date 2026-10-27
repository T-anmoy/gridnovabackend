"""Staff side of associate-led onboarding: seeing associate-sourced customers, the
verification call, and Admin decisions on attribution conflicts. Lead visibility
follows app/auth/scoping.py (Sales: own + unassigned) exactly as for any other lead."""
from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.routes.dashboard import _require_admin_or_internal_key
from app.api.routes.associate_onboarding import InPersonAcceptanceRequest
from app.associates.service import AssociateError
from app.auth.dependencies import AuthenticatedEmployee, get_current_employee
from app.auth.scoping import onboarding_lead_scope, require_onboarding_access
from app.memory.db import get_db
from app.memory.memory_manager import MemoryManager
from app.memory.models import (
    AttributionFlagStatus, AttributionResolution, CustomerOnboarding, Lead, OnboardingAttributionFlag,
    OnboardingStatus,
)
from app.onboarding import serialize, service

router = APIRouter(prefix="/api/v1/dashboard")


def _error(exc: AssociateError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.body())


def _load(db: Session, employee: AuthenticatedEmployee, onboarding_id: int) -> CustomerOnboarding:
    onboarding = service.get(db, onboarding_id)
    if onboarding is None:
        raise HTTPException(status_code=404, detail="Onboarding not found")
    # Network Manager: every associate-sourced customer (onboardings.view_all).
    require_onboarding_access(employee, MemoryManager(db).get_lead(onboarding.lead_id))
    return onboarding


@router.get("/onboardings")
def list_onboardings(status: Optional[OnboardingStatus] = None, flagged: Optional[bool] = None,
                     needs_attention: Optional[bool] = None,
                     page: int = Query(default=1, ge=1), page_size: int = Query(default=25, ge=1, le=100),
                     db: Session = Depends(get_db),
                     employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """`needs_attention=true`: every customer whose formal proposal is blocked
    (`formal_proposal_blocked` set — requested, but the record lacks what a draft
    needs); `false`: everyone else."""
    if not employee.can_view_onboardings():
        raise HTTPException(status_code=403, detail="Your role cannot view associate-sourced customers.")
    q = (select(CustomerOnboarding).join(Lead, Lead.id == CustomerOnboarding.lead_id)
         .where(onboarding_lead_scope(employee).condition()))
    if status:
        q = q.where(CustomerOnboarding.status == status)
    if flagged is not None:
        open_ids = select(OnboardingAttributionFlag.onboarding_id).where(
            OnboardingAttributionFlag.status == AttributionFlagStatus.open).union(
            select(OnboardingAttributionFlag.existing_onboarding_id).where(
                OnboardingAttributionFlag.status == AttributionFlagStatus.open))
        q = q.where(CustomerOnboarding.id.in_(open_ids) if flagged else CustomerOnboarding.id.not_in(open_ids))
    ordered = q.order_by(CustomerOnboarding.created_at.desc(), CustomerOnboarding.id.desc())
    if needs_attention is not None:
        # Blocked-ness depends on the lead's current fields, so it is decided per row.
        from app.onboarding import formal
        rows = [o for o in db.execute(ordered).scalars() if bool(formal.blocked(db, o)) == needs_attention]
        start = (page - 1) * page_size
        return {"items": serialize.list_items(db, rows[start:start + page_size], lambda o: {"staff": True}),
                "page": page, "page_size": page_size, "total": len(rows)}
    total = db.execute(select(func.count()).select_from(q.subquery())).scalar_one()
    rows = list(db.execute(ordered.offset((page - 1) * page_size).limit(page_size)).scalars())
    return {"items": serialize.list_items(db, rows, lambda o: {"staff": True}),
            "page": page, "page_size": page_size, "total": total}


@router.get("/onboardings/{onboarding_id}")
def get_onboarding(onboarding_id: int, db: Session = Depends(get_db),
                   employee: AuthenticatedEmployee = Depends(get_current_employee)):
    if not employee.can_view_onboardings():
        raise HTTPException(status_code=403, detail="Your role cannot view associate-sourced customers.")
    return serialize.onboarding(db, _load(db, employee, onboarding_id), staff=True)


class VerificationCallRequest(BaseModel):
    verification_call_completed: bool
    note: str = Field(max_length=2000)


@router.post("/onboardings/{onboarding_id}/verification-call")
def verification_call(onboarding_id: int, body: VerificationCallRequest, db: Session = Depends(get_db),
                      employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """The staff verification call. Required before any future commission
    eligibility; it also starts the attribution lock."""
    if not employee.can_verify_onboarding():
        raise HTTPException(status_code=403, detail="Your role cannot record verification calls.")
    if not body.verification_call_completed:
        raise HTTPException(status_code=422, detail={"code": "verification_call_required",
                                                     "field": "verification_call_completed",
                                                     "message": "Complete the verification call first."})
    onboarding = _load(db, employee, onboarding_id)
    try:
        service.record_verification_call(db, onboarding, employee.employee_id, body.note)
    except AssociateError as exc:
        db.rollback()
        raise _error(exc)
    db.commit()
    return serialize.onboarding(db, onboarding, staff=True)


class StaffSiteLocationRequest(BaseModel):
    source: Literal["google_maps_link", "pasted_coordinates"]
    link: Optional[str] = Field(default=None, min_length=1, max_length=500)
    text: Optional[str] = Field(default=None, min_length=1, max_length=100)
    reason: str = Field(max_length=1000)

    @model_validator(mode="after")
    def _input(self):
        if self.source == "google_maps_link" and not self.link:
            raise ValueError("link is required for google_maps_link")
        if self.source == "pasted_coordinates" and not self.text:
            raise ValueError("text is required for pasted_coordinates")
        return self


@router.post("/onboardings/{onboarding_id}/site-location")
def correct_site_location(onboarding_id: int, body: StaffSiteLocationRequest, db: Session = Depends(get_db),
                          employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """Correct the customer's site location during the verification call (Admin, Sales).
    Stored as `staff_entered`, unverified, confirmed by this staff member; a reason is
    required and the change is audited with before/after. The status does not change."""
    from app.api.routes.associate_onboarding import link_resolver
    from app.onboarding import site_location

    if not employee.can_verify_onboarding():
        raise HTTPException(status_code=403, detail="Your role cannot correct a customer's site location.")
    onboarding = _load(db, employee, onboarding_id)
    try:
        location_warnings = site_location.staff_correct(db, onboarding, employee.employee_id, body.model_dump(),
                                                        resolve_link=link_resolver(f"staff:{employee.employee_id}"))
    except AssociateError as exc:
        db.rollback()
        raise _error(exc)
    db.commit()
    data = serialize.onboarding(db, onboarding, staff=True)
    return {**data, "warnings": data["warnings"] + location_warnings}


@router.get("/attribution-flags")
def list_flags(status: Optional[AttributionFlagStatus] = AttributionFlagStatus.open,
               db: Session = Depends(get_db), employee: AuthenticatedEmployee = Depends(get_current_employee)):
    if not employee.can_resolve_attribution():
        raise HTTPException(status_code=403, detail="Only Admin can review attribution conflicts.")
    q = select(OnboardingAttributionFlag)
    if status:
        q = q.where(OnboardingAttributionFlag.status == status)
    items = []
    for flag in db.execute(q.order_by(OnboardingAttributionFlag.id)).scalars():
        items.append({**serialize.flag(flag),
                      "onboarding": serialize.onboarding(db, service.get(db, flag.onboarding_id), staff=True),
                      "existing_onboarding": serialize.onboarding(db, service.get(db, flag.existing_onboarding_id),
                                                                  staff=True)})
    return {"items": items}


class ResolveFlagRequest(BaseModel):
    resolution: AttributionResolution
    note: str = Field(max_length=2000)


@router.post("/attribution-flags/{flag_id}/resolve")
def resolve_flag(flag_id: int, body: ResolveFlagRequest, db: Session = Depends(get_db),
                 employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """keep_existing: the earlier onboarding keeps the customer and the later one is
    closed. award_new: the later one takes the customer and the earlier one is closed
    (its attribution lock ends). Closed records are kept, never deleted."""
    if not employee.can_resolve_attribution():
        raise HTTPException(status_code=403, detail="Only Admin can resolve attribution conflicts.")
    flag = db.get(OnboardingAttributionFlag, flag_id)
    if flag is None:
        raise HTTPException(status_code=404, detail="Flag not found")
    try:
        service.resolve_flag(db, flag, body.resolution, body.note, employee.employee_id)
    except AssociateError as exc:
        db.rollback()
        raise _error(exc)
    db.commit()
    return serialize.flag(flag)


@router.post("/internal/check-onboarding-escalations")
def run_onboarding_escalations(db: Session = Depends(get_db), _: None = Depends(_require_admin_or_internal_key)):
    """Admin or INTERNAL_API_KEY (Cloud Scheduler). Re-scores submitted associate-sourced
    customers and escalates overdue verification calls one step up the chain. Idempotent;
    an overlapping run is skipped."""
    from app.jobs.locks import run_exclusive
    from app.jobs.onboarding_escalations import check_onboarding_escalations
    return run_exclusive(db, "check-onboarding-escalations", lambda: check_onboarding_escalations(db))


@router.get("/onboardings/{onboarding_id}/progress")
def progress_timeline(onboarding_id: int, db: Session = Depends(get_db),
                      employee: AuthenticatedEmployee = Depends(get_current_employee)):
    from app.onboarding import progress

    if not employee.can_view_onboardings():
        raise HTTPException(status_code=403, detail="Your role cannot view associate-sourced customers.")
    return progress.timeline(db, _load(db, employee, onboarding_id))


class RecordStageRequest(BaseModel):
    note: str = Field(max_length=2000)
    reference: Optional[str] = Field(default=None, max_length=200)


@router.post("/onboardings/{onboarding_id}/progress/{stage}")
def record_stage(onboarding_id: int, stage: str, body: RecordStageRequest, db: Session = Depends(get_db),
                 employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """Hand-record a stage that has no action of its own: `accepted` (the customer
    accepted offline) or `portal_agreement_approved` (the customer approved the
    Vendor-Consumer Agreement on the PM Surya Ghar portal). A note is required; an
    optional reference (e.g. a portal application number) is kept as evidence."""
    from app.memory.models import ProjectStage
    from app.onboarding import progress

    if not employee.can_record_project_stage():
        raise HTTPException(status_code=403, detail="Your role cannot record progress stages.")
    try:
        target = ProjectStage(stage)
    except ValueError:
        raise HTTPException(status_code=404, detail="Unknown stage")
    if target not in progress.MANUAL_STAGES:
        raise HTTPException(status_code=409, detail={
            "code": "stage_recorded_by_its_action",
            "message": f"'{stage}' is recorded automatically by the action that proves it."})
    note = body.note.strip()
    if not note:
        raise HTTPException(status_code=422, detail={"code": "note_required", "field": "note",
                                                     "message": "Say what confirms this stage."})
    onboarding = _load(db, employee, onboarding_id)
    evidence = {"recorded_manually": True}
    if body.reference:
        evidence["reference"] = body.reference.strip()
    try:
        progress.record(db, onboarding, target, employee.employee_id, evidence=evidence, note=note)
    except AssociateError as exc:
        db.rollback()
        raise _error(exc)
    db.commit()
    return progress.timeline(db, onboarding)


@router.post("/onboardings/{onboarding_id}/agreement")
def generate_agreement(onboarding_id: int, db: Session = Depends(get_db),
                       employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """Generate (or, until it is signed, regenerate) the customer agreement PDF for
    printing on ₹10 stamp paper. Needs the customer's acceptance on record."""
    from app.associates.service import today_ist
    from app.onboarding import agreement
    from app.storage import r2

    if not employee.can_record_project_stage():
        raise HTTPException(status_code=403, detail="Your role cannot prepare customer agreements.")
    onboarding = _load(db, employee, onboarding_id)
    try:
        record = agreement.generate(db, onboarding, employee.employee_id, today_ist())
    except AssociateError as exc:
        db.rollback()
        raise _error(exc)
    except r2.StorageNotConfiguredError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail={"code": "storage_not_configured", "message": str(exc)})
    db.commit()
    return {**agreement.serialize(record), "links": agreement.links(record)}


@router.get("/onboardings/{onboarding_id}/agreement")
def get_agreement(onboarding_id: int, db: Session = Depends(get_db),
                  employee: AuthenticatedEmployee = Depends(get_current_employee)):
    from app.onboarding import agreement

    if not employee.can_view_onboardings():
        raise HTTPException(status_code=403, detail="Your role cannot view associate-sourced customers.")
    onboarding = _load(db, employee, onboarding_id)
    record = agreement.get(db, onboarding)
    links = agreement.links(record)
    if links:
        from app.onboarding.service import audit_document_link
        audit_document_link(db, onboarding, employee.employee_id, "agreement", ",".join(sorted(links)))
        db.commit()
    return {**agreement.serialize(record), "links": links}


@router.post("/onboardings/{onboarding_id}/agreement/signed")
def upload_signed_agreement(onboarding_id: int, file: UploadFile = File(...),
                            stamp_paper_number: str = Form(..., max_length=50),
                            stamp_paper_date: str = Form(...),
                            db: Session = Depends(get_db),
                            employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """Upload the signed agreement scan with the stamp paper's number and date."""
    from app.api.routes.associate_onboarding import _signed_upload

    if not employee.can_record_project_stage():
        raise HTTPException(status_code=403, detail="Your role cannot record signed agreements.")
    onboarding = _load(db, employee, onboarding_id)
    return _signed_upload(db, onboarding, employee.employee_id, file, stamp_paper_number, stamp_paper_date)



@router.get("/onboardings/{onboarding_id}/estimates")
def list_estimates(onboarding_id: int, db: Session = Depends(get_db),
                   employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """The indicative estimates the associate showed this customer (read-only)."""
    from app.onboarding import estimate

    if not employee.can_view_onboardings():
        raise HTTPException(status_code=403, detail="Your role cannot view associate-sourced customers.")
    onboarding = _load(db, employee, onboarding_id)
    return {"items": [estimate.serialize(e) for e in estimate.list_for(db, onboarding)]}


@router.get("/onboardings/{onboarding_id}/formal-proposal")
def get_formal_proposal(onboarding_id: int, db: Session = Depends(get_db),
                        employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """The latest formal proposal for this customer, if Grid Nova has prepared one —
    same shape as the associate-facing endpoint."""
    from app.onboarding import formal

    if not employee.can_view_onboardings():
        raise HTTPException(status_code=403, detail="Your role cannot view associate-sourced customers.")
    onboarding = _load(db, employee, onboarding_id)
    return formal.serialize_latest(db, onboarding)


class FormalProposalRequest(BaseModel):
    customer_email: Optional[str] = None

    @field_validator("customer_email")
    @classmethod
    def _email(cls, v):
        from app.associates.validation import normalize_email
        return normalize_email(v) if v else None


@router.post("/onboardings/{onboarding_id}/formal-proposal")
def prepare_formal_proposal(onboarding_id: int, body: Optional[FormalProposalRequest] = None,
                            db: Session = Depends(get_db),
                            employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """Prepare (draft) the FORMAL proposal — the deliberate, separate step after the
    site assessment. Drafts through the usual Proposal Agent; approval tiers, manager
    sign-off and the send-guard then apply exactly as for any proposal.

    Fix-in-place: `customer_email` fills in a missing email first, in the same call —
    for a request blocked on "no email on file"."""
    from app.onboarding import formal, progress

    if not employee.can_trigger_proposal_draft():
        raise HTTPException(status_code=403, detail="Your role cannot prepare proposals.")
    onboarding = _load(db, employee, onboarding_id)
    try:
        prepared = formal.prepare_formal_proposal(db, onboarding, employee.employee_id,
                                                  customer_email=body.customer_email if body else None)
    except AssociateError as exc:
        db.rollback()
        raise _error(exc)
    onboarding = service.get(db, onboarding_id)
    return {"formal_proposal": prepared, "progress": progress.timeline(db, onboarding)}


@router.post("/onboardings/{onboarding_id}/proposal/in-person-acceptance")
def in_person_acceptance(onboarding_id: int, body: InPersonAcceptanceRequest, db: Session = Depends(get_db),
                         employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """Record that the approved formal proposal was handed over and accepted in person."""
    from app.api.routes.associate_onboarding import _record_in_person

    if not employee.can_send_proposal():
        raise HTTPException(status_code=403, detail="Your role cannot record proposal delivery.")
    return _record_in_person(db, _load(db, employee, onboarding_id), employee.employee_id, body)
