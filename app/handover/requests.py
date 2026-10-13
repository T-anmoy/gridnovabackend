"""Service requests (warranty / O&M) after handover.

Raised by staff on a customer's behalf, by the owning associate, or by the customer through
their link. `within_warranty` is computed from the warranty register when the request is
raised (NULL when the project has none) and kept; status moves open → in_progress →
resolved | rejected, a resolved or rejected request is closed; every change appends to
service_request_events (a trigger refuses UPDATE and DELETE there). Customer photos have
their EXIF (GPS included) removed before they are stored.
"""
from __future__ import annotations

import io
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.associates.service import AssociateError
from app.handover import config, pack as pack_module, warranty as warranty_register
from app.memory.models import (
    AuditLog, Customer, CustomerOnboarding, Employee, EmployeeRole, Project, ServiceRequest, ServiceRequestCategory,
    ServiceRequestEvent, ServiceRequestStatus,
)

OPEN = (ServiceRequestStatus.open, ServiceRequestStatus.in_progress)
TRANSITIONS = {
    ServiceRequestStatus.open: (ServiceRequestStatus.in_progress, ServiceRequestStatus.resolved,
                                ServiceRequestStatus.rejected),
    ServiceRequestStatus.in_progress: (ServiceRequestStatus.resolved, ServiceRequestStatus.rejected),
}
VIAS = ("staff", "associate", "customer")
MAX_DESCRIPTION = 2000
MAX_NOTE = 2000
ASSIGNEE_ROLES = (EmployeeRole.admin, EmployeeRole.operations, EmployeeRole.engineer)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    return None if dt is None else (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc))


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return _aware(dt).isoformat() if dt else None


def strip_exif(content: bytes, content_type: str) -> bytes:
    """Re-encode an image without any metadata (a customer's phone photo carries GPS)."""
    from PIL import Image

    fmt = {"image/jpeg": "JPEG", "image/png": "PNG", "image/webp": "WEBP"}[content_type]
    with Image.open(io.BytesIO(content)) as img:
        img.load()
        clean = img.copy()
        clean.info = {}
        out = io.BytesIO()
        clean.save(out, format=fmt)
        return out.getvalue()


def _event(db: Session, req: ServiceRequest, kind: str, actor: str, **kw) -> None:
    db.add(ServiceRequestEvent(request_id=req.id, kind=kind, actor=actor, at=_now(), **kw))


def raise_request(db: Session, onboarding: CustomerOnboarding, *, via: str, actor: str, category: str,
                  description: str, contact_phone: Optional[str] = None, photos: Optional[list] = None) -> ServiceRequest:
    """`photos`: uploads.Document objects (already type-checked and size-limited)."""
    from app.memory.memory_manager import MemoryManager
    from app.notifications import notifier as notifier_module
    from app.storage import r2

    if not pack_module.handed_over(db, onboarding):
        raise AssociateError("A service request can be raised once the project is handed over.",
                             code="not_handed_over", status_code=409)
    try:
        cat = ServiceRequestCategory(category)
    except ValueError:
        raise AssociateError("Choose a valid category.", code="invalid_category", status_code=422, field="category")
    description = (description or "").strip()
    if not description or len(description) > MAX_DESCRIPTION:
        raise AssociateError(f"Describe the problem in 1 to {MAX_DESCRIPTION} characters.", code="invalid_description",
                             status_code=422, field="description")
    photos = photos or []
    if len(photos) > config.service()["max_photos"]:
        raise AssociateError(f"At most {config.service()['max_photos']} photos can be attached.",
                             code="too_many_photos", status_code=422, field="files")
    phone = (contact_phone or "").strip()[:32] or None
    warranty = warranty_register.ensure(db, onboarding, actor)
    now = _now()
    sla = config.service()["sla_hours"]
    req = ServiceRequest(project_id=onboarding.project_id, onboarding_id=onboarding.id, category=cat,
                         description=description, status=ServiceRequestStatus.open, raised_via=via, raised_by=actor,
                         contact_phone=phone, within_warranty=warranty_register.within_warranty(warranty),
                         warranty_end=warranty.system_warranty_end if warranty else None, photos=[],
                         sla_due_at=now + timedelta(hours=sla) if sla else None, created_at=now)
    db.add(req)
    db.flush()
    stored = []
    for doc in photos:
        content = strip_exif(doc.content, doc.content_type) if via == "customer" else doc.content
        key = f"service-requests/{req.id}/{uuid.uuid4().hex}{doc.extension}"
        try:
            r2.upload_bytes(key, content, doc.content_type)
        except r2.StorageNotConfiguredError as exc:
            raise AssociateError(str(exc), code="storage_not_configured", status_code=503)
        stored.append({"file_ref": uuid.uuid4().hex, "filename": doc.filename, "storage_key": key,
                       "content_type": doc.content_type, "size_bytes": len(content), "uploaded_by": actor,
                       "received_at": now.isoformat()})
    req.photos = stored
    _event(db, req, "raised", actor, to_status=req.status.value,
           detail={"via": via, "category": cat.value, "photos": len(stored), "within_warranty": req.within_warranty})
    db.add(AuditLog(entity_type="service_request", entity_id=str(req.id), action="raised", performed_by=actor,
                    after_value={"onboarding_id": onboarding.id, "category": cat.value, "via": via}))
    db.flush()
    payload = {"request_id": req.id, "onboarding_id": onboarding.id, "category": cat.value, "raised_via": via,
               "within_warranty": req.within_warranty,
               "customer_name": (db.get(Customer, onboarding.customer_id) or Customer(name=None)).name,
               "project_reference_id": db.get(Project, onboarding.project_id).reference_id}
    MemoryManager(db).notify_after_commit(
        lambda: notifier_module.notifier.notify("service_request_raised", payload), "service_request_raised")
    return req


def _closed(req: ServiceRequest) -> bool:
    return req.status not in OPEN


def change_status(db: Session, req: ServiceRequest, to: str, actor: str, note: Optional[str] = None) -> ServiceRequest:
    try:
        target = ServiceRequestStatus(to)
    except ValueError:
        raise AssociateError("Choose a valid status.", code="invalid_status", status_code=422, field="status")
    allowed = TRANSITIONS.get(req.status, ())
    if target not in allowed:
        raise AssociateError(f"A {req.status.value.replace('_', ' ')} request cannot move to "
                             f"{target.value.replace('_', ' ')}.", code="invalid_transition", status_code=409)
    note = (note or "").strip() or None
    if note and len(note) > MAX_NOTE:
        raise AssociateError(f"The note is longer than {MAX_NOTE} characters.", code="note_too_long", status_code=422,
                             field="note")
    if target in (ServiceRequestStatus.resolved, ServiceRequestStatus.rejected) and not note:
        raise AssociateError("Say what was done (or why it was rejected).", code="note_required", status_code=422,
                             field="note")
    before = req.status
    req.status, req.updated_at = target, _now()
    if _closed(req):
        req.closed_at, req.resolution_note = req.updated_at, note
    _event(db, req, "status", actor, from_status=before.value, to_status=target.value, note=note)
    db.add(AuditLog(entity_type="service_request", entity_id=str(req.id), action="status_changed", performed_by=actor,
                    before_value={"status": before.value}, after_value={"status": target.value, "note": note}))
    db.flush()
    return req


def assign(db: Session, req: ServiceRequest, assignee_id: Optional[str], actor: str) -> ServiceRequest:
    if _closed(req):
        raise AssociateError("This request is closed.", code="request_closed", status_code=409)
    if assignee_id is not None:
        employee = db.get(Employee, assignee_id)
        if employee is None or not employee.is_active or employee.role not in ASSIGNEE_ROLES:
            raise AssociateError("Choose an Admin, Operations or Engineer employee.", code="invalid_assignee",
                                 status_code=422, field="assignee_id")
    before = req.assignee_id
    req.assignee_id, req.updated_at = assignee_id, _now()
    _event(db, req, "assigned", actor, detail={"from": before, "to": assignee_id})
    db.add(AuditLog(entity_type="service_request", entity_id=str(req.id), action="assigned", performed_by=actor,
                    before_value={"assignee_id": before}, after_value={"assignee_id": assignee_id}))
    db.flush()
    return req


def add_note(db: Session, req: ServiceRequest, note: str, actor: str) -> ServiceRequest:
    note = (note or "").strip()
    if not note or len(note) > MAX_NOTE:
        raise AssociateError(f"Write a note of 1 to {MAX_NOTE} characters.", code="invalid_note", status_code=422,
                             field="note")
    req.updated_at = _now()
    _event(db, req, "note", actor, note=note)
    db.flush()
    return req


# ── Reading ──────────────────────────────────────────────────────────────────────

def is_overdue(req: ServiceRequest, now: Optional[datetime] = None) -> bool:
    return bool(req.sla_due_at is not None and req.status in OPEN and _aware(req.sla_due_at) < (now or _now()))


def request_json(db: Session, req: ServiceRequest, *, audience: str, events: bool = False) -> dict:
    """audience: staff | associate | customer. The customer sees status and category, never
    internal notes, the assignee or who else was involved."""
    body = {"request_id": req.id, "onboarding_id": req.onboarding_id, "category": req.category.value,
            "description": req.description, "status": req.status.value, "raised_via": req.raised_via,
            "within_warranty": req.within_warranty, "warranty_end": req.warranty_end.isoformat() if req.warranty_end else None,
            "photo_count": len(req.photos or []), "created_at": _iso(req.created_at), "closed_at": _iso(req.closed_at),
            "resolution_note": req.resolution_note, "sla_due_at": _iso(req.sla_due_at),
            "overdue": is_overdue(req)}
    if audience != "customer":
        body["raised_by"] = req.raised_by
        body["assignee_id"] = req.assignee_id
        body["contact_phone"] = req.contact_phone
        body["updated_at"] = _iso(req.updated_at)
        body["photos"] = [{"file_ref": p["file_ref"], "filename": p["filename"], "content_type": p["content_type"],
                           "size_bytes": p["size_bytes"], "received_at": p["received_at"]} for p in req.photos or []]
    if events and audience == "staff":
        rows = db.execute(select(ServiceRequestEvent).where(ServiceRequestEvent.request_id == req.id)
                          .order_by(ServiceRequestEvent.id)).scalars()
        body["history"] = [{"kind": e.kind, "actor": e.actor, "from_status": e.from_status, "to_status": e.to_status,
                            "note": e.note, "detail": e.detail, "at": _iso(e.at)} for e in rows]
    return body


def overdue_filter(now: datetime):
    return (ServiceRequest.status.in_(OPEN), ServiceRequest.sla_due_at.is_not(None), ServiceRequest.sla_due_at < now)


def read_photos(files: list) -> list:
    """Type-check and size-limit the uploaded photos (JPEG / PNG / WEBP, the registration
    documents' limit). Raises uploads.UploadRejected, or AssociateError for too many."""
    from app.associates import uploads

    files = [f for f in files or [] if getattr(f, "filename", None)]
    if len(files) > config.service()["max_photos"]:
        raise AssociateError(f"At most {config.service()['max_photos']} photos can be attached.",
                             code="too_many_photos", status_code=422, field="files")
    return [uploads.read_document_sync(f, "photo", allowed=uploads.IMAGE_TYPES) for f in files]
