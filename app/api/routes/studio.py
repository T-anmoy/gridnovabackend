"""BE-18 — the Proposal Studio for staff: customers, proposals, the guided section edit,
pricing, the proof-check, approval, issue, delivery, revision, acceptance, conversion to a
lead, the Excel import, the optional assistant, the library and the dashboard summary.
Contract: docs/api/proposal_studio.md.

Who (config/access_control.yaml): studio.view to open the Studio (own proposals),
studio.view_all for everyone's, studio.edit to change and issue, studio.price for prices,
studio.approve for approval (never the preparer), studio.library.manage, studio.convert.
Proposal Desk and Admin hold all but approve; Management views and approves.

No route here calls an AI model, except suggest-from-notes when STUDIO_AI_FILL_ENABLED is on.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Optional, Union

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Response, UploadFile
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.routes.dashboard import _require_admin_or_internal_key
from app.auth.dependencies import AuthenticatedEmployee, get_current_employee
from app.memory.db import get_db
from app.memory.models import StudioCustomer, StudioLibraryItem, StudioProposal, StudioStatus
from app.security.rate_limit import RateLimitLog
from app.studio import service
from app.studio.service import StudioError

router = APIRouter(prefix="/api/v1/dashboard")

PREVIEW_LIMIT = 20                      # renders per employee per window (as the lead preview)
PREVIEW_WINDOW_SECONDS = 60
_preview_log = RateLimitLog(PREVIEW_WINDOW_SECONDS)
SIGNED_COPY_TYPES = ("application/pdf", "image/jpeg", "image/png")
MAX_SIGNED_COPY = 10 * 1024 * 1024
LIBRARY_KINDS = ("note", "customer_scope_item", "boq_preset", "template")


def _fail(exc: StudioError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.body())


def _need(employee: AuthenticatedEmployee, permission: str, what: str) -> None:
    if not employee.has(permission):
        raise HTTPException(status_code=403, detail={"code": "not_permitted", "message": f"Your role cannot {what}."})


def _proposal(db: Session, employee: AuthenticatedEmployee, proposal_id: int, *, lock: bool = False) -> StudioProposal:
    _need(employee, "studio.view", "open the Proposal Studio")
    p = service.lock_proposal(db, proposal_id) if lock else db.get(StudioProposal, proposal_id)
    if p is None:
        raise HTTPException(status_code=404, detail="Proposal not found")
    if p.owner_id != employee.employee_id and not employee.holds("studio.view_all"):
        raise HTTPException(status_code=403, detail={"code": "not_permitted", "message": "This proposal is not yours."})
    return p


def _commit_view(db: Session, p: StudioProposal, employee: AuthenticatedEmployee, extra: Optional[dict] = None) -> dict:
    db.commit()
    p = db.get(StudioProposal, p.id)
    return {**service.view(db, p, employee=employee), **(extra or {})}


def _run(db: Session, fn):
    try:
        return fn()
    except StudioError as exc:
        db.rollback()
        raise _fail(exc)


# ── Options ──────────────────────────────────────────────────────────────────────

@router.get("/studio/options")
def studio_options(employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """Everything the wizard's drop-downs need: project types, packages, phases, categories,
    DISCOMs, titles, technologies, item units and categories, sections and limits."""
    _need(employee, "studio.view", "open the Proposal Studio")
    from app.pricing import price_list
    from app.proposals import document as d
    from app.proposals import document_config
    from app.studio import config as studio_config
    from app.survey import config as survey_config
    cfg = studio_config.load()
    return {
        "project_types": [{"id": k, "label": v["label"], "layout": v["layout"]} for k, v in cfg["project_types"].items()],
        "packages": [{"id": p, "label": price_list.display_name(p)} for p in price_list.PACKAGES],
        "phases": ["single", "three"], "categories": list(d.CATEGORIES),
        "discoms": list(survey_config.load()["discom_options"]),
        "titles": document_config.load()["titles"], "salutations": document_config.load()["salutations"],
        "module_technologies": d.technologies(),
        "item_units": cfg["itemised"]["units"],
        "item_categories": [{"id": k, "label": v["label"], "gst_rate": str(v["gst_rate"])}
                            for k, v in cfg["itemised"]["categories"].items()],
        "sections": list(service.SECTIONS), "validity_days_default": studio_config.validity_days(),
        "price_modes": ["price_list", "staff_basic", "staff_total", "none"],
        "assistant_enabled": __import__("app.studio.assistant", fromlist=["enabled"]).enabled(),
    }


# ── Customers ────────────────────────────────────────────────────────────────────

class CustomerIn(BaseModel):
    customer_type: str = "individual"
    title: Optional[str] = None
    name: str = Field(max_length=200)
    attention: Optional[str] = Field(default=None, max_length=200)
    mobile: str = Field(max_length=20)
    email: Optional[str] = Field(default=None, max_length=254)
    address_lines: list[str] = Field(default_factory=list, max_length=5)
    gstin: Optional[str] = Field(default=None, max_length=20)


class CustomerPatch(BaseModel):
    customer_type: Optional[str] = None
    title: Optional[str] = None
    name: Optional[str] = Field(default=None, max_length=200)
    attention: Optional[str] = Field(default=None, max_length=200)
    mobile: Optional[str] = Field(default=None, max_length=20)
    email: Optional[str] = Field(default=None, max_length=254)
    address_lines: Optional[list[str]] = Field(default=None, max_length=5)
    gstin: Optional[str] = Field(default=None, max_length=20)


@router.get("/studio/customers")
def list_customers(q: Optional[str] = Query(default=None, max_length=100), limit: int = Query(default=20, ge=1, le=100),
                   db: Session = Depends(get_db), employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """Search by name, mobile (last digits) or GSTIN."""
    _need(employee, "studio.view", "open the Proposal Studio")
    return {"items": [service.customer_json(c) for c in service.search_customers(db, q, limit)]}


@router.post("/studio/customers", status_code=201)
def create_customer(body: CustomerIn, db: Session = Depends(get_db),
                    employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """A new Studio customer. Same mobile or GSTIN as an existing one is a warning
    (`duplicates`), never a refusal."""
    _need(employee, "studio.edit", "add Studio customers")
    c, dups = _run(db, lambda: service.create_customer(db, body.model_dump(), employee.employee_id))
    db.commit()
    return {"customer": service.customer_json(c), "duplicates": dups}


@router.get("/studio/customers/{customer_id}")
def get_customer(customer_id: int, db: Session = Depends(get_db),
                 employee: AuthenticatedEmployee = Depends(get_current_employee)):
    _need(employee, "studio.view", "open the Proposal Studio")
    c = db.get(StudioCustomer, customer_id)
    if c is None:
        raise HTTPException(status_code=404, detail="Customer not found")
    return {"customer": service.customer_json(c),
            "duplicates": service.duplicates(db, c.mobile, c.gstin, exclude_id=c.id)}


@router.patch("/studio/customers/{customer_id}")
def update_customer(customer_id: int, body: CustomerPatch, db: Session = Depends(get_db),
                    employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """Changes the customer record only; a proposal's printed addressee changes through its
    `addressee` section."""
    _need(employee, "studio.edit", "edit Studio customers")
    c = db.get(StudioCustomer, customer_id)
    if c is None:
        raise HTTPException(status_code=404, detail="Customer not found")
    dups = _run(db, lambda: service.update_customer(db, c, body.model_dump(exclude_unset=True), employee.employee_id))
    db.commit()
    return {"customer": service.customer_json(c), "duplicates": dups}


# ── Proposals ────────────────────────────────────────────────────────────────────

class ProposalIn(BaseModel):
    customer_id: int
    project_type: str = Field(max_length=32)
    template_id: Optional[int] = None


@router.get("/studio/proposals")
def list_proposals(status: Optional[str] = Query(default=None), project_type: Optional[str] = Query(default=None),
                   owner_id: Optional[str] = Query(default=None), date_from: Optional[date] = Query(default=None),
                   date_to: Optional[date] = Query(default=None), q: Optional[str] = Query(default=None, max_length=100),
                   page: int = Query(default=1, ge=1), page_size: int = Query(default=25, ge=1, le=100),
                   db: Session = Depends(get_db), employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """Filters: status, project type, owner, created date range, search on customer name /
    mobile / reference / code. Without studio.view_all only your own proposals."""
    _need(employee, "studio.view", "open the Proposal Studio")
    from app.studio import queries
    if status and status not in {s.value for s in StudioStatus}:
        raise HTTPException(status_code=422, detail={"code": "invalid_value", "message": "Unknown status."})
    if not employee.holds("studio.view_all"):
        owner_id = employee.employee_id
    return queries.proposals(db, status=status, project_type=project_type, owner_id=owner_id, date_from=date_from,
                             date_to=date_to, q=q, page=page, page_size=page_size)


@router.post("/studio/proposals", status_code=201)
def create_proposal(body: ProposalIn, db: Session = Depends(get_db),
                    employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """A draft for a customer and project type, the defaults generated (or from a template)."""
    _need(employee, "studio.edit", "create Studio proposals")
    c = db.get(StudioCustomer, body.customer_id)
    if c is None:
        raise HTTPException(status_code=404, detail="Customer not found")
    template = None
    if body.template_id is not None:
        template = db.get(StudioLibraryItem, body.template_id)
        if template is None or template.kind != "template" or not template.active:
            raise HTTPException(status_code=404, detail="Template not found")
    p = _run(db, lambda: service.create_proposal(db, customer=c, project_type=body.project_type,
                                                 owner_id=employee.employee_id, actor=employee.employee_id,
                                                 template=template))
    return _commit_view(db, p, employee)


@router.get("/studio/proposals/{proposal_id}")
def get_proposal(proposal_id: int, version: Optional[int] = Query(default=None, ge=1), db: Session = Depends(get_db),
                 employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """The proposal with one version (default the current): document, figures, proof-check
    (`checks`, errors block, warnings need acknowledging), approval rule and what you may do."""
    p = _proposal(db, employee, proposal_id)
    v = _run(db, lambda: service.version_of(db, p, version))
    return service.view(db, p, v, employee=employee)


class SectionEdit(BaseModel):
    """Replace one section. `value` is the section's object or list (see the contract)."""
    base_revision: int = Field(ge=0)
    section: str = Field(min_length=1, max_length=40)
    value: Union[dict[str, Any], list[Any], str, int, None] = None


@router.patch("/studio/proposals/{proposal_id}/document")
def edit_document(proposal_id: int, body: SectionEdit, db: Session = Depends(get_db),
                  employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """One section of the current draft. `price` needs studio.price. 409 proposal_locked /
    stale_version, 422 invalid_value / payment_terms_total / make_not_in_catalogue /
    reason_required. Returns the view plus `changes` and `regenerated_sections`."""
    _need(employee, "studio.edit", "edit Studio proposals")
    if body.section == "price":
        _need(employee, "studio.price", "set prices")       # sensitive: may answer two_factor_required
    p = _proposal(db, employee, proposal_id, lock=True)
    result = _run(db, lambda: service.edit(db, p, section=body.section, value=body.value,
                                           base_revision=body.base_revision, actor=employee.employee_id,
                                           can_price=body.section != "price" or employee.holds("studio.price")))
    return _commit_view(db, p, employee, result)


class Acknowledge(BaseModel):
    fingerprints: list[str] = Field(min_length=1, max_length=50)


@router.post("/studio/proposals/{proposal_id}/acknowledge-warnings")
def acknowledge_warnings(proposal_id: int, body: Acknowledge, db: Session = Depends(get_db),
                         employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """"I have checked this" for current warnings (by fingerprint); recorded in the event log."""
    _need(employee, "studio.edit", "edit Studio proposals")
    p = _proposal(db, employee, proposal_id, lock=True)
    _run(db, lambda: service.acknowledge(db, p, body.fingerprints, employee.employee_id))
    return _commit_view(db, p, employee)


class IgnoreWord(BaseModel):
    word: str = Field(min_length=1, max_length=40)


@router.post("/studio/proposals/{proposal_id}/spelling/ignore")
def ignore_word(proposal_id: int, body: IgnoreWord, db: Session = Depends(get_db),
                employee: AuthenticatedEmployee = Depends(get_current_employee)):
    _need(employee, "studio.edit", "edit Studio proposals")
    p = _proposal(db, employee, proposal_id, lock=True)
    _run(db, lambda: service.ignore_word(db, p, body.word, employee.employee_id))
    return _commit_view(db, p, employee)


@router.post("/studio/proposals/{proposal_id}/submit")
def submit(proposal_id: int, db: Session = Depends(get_db),
           employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """draft → pending_approval (approvers alerted) or → ready. 409 proof_check_failed /
    warnings_unacknowledged / content_not_ready."""
    _need(employee, "studio.edit", "submit Studio proposals")
    p = _proposal(db, employee, proposal_id, lock=True)
    status = _run(db, lambda: service.submit(db, p, employee.employee_id))
    return _commit_view(db, p, employee, {"result": status})


class Decision(BaseModel):
    comment: Optional[str] = Field(default=None, max_length=2000)


@router.post("/studio/proposals/{proposal_id}/approve")
def approve(proposal_id: int, body: Decision, db: Session = Depends(get_db),
            employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """By a holder of studio.approve who did not prepare this version (403 maker_checker)."""
    _need(employee, "studio.approve", "approve Studio proposals")
    p = _proposal(db, employee, proposal_id, lock=True)
    _run(db, lambda: service.approve(db, p, employee.employee_id, body.comment))
    return _commit_view(db, p, employee)


@router.post("/studio/proposals/{proposal_id}/reject")
def reject(proposal_id: int, body: Decision, db: Session = Depends(get_db),
           employee: AuthenticatedEmployee = Depends(get_current_employee)):
    _need(employee, "studio.approve", "reject Studio proposals")
    p = _proposal(db, employee, proposal_id, lock=True)
    _run(db, lambda: service.reject(db, p, employee.employee_id, body.comment or ""))
    return _commit_view(db, p, employee)


@router.post("/studio/proposals/{proposal_id}/issue")
def issue(proposal_id: int, db: Session = Depends(get_db),
          employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """Allocate the reference and date, render and store the PDF/A-3b (SHA-256 recorded),
    lock the version and create the customer's online link (returned once here as
    `public_url`; later from whatsapp-link / email). 409 approval_required /
    proof_check_failed / warnings_unacknowledged / content_not_ready; 503 storage_unavailable."""
    _need(employee, "studio.edit", "issue Studio proposals")
    p = _proposal(db, employee, proposal_id, lock=True)
    out = _run(db, lambda: service.issue(db, p, employee.employee_id))
    return _commit_view(db, p, employee, {"public_url": out["url"]})


@router.get("/studio/proposals/{proposal_id}/preview.pdf", response_class=Response,
            responses={200: {"content": {"application/pdf": {}}, "description": "The PDF"}})
def preview(proposal_id: int, version: Optional[int] = Query(default=None, ge=1), db: Session = Depends(get_db),
            employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """The version rendered now, never stored: stamped "DRAFT — NOT FOR CUSTOMER" until issued.
    An issued version: use /pdf for the stored file."""
    from app.proposals import document as d
    from app.proposals import document_render
    p = _proposal(db, employee, proposal_id)
    v = _run(db, lambda: service.version_of(db, p, version))
    if not _preview_log.hit(employee.employee_id, PREVIEW_LIMIT):
        raise HTTPException(status_code=429, detail={"code": "rate_limited",
                                                     "message": "Too many previews. Wait a minute and try again."})
    doc = service.doc_of(v)
    if d.figures(doc)["total"] is None:
        raise HTTPException(status_code=409, detail={"code": "missing_required",
                                                     "message": "Set a price (or the items) before previewing."})
    try:
        pdf = document_render.render(doc, reference=v.document_reference, doc_date=v.document_date or service.today_ist(),
                                     draft=v.issued_at is None, verify_url=service.verify_url(v))
    except document_render.DocumentRenderError as exc:
        raise HTTPException(status_code=409, detail={"code": "render_failed", "message": str(exc)})
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{p.code}-v{v.version}-DRAFT.pdf"',
                             "Cache-Control": "no-store"})


@router.get("/studio/proposals/{proposal_id}/pdf")
def issued_pdf(proposal_id: int, version: Optional[int] = Query(default=None, ge=1), db: Session = Depends(get_db),
               employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """The issued PDF: a redirect to a short-lived signed link (recorded as a staff download)."""
    from app.storage import r2
    p = _proposal(db, employee, proposal_id)
    v = _run(db, lambda: service.version_of(db, p, version))
    if not v.pdf_storage_key:
        raise HTTPException(status_code=409, detail={"code": "not_issued", "message": "This version is not issued."})
    try:
        url, _ = r2.signed_get_url(v.pdf_storage_key, filename=v.pdf_filename, as_attachment=True)
    except r2.StorageNotConfiguredError:
        raise HTTPException(status_code=503, detail={"code": "storage_unavailable", "message": "Storage unavailable."})
    service.event(db, p, "downloaded", version=v.version, actor=employee.employee_id, detail={"by": "staff"})
    db.commit()
    return RedirectResponse(url, status_code=302)


class EmailIn(BaseModel):
    to: Optional[str] = Field(default=None, max_length=254)


@router.post("/studio/proposals/{proposal_id}/email")
def email(proposal_id: int, body: EmailIn, db: Session = Depends(get_db),
          employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """Email the customer the PDF and the online link (Resend, from NOTIFY_FROM_EMAIL,
    reply-to the owner). 422 no_customer_email; 503 email_not_configured."""
    from app.studio import public
    _need(employee, "studio.edit", "send Studio proposals")
    p = _proposal(db, employee, proposal_id)
    out = _run(db, lambda: public.email(db, p, employee.employee_id, body.to))
    db.commit()
    return out


@router.post("/studio/proposals/{proposal_id}/whatsapp-link")
def whatsapp_link(proposal_id: int, db: Session = Depends(get_db),
                  employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """A wa.me link with the prefilled message and the online link (no WhatsApp API, no cost)."""
    from app.studio import public
    _need(employee, "studio.edit", "send Studio proposals")
    p = _proposal(db, employee, proposal_id)
    out = _run(db, lambda: public.whatsapp_link(db, p, employee.employee_id))
    db.commit()
    return out


@router.post("/studio/proposals/{proposal_id}/revise")
def revise(proposal_id: int, db: Session = Depends(get_db),
           employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """The next version, everything carried over; issued as <reference>-R1, -R2 …"""
    _need(employee, "studio.edit", "revise Studio proposals")
    p = _proposal(db, employee, proposal_id, lock=True)
    _run(db, lambda: service.revise(db, p, employee.employee_id))
    return _commit_view(db, p, employee)


class DuplicateIn(BaseModel):
    version: Optional[int] = Field(default=None, ge=1)
    customer_id: Optional[int] = None


@router.post("/studio/proposals/{proposal_id}/duplicate", status_code=201)
def duplicate(proposal_id: int, body: DuplicateIn, db: Session = Depends(get_db),
              employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """A new proposal from any version — for the same customer or another (customer_id)."""
    _need(employee, "studio.edit", "duplicate Studio proposals")
    p = _proposal(db, employee, proposal_id)
    customer = None
    if body.customer_id is not None:
        customer = db.get(StudioCustomer, body.customer_id)
        if customer is None:
            raise HTTPException(status_code=404, detail="Customer not found")
    q = _run(db, lambda: service.duplicate(db, p, version=body.version, customer=customer,
                                           owner_id=employee.employee_id, actor=employee.employee_id))
    return _commit_view(db, q, employee)


@router.post("/studio/proposals/{proposal_id}/mark-accepted")
async def mark_accepted(proposal_id: int, accepted_on: date = Form(...), signed_copy: UploadFile = File(...),
                        db: Session = Depends(get_db), employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """Accepted in person: the date and the signed copy (PDF, JPEG or PNG, ≤ 10 MB; stored and
    hashed). Offers conversion to a lead (`convert_offered`)."""
    _need(employee, "studio.edit", "record acceptance")
    raw = await signed_copy.read(MAX_SIGNED_COPY + 1)
    if signed_copy.content_type not in SIGNED_COPY_TYPES or not raw:
        raise HTTPException(status_code=422, detail={"code": "invalid_file", "message": "Upload a PDF, JPEG or PNG."})
    if len(raw) > MAX_SIGNED_COPY:
        raise HTTPException(status_code=413, detail={"code": "file_too_large", "message": "At most 10 MB."})
    magic = {b"%PDF": "application/pdf", b"\xff\xd8\xff": "image/jpeg", b"\x89PNG": "image/png"}
    if not any(raw.startswith(m) and t == signed_copy.content_type for m, t in magic.items()):
        raise HTTPException(status_code=422, detail={"code": "invalid_file", "message": "The file is not what it says."})
    p = _proposal(db, employee, proposal_id, lock=True)
    _run(db, lambda: service.mark_accepted_in_person(db, p, accepted_on=accepted_on, signed_copy=raw,
                                                     content_type=signed_copy.content_type,
                                                     filename=signed_copy.filename or "signed",
                                                     actor=employee.employee_id))
    return _commit_view(db, p, employee, {"convert_offered": p.lead_id is None})


class LostIn(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


@router.post("/studio/proposals/{proposal_id}/mark-lost")
def mark_lost(proposal_id: int, body: LostIn, db: Session = Depends(get_db),
              employee: AuthenticatedEmployee = Depends(get_current_employee)):
    _need(employee, "studio.edit", "mark Studio proposals lost")
    p = _proposal(db, employee, proposal_id, lock=True)
    _run(db, lambda: service.mark_lost(db, p, body.reason, employee.employee_id))
    return _commit_view(db, p, employee)


@router.post("/studio/proposals/{proposal_id}/convert")
def convert(proposal_id: int, db: Session = Depends(get_db),
            employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """Create the lead (and its proposal with the same document, reference, PDF and status).
    Idempotent: a second call returns the same lead with created=false."""
    from app.studio import convert as conversion
    _need(employee, "studio.convert", "create leads from Studio proposals")
    p = _proposal(db, employee, proposal_id, lock=True)
    out = _run(db, lambda: conversion.convert(db, p, employee.employee_id))
    db.commit()
    return out


@router.get("/studio/proposals/{proposal_id}/events")
def proposal_events(proposal_id: int, db: Session = Depends(get_db),
                    employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """The timeline: every staff action, customer view / download / accept / decline, reminder."""
    p = _proposal(db, employee, proposal_id)
    return {"items": service.events(db, p)}


@router.get("/studio/proposals/{proposal_id}/versions/{a}/diff/{b}")
def version_diff(proposal_id: int, a: int, b: int, db: Session = Depends(get_db),
                 employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """Field-level changes from version a to version b."""
    p = _proposal(db, employee, proposal_id)
    return {"from": a, "to": b, "changes": _run(db, lambda: service.version_diff(db, p, a, b))}


class NotesIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


@router.post("/studio/proposals/{proposal_id}/suggest-from-notes")
def suggest_from_notes(proposal_id: int, body: NotesIn, db: Session = Depends(get_db),
                       employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """Optional and off by default (409 assistant_disabled, no model call). Suggestions for
    customer / site / connection fields only, unsaved, each marked "check"."""
    from app.studio import assistant
    _need(employee, "studio.edit", "use the assistant")
    p = _proposal(db, employee, proposal_id)
    if not assistant.enabled():
        raise HTTPException(status_code=409, detail={"code": "assistant_disabled",
                                                     "message": "The assistant is switched off."})
    out = assistant.suggest(body.text)
    service.event(db, p, "assistant_suggested", version=p.current_version, actor=employee.employee_id,
                  detail={"fields": [f"{s['section']}.{s['field']}" for s in out["suggestions"]],
                          "dropped": out["dropped"], "model": out["model"]})
    db.commit()
    return out


class TemplateIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)


@router.post("/studio/proposals/{proposal_id}/save-as-template", status_code=201)
def save_as_template(proposal_id: int, body: TemplateIn, db: Session = Depends(get_db),
                     employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """The current version's document minus customer data, into the library."""
    _need(employee, "studio.library.manage", "manage the Studio library")
    p = _proposal(db, employee, proposal_id)
    item = service.save_as_template(db, p, body.name, employee.employee_id)
    db.commit()
    return _library_json(item)


# ── Library ──────────────────────────────────────────────────────────────────────

class LibraryIn(BaseModel):
    kind: str
    name: str = Field(min_length=2, max_length=120)
    project_type: Optional[str] = None
    body: dict[str, Any]


class LibraryPatch(BaseModel):
    name: Optional[str] = Field(default=None, min_length=2, max_length=120)
    body: Optional[dict[str, Any]] = None
    active: Optional[bool] = None


def _library_json(item: StudioLibraryItem) -> dict:
    return {"id": item.id, "kind": item.kind, "name": item.name, "project_type": item.project_type, "body": item.body,
            "active": item.active, "created_by": item.created_by,
            "created_at": service._aware(item.created_at).isoformat()}


def _library_body(kind: str, body: dict) -> dict:
    """notes / scope items: {"text"}; BOQ presets: {"rows": [BOQ rows]}; templates: {"document"}."""
    from app.proposals import document as d
    try:
        if kind in ("note", "customer_scope_item"):
            text = d._bullets([str(body.get("text") or "")], kind)
            if not text:
                raise ValueError("text is required")
            return {"text": text[0]}
        if kind == "boq_preset":
            return {"rows": [d.BoqRow.model_validate(r).model_dump(mode="json") for r in body.get("rows") or []]}
        doc = d.StudioDocument.model_validate(body.get("document") or {})
        return {"document": d.dump(doc)}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"code": "invalid_value", "message": str(exc)[:300]})


@router.get("/studio/library")
def list_library(kind: Optional[str] = Query(default=None), project_type: Optional[str] = Query(default=None),
                 include_retired: bool = Query(default=False), db: Session = Depends(get_db),
                 employee: AuthenticatedEmployee = Depends(get_current_employee)):
    _need(employee, "studio.view", "open the Proposal Studio")
    q = select(StudioLibraryItem).order_by(StudioLibraryItem.kind, StudioLibraryItem.name)
    if kind:
        q = q.where(StudioLibraryItem.kind == kind)
    if project_type:
        q = q.where((StudioLibraryItem.project_type == project_type) | StudioLibraryItem.project_type.is_(None))
    if not include_retired:
        q = q.where(StudioLibraryItem.active.is_(True))
    return {"items": [_library_json(i) for i in db.execute(q).scalars()]}


@router.post("/studio/library", status_code=201)
def create_library_item(body: LibraryIn, db: Session = Depends(get_db),
                        employee: AuthenticatedEmployee = Depends(get_current_employee)):
    from app.studio import config as studio_config
    _need(employee, "studio.library.manage", "manage the Studio library")
    if body.kind not in LIBRARY_KINDS:
        raise HTTPException(status_code=422, detail={"code": "invalid_value", "message": f"kind: {LIBRARY_KINDS}"})
    if body.project_type is not None and body.project_type not in studio_config.PROJECT_TYPES:
        raise HTTPException(status_code=422, detail={"code": "invalid_value", "message": "Unknown project type."})
    item = StudioLibraryItem(kind=body.kind, name=body.name.strip(), project_type=body.project_type,
                             body=_library_body(body.kind, body.body), created_by=employee.employee_id,
                             created_at=service.now())
    db.add(item)
    db.commit()
    return _library_json(item)


@router.patch("/studio/library/{item_id}")
def update_library_item(item_id: int, body: LibraryPatch, db: Session = Depends(get_db),
                        employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """Rename, replace the body, or retire (active=false) — never deleted."""
    _need(employee, "studio.library.manage", "manage the Studio library")
    item = db.get(StudioLibraryItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Library item not found")
    if body.name is not None:
        item.name = body.name.strip()
    if body.body is not None:
        item.body = _library_body(item.kind, body.body)
    if body.active is not None:
        item.active = body.active
    item.updated_at = service.now()
    db.commit()
    return _library_json(item)


# ── Summary, import ──────────────────────────────────────────────────────────────

@router.get("/studio/summary")
def summary(owner_id: Optional[str] = Query(default=None), db: Session = Depends(get_db),
            employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """Drafts, awaiting approval, issued this month (count, value), viewed-not-accepted,
    accepted (count, value), 90-day win rate, expiring in 7 days, median days to accept."""
    from app.studio import queries
    _need(employee, "studio.view", "open the Proposal Studio")
    if not employee.holds("studio.view_all"):
        owner_id = employee.employee_id
    return queries.summary(db, owner_id=owner_id)


@router.get("/studio/import-template.xlsx", response_class=Response,
            responses={200: {"content": {"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": {}},
                             "description": "The workbook"}})
def import_template(employee: AuthenticatedEmployee = Depends(get_current_employee)):
    from app.studio import importer
    _need(employee, "studio.edit", "import Studio proposals")
    return Response(content=importer.template(),
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": 'attachment; filename="GridNova_Proposal_Studio_Import.xlsx"',
                             "Cache-Control": "no-store"})


@router.post("/studio/import", status_code=201)
async def import_sheet(file: UploadFile = File(...), db: Session = Depends(get_db),
                       employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """One draft per filled 'Proposal…' sheet, all or nothing. 422 {code: import_failed,
    errors: [{sheet, cell, field, message}]}."""
    from app.studio import importer
    _need(employee, "studio.edit", "import Studio proposals")
    raw = await file.read(importer.MAX_BYTES + 1)
    try:
        importer.check_file(raw, file.filename or "", file.content_type)
        rows = importer.load(raw)
        created = importer.create(db, rows, actor=employee.employee_id, can_price=employee.holds("studio.price"))
    except importer.ImportError_ as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail={"code": "import_failed", "message": "Nothing was imported.",
                                                     "errors": exc.errors})
    db.commit()
    return {"created": created}


# ── The daily job ────────────────────────────────────────────────────────────────

@router.post("/internal/check-studio-reminders")
def run_check_studio_reminders(db: Session = Depends(get_db), _: None = Depends(_require_admin_or_internal_key)):
    """Admin or INTERNAL_API_KEY (Cloud Scheduler, gridnova-jobs). Reminds owners of proposals
    not opened and about to expire, expires the overdue ones. Idempotent."""
    from app.jobs.locks import run_exclusive
    from app.studio import reminders

    def job() -> dict:
        out = reminders.run(db)
        db.commit()
        return out
    return run_exclusive(db, "check-studio-reminders", job)
