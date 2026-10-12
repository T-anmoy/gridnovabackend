"""Execution for staff — the Operations queue, per-customer execution, verifying or
rejecting an associate's stage submission, and recording a stage directly on a project
Grid Nova executes itself.

Who sees what: app/auth/scoping.py require_execution_access (Operations: every project
at or after agreement_signed; Sales: own or unassigned; Accounts: none). Who acts:
AuthenticatedEmployee.can_verify_execution / can_record_execution (Operations, Admin).
No response here carries a price, a payment or a commission.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.routes.dashboard import _require_admin_or_internal_key
from app.associates import uploads
from app.associates.service import AssociateError
from app.auth.dependencies import AuthenticatedEmployee, get_current_employee
from app.auth.scoping import lead_scope, require_execution_access
from app.execution import files as execution_files
from app.execution import service as execution
from app.memory.db import get_db
from app.memory.memory_manager import MemoryManager
from app.memory.models import (
    Associate, Customer, CustomerOnboarding, EmployeeRole, ExecutionMode, Lead, OnboardingStatus, Project,
    ProjectExecution, ProjectStage, ProjectStageEvent, StageSubmission, StageSubmissionStatus,
)

router = APIRouter(prefix="/api/v1/dashboard")


def _error(exc: AssociateError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.body())


def _load(db: Session, employee: AuthenticatedEmployee, onboarding_id: int) -> CustomerOnboarding:
    onboarding = db.get(CustomerOnboarding, onboarding_id)
    if onboarding is None:
        raise HTTPException(status_code=404, detail="Customer not found")
    require_execution_access(employee, MemoryManager(db).get_lead(onboarding.lead_id),
                             execution.get(db, onboarding) is not None)
    return onboarding


def customer_block(db: Session, onboarding: CustomerOnboarding, employee: AuthenticatedEmployee) -> dict:
    """Who and where. Engineers get the site, not the customer's contact details."""
    from app.memory.models import ProposalInputs
    from app.onboarding.serialize import _territory

    customer = db.get(Customer, onboarding.customer_id)
    inputs = db.get(ProposalInputs, onboarding.proposal_inputs_id)
    owner = db.get(Associate, onboarding.owner_associate_id)
    project = db.get(Project, onboarding.project_id)
    block = {"name": customer.name if customer else None,
             "project_reference_id": project.reference_id if project else None,
             "site": {"address": inputs.full_address if inputs else None,
                      "pin_code": inputs.pin_code if inputs else None,
                      "territory": _territory(db, onboarding), "latitude": onboarding.site_latitude,
                      "longitude": onboarding.site_longitude},
             "associate": {"associate_id": owner.associate_id, "full_name": owner.full_name,
                           "associate_type": owner.associate_type.value, "mobile": owner.mobile}}
    if employee.role != EmployeeRole.engineer:
        block["mobile"] = onboarding.customer_mobile
    return block


def _detail(db: Session, onboarding: CustomerOnboarding, employee: AuthenticatedEmployee) -> dict:
    try:
        body = execution.status(db, onboarding, staff=True)
    except AssociateError as exc:
        raise _error(exc)
    return {**body, "customer": customer_block(db, onboarding, employee),
            "can_verify": employee.can_verify_execution(),
            "can_record": employee.can_record_execution() and body["execution_mode"] == ExecutionMode.grid_nova.value}


# ── Queue ────────────────────────────────────────────────────────────────────────

def queue_rows(db: Session, employee: AuthenticatedEmployee) -> list[dict]:
    """Every execution the employee may see, with its computed state. Loaded in bulk:
    stage events and pending submissions are two queries, not one per project."""
    from app.onboarding import progress

    q = (select(ProjectExecution, CustomerOnboarding, Lead)
         .join(CustomerOnboarding, CustomerOnboarding.id == ProjectExecution.onboarding_id)
         .join(Lead, Lead.id == CustomerOnboarding.lead_id))
    if employee.role == EmployeeRole.sales:
        q = q.where(lead_scope(employee).condition())
    rows = db.execute(q).all()
    project_ids = [r[0].project_id for r in rows]
    events: dict[int, dict] = {pid: {} for pid in project_ids}
    pending: dict[int, list] = {pid: [] for pid in project_ids}
    if project_ids:
        for e in db.execute(select(ProjectStageEvent).where(ProjectStageEvent.project_id.in_(project_ids))).scalars():
            events[e.project_id][e.stage] = e
        for s in db.execute(select(StageSubmission).where(StageSubmission.project_id.in_(project_ids),
                                                          StageSubmission.status == StageSubmissionStatus.pending)
                            ).scalars():
            pending[s.project_id].append(s)
    out = []
    for record, onboarding, lead in rows:
        stages = progress.applicable(db, onboarding)
        done = events[record.project_id]
        age = execution.aging(stages, done)
        subs = pending[record.project_id]
        flags = execution.project_flags(db, onboarding, record, staff=True)
        reasons = (["pending_verification"] if subs else []) + (["stuck"] if age["is_stuck"] else []) + flags
        customer = db.get(Customer, onboarding.customer_id)
        nxt = next((s for s in stages if s not in done), None)
        out.append({
            "onboarding_id": onboarding.id, "project_id": record.project_id,
            "project_reference_id": db.get(Project, record.project_id).reference_id,
            "customer_name": customer.name if customer else None,
            "execution_mode": record.execution_mode.value,
            "current_stage": next((s.value for s in reversed(stages) if s in done), None),
            "next_stage": nxt.value if nxt else None,
            "next_stage_label": progress.LABELS[nxt] if nxt else None,
            "days_in_stage": age["days_in_stage"], "stuck_threshold_days": age["stuck_threshold_days"],
            "is_stuck": age["is_stuck"],
            "pending_submission": ({"submission_id": subs[0].id, "stage": subs[0].stage.value,
                                    "submitted_at": subs[0].submitted_at.isoformat()} if subs else None),
            "flags": flags,
            "needs_attention": bool(reasons), "attention_reasons": reasons,
            "started_at": record.started_at.isoformat(),
        })
    return out


@router.get("/execution")
def execution_queue(needs_attention: Optional[bool] = None, stuck: Optional[bool] = None,
                    stage: Optional[str] = None, mode: Optional[ExecutionMode] = None,
                    page: int = Query(default=1, ge=1), page_size: int = Query(default=25, ge=1, le=100),
                    db: Session = Depends(get_db), employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """Projects in execution. `needs_attention=true`: a submission awaiting
    verification, stuck past its stage's threshold, or a project flag (e.g.
    dcr_mismatch). `stage` matches the current (last recorded) stage."""
    if not employee.can_view_execution():
        raise HTTPException(status_code=403, detail="Your role cannot view project execution.")
    if stage is not None and stage not in {s.value for s in ProjectStage}:
        raise HTTPException(status_code=422, detail={"code": "unknown_stage", "field": "stage",
                                                     "message": "Unknown stage."})
    items = queue_rows(db, employee)
    if needs_attention is not None:
        items = [i for i in items if i["needs_attention"] == needs_attention]
    if stuck is not None:
        items = [i for i in items if i["is_stuck"] == stuck]
    if stage is not None:
        items = [i for i in items if i["current_stage"] == stage]
    if mode is not None:
        items = [i for i in items if i["execution_mode"] == mode.value]
    # Oldest waiting first: pending verifications, then the longest in stage.
    items.sort(key=lambda i: (not i["needs_attention"], -(i["days_in_stage"] or 0), i["onboarding_id"]))
    total = len(items)
    return {"items": items[(page - 1) * page_size: page * page_size], "page": page, "page_size": page_size,
            "total": total}


# ── One customer ─────────────────────────────────────────────────────────────────

@router.get("/onboardings/{onboarding_id}/execution")
def execution_detail(onboarding_id: int, db: Session = Depends(get_db),
                     employee: AuthenticatedEmployee = Depends(get_current_employee)):
    return _detail(db, _load(db, employee, onboarding_id), employee)


@router.get("/onboardings/{onboarding_id}/execution/files/{file_ref}")
def execution_file_link(onboarding_id: int, file_ref: str, db: Session = Depends(get_db),
                        employee: AuthenticatedEmployee = Depends(get_current_employee)):
    from app.storage import r2

    onboarding = _load(db, employee, onboarding_id)
    try:
        record = execution.require(db, onboarding)
    except AssociateError as exc:
        raise _error(exc)
    item = execution.find_file(record, file_ref) or execution.file_from_submissions(db, record, file_ref)
    if item is None:
        raise HTTPException(status_code=404, detail="File not found")
    try:
        url, expires_at = r2.signed_get_url(item["storage_key"], filename=item["filename"])
    except r2.StorageNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail={"code": "storage_not_configured", "message": str(exc)})
    from app.onboarding.service import audit_document_link
    audit_document_link(db, onboarding, employee.employee_id, "execution_file", file_ref, item["filename"])
    db.commit()
    return {"url": url, "expires_at": expires_at.isoformat(), "filename": item["filename"], "file_ref": file_ref}


@router.post("/onboardings/{onboarding_id}/execution/files", status_code=201)
def upload_execution_file(onboarding_id: int, file: UploadFile = File(...),
                          kind: Literal["document", "photo"] = Form(...),
                          document_type: Optional[str] = Form(default=None),
                          photo_slot: Optional[str] = Form(default=None),
                          latitude: Optional[float] = Form(default=None, ge=-90, le=90),
                          longitude: Optional[float] = Form(default=None, ge=-180, le=180),
                          captured_at: Optional[str] = Form(default=None, max_length=40),
                          capture_method: Optional[Literal["camera", "gallery"]] = Form(default=None),
                          exif_taken_at_client: Optional[str] = Form(default=None, max_length=40),
                          db: Session = Depends(get_db),
                          employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """Grid Nova's own team uploads execution evidence (grid_nova mode only)."""
    if not employee.can_record_execution():
        raise HTTPException(status_code=403, detail="Your role cannot record execution.")
    onboarding = _load(db, employee, onboarding_id)
    try:
        record = execution.require(db, onboarding)
    except AssociateError as exc:
        raise _error(exc)
    if record.execution_mode != ExecutionMode.grid_nova:
        raise HTTPException(status_code=409, detail={"code": "execution_mode_associate",
                                                     "message": "The associate uploads this project's evidence."})
    if onboarding.status == OnboardingStatus.closed:
        raise HTTPException(status_code=409, detail={"code": "not_editable", "message": "This customer record is closed."})
    try:
        execution_files.store(db, onboarding, record, file, kind=kind,
                                    name=photo_slot if kind == "photo" else document_type,
                                    uploaded_by=employee.employee_id, latitude=latitude, longitude=longitude,
                                    captured_at=captured_at, capture_method=capture_method,
                                    exif_taken_at_client=exif_taken_at_client)
    except uploads.UploadRejected as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": str(exc)})
    except AssociateError as exc:
        db.rollback()
        raise _error(exc)
    db.commit()
    return _detail(db, onboarding, employee)


# ── Verify / reject / record ─────────────────────────────────────────────────────

class ReviewRequest(BaseModel):
    note: Optional[str] = Field(default=None, max_length=2000)


def _submission(db: Session, employee: AuthenticatedEmployee, submission_id: int, *,
                lock: bool = False) -> StageSubmission:
    if not employee.can_verify_execution():
        raise HTTPException(status_code=403, detail="Only Operations or Admin can check a stage submission.")
    sub = db.get(StageSubmission, submission_id)
    if sub is None:
        raise HTTPException(status_code=404, detail="Submission not found")
    _load(db, employee, sub.onboarding_id)
    if lock:
        # Verify/reject decide on the row's status: lock it and re-read, so a second
        # reviewer acting at the same moment sees the first decision.
        sub = db.execute(select(StageSubmission).where(StageSubmission.id == submission_id).with_for_update()
                         .execution_options(populate_existing=True)).scalar_one()
    return sub


def _review_conflict(db: Session) -> HTTPException:
    db.rollback()
    return HTTPException(status_code=409, detail={"code": "submission_not_pending",
                                                 "message": "This submission was just checked by someone else."})


@router.post("/execution/submissions/{submission_id}/verify")
def verify_submission(submission_id: int, body: ReviewRequest, db: Session = Depends(get_db),
                      employee: AuthenticatedEmployee = Depends(get_current_employee)):
    sub = _submission(db, employee, submission_id, lock=True)
    try:
        execution.verify(db, sub, employee.employee_id, body.note)
        db.commit()
    except AssociateError as exc:
        db.rollback()
        raise _error(exc)
    except IntegrityError:  # the stage was recorded by a simultaneous review
        raise _review_conflict(db)
    return _detail(db, db.get(CustomerOnboarding, sub.onboarding_id), employee)


@router.post("/execution/submissions/{submission_id}/reject")
def reject_submission(submission_id: int, body: ReviewRequest, db: Session = Depends(get_db),
                      employee: AuthenticatedEmployee = Depends(get_current_employee)):
    sub = _submission(db, employee, submission_id, lock=True)
    try:
        execution.reject(db, sub, employee.employee_id, body.note or "")
        db.commit()
    except AssociateError as exc:
        db.rollback()
        raise _error(exc)
    except IntegrityError:
        raise _review_conflict(db)
    return _detail(db, db.get(CustomerOnboarding, sub.onboarding_id), employee)


class SwitchModeRequest(BaseModel):
    execution_mode: Literal["grid_nova"]
    reason: str = Field(max_length=2000)


# Defined BEFORE .../execution/{stage}, which would otherwise take "mode" for a stage.
@router.post("/onboardings/{onboarding_id}/execution/mode")
def switch_execution_mode(onboarding_id: int, body: SwitchModeRequest, db: Session = Depends(get_db),
                          employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """Admin only: Grid Nova takes over this project's execution from the associate."""
    if not employee.is_admin:
        raise HTTPException(status_code=403, detail="Only Admin can change a project's execution mode.")
    onboarding = _load(db, employee, onboarding_id)
    try:
        execution.switch_to_grid_nova(db, onboarding, employee.employee_id, body.reason)
    except AssociateError as exc:
        db.rollback()
        raise _error(exc)
    db.commit()
    return _detail(db, onboarding, employee)


class RecordExecutionStageRequest(BaseModel):
    text: dict[str, str] = Field(default_factory=dict)
    note: Optional[str] = Field(default=None, max_length=2000)
    admin_override_reason: Optional[str] = Field(default=None, max_length=2000)


@router.post("/onboardings/{onboarding_id}/execution/{stage}")
def record_execution_stage(onboarding_id: int, stage: str, body: RecordExecutionStageRequest,
                           db: Session = Depends(get_db),
                           employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """Record an execution stage directly — projects Grid Nova executes itself. On an
    associate-mode project only an Admin may, and only with `admin_override_reason`
    (recorded with via `admin_override`, audited)."""
    if not employee.can_record_execution():
        raise HTTPException(status_code=403, detail="Your role cannot record execution.")
    onboarding = _load(db, employee, onboarding_id)
    try:
        execution.record_direct(db, onboarding, employee.employee_id, stage, body.text, body.note,
                                is_admin=employee.is_admin, admin_override_reason=body.admin_override_reason)
    except AssociateError as exc:
        db.rollback()
        raise _error(exc)
    db.commit()
    return _detail(db, onboarding, employee)


# ── Stuck alerts (Cloud Scheduler) ───────────────────────────────────────────────

@router.post("/internal/check-execution-alerts")
def run_execution_alerts(db: Session = Depends(get_db), _: None = Depends(_require_admin_or_internal_key)):
    """Admin or INTERNAL_API_KEY. Emails Operations once about each project that has
    become stuck at a stage (it is not emailed again for the same stage). Idempotent; an
    overlapping run is skipped."""
    from app.jobs.execution_alerts import check_execution_alerts
    from app.jobs.locks import run_exclusive
    return run_exclusive(db, "check-execution-alerts",
                         lambda: check_execution_alerts(db, datetime.now(timezone.utc)))
