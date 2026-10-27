"""Handover pack, warranty register and service requests for staff (BE-5, Phase 6A).

Who sees what: the same visibility as execution (app/auth/scoping.py
require_execution_access — Admin, Management and Engineer every project; Operations every
project in execution; Sales its own or unassigned leads; Accounts none). Who acts: the pack
(ticks, acknowledgement, the customer's link) — Operations and Admin
(can_record_execution); service requests — Operations, Admin and Engineer
(can_handle_service_requests). No response here carries a price, a payment or a commission:
the pack's final-payment item is a yes/no only.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.routes.dashboard import _require_admin_or_internal_key
from app.api.routes.execution_admin import _load
from app.associates import uploads
from app.associates.service import AssociateError
from app.auth.dependencies import AuthenticatedEmployee, get_current_employee
from app.auth.scoping import lead_scope, narrowed_to_assigned, require_execution_access
from app.handover import link, pack as pack_module, requests, service, warranty as warranty_register
from app.memory.db import get_db
from app.memory.memory_manager import MemoryManager
from app.memory.models import (
    Customer, CustomerOnboarding, Lead, Project, ServiceRequest, ServiceRequestCategory,
    ServiceRequestStatus,
)

router = APIRouter(prefix="/api/v1/dashboard")


def _error(exc: AssociateError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.body())


def _upload_error(exc: uploads.UploadRejected) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": str(exc)})


def _act_on_pack(employee: AuthenticatedEmployee) -> None:
    if not employee.can_record_execution():
        raise HTTPException(status_code=403, detail="Only Operations or Admin can change the handover pack.")


def _act_on_requests(employee: AuthenticatedEmployee) -> None:
    if not employee.can_handle_service_requests():
        raise HTTPException(status_code=403, detail="Only Operations, Admin or an Engineer can handle service requests.")


def _pack_detail(db: Session, onboarding: CustomerOnboarding, employee: AuthenticatedEmployee) -> dict:
    return {**service.detail(db, onboarding, staff=True), "can_act": employee.can_record_execution()}


# ── Pack ─────────────────────────────────────────────────────────────────────────

@router.get("/onboardings/{onboarding_id}/handover")
def handover_pack(onboarding_id: int, db: Session = Depends(get_db),
                  employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """The checklist (computed items and manual ticks), whether it is complete, the
    warranty register once handed over, the PDF state and — staff only — the customer
    link state (never the token) and email state."""
    onboarding = _load(db, employee, onboarding_id)
    return _pack_detail(db, onboarding, employee)


class TickRequest(BaseModel):
    done: bool = True
    note: Optional[str] = Field(default=None, max_length=1000)


def _tick(item: str, db: Session, employee: AuthenticatedEmployee, onboarding_id: int, body: TickRequest) -> dict:
    _act_on_pack(employee)
    onboarding = _load(db, employee, onboarding_id)
    try:
        pack_module.set_tick(db, onboarding, item, body.done, employee.employee_id, body.note)
    except AssociateError as exc:
        db.rollback()
        raise _error(exc)
    db.commit()
    return _pack_detail(db, onboarding, employee)


@router.post("/onboardings/{onboarding_id}/handover/orientation")
def tick_orientation(onboarding_id: int, body: TickRequest, db: Session = Depends(get_db),
                     employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """Operations / Admin: the customer orientation was (or was not) given."""
    return _tick("orientation", db, employee, onboarding_id, body)


@router.post("/onboardings/{onboarding_id}/handover/om-contact")
def tick_om_contact(onboarding_id: int, body: TickRequest, db: Session = Depends(get_db),
                    employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """Operations / Admin: the O&M contact details were (or were not) given to the customer."""
    return _tick("om_contact", db, employee, onboarding_id, body)


@router.post("/onboardings/{onboarding_id}/handover/acknowledgement")
def acknowledge(onboarding_id: int, method: Literal["signature", "in_person"] = Form(...),
                customer_name: str = Form(..., max_length=200), note: Optional[str] = Form(default=None, max_length=1000),
                file: Optional[UploadFile] = File(default=None), db: Session = Depends(get_db),
                employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """Operations / Admin: the customer's acknowledgement — a signature image (PNG, method
    `signature`) or an in-person tick (method `in_person`, no file)."""
    _act_on_pack(employee)
    onboarding = _load(db, employee, onboarding_id)
    content = None
    if file is not None and file.filename:
        file.file.seek(0)
        content = file.file.read()
    try:
        pack_module.acknowledge(db, onboarding, employee.employee_id, method=method, customer_name=customer_name,
                                note=note, signature=content)
    except AssociateError as exc:
        db.rollback()
        raise _error(exc)
    db.commit()
    return _pack_detail(db, onboarding, employee)


class LinkRequest(BaseModel):
    send_email: bool = True


@router.post("/onboardings/{onboarding_id}/handover/link")
def issue_link(onboarding_id: int, body: Optional[LinkRequest] = None, db: Session = Depends(get_db),
               employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """Operations / Admin: issue a NEW customer link (any earlier one stops working),
    render the PDF if needed and email the customer (unless send_email is false). The link
    is returned this once. 409 handover_not_recorded / handover_pdf_held."""
    _act_on_pack(employee)
    onboarding = _load(db, employee, onboarding_id)
    try:
        issued = service.send_link(db, onboarding, employee.employee_id, send_email=(body or LinkRequest()).send_email)
    except AssociateError as exc:
        db.rollback()
        raise _error(exc)
    db.commit()
    return {"link": {"url": issued["url"], "expires_at": issued["expires_at"]},
            "pack": _pack_detail(db, onboarding, employee)}


@router.post("/onboardings/{onboarding_id}/handover/link/revoke")
def revoke_link(onboarding_id: int, db: Session = Depends(get_db),
                employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """Operations / Admin: the customer's link stops working at once."""
    _act_on_pack(employee)
    onboarding = _load(db, employee, onboarding_id)
    pack = pack_module.find(db, onboarding.project_id)
    if pack is None or link.state(pack) == "none":
        raise HTTPException(status_code=409, detail={"code": "no_link", "message": "No customer link has been issued."})
    link.revoke(db, pack, employee.employee_id)
    db.commit()
    return _pack_detail(db, onboarding, employee)


@router.post("/onboardings/{onboarding_id}/handover/pdf/regenerate")
def regenerate_pdf(onboarding_id: int, db: Session = Depends(get_db),
                   employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """Operations / Admin: render the handover PDF again (after the register changed or a
    failed render). Runs in the background; poll GET .../handover `pdf.status`."""
    _act_on_pack(employee)
    onboarding = _load(db, employee, onboarding_id)
    try:
        service.regenerate_pdf(db, onboarding, employee.employee_id)
    except AssociateError as exc:
        db.rollback()
        raise _error(exc)
    db.commit()
    return _pack_detail(db, onboarding, employee)


@router.get("/onboardings/{onboarding_id}/handover/pdf")
def handover_pdf(onboarding_id: int, db: Session = Depends(get_db),
                 employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """A short-lived signed URL for the rendered handover PDF (404 until it exists)."""
    from app.storage import r2

    onboarding = _load(db, employee, onboarding_id)
    pack = pack_module.find(db, onboarding.project_id)
    if pack is None or not pack.pdf_key:
        raise HTTPException(status_code=404, detail="The handover PDF has not been generated.")
    try:
        url, expires_at = r2.signed_get_url(pack.pdf_key, filename="handover-pack.pdf")
    except r2.StorageNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail={"code": "storage_not_configured", "message": str(exc)})
    return {"url": url, "expires_at": expires_at.isoformat()}


# ── Service requests ─────────────────────────────────────────────────────────────

def _request(db: Session, employee: AuthenticatedEmployee, request_id: int, *, lock: bool = False) -> ServiceRequest:
    if not employee.can_view_execution():
        raise HTTPException(status_code=403, detail="Your role cannot view service requests.")
    req = db.get(ServiceRequest, request_id)
    if req is None:
        raise HTTPException(status_code=404, detail="Service request not found")
    _load(db, employee, req.onboarding_id)
    if lock:
        req = db.execute(select(ServiceRequest).where(ServiceRequest.id == request_id).with_for_update()
                         .execution_options(populate_existing=True)).scalar_one()
    return req


def _detail(db: Session, req: ServiceRequest) -> dict:
    return requests.request_json(db, req, audience="staff", events=True)


@router.get("/service-requests")
def service_request_queue(status: Optional[ServiceRequestStatus] = None, open: Optional[bool] = None,
                          overdue: Optional[bool] = None, within_warranty: Optional[bool] = None,
                          category: Optional[ServiceRequestCategory] = None,
                          assignee: Optional[str] = Query(default=None, max_length=64,
                                                          description="an employee id, `me` or `unassigned`"),
                          onboarding_id: Optional[int] = None,
                          page: int = Query(default=1, ge=1), page_size: int = Query(default=25, ge=1, le=100),
                          db: Session = Depends(get_db),
                          employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """Service requests the caller may see, newest first. `open=true`: open or in
    progress. `overdue=true`: past the SLA (none while service.sla_hours is null)."""
    if not employee.can_view_execution():
        raise HTTPException(status_code=403, detail="Your role cannot view service requests.")
    q = (select(ServiceRequest, Customer.name, Project.reference_id)
         .join(CustomerOnboarding, CustomerOnboarding.id == ServiceRequest.onboarding_id)
         .join(Customer, Customer.id == CustomerOnboarding.customer_id)
         .join(Project, Project.id == ServiceRequest.project_id)
         .join(Lead, Lead.id == CustomerOnboarding.lead_id))
    if narrowed_to_assigned(employee):
        q = q.where(lead_scope(employee).condition())
    now = datetime.now(timezone.utc)
    if status is not None:
        q = q.where(ServiceRequest.status == status)
    if open is not None:
        q = q.where(ServiceRequest.status.in_(requests.OPEN) if open else ServiceRequest.status.not_in(requests.OPEN))
    if overdue is not None:
        cond = requests.overdue_filter(now)
        q = q.where(*cond) if overdue else q.where(~(cond[0] & cond[1] & cond[2]))
    if within_warranty is not None:
        q = q.where(ServiceRequest.within_warranty.is_(within_warranty))
    if category is not None:
        q = q.where(ServiceRequest.category == category)
    if onboarding_id is not None:
        q = q.where(ServiceRequest.onboarding_id == onboarding_id)
    if assignee == "unassigned":
        q = q.where(ServiceRequest.assignee_id.is_(None))
    elif assignee:
        q = q.where(ServiceRequest.assignee_id == (employee.employee_id if assignee == "me" else assignee))
    total = db.execute(select(func.count()).select_from(q.subquery())).scalar_one()
    rows = db.execute(q.order_by(ServiceRequest.created_at.desc(), ServiceRequest.id.desc())
                      .limit(page_size).offset((page - 1) * page_size)).all()
    return {"items": [{**requests.request_json(db, r, audience="staff"), "customer_name": name,
                       "project_reference_id": reference} for r, name, reference in rows],
            "page": page, "page_size": page_size, "total": total,
            "can_handle": employee.can_handle_service_requests()}


@router.get("/onboardings/{onboarding_id}/service-requests")
def customer_service_requests(onboarding_id: int, db: Session = Depends(get_db),
                              employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """Every service request for one customer, newest first."""
    onboarding = _load(db, employee, onboarding_id)
    rows = db.execute(select(ServiceRequest).where(ServiceRequest.onboarding_id == onboarding.id)
                      .order_by(ServiceRequest.created_at.desc(), ServiceRequest.id.desc())).scalars()
    return {"items": [requests.request_json(db, r, audience="staff") for r in rows],
            "can_handle": employee.can_handle_service_requests()}


@router.post("/onboardings/{onboarding_id}/service-requests", status_code=201)
def raise_service_request(onboarding_id: int, category: ServiceRequestCategory = Form(...),
                          description: str = Form(..., max_length=requests.MAX_DESCRIPTION),
                          contact_phone: Optional[str] = Form(default=None, max_length=32),
                          files: list[UploadFile] = File(default=[]), db: Session = Depends(get_db),
                          employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """Operations / Admin / Engineer: raise a request on a customer's behalf (multipart;
    up to service.max_photos images in `files`). 409 not_handed_over before handover."""
    _act_on_requests(employee)
    onboarding = _load(db, employee, onboarding_id)
    try:
        photos = requests.read_photos(files)
        req = requests.raise_request(db, onboarding, via="staff", actor=employee.employee_id, category=category.value,
                                     description=description, contact_phone=contact_phone, photos=photos)
    except uploads.UploadRejected as exc:
        db.rollback()
        raise _upload_error(exc)
    except AssociateError as exc:
        db.rollback()
        raise _error(exc)
    db.commit()
    return _detail(db, req)


@router.get("/service-requests/{request_id}")
def service_request_detail(request_id: int, db: Session = Depends(get_db),
                           employee: AuthenticatedEmployee = Depends(get_current_employee)):
    req = _request(db, employee, request_id)
    onboarding = db.get(CustomerOnboarding, req.onboarding_id)
    customer = db.get(Customer, onboarding.customer_id)
    return {**_detail(db, req), "customer_name": customer.name if customer else None,
            "project_reference_id": db.get(Project, req.project_id).reference_id,
            "can_handle": employee.can_handle_service_requests()}


class AssignRequest(BaseModel):
    assignee_id: Optional[str] = Field(default=None, max_length=64)


class StatusRequest(BaseModel):
    status: Literal["in_progress", "resolved", "rejected"]
    note: Optional[str] = Field(default=None, max_length=requests.MAX_NOTE)


class NoteRequest(BaseModel):
    note: str = Field(min_length=1, max_length=requests.MAX_NOTE)


def _handle(db: Session, employee: AuthenticatedEmployee, request_id: int, fn) -> dict:
    _act_on_requests(employee)
    req = _request(db, employee, request_id, lock=True)
    try:
        fn(req)
    except AssociateError as exc:
        db.rollback()
        raise _error(exc)
    db.commit()
    return _detail(db, req)


@router.post("/service-requests/{request_id}/assign")
def assign_service_request(request_id: int, body: AssignRequest, db: Session = Depends(get_db),
                           employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """Assign to an active Admin / Operations / Engineer employee, or unassign (null)."""
    return _handle(db, employee, request_id, lambda r: requests.assign(db, r, body.assignee_id, employee.employee_id))


@router.post("/service-requests/{request_id}/status")
def set_service_request_status(request_id: int, body: StatusRequest, db: Session = Depends(get_db),
                               employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """open → in_progress → resolved | rejected (open may go straight to either end).
    resolved and rejected need a note and close the request."""
    return _handle(db, employee, request_id,
                   lambda r: requests.change_status(db, r, body.status, employee.employee_id, body.note))


@router.post("/service-requests/{request_id}/notes")
def add_service_request_note(request_id: int, body: NoteRequest, db: Session = Depends(get_db),
                             employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """An internal note (never shown to the customer)."""
    return _handle(db, employee, request_id, lambda r: requests.add_note(db, r, body.note, employee.employee_id))


@router.get("/service-requests/{request_id}/photos/{file_ref}")
def service_request_photo(request_id: int, file_ref: str, db: Session = Depends(get_db),
                          employee: AuthenticatedEmployee = Depends(get_current_employee)):
    from app.storage import r2

    req = _request(db, employee, request_id)
    item = next((p for p in req.photos or [] if p["file_ref"] == file_ref), None)
    if item is None:
        raise HTTPException(status_code=404, detail="Photo not found")
    try:
        url, expires_at = r2.signed_get_url(item["storage_key"], filename=item["filename"])
    except r2.StorageNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail={"code": "storage_not_configured", "message": str(exc)})
    return {"url": url, "expires_at": expires_at.isoformat(), "filename": item["filename"], "file_ref": file_ref}


@router.post("/internal/check-service-requests")
def run_check_service_requests(db: Session = Depends(get_db), _: None = Depends(_require_admin_or_internal_key)):
    """Admin or INTERNAL_API_KEY (Cloud Scheduler). Emails Operations once about each open
    request past its SLA. Idempotent; an overlapping run answers
    {"status": "skipped", "reason": "already_running"}."""
    from app.jobs.locks import run_exclusive
    from app.jobs.service_request_alerts import check_service_requests
    return run_exclusive(db, "check-service-requests", lambda: check_service_requests(db, datetime.now(timezone.utc)))
