"""The customer's handover page and service requests, by the random token in their handover
email (BE-5). Unauthenticated; the token is the credential, so:

  * 32+ random bytes, only a SHA-256 stored, revocable by staff, optionally expiring;
  * an unknown, revoked or expired token — and a token on a project that is not handed over
    — are all the same 404;
  * rate-limited per IP (the shared RateLimitLog, like the receipt and proposal links);
  * every response carries `X-Robots-Tag: noindex, nofollow` and `Cache-Control: no-store`;
  * customer photos are typed by their bytes (uploads.read_document), size-limited and have
    their EXIF (GPS included) stripped before storage.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.associates import uploads
from app.associates.service import AssociateError
from app.handover import config, link, pdf, requests, service, warranty as warranty_register
from app.memory.db import get_db
from app.memory.models import Customer, CustomerOnboarding, Project, ProposalInputs, ServiceRequest, ServiceRequestCategory
from app.security.client_ip import client_ip
from app.security.rate_limit import RateLimitLog

router = APIRouter(prefix="/api/v1/public/handover")
_HEADERS = {"X-Robots-Tag": "noindex, nofollow", "Cache-Control": "no-store"}
# Reads match the receipt / proposal links; raising a request is stricter.
_READ_LIMIT, _READ_WINDOW_SECONDS = 30, 60
_WRITE_LIMIT, _WRITE_WINDOW_SECONDS = 5, 600
_read_log = RateLimitLog(_READ_WINDOW_SECONDS)
_write_log = RateLimitLog(_WRITE_WINDOW_SECONDS)


def _not_found() -> HTTPException:
    return HTTPException(status_code=404, detail="Not found", headers=_HEADERS)


def _pack(db: Session, request: Request, token: str, *, write: bool = False):
    log, limit = (_write_log, _WRITE_LIMIT) if write else (_read_log, _READ_LIMIT)
    if not log.hit(client_ip(request), limit):
        raise HTTPException(status_code=429, detail="Too many requests. Please slow down.", headers=_HEADERS)
    pack = link.find(db, token)
    if pack is None:
        raise _not_found()
    return pack, db.get(CustomerOnboarding, pack.onboarding_id)


def _json(body, status_code: int = 200) -> JSONResponse:
    return JSONResponse(body, status_code=status_code, headers=_HEADERS)


@router.get("/{token}")
def handover_page(token: str, request: Request, db: Session = Depends(get_db)):
    """What the customer's page shows: their system, the warranty register, how to get
    service, and whether the PDF can be downloaded."""
    from app.execution import register

    pack, onboarding = _pack(db, request, token)
    customer = db.get(Customer, onboarding.customer_id)
    inputs = db.get(ProposalInputs, onboarding.proposal_inputs_id)
    contact = pdf.service_contact()
    capacity = register.capacity(db, onboarding)
    return _json({
        "customer_name": customer.name if customer else None,
        "project_reference_id": db.get(Project, onboarding.project_id).reference_id,
        "site_address": inputs.full_address if inputs else None,
        "system": {"installed_module_kw": capacity["installed_module_kw"],
                   "connection_phase": inputs.connection_phase.value if inputs and inputs.connection_phase else None},
        "warranty": warranty_register.serialize(db, warranty_register.ensure(db, onboarding, "system")),
        "service": {"phone": contact["phone"], "email": contact["email"], "sla_hours": config.service()["sla_hours"],
                    "categories": [c.value for c in ServiceRequestCategory], "max_photos": config.service()["max_photos"]},
        "pdf_available": bool(pack.pdf_key) and pdf.hold_reason(db) is None,
        "link_expires_at": pack.link_expires_at.isoformat() if pack.link_expires_at else None,
    })


@router.get("/{token}/pdf")
def handover_pdf(token: str, request: Request, db: Session = Depends(get_db)):
    """302 to a short-lived signed URL for the handover PDF. 404 while there is none (not
    rendered yet, or held because the pack still contains placeholder text)."""
    from app.storage import r2

    pack, _ = _pack(db, request, token)
    if not pack.pdf_key or pdf.hold_reason(db) is not None:
        raise _not_found()
    try:
        url, _expires = r2.signed_get_url(pack.pdf_key, filename="handover-pack.pdf", as_attachment=True)
    except r2.StorageNotConfiguredError:
        raise HTTPException(status_code=503, detail="Handover packs are temporarily unavailable.", headers=_HEADERS)
    return RedirectResponse(url, status_code=302, headers=_HEADERS)


@router.get("/{token}/service-requests")
def list_requests(token: str, request: Request, db: Session = Depends(get_db)):
    pack, onboarding = _pack(db, request, token)
    rows = db.execute(select(ServiceRequest).where(ServiceRequest.onboarding_id == onboarding.id)
                      .order_by(ServiceRequest.created_at.desc(), ServiceRequest.id.desc())).scalars()
    return _json({"items": [requests.request_json(db, r, audience="customer") for r in rows]})


@router.post("/{token}/service-requests", status_code=201)
def raise_request(token: str, request: Request, category: ServiceRequestCategory = Form(...),
                  description: str = Form(..., max_length=requests.MAX_DESCRIPTION),
                  contact_phone: Optional[str] = Form(default=None, max_length=32),
                  files: list[UploadFile] = File(default=[]), db: Session = Depends(get_db)):
    """The customer raises a service request (multipart; up to service.max_photos images)."""
    pack, onboarding = _pack(db, request, token, write=True)
    try:
        photos = requests.read_photos(files)
        req = requests.raise_request(db, onboarding, via="customer", actor="customer", category=category.value,
                                     description=description, contact_phone=contact_phone, photos=photos)
    except uploads.UploadRejected as exc:
        db.rollback()
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": str(exc)},
                            headers=_HEADERS)
    except AssociateError as exc:
        db.rollback()
        raise HTTPException(status_code=exc.status_code, detail=exc.body(), headers=_HEADERS)
    db.commit()
    return _json(requests.request_json(db, req, audience="customer"), 201)
