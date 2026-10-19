"""Admin surface for the associate programme: reviewing and approving applications,
the associate lifecycle, territories and seats. Admin only — every route checks the
matching AuthenticatedEmployee.can_*() gate. Every change is audited (see
app/associates/service.py); nothing is ever deleted.
"""
from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, literal, select, union_all
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import app.notifications.notifier as notifier_module
from app.associates import issuance, service
from app.auth.dependencies import AuthenticatedEmployee, get_current_employee
from app.memory.db import get_db
from app.memory.models import (
    APPLICATION_DOCUMENTS,
    ApplicationStatus,
    Associate,
    AssociateStatus,
    AssociateType,
    TerritoryBlock,
    TerritoryDistrict,
    TerritoryLevel,
    TerritorySeat,
    TerritoryState,
)
from app.notifications.deferred import notify_after_commit
from app.security import pii

router = APIRouter(prefix="/api/v1/dashboard")


def _require(allowed: bool, what: str) -> None:
    if not allowed:
        raise HTTPException(status_code=403, detail=f"Only Admin can {what}.")


def _error(exc: service.AssociateError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.body())


def _name(db: Session, model, pk: Optional[int]) -> Optional[str]:
    return db.get(model, pk).name if pk is not None else None


def _code(db: Session, model, pk: Optional[int]) -> Optional[int]:
    return db.get(model, pk).lgd_code if pk is not None else None


def _territory(db: Session, state_id, district_id, block_id) -> dict:
    return {
        "state": {"lgd_code": _code(db, TerritoryState, state_id), "name": _name(db, TerritoryState, state_id)},
        "district": ({"lgd_code": _code(db, TerritoryDistrict, district_id),
                      "name": _name(db, TerritoryDistrict, district_id)} if district_id else None),
        "block": ({"lgd_code": _code(db, TerritoryBlock, block_id), "name": _name(db, TerritoryBlock, block_id)}
                  if block_id else None),
    }


def _iso(value) -> Optional[str]:
    return value.isoformat() if value is not None else None


# ── Applications ─────────────────────────────────────────────────────────────────

def _serialize_application(db: Session, application, application_type: AssociateType, full: bool,
                           reviewer: Optional[str] = None) -> dict:
    """`full` adds the KYC detail. The PAN is decrypted for the reviewing Admin through
    the audited helper (app/security/pii.py) — the caller commits that audit row."""
    associate = None
    if application.status == ApplicationStatus.approved:
        column = (Associate.associate_application_id if application_type == AssociateType.associate
                  else Associate.preferred_application_id)
        associate = db.execute(select(Associate.associate_id).where(column == application.id)).scalar_one_or_none()
    data = {
        "application_id": application.id,
        "application_type": application_type.value,
        "status": application.status.value,
        "full_name": application.full_name,
        "mobile": application.mobile,
        "email": application.email,
        "submitted_at": _iso(application.submitted_at),
        "status_changed_at": _iso(application.status_changed_at),
        "address_territory": _territory(db, application.state_id, application.district_id, application.block_id),
        "associate_id": associate,
    }
    if application_type == AssociateType.associate:
        data["capability"] = application.capability.value
    else:
        data["requested_level"] = application.requested_level.value
        data["requested_territory"] = _territory(db, application.requested_state_id,
                                                 application.requested_district_id, application.requested_block_id)
    if not full:
        return data
    data.update({
        "date_of_birth": application.date_of_birth.isoformat(),
        "address_line": application.address_line,
        "pin_code": application.pin_code,
        "aadhaar_last4": application.aadhaar_last4,
        "pan": pii.reveal(db, application, "pan", actor=reviewer, purpose="application_review") if reviewer else None,
        "pan_last4": application.pan_last4,
        "bank_account_last4": application.bank_account_last4,
        "bank_ifsc": application.bank_ifsc,
        "kyc_consent_version": application.kyc_consent_version,
        "kyc_consent_accepted_at": _iso(application.kyc_consent_accepted_at),
        "documents": [
            {"document": doc, "filename": meta.get("filename"), "content_type": meta.get("content_type"),
             "size": meta.get("size")}
            for doc, meta in sorted((application.documents or {}).items())
        ],
        "needs_info_note": application.needs_info_note,
        "rejection_reason": application.rejection_reason,
        "verification_call_completed": application.verification_call_completed,
        "verification_call_note": application.verification_call_note,
        "decided_at": _iso(application.decided_at),
        "decided_by_employee_id": application.decided_by_employee_id,
    })
    if application_type == AssociateType.preferred_associate:
        data.update({
            "business_type": application.business_type,
            "gstin": application.gstin,
            "office_address": application.office_address,
            "prior_experience": application.prior_experience,
            "team_size": application.team_size,
            "references": application.references,
        })
    return data


def _load_application(db: Session, application_type: AssociateType, application_id: int):
    application = service.get_application(db, application_type, application_id)
    if application is None:
        raise HTTPException(status_code=404, detail="Application not found")
    return application


@router.get("/associate-applications")
def list_applications(
    application_type: Optional[AssociateType] = None,
    status: Optional[ApplicationStatus] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
    db: Session = Depends(get_db),
    employee: AuthenticatedEmployee = Depends(get_current_employee),
):
    """Both programmes, newest first. Same {items, page, page_size, total} shape as
    the other paginated dashboard lists. The page and the count come from SQL (one
    UNION over both programmes); only the page's applications are loaded."""
    _require(employee.can_review_associate_applications(), "review associate applications")
    parts = []
    for kind, model in service.APPLICATION_MODELS.items():
        if application_type and kind != application_type:
            continue
        q = select(literal(kind.value).label("kind"), model.id.label("id"), model.submitted_at.label("submitted_at"))
        if status:
            q = q.where(model.status == status)
        parts.append(q)
    both = union_all(*parts).subquery() if len(parts) > 1 else parts[0].subquery()
    total = db.execute(select(func.count()).select_from(both)).scalar_one()
    page_keys = [(AssociateType(kind), app_id) for kind, app_id in db.execute(
        select(both.c.kind, both.c.id).order_by(both.c.submitted_at.desc(), both.c.id.desc(), both.c.kind)
        .offset((page - 1) * page_size).limit(page_size))]
    loaded = {}
    for kind, model in service.APPLICATION_MODELS.items():
        ids = [app_id for k, app_id in page_keys if k == kind]
        if ids:
            loaded.update({(kind, a.id): a for a in db.execute(select(model).where(model.id.in_(ids))).scalars()})
    return {
        "items": [_serialize_application(db, loaded[key], key[0], full=False) for key in page_keys],
        "page": page, "page_size": page_size, "total": total,
    }


@router.get("/associate-applications/{application_type}/{application_id}")
def get_application(application_type: AssociateType, application_id: int, db: Session = Depends(get_db),
                    employee: AuthenticatedEmployee = Depends(get_current_employee)):
    _require(employee.can_review_associate_applications(), "review associate applications")
    body = _serialize_application(db, _load_application(db, application_type, application_id), application_type,
                                  full=True, reviewer=employee.employee_id)
    db.commit()   # the PAN-decryption audit row
    return body


@router.get("/associate-applications/{application_type}/{application_id}/documents/{document}")
def application_document_link(application_type: AssociateType, application_id: int, document: str,
                              db: Session = Depends(get_db),
                              employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """A short-lived signed link to one uploaded KYC document."""
    _require(employee.can_review_associate_applications(), "review associate applications")
    application = _load_application(db, application_type, application_id)
    meta = (application.documents or {}).get(document) if document in APPLICATION_DOCUMENTS else None
    if not meta:
        raise HTTPException(status_code=404, detail="Document not found")
    from app.storage import r2
    try:
        url, expires_at = r2.signed_get_url(meta["storage_key"], filename=meta.get("filename"))
    except r2.StorageNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail={"code": "storage_not_configured", "message": str(exc)})
    service.audit(db, service.application_entity(application_type), str(application_id), "document_viewed",
                  employee.employee_id, after={"document": document})
    db.commit()
    return {"url": url, "expires_at": expires_at.isoformat(), "filename": meta.get("filename"), "document": document}


class ApplicationStatusRequest(BaseModel):
    status: Literal["under_review", "needs_info", "rejected"]
    note: Optional[str] = Field(default=None, max_length=2000)


@router.post("/associate-applications/{application_type}/{application_id}/status")
def change_application_status(application_type: AssociateType, application_id: int, body: ApplicationStatusRequest,
                              db: Session = Depends(get_db),
                              employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """submitted → under_review; under_review → needs_info (note required) or
    rejected (reason required, as `note`); needs_info → under_review."""
    _require(employee.can_review_associate_applications(), "review associate applications")
    application = _load_application(db, application_type, application_id)
    try:
        service.change_application_status(db, application, application_type, ApplicationStatus(body.status),
                                          employee.employee_id, body.note)
    except service.AssociateError as exc:
        db.rollback()
        raise _error(exc)
    db.commit()
    body = _serialize_application(db, application, application_type, full=True, reviewer=employee.employee_id)
    db.commit()   # the PAN-decryption audit row
    return body


class ApproveRequest(BaseModel):
    verification_call_completed: bool
    verification_call_note: str = Field(max_length=2000)


def _approval_conflict(db: Session, application_type: AssociateType, application_id: int) -> HTTPException:
    """After a refused insert: re-read what is true now and say which conflict it was —
    the application was approved meanwhile, or the seat was taken."""
    application = service.get_application(db, application_type, application_id)
    if application is not None and application.status == ApplicationStatus.approved:
        return HTTPException(status_code=409, detail={
            "code": "invalid_transition", "message": "This application was just approved by someone else."})
    return HTTPException(status_code=409, detail={"code": "seat_full",
                                                 "message": "That seat was just filled by another approval."})


@router.post("/associate-applications/{application_type}/{application_id}/approve")
def approve_application(application_type: AssociateType, application_id: int, body: ApproveRequest,
                        db: Session = Depends(get_db),
                        employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """Approve an application under review. Requires the verification call — this
    stands in for OTP verification — to be recorded as completed, with a note.

    In one transaction: the Associate ID is issued, the associate created and (for a
    Preferred Associate) the seat taken. After it commits, on the background runner
    (app/jobs/runner.py): the Clerk invitation, the ID card and welcome letter PDFs,
    and the notification — so the response does not wait for them. A failure in any of those
    is recorded on the associate and retried from the dashboard; the approval stands.
    """
    _require(employee.can_review_associate_applications(), "approve associate applications")
    if not body.verification_call_completed:
        raise HTTPException(status_code=422, detail={
            "code": "verification_call_required", "field": "verification_call_completed",
            "message": "Complete the verification call before approving.",
        })
    _load_application(db, application_type, application_id)
    # Lock the application row: a second, simultaneous approval waits here and then
    # sees it approved (invalid_transition) instead of racing to insert a duplicate.
    model = service.APPLICATION_MODELS[application_type]
    application = db.execute(select(model).where(model.id == application_id).with_for_update()
                             .execution_options(populate_existing=True)).scalar_one()
    today = service.today_ist()
    try:
        associate = service.approve(db, application, application_type, employee.employee_id,
                                    body.verification_call_note, today)
    except service.AssociateError as exc:
        db.rollback()
        raise _error(exc)
    except IntegrityError:
        db.rollback()
        raise _approval_conflict(db, application_type, application_id)

    payload = {"associate_id": associate.associate_id, "associate_type": application_type.value,
               "name": associate.full_name, "level": associate.level.value if associate.level else None,
               "approved_by": employee.employee_id}
    notify_after_commit(db, lambda: notifier_module.notifier.notify("associate_approved", payload),
                        f"associate approved {associate.associate_id}")
    try:
        db.commit()
    except IntegrityError:
        # The seat index is the last line of defence against two approvals racing.
        db.rollback()
        raise _approval_conflict(db, application_type, application_id)

    # The Clerk invitation and the two PDFs run on the background runner: the response
    # shows the associate as approved, with clerk_invite_status null and the documents
    # not yet generated until that work finishes (GET .../associates/{id} shows it).
    from app.jobs import runner
    associate_pk, approver = associate.id, employee.employee_id
    runner.submit(lambda: issuance.issue_after_approval(associate_pk, approver, today),
                  f"associate issuance {associate.associate_id}")
    db.refresh(associate)
    body = {"application": _serialize_application(db, application, application_type, full=True,
                                                  reviewer=employee.employee_id),
            "associate": serialize_associate(db, associate)}
    db.commit()   # the PAN-decryption audit row
    return body


# ── Associates ───────────────────────────────────────────────────────────────────

def serialize_associate(db: Session, associate: Associate) -> dict:
    seat = db.execute(select(TerritorySeat).where(TerritorySeat.holder_id == associate.id,
                                                  TerritorySeat.released_at.is_(None))).scalar_one_or_none()
    return {
        "associate_id": associate.associate_id,
        "associate_type": associate.associate_type.value,
        "status": associate.status.value,
        "status_reason": associate.status_reason,
        "status_changed_at": _iso(associate.status_changed_at),
        "full_name": associate.full_name,
        "email": associate.email,
        "mobile": associate.mobile,
        "capability": associate.capability.value if associate.capability else None,
        "level": associate.level.value if associate.level else None,
        "territory": (_territory(db, associate.territory_state_id, associate.territory_district_id,
                                 associate.territory_block_id) if associate.territory_state_id else None),
        "seat": ({"seat_id": seat.id, "seat_no": seat.seat_no, "claimed_at": _iso(seat.claimed_at)}
                 if seat else None),
        "application": {
            "application_type": associate.associate_type.value,
            "application_id": associate.associate_application_id or associate.preferred_application_id,
        },
        "approved_at": _iso(associate.approved_at),
        "approved_by_employee_id": associate.approved_by_employee_id,
        "clerk_invite_status": associate.clerk_invite_status.value if associate.clerk_invite_status else None,
        "clerk_user_bound": associate.clerk_user_id is not None,
        "documents": {
            "id_card": associate.id_card_storage_key is not None,
            "welcome_letter": associate.welcome_letter_storage_key is not None,
            "generated_at": _iso(associate.documents_generated_at),
            "error": associate.documents_error,
        },
    }


def _load_associate(db: Session, associate_id: str) -> Associate:
    associate = service.get_associate(db, associate_id)
    if associate is None:
        raise HTTPException(status_code=404, detail="Associate not found")
    return associate


@router.get("/associates")
def list_associates(status: Optional[AssociateStatus] = None, associate_type: Optional[AssociateType] = None,
                    page: int = Query(default=1, ge=1), page_size: int = Query(default=25, ge=1, le=100),
                    db: Session = Depends(get_db),
                    employee: AuthenticatedEmployee = Depends(get_current_employee)):
    _require(employee.can_review_associate_applications(), "view associates")
    q = select(Associate)
    if status:
        q = q.where(Associate.status == status)
    if associate_type:
        q = q.where(Associate.associate_type == associate_type)
    total = db.execute(select(func.count()).select_from(q.subquery())).scalar_one()
    rows = db.execute(q.order_by(Associate.approved_at.desc(), Associate.id.desc())
                      .offset((page - 1) * page_size).limit(page_size)).scalars()
    return {"items": [serialize_associate(db, a) for a in rows], "page": page, "page_size": page_size,
            "total": total}


@router.get("/associates/{associate_id}")
def get_associate(associate_id: str, db: Session = Depends(get_db),
                  employee: AuthenticatedEmployee = Depends(get_current_employee)):
    _require(employee.can_review_associate_applications(), "view associates")
    return serialize_associate(db, _load_associate(db, associate_id))


class AssociateStatusRequest(BaseModel):
    status: Literal["active", "suspended", "deactivated"]
    reason: Optional[str] = Field(default=None, max_length=2000)


@router.post("/associates/{associate_id}/status")
def change_associate_status(associate_id: str, body: AssociateStatusRequest, db: Session = Depends(get_db),
                            employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """active ⇄ suspended → deactivated. Suspending or deactivating needs a reason.
    Deactivation is final and releases the associate's seat; suspension keeps it."""
    _require(employee.can_review_associate_applications(), "change an associate's status")
    associate = _load_associate(db, associate_id)
    try:
        service.change_associate_status(db, associate, AssociateStatus(body.status), employee.employee_id,
                                        body.reason)
    except service.AssociateError as exc:
        db.rollback()
        raise _error(exc)
    db.commit()
    return serialize_associate(db, associate)


@router.post("/associates/{associate_id}/clerk-invite")
def retry_clerk_invite(associate_id: str, db: Session = Depends(get_db),
                       employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """Send (or re-send after a failure) the associate's Clerk invitation."""
    _require(employee.can_review_associate_applications(), "invite associates")
    associate = _load_associate(db, associate_id)
    try:
        issuance.send_clerk_invite(db, associate, employee.employee_id)
    except service.AssociateError as exc:
        db.rollback()
        raise _error(exc)
    return serialize_associate(db, associate)


@router.post("/associates/{associate_id}/documents/regenerate")
def regenerate_documents(associate_id: str, db: Session = Depends(get_db),
                         employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """Render and store the ID card and welcome letter again (after a failure, or
    once the welcome-letter text has been supplied). The associate ID never changes."""
    _require(employee.can_review_associate_applications(), "issue associate documents")
    associate = _load_associate(db, associate_id)
    issuance.generate_documents(db, associate, employee.employee_id, service.today_ist())
    db.refresh(associate)
    return serialize_associate(db, associate)


def document_link(associate: Associate, document: str) -> dict:
    key = {"id_card": associate.id_card_storage_key,
           "welcome_letter": associate.welcome_letter_storage_key}.get(document)
    if key is None:
        raise HTTPException(status_code=404, detail="Document not found")
    from app.storage import r2
    filename = f"{associate.associate_id}-{document.replace('_', '-')}.pdf"
    try:
        url, expires_at = r2.signed_get_url(key, filename=filename)
    except r2.StorageNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail={"code": "storage_not_configured", "message": str(exc)})
    return {"url": url, "expires_at": expires_at.isoformat(), "filename": filename, "document": document}


@router.get("/associates/{associate_id}/documents/{document}")
def associate_document_link(associate_id: str, document: Literal["id_card", "welcome_letter"],
                            db: Session = Depends(get_db),
                            employee: AuthenticatedEmployee = Depends(get_current_employee)):
    _require(employee.can_review_associate_applications(), "view associate documents")
    return document_link(_load_associate(db, associate_id), document)


# ── Territories ──────────────────────────────────────────────────────────────────

_LEVEL_MODELS = {"states": TerritoryState, "districts": TerritoryDistrict, "blocks": TerritoryBlock}


def _serialize_territory(db: Session, level: str, row) -> dict:
    data = {"lgd_code": row.lgd_code, "name": row.name, "is_active": row.is_active}
    if level == "states":
        data["iso_code"] = row.iso_code
    elif level == "districts":
        data["state_lgd_code"] = db.get(TerritoryState, row.state_id).lgd_code
    else:
        data["district_lgd_code"] = db.get(TerritoryDistrict, row.district_id).lgd_code
    return data


@router.get("/territories/{level}")
def admin_list_territories(level: Literal["states", "districts", "blocks"],
                           parent_lgd_code: Optional[int] = None, include_inactive: bool = True,
                           db: Session = Depends(get_db),
                           employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """Every territory at one level, including switched-off ones. `parent_lgd_code`
    narrows districts to a state, or blocks to a district."""
    _require(employee.can_manage_territories(), "manage territories")
    model = _LEVEL_MODELS[level]
    q = select(model)
    if not include_inactive:
        q = q.where(model.is_active.is_(True))
    if parent_lgd_code is not None and level != "states":
        parent_model = TerritoryState if level == "districts" else TerritoryDistrict
        parent = db.execute(select(parent_model).where(parent_model.lgd_code == parent_lgd_code)).scalar_one_or_none()
        if parent is None:
            raise HTTPException(status_code=404, detail="Unknown parent territory")
        q = q.where((model.state_id if level == "districts" else model.district_id) == parent.id)
    return {"items": [_serialize_territory(db, level, r) for r in db.execute(q.order_by(model.name)).scalars()]}


class NewDistrictRequest(BaseModel):
    state_lgd_code: int
    lgd_code: int = Field(gt=0)
    name: str = Field(min_length=1, max_length=200)


class NewBlockRequest(BaseModel):
    district_lgd_code: int
    lgd_code: int = Field(gt=0)
    name: str = Field(min_length=1, max_length=200)


def _add_territory(db: Session, level: str, row, performed_by: str) -> dict:
    db.add(row)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail={"code": "duplicate_lgd_code",
                                                     "message": "A territory with this LGD code already exists."})
    service.audit(db, f"territory_{level[:-1]}", str(row.id), "created", performed_by,
                  after={"lgd_code": row.lgd_code, "name": row.name})
    db.commit()
    return _serialize_territory(db, level, row)


@router.post("/territories/districts", status_code=201)
def add_district(body: NewDistrictRequest, db: Session = Depends(get_db),
                 employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """Add a district missing from the seed data (use its official LGD code)."""
    _require(employee.can_manage_territories(), "manage territories")
    state = db.execute(select(TerritoryState).where(TerritoryState.lgd_code == body.state_lgd_code)).scalar_one_or_none()
    if state is None:
        raise HTTPException(status_code=404, detail="Unknown state")
    return _add_territory(db, "districts",
                          TerritoryDistrict(state_id=state.id, lgd_code=body.lgd_code, name=body.name.strip(),
                                            is_active=True), employee.employee_id)


@router.post("/territories/blocks", status_code=201)
def add_block(body: NewBlockRequest, db: Session = Depends(get_db),
              employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """Add a block missing from the seed data (use its official LGD code)."""
    _require(employee.can_manage_territories(), "manage territories")
    district = db.execute(select(TerritoryDistrict).where(
        TerritoryDistrict.lgd_code == body.district_lgd_code)).scalar_one_or_none()
    if district is None:
        raise HTTPException(status_code=404, detail="Unknown district")
    return _add_territory(db, "blocks",
                          TerritoryBlock(district_id=district.id, lgd_code=body.lgd_code, name=body.name.strip(),
                                         is_active=True), employee.employee_id)


class TerritoryUpdateRequest(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    is_active: Optional[bool] = None


@router.patch("/territories/{level}/{lgd_code}")
def update_territory(level: Literal["states", "districts", "blocks"], lgd_code: int, body: TerritoryUpdateRequest,
                     db: Session = Depends(get_db),
                     employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """Rename a territory or switch it off/on. Switching off stops it being offered on
    the registration forms; existing applications, associates and seats keep it."""
    _require(employee.can_manage_territories(), "manage territories")
    model = _LEVEL_MODELS[level]
    row = db.execute(select(model).where(model.lgd_code == lgd_code)).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Territory not found")
    changes = {k: v for k, v in body.model_dump(exclude_none=True).items() if getattr(row, k) != v}
    if changes:
        before = {k: getattr(row, k) for k in changes}
        for k, v in changes.items():
            setattr(row, k, v.strip() if isinstance(v, str) else v)
        db.flush()
        service.audit(db, f"territory_{level[:-1]}", str(row.id), "updated", employee.employee_id,
                      before=before, after=changes)
        db.commit()
    return _serialize_territory(db, level, row)


# ── Seats ────────────────────────────────────────────────────────────────────────

def _serialize_seat(db: Session, seat: TerritorySeat) -> dict:
    holder = db.get(Associate, seat.holder_id)
    return {
        "seat_id": seat.id,
        "level": seat.level.value,
        "seat_no": seat.seat_no,
        "territory": _territory(db, seat.state_id, seat.district_id, seat.block_id),
        "holder": {"associate_id": holder.associate_id, "full_name": holder.full_name,
                   "status": holder.status.value},
        "claimed_at": _iso(seat.claimed_at),
        "released_at": _iso(seat.released_at),
        "released_by": seat.released_by,
        "release_reason": seat.release_reason,
    }


@router.get("/territory-seats")
def list_seats(state_lgd_code: Optional[int] = None, level: Optional[TerritoryLevel] = None,
               include_released: bool = False, db: Session = Depends(get_db),
               employee: AuthenticatedEmployee = Depends(get_current_employee)):
    _require(employee.can_manage_seats(), "manage seats")
    q = select(TerritorySeat)
    if not include_released:
        q = q.where(TerritorySeat.released_at.is_(None))
    if level:
        q = q.where(TerritorySeat.level == level)
    if state_lgd_code is not None:
        state = db.execute(select(TerritoryState).where(TerritoryState.lgd_code == state_lgd_code)).scalar_one_or_none()
        if state is None:
            raise HTTPException(status_code=404, detail="Unknown state")
        q = q.where(TerritorySeat.state_id == state.id)
    return {"items": [_serialize_seat(db, s) for s in db.execute(q.order_by(TerritorySeat.id)).scalars()]}


class SeatReleaseRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)


@router.post("/territory-seats/{seat_id}/release")
def release_seat(seat_id: int, body: SeatReleaseRequest, db: Session = Depends(get_db),
                 employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """Free a seat so someone else can hold it. Only a suspended holder's seat can be
    released here — an active holder must be suspended first, and deactivation
    releases the seat on its own."""
    _require(employee.can_manage_seats(), "manage seats")
    seat = db.get(TerritorySeat, seat_id)
    if seat is None:
        raise HTTPException(status_code=404, detail="Seat not found")
    holder = db.get(Associate, seat.holder_id)
    if seat.released_at is None and holder.status == AssociateStatus.active:
        raise HTTPException(status_code=409, detail={
            "code": "holder_active", "message": "Suspend the associate holding this seat before releasing it.",
        })
    try:
        service.release_seat(db, seat, employee.employee_id, body.reason.strip())
    except service.AssociateError as exc:
        db.rollback()
        raise _error(exc)
    db.commit()
    return _serialize_seat(db, seat)
