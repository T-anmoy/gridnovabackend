"""Handover pack, warranty register and service requests for associates (BE-5).

Visibility follows OnboardingScope (app/auth/scoping.py): the owner, and a Preferred
Associate whose territory holds the site, may read a customer's pack, warranty and service
requests; only the owning associate raises a request. Associates see no link state, no
email state and no internal notes. Nothing here carries money.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.associates import uploads
from app.associates.service import AssociateError
from app.auth.dependencies import AuthenticatedAssociate, get_current_associate
from app.auth.scoping import can_edit_onboarding, require_onboarding_documents, require_onboarding_edit, require_onboarding_view
from app.handover import pack as pack_module, requests, service
from app.memory.db import get_db
from app.memory.models import Associate, CustomerOnboarding, ServiceRequest, ServiceRequestCategory
from app.onboarding import service as onboarding_service

router = APIRouter(prefix="/api/v1/associate/onboardings/{onboarding_id}")


def _error(exc: AssociateError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.body())


def _load(db: Session, current: AuthenticatedAssociate, onboarding_id: int, *, edit: bool = False):
    associate = db.get(Associate, current.associate_pk)
    onboarding = db.get(CustomerOnboarding, onboarding_id)
    if onboarding is None:
        raise HTTPException(status_code=404, detail="Customer not found")
    (require_onboarding_edit if edit else require_onboarding_view)(associate, onboarding)
    return associate, onboarding


@router.get("/handover")
def handover_pack(onboarding_id: int, db: Session = Depends(get_db),
                  current: AuthenticatedAssociate = Depends(get_current_associate)):
    """The handover checklist, the warranty register (once handed over) and the PDF state."""
    associate, onboarding = _load(db, current, onboarding_id)
    body = service.detail(db, onboarding, staff=False)
    return {**body, "can_raise_service_request": can_edit_onboarding(associate, onboarding) and body["handover_recorded"]}


@router.get("/handover/pdf")
def handover_pdf(onboarding_id: int, db: Session = Depends(get_db),
                 current: AuthenticatedAssociate = Depends(get_current_associate)):
    """A short-lived signed URL for the handover PDF — the owner (a territory viewer only
    when territory_viewers_see_documents is on)."""
    from app.storage import r2

    associate = db.get(Associate, current.associate_pk)
    onboarding = db.get(CustomerOnboarding, onboarding_id)
    if onboarding is None:
        raise HTTPException(status_code=404, detail="Customer not found")
    require_onboarding_documents(associate, onboarding)
    pack = pack_module.find(db, onboarding.project_id)
    if pack is None or not pack.pdf_key:
        raise HTTPException(status_code=404, detail="The handover PDF is not available.")
    try:
        url, expires_at = r2.signed_get_url(pack.pdf_key, filename="handover-pack.pdf")
    except r2.StorageNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail={"code": "storage_not_configured", "message": str(exc)})
    return {"url": url, "expires_at": expires_at.isoformat()}


@router.get("/service-requests")
def list_service_requests(onboarding_id: int, db: Session = Depends(get_db),
                          current: AuthenticatedAssociate = Depends(get_current_associate)):
    associate, onboarding = _load(db, current, onboarding_id)
    rows = db.execute(select(ServiceRequest).where(ServiceRequest.onboarding_id == onboarding.id)
                      .order_by(ServiceRequest.created_at.desc(), ServiceRequest.id.desc())).scalars()
    return {"items": [requests.request_json(db, r, audience="associate") for r in rows],
            "can_raise": can_edit_onboarding(associate, onboarding) and pack_module.handed_over(db, onboarding)}


@router.post("/service-requests", status_code=201)
def raise_service_request(onboarding_id: int, category: ServiceRequestCategory = Form(...),
                          description: str = Form(..., max_length=requests.MAX_DESCRIPTION),
                          contact_phone: Optional[str] = Form(default=None, max_length=32),
                          files: list[UploadFile] = File(default=[]), db: Session = Depends(get_db),
                          current: AuthenticatedAssociate = Depends(get_current_associate)):
    """The owning associate raises a request for their customer (multipart; up to
    service.max_photos images in `files`). 409 not_handed_over before the handover."""
    associate, onboarding = _load(db, current, onboarding_id, edit=True)
    try:
        photos = requests.read_photos(files)
        req = requests.raise_request(db, onboarding, via="associate", actor=onboarding_service.actor(associate),
                                     category=category.value, description=description, contact_phone=contact_phone,
                                     photos=photos)
    except uploads.UploadRejected as exc:
        db.rollback()
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": str(exc)})
    except AssociateError as exc:
        db.rollback()
        raise _error(exc)
    db.commit()
    return requests.request_json(db, req, audience="associate")


@router.get("/service-requests/{request_id}")
def service_request(onboarding_id: int, request_id: int, db: Session = Depends(get_db),
                    current: AuthenticatedAssociate = Depends(get_current_associate)):
    _, onboarding = _load(db, current, onboarding_id)
    req = db.get(ServiceRequest, request_id)
    if req is None or req.onboarding_id != onboarding.id:
        raise HTTPException(status_code=404, detail="Service request not found")
    return requests.request_json(db, req, audience="associate")
