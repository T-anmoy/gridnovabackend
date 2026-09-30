"""Execution for associates — associate-authenticated (get_current_associate).

Visibility follows OnboardingScope (app/auth/scoping.py): the owner, and a Preferred
Associate whose territory holds the site, may read a customer's execution; only the
owning associate uploads evidence or submits a stage, and only on a project in
`associate` execution mode. Nothing here carries money.
"""
from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.associates import uploads
from app.associates.service import AssociateError
from app.auth.dependencies import AuthenticatedAssociate, get_current_associate
from app.auth.scoping import require_onboarding_edit, require_onboarding_view
from app.execution import files as execution_files
from app.execution import register
from app.execution import service as execution
from app.memory.db import get_db
from app.memory.models import Associate, CustomerOnboarding, ExecutionMode, OnboardingStatus, StageSubmission
from app.onboarding import service as onboarding_service

router = APIRouter(prefix="/api/v1/associate/onboardings/{onboarding_id}/execution")


def _error(exc: AssociateError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.body())


def _load(db: Session, current: AuthenticatedAssociate, onboarding_id: int, *, edit: bool = False):
    associate = db.get(Associate, current.associate_pk)
    onboarding = db.get(CustomerOnboarding, onboarding_id)
    if onboarding is None:
        raise HTTPException(status_code=404, detail="Customer not found")
    (require_onboarding_edit if edit else require_onboarding_view)(associate, onboarding)
    return associate, onboarding


@router.get("")
def execution_status(onboarding_id: int, db: Session = Depends(get_db),
                     current: AuthenticatedAssociate = Depends(get_current_associate)):
    """Stages, what each needs, what has been uploaded, and the pending/rejected
    submission — 409 execution_not_started before the signed agreement."""
    associate, onboarding = _load(db, current, onboarding_id)
    try:
        body = execution.status(db, onboarding)
    except AssociateError as exc:
        raise _error(exc)
    return {**body, "can_submit": onboarding.owner_associate_id == associate.id
            and body["execution_mode"] == ExecutionMode.associate.value}


def _editable(db: Session, onboarding: CustomerOnboarding):
    if onboarding.status == OnboardingStatus.closed:
        raise HTTPException(status_code=409, detail={"code": "not_editable",
                                                     "message": "This customer record is closed."})
    try:
        record = execution.require(db, onboarding)
    except AssociateError as exc:
        raise _error(exc)
    if record.execution_mode != ExecutionMode.associate:
        raise HTTPException(status_code=409, detail={"code": "execution_mode_grid_nova",
                                                     "message": "Grid Nova's team records this project's execution."})
    return record


@router.post("/files", status_code=201)
async def upload_file(onboarding_id: int, file: UploadFile = File(...),
                      kind: Literal["document", "photo"] = Form(...),
                      document_type: Optional[str] = Form(default=None),
                      photo_slot: Optional[str] = Form(default=None),
                      latitude: Optional[float] = Form(default=None, ge=-90, le=90),
                      longitude: Optional[float] = Form(default=None, ge=-180, le=180),
                      captured_at: Optional[str] = Form(default=None, max_length=40),
                      capture_method: Optional[Literal["camera", "gallery"]] = Form(default=None),
                      exif_taken_at_client: Optional[str] = Form(default=None, max_length=40),
                      db: Session = Depends(get_db),
                      current: AuthenticatedAssociate = Depends(get_current_associate)):
    """One execution photo (a slot from photo_slots) or document (a type from
    document_types). Replaces the earlier file in that slot/type; a submission that
    already used the earlier one keeps it."""
    associate, onboarding = _load(db, current, onboarding_id, edit=True)
    record = _editable(db, onboarding)
    try:
        await execution_files.store(db, onboarding, record, file, kind=kind,
                                    name=photo_slot if kind == "photo" else document_type,
                                    uploaded_by=onboarding_service.actor(associate), latitude=latitude,
                                    longitude=longitude, captured_at=captured_at, capture_method=capture_method,
                                    exif_taken_at_client=exif_taken_at_client)
    except uploads.UploadRejected as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": str(exc)})
    except AssociateError as exc:
        db.rollback()
        raise _error(exc)
    db.commit()
    return execution_status(onboarding_id, db, current)


@router.get("/files/{file_ref}")
def file_link(onboarding_id: int, file_ref: str, db: Session = Depends(get_db),
              current: AuthenticatedAssociate = Depends(get_current_associate)):
    """A short-lived signed link to one execution file."""
    from app.storage import r2

    _, onboarding = _load(db, current, onboarding_id)
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
    return {"url": url, "expires_at": expires_at.isoformat(), "filename": item["filename"], "file_ref": file_ref}


class SubmitStageRequest(BaseModel):
    stage: str = Field(max_length=40)
    text: dict[str, str] = Field(default_factory=dict)
    note: Optional[str] = Field(default=None, max_length=2000)


@router.post("/submissions", status_code=201)
def submit_stage(onboarding_id: int, body: SubmitStageRequest, db: Session = Depends(get_db),
                 current: AuthenticatedAssociate = Depends(get_current_associate)):
    """Submit an execution stage for Grid Nova to check. Its evidence must already be
    uploaded; text evidence (references, serials) comes in `text`."""
    associate, onboarding = _load(db, current, onboarding_id, edit=True)
    _editable(db, onboarding)
    if any(len(v) > 500 for v in body.text.values()):
        raise HTTPException(status_code=422, detail={"code": "text_too_long", "message": "Keep each value short."})
    try:
        sub = execution.submit(db, onboarding, onboarding_service.actor(associate), body.stage, body.text, body.note)
    except AssociateError as exc:
        db.rollback()
        raise _error(exc)
    db.commit()
    return execution.submission_json(sub)


@router.get("/submissions")
def list_submissions(onboarding_id: int, db: Session = Depends(get_db),
                     current: AuthenticatedAssociate = Depends(get_current_associate)):
    _, onboarding = _load(db, current, onboarding_id)
    rows = db.execute(select(StageSubmission).where(StageSubmission.onboarding_id == onboarding.id)
                      .order_by(StageSubmission.submitted_at.desc(), StageSubmission.id.desc())).scalars()
    return {"items": [execution.submission_json(s) for s in rows]}


# ── Serial register (app/execution/register.py) ─────────────────────────────────

class UnitIn(BaseModel):
    category: str = Field(max_length=20)
    equipment_id: Optional[int] = None
    serial_number: str = Field(max_length=80)
    rating_wp: Optional[str | float] = None
    rating_kw: Optional[str | float] = None
    is_dcr: Optional[bool] = None
    warranty_start: Optional[str] = Field(default=None, max_length=10)
    warranty_end: Optional[str] = Field(default=None, max_length=10)


class UnitPatch(BaseModel):
    category: Optional[str] = Field(default=None, max_length=20)
    equipment_id: Optional[int] = None
    serial_number: Optional[str] = Field(default=None, max_length=80)
    rating_wp: Optional[str | float] = None
    rating_kw: Optional[str | float] = None
    is_dcr: Optional[bool] = None
    warranty_start: Optional[str] = Field(default=None, max_length=10)
    warranty_end: Optional[str] = Field(default=None, max_length=10)
    reason: Optional[str] = Field(default=None, max_length=2000)


class BulkUnitsIn(BaseModel):
    units: list[UnitIn]
    reason: Optional[str] = Field(default=None, max_length=2000)


class VoidIn(BaseModel):
    reason: str = Field(max_length=2000)


def _register_target(db: Session, current: AuthenticatedAssociate, onboarding_id: int, *, edit: bool):
    associate, onboarding = _load(db, current, onboarding_id, edit=edit)
    record = _editable(db, onboarding) if edit else _require(db, onboarding)
    return onboarding, record, register.Viewer(associate=associate)


def _require(db: Session, onboarding: CustomerOnboarding):
    try:
        return execution.require(db, onboarding)
    except AssociateError as exc:
        raise _error(exc)


def _unit(db: Session, onboarding: CustomerOnboarding, unit_id: int):
    from app.memory.models import InstalledUnit
    unit = db.get(InstalledUnit, unit_id)
    if unit is None or unit.onboarding_id != onboarding.id:
        raise HTTPException(status_code=404, detail="Unit not found")
    return unit


@router.get("/units")
def list_units(onboarding_id: int, db: Session = Depends(get_db),
               current: AuthenticatedAssociate = Depends(get_current_associate)):
    onboarding, record, _ = _register_target(db, current, onboarding_id, edit=False)
    return register.summary(db, onboarding, record)["register"]


@router.get("/units/check-serial")
def check_serial(onboarding_id: int, serial: str, db: Session = Depends(get_db),
                 current: AuthenticatedAssociate = Depends(get_current_associate)):
    """Instant duplicate check. Another project's details are shown only when that
    project is one you can already see."""
    onboarding, _, viewer = _register_target(db, current, onboarding_id, edit=False)
    return register.check_serial(db, serial[:200], viewer, project_id=onboarding.project_id)


def _register_write(db: Session, fn):
    try:
        result = fn()
    except AssociateError as exc:
        db.rollback()
        raise _error(exc)
    db.commit()
    return result


@router.post("/units", status_code=201)
def add_unit(onboarding_id: int, body: UnitIn, db: Session = Depends(get_db),
             current: AuthenticatedAssociate = Depends(get_current_associate)):
    onboarding, record, viewer = _register_target(db, current, onboarding_id, edit=True)
    units, warnings = _register_write(db, lambda: register.add_units(db, onboarding, record, [body.model_dump()],
                                                                     viewer))
    return {"unit": register.unit_json(db, units[0]), "warnings": warnings,
            "register": register.summary(db, onboarding, record)["register"]}


@router.post("/units/bulk", status_code=201)
def add_units_bulk(onboarding_id: int, body: BulkUnitsIn, db: Session = Depends(get_db),
                   current: AuthenticatedAssociate = Depends(get_current_associate)):
    """All or nothing: any bad row saves none, and every bad row is listed."""
    onboarding, record, viewer = _register_target(db, current, onboarding_id, edit=True)
    units, warnings = _register_write(db, lambda: register.add_units(
        db, onboarding, record, [u.model_dump() for u in body.units], viewer, body.reason))
    return {"units": [register.unit_json(db, u) for u in units], "warnings": warnings,
            "register": register.summary(db, onboarding, record)["register"]}


@router.patch("/units/{unit_id}")
def update_unit(onboarding_id: int, unit_id: int, body: UnitPatch, db: Session = Depends(get_db),
                current: AuthenticatedAssociate = Depends(get_current_associate)):
    onboarding, record, viewer = _register_target(db, current, onboarding_id, edit=True)
    unit = _unit(db, onboarding, unit_id)
    changes = body.model_dump(exclude_unset=True)
    reason = changes.pop("reason", None)
    unit, warnings = _register_write(db, lambda: register.update_unit(db, onboarding, record, unit, changes, viewer,
                                                                      reason))
    return {"unit": register.unit_json(db, unit), "warnings": warnings,
            "register": register.summary(db, onboarding, record)["register"]}


@router.post("/units/{unit_id}/void")
def void_unit(onboarding_id: int, unit_id: int, body: VoidIn, db: Session = Depends(get_db),
              current: AuthenticatedAssociate = Depends(get_current_associate)):
    onboarding, record, viewer = _register_target(db, current, onboarding_id, edit=True)
    unit = _unit(db, onboarding, unit_id)
    _register_write(db, lambda: register.void_unit(db, onboarding, record, unit, viewer, body.reason))
    return {"unit": register.unit_json(db, unit), "register": register.summary(db, onboarding, record)["register"]}
