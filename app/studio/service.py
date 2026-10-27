"""The Proposal Studio (BE-18): customers, proposals and their versions, the guided edit,
pricing, the proof-check, approval, issue, revision, duplication, acceptance and the event
log. Every figure comes from app/proposals/document.figures; the PDF from
app/proposals/document_render. No AI call anywhere in this module.

Roles are checked by the routes (app/api/routes/studio.py); this module enforces the rules
that hold whoever calls it: a locked version never changes, a stale edit is refused, the
approver is never the preparer, a reference is allocated once from the counter lead
proposals use, and the issued PDF passes the proof-check on its own bytes.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
from collections import OrderedDict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.memory.models import (
    Employee, StudioCustomer, StudioEvent, StudioLibraryItem, StudioProposal, StudioProposalVersion, StudioStatus,
)
from app.proposals import document as d
from app.studio import checks as proof
from app.studio import config as studio_config

EDITABLE = (StudioStatus.draft, StudioStatus.pending_approval, StudioStatus.ready)
OPEN_FOR_CUSTOMER = (StudioStatus.issued, StudioStatus.viewed)
# Sections the BE-13 engine edits (app/proposals/document.apply_edit) and the Studio's own.
BASE_SECTIONS = ("addressee", "connection", "site", "pm_surya_ghar", "plant", "system", "boq", "customer_scope",
                 "payment_terms", "notes")
STUDIO_SECTIONS = ("project", "items", "display", "savings", "notes_free", "cover_highlight", "validity_days", "price")
SECTIONS = BASE_SECTIONS + STUDIO_SECTIONS
# What counts as preparing a version (maker–checker: none of these actors may approve it).
PREPARER_EVENTS = ("created", "edited", "priced", "submitted_for_approval", "revised", "duplicated")
CODE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"     # no 0/O, 1/I


class StudioError(Exception):
    """A structured 4xx: {"code", "message", "field"?, …}."""

    def __init__(self, code: str, message: str, status_code: int = 409, field: Optional[str] = None,
                 extra: Optional[dict] = None):
        super().__init__(message)
        self.code, self.message, self.status_code, self.field = code, message, status_code, field
        self.extra = extra or {}

    def body(self) -> dict:
        out = {"code": self.code, "message": self.message, **self.extra}
        if self.field:
            out["field"] = self.field
        return out


def now() -> datetime:
    return datetime.now(timezone.utc)


def today_ist(at: Optional[datetime] = None) -> date:
    from app.proposals.reference import today_ist as _today
    return _today(at)


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    return None if dt is None else (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc))


# ── Events ──────────────────────────────────────────────────────────────────────

def event(db: Session, proposal: StudioProposal, type_: str, *, version: Optional[int] = None,
          actor_type: str = "employee", actor: Optional[str] = None, detail: Optional[dict] = None,
          ip: Optional[str] = None, user_agent: Optional[str] = None) -> StudioEvent:
    row = StudioEvent(proposal_id=proposal.id, version=version, type=type_, actor_type=actor_type, actor=actor,
                      detail=detail, ip=ip, user_agent=(user_agent or "")[:400] or None, at=now())
    db.add(row)
    return row


def events(db: Session, proposal: StudioProposal) -> list[dict]:
    rows = db.execute(select(StudioEvent).where(StudioEvent.proposal_id == proposal.id)
                      .order_by(StudioEvent.at, StudioEvent.id)).scalars()
    return [{"id": e.id, "version": e.version, "type": e.type, "actor_type": e.actor_type, "actor": e.actor,
             "detail": e.detail, "at": _aware(e.at).isoformat()} for e in rows]


# ── Customers ───────────────────────────────────────────────────────────────────

def normalize_mobile(raw: Optional[str]) -> Optional[str]:
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    return digits or None


def clean_customer(data: dict) -> dict:
    """Validate and normalise a customer's fields (422 on anything wrong)."""
    out = {}
    ctype = data.get("customer_type") or "individual"
    if ctype not in ("individual", "organisation"):
        raise StudioError("invalid_value", "customer_type must be individual or organisation.", 422, "customer_type")
    out["customer_type"] = ctype
    title = data.get("title") or None
    from app.proposals import document_config
    if title is not None and title not in document_config.load()["titles"]:
        raise StudioError("invalid_value", f"title must be one of {document_config.load()['titles']}.", 422, "title")
    out["title"] = title
    name = " ".join(str(data.get("name") or "").split())
    if not 2 <= len(name) <= 200:
        raise StudioError("invalid_value", "Enter the customer's name (2–200 characters).", 422, "name")
    out["name"] = name
    attention = " ".join(str(data.get("attention") or "").split()) or None
    if attention and len(attention) > 200:
        raise StudioError("invalid_value", "The contact person may be at most 200 characters.", 422, "attention")
    out["attention"] = attention
    mobile = normalize_mobile(data.get("mobile"))
    if not mobile or not re.fullmatch(r"[6-9]\d{9}", mobile):
        raise StudioError("invalid_value", "Enter a 10-digit Indian mobile number.", 422, "mobile")
    out["mobile"] = mobile
    email = (data.get("email") or "").strip() or None
    if email and (len(email) > 254 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email)):
        raise StudioError("invalid_value", "Enter a valid email address.", 422, "email")
    out["email"] = email
    lines = [" ".join(str(x).split()) for x in (data.get("address_lines") or []) if str(x).strip()]
    if not lines or len(lines) > 5 or any(len(x) > 120 for x in lines):
        raise StudioError("invalid_value", "Enter the billing address (1–5 lines, each at most 120 characters).", 422,
                          "address_lines")
    out["address_lines"] = lines
    gstin = (data.get("gstin") or "").strip().upper() or None
    if gstin and not proof.gstin_valid(gstin):
        raise StudioError("invalid_value", "The GSTIN is not valid (format or check digit).", 422, "gstin")
    out["gstin"] = gstin
    for key in ("name", "attention"):
        if out.get(key) and (d._MARKERS.search(out[key]) or "{" in out[key]):
            raise StudioError("invalid_value", "Placeholder text is not allowed.", 422, key)
    return out


def duplicates(db: Session, mobile: str, gstin: Optional[str], exclude_id: Optional[int] = None) -> list[dict]:
    q = select(StudioCustomer).where(or_(StudioCustomer.mobile == mobile,
                                         *( [StudioCustomer.gstin == gstin] if gstin else [] )))
    if exclude_id is not None:
        q = q.where(StudioCustomer.id != exclude_id)
    return [{"id": c.id, "name": c.name, "mobile": c.mobile, "gstin": c.gstin,
             "matched_on": [k for k, v in (("mobile", c.mobile == mobile), ("gstin", bool(gstin) and c.gstin == gstin))
                            if v]} for c in db.execute(q.limit(10)).scalars()]


def customer_json(c: StudioCustomer) -> dict:
    return {"id": c.id, "customer_type": c.customer_type, "title": c.title, "name": c.name, "attention": c.attention,
            "mobile": c.mobile, "email": c.email, "address_lines": list(c.address_lines or []), "gstin": c.gstin,
            "created_by": c.created_by, "created_at": _aware(c.created_at).isoformat(),
            "updated_at": _aware(c.updated_at).isoformat() if c.updated_at else None}


def create_customer(db: Session, data: dict, actor: str) -> tuple[StudioCustomer, list[dict]]:
    fields = clean_customer(data)
    dups = duplicates(db, fields["mobile"], fields["gstin"])
    c = StudioCustomer(**fields, created_by=actor, created_at=now())
    db.add(c)
    db.flush()
    return c, dups


def update_customer(db: Session, c: StudioCustomer, data: dict, actor: str) -> list[dict]:
    fields = clean_customer({**customer_json(c), **data})
    for k, v in fields.items():
        setattr(c, k, v)
    c.updated_at = now()
    db.flush()
    return duplicates(db, c.mobile, c.gstin, exclude_id=c.id)


def search_customers(db: Session, q: Optional[str], limit: int = 20) -> list[StudioCustomer]:
    stmt = select(StudioCustomer).order_by(StudioCustomer.id.desc()).limit(limit)
    if q:
        text = q.strip()
        digits = re.sub(r"\D", "", text)
        conds = [func.lower(StudioCustomer.name).contains(text.lower()), StudioCustomer.gstin == text.upper()]
        if len(digits) >= 4:
            conds.append(StudioCustomer.mobile.contains(digits[-10:]))
        stmt = stmt.where(or_(*conds))
    return list(db.execute(stmt).scalars())


def addressee_for(c: StudioCustomer) -> d.Addressee:
    return d.Addressee(customer_type=c.customer_type, title=c.title or ("M/s" if c.customer_type == "organisation"
                                                                         else None),
                       name=c.name, attention=c.attention if c.customer_type == "organisation" else None,
                       address_lines=list(c.address_lines or []), mobile=c.mobile, salutation="")


# ── Proposals and versions ──────────────────────────────────────────────────────

def _code(db: Session, table_col) -> str:
    for _ in range(20):
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(12))
        if db.execute(select(table_col).where(table_col == code)).first() is None:
            return code
    raise RuntimeError("could not allocate a unique code")


def title_for(doc: d.StudioDocument, customer: StudioCustomer) -> str:
    label = studio_config.project_type(doc.project.project_type)["label"]
    cap = d.kw_text(doc.plant.capacity_kwp)
    return (f"{cap} kWp {label} — {customer.name}" if cap else f"{label} — {customer.name}")[:240]


def create_proposal(db: Session, *, customer: StudioCustomer, project_type: str, owner_id: str, actor: str,
                    template: Optional[StudioLibraryItem] = None) -> StudioProposal:
    if project_type not in studio_config.PROJECT_TYPES:
        raise StudioError("invalid_value", f"project_type must be one of {list(studio_config.PROJECT_TYPES)}.", 422,
                          "project_type")
    if template is not None:
        doc = d.StudioDocument.model_validate(template.body["document"])
        if doc.project.project_type != project_type:
            raise StudioError("template_mismatch", "The template is for another project type.", 422, "template_id")
        doc.revision = 0
    else:
        doc = d.new_studio_document(project_type)
    doc.addressee = addressee_for(customer)
    if not doc.site.address_lines:
        doc.site.address_lines = list(customer.address_lines or [])
    doc = d.StudioDocument.model_validate(d.dump(doc))
    p = StudioProposal(customer_id=customer.id, owner_id=owner_id, project_type=project_type,
                       status=StudioStatus.draft, current_version=1, title=title_for(doc, customer),
                       created_at=now(), updated_at=now())
    db.add(p)
    db.flush()
    p.code = f"SP-{p.id:06d}"
    v = _new_version(db, p, 1, doc, actor)
    event(db, p, "created", version=1, actor=actor,
          detail={"project_type": project_type, "template_id": template.id if template else None})
    db.flush()
    _store(v, doc)
    return p


def _new_version(db: Session, p: StudioProposal, number: int, doc: d.StudioDocument, actor: str,
                 supersedes: Optional[int] = None) -> StudioProposalVersion:
    v = StudioProposalVersion(proposal_id=p.id, version=number, status=StudioStatus.draft, document=d.dump(doc),
                              verification_code=_code(db, StudioProposalVersion.verification_code),
                              supersedes_version=supersedes, created_by=actor, created_at=now(), updated_at=now())
    db.add(v)
    db.flush()
    return v


def version_of(db: Session, p: StudioProposal, number: Optional[int] = None) -> StudioProposalVersion:
    v = db.execute(select(StudioProposalVersion).where(StudioProposalVersion.proposal_id == p.id,
                                                       StudioProposalVersion.version == (number or p.current_version))
                   ).scalar_one_or_none()
    if v is None:
        raise StudioError("not_found", "No such version.", 404)
    return v


def versions(db: Session, p: StudioProposal) -> list[StudioProposalVersion]:
    return list(db.execute(select(StudioProposalVersion).where(StudioProposalVersion.proposal_id == p.id)
                           .order_by(StudioProposalVersion.version)).scalars())


def doc_of(v: StudioProposalVersion) -> d.StudioDocument:
    return d.StudioDocument.model_validate(v.document)


def _store(v: StudioProposalVersion, doc: d.StudioDocument, figs: Optional[dict] = None) -> dict:
    figs = figs or d.figures(doc)
    v.document = d.dump(doc)
    v.total = Decimal(figs["total"]) if figs["total"] is not None else None
    v.updated_at = now()
    return figs


def lock_proposal(db: Session, proposal_id: int) -> Optional[StudioProposal]:
    return db.execute(select(StudioProposal).where(StudioProposal.id == proposal_id).with_for_update()
                      .execution_options(populate_existing=True)).scalar_one_or_none()


def verify_url(v: StudioProposalVersion) -> str:
    return studio_config.load()["render"]["verify_url_base"] + v.verification_code


# ── The proof-check (cached per document state) ─────────────────────────────────

_CHECK_CACHE: "OrderedDict[str, list]" = OrderedDict()
_CACHE_SIZE = 256


def run_checks(db: Session, p: StudioProposal, v: StudioProposalVersion, doc: Optional[d.StudioDocument] = None,
               figs: Optional[dict] = None, *, render: bool = True) -> list[dict]:
    """Every check for the version as it stands. The rendered-PDF checks run on a fresh
    render of the draft (the same code that issues it); results are cached by content."""
    from app.proposals import document_render
    doc = doc or doc_of(v)
    figs = figs or d.figures(doc)
    customer = db.get(StudioCustomer, p.customer_id)
    today = today_ist()
    valid_until = v.valid_until if v.locked else None
    key = hashlib.sha256(json.dumps([v.document if doc is None else d.dump(doc), sorted(v.spell_ignore or []),
                                     customer.gstin if customer else None, str(today), str(valid_until), render,
                                     v.verification_code], sort_keys=True, default=str).encode()).hexdigest()
    if key in _CHECK_CACHE:
        _CHECK_CACHE.move_to_end(key)
        return [dict(c) for c in _CHECK_CACHE[key]]
    out = proof.document_checks(doc, figs, today=today, valid_until=valid_until, spell_ignore=v.spell_ignore,
                                customer=customer, db=db)
    if render and figs["total"] is not None:
        try:
            pdf = document_render.render(doc, reference=v.document_reference, doc_date=v.document_date or today,
                                         draft=False, verify_url=verify_url(v))
            out += proof.pdf_checks(doc, pdf, qr_size=document_render.qr_png_size(verify_url(v)))
        except document_render.DocumentRenderError as exc:
            out.append(proof.check("render_failed", proof.ERROR, None, str(exc)))
        except OSError:
            # WeasyPrint's native libraries are missing on this machine (never in the image).
            out.append(proof.check("render_unavailable", proof.ERROR, None, "The PDF renderer is not available here; "
                                                                           "the rendered document cannot be checked."))
            return out
    for item in out:
        item["fingerprint"] = proof.fingerprint(item)
    _CHECK_CACHE[key] = [dict(c) for c in out]
    while len(_CHECK_CACHE) > _CACHE_SIZE:
        _CHECK_CACHE.popitem(last=False)
    return out


def errors_of(items: list[dict]) -> list[dict]:
    return [c for c in items if c["level"] == proof.ERROR]


def unacknowledged(v: StudioProposalVersion, items: list[dict]) -> list[dict]:
    acked = set(v.acknowledged or [])
    return [c for c in items if c["level"] == proof.WARNING and c["fingerprint"] not in acked]


# ── Approval rule ───────────────────────────────────────────────────────────────

def approval_reasons(doc: d.StudioDocument, figs: dict) -> list[str]:
    """Why this version needs a second person's approval (empty: it may be issued directly)."""
    from app.config import settings
    reasons = []
    if figs.get("below_price_master") and studio_config.load()["workflow"]["approval_when_below_price_master"]:
        reasons.append("price_below_price_master")
    if figs["total"] is not None and Decimal(figs["total"]) > Decimal(str(settings.proposal_value_manager_approval_threshold)):
        reasons.append("total_above_threshold")
    return reasons


def preparers(db: Session, p: StudioProposal, version: int) -> set[str]:
    rows = db.execute(select(StudioEvent.actor).where(StudioEvent.proposal_id == p.id, StudioEvent.version == version,
                                                      StudioEvent.type.in_(PREPARER_EVENTS),
                                                      StudioEvent.actor_type == "employee")).scalars()
    return {a for a in rows if a}


# ── The view ────────────────────────────────────────────────────────────────────

def proposal_json(db: Session, p: StudioProposal, v: Optional[StudioProposalVersion] = None) -> dict:
    v = v or version_of(db, p)
    c = db.get(StudioCustomer, p.customer_id)
    return {"id": p.id, "code": p.code, "title": p.title, "project_type": p.project_type,
            "status": StudioStatus(p.status).value, "current_version": p.current_version, "owner_id": p.owner_id,
            "customer": {"id": c.id, "name": c.name, "mobile": c.mobile} if c else None,
            "lead_id": p.lead_id, "lost_reason": p.lost_reason, "total": str(v.total) if v.total is not None else None,
            "document_reference": v.document_reference, "valid_until": v.valid_until.isoformat() if v.valid_until else None,
            "issued_at": _aware(v.issued_at).isoformat() if v.issued_at else None,
            "created_at": _aware(p.created_at).isoformat(), "updated_at": _aware(p.updated_at).isoformat()}


def version_json(v: StudioProposalVersion) -> dict:
    return {"version": v.version, "status": StudioStatus(v.status).value, "locked": v.locked,
            "document_reference": v.document_reference,
            "document_date": v.document_date.isoformat() if v.document_date else None,
            "valid_until": v.valid_until.isoformat() if v.valid_until else None,
            "total": str(v.total) if v.total is not None else None,
            "verification_code": v.verification_code if v.issued_at else None,
            "pdf_sha256": v.pdf_sha256, "pdf_filename": v.pdf_filename,
            "submitted_by": v.submitted_by, "submitted_at": _aware(v.submitted_at).isoformat() if v.submitted_at else None,
            "approved_by": v.approved_by, "approved_at": _aware(v.approved_at).isoformat() if v.approved_at else None,
            "approval_comment": v.approval_comment,
            "issued_by": v.issued_by, "issued_at": _aware(v.issued_at).isoformat() if v.issued_at else None,
            "first_viewed_at": _aware(v.first_viewed_at).isoformat() if v.first_viewed_at else None,
            "view_count": v.view_count or 0,
            "accepted_at": _aware(v.accepted_at).isoformat() if v.accepted_at else None,
            "accepted_via": v.accepted_via, "accepted_name": v.accepted_name,
            "declined_at": _aware(v.declined_at).isoformat() if v.declined_at else None,
            "decline_reason": v.decline_reason, "supersedes_version": v.supersedes_version,
            "change_summary": v.change_summary or [], "spell_ignore": v.spell_ignore or []}


def view(db: Session, p: StudioProposal, v: Optional[StudioProposalVersion] = None, *, employee=None) -> dict:
    v = v or version_of(db, p)
    doc = doc_of(v)
    figs = d.figures(doc)
    items = run_checks(db, p, v, doc, figs, render=not v.locked)
    reasons = approval_reasons(doc, figs)
    out = {"proposal": proposal_json(db, p, v), "version": version_json(v), "revision": doc.revision,
           "layout": figs["layout"], "document": d.dump(doc), "figures": figs,
           "missing_required": d.studio_missing_required(doc, figs),
           "content_blockers": d.content_blockers(doc),
           "checks": items, "errors": len(errors_of(items)),
           "warnings_unacknowledged": [c["fingerprint"] for c in unacknowledged(v, items)],
           "approval_required": bool(reasons), "approval_reasons": reasons,
           "signatory": __import__("app.proposals.document_config", fromlist=["load"]).load()["signatory"],
           "versions": [{"version": x.version, "status": StudioStatus(x.status).value,
                         "document_reference": x.document_reference} for x in versions(db, p)]}
    if employee is not None:
        out["can"] = abilities(db, p, v, employee, items, reasons)
    return out


def abilities(db: Session, p: StudioProposal, v: StudioProposalVersion, employee, items: list, reasons: list) -> dict:
    status = StudioStatus(v.status)
    edit = employee.holds("studio.edit")
    errors = bool(errors_of(items))
    approved_now = v.approved_at is not None and v.approval_revision == doc_of(v).revision
    return {
        "edit": edit and status in EDITABLE,
        "price": employee.holds("studio.price") and status in EDITABLE,
        "submit": edit and status == StudioStatus.draft and not errors,
        "approve": (employee.holds("studio.approve") and status == StudioStatus.pending_approval
                    and employee.employee_id not in preparers(db, p, v.version)),
        "issue": edit and not errors and (status == StudioStatus.ready or (status == StudioStatus.draft and not reasons))
        and (not reasons or approved_now),
        "deliver": edit and status in OPEN_FOR_CUSTOMER,
        "revise": edit and v.locked and status not in (StudioStatus.accepted,) and p.status not in
        (StudioStatus.converted, StudioStatus.lost) and v.version == p.current_version,
        "mark_accepted": edit and status in OPEN_FOR_CUSTOMER,
        "mark_lost": edit and StudioStatus(p.status) not in (StudioStatus.accepted, StudioStatus.converted,
                                                             StudioStatus.lost),
        "convert": employee.holds("studio.convert") and p.lead_id is None and v.issued_at is not None,
    }


# ── Editing ─────────────────────────────────────────────────────────────────────

def _require_editable(v: StudioProposalVersion) -> None:
    if v.locked or StudioStatus(v.status) not in EDITABLE:
        raise StudioError("proposal_locked", f"Version {v.version} is {StudioStatus(v.status).value} and cannot change. "
                                             "Revise it to make a new version.")


def _back_to_draft(db: Session, p: StudioProposal, v: StudioProposalVersion, actor: str) -> bool:
    """Any change after submission or approval sends the version back to draft."""
    if StudioStatus(v.status) == StudioStatus.draft:
        return False
    v.status = StudioStatus.draft
    v.approved_by = v.approved_at = v.approval_revision = v.submitted_at = v.submitted_by = None
    p.status = StudioStatus.draft
    return True


def _section_value(doc: d.StudioDocument, section: str, value: Any) -> d.StudioDocument:
    new = doc.model_copy(deep=True)
    try:
        if section == "project":
            if not isinstance(value, dict):
                raise StudioError("invalid_value", "project must be an object.", 422, section)
            project = d.StudioProject.model_validate({**new.project.model_dump(), **value})
            changed_type = project.project_type != new.project.project_type
            new.project = project
            if changed_type:
                d.apply_project_type(new)
        elif section == "items":
            new.items = [d.ItemRow.model_validate(r) for r in (value or [])]
        elif section == "display":
            new.display = d.Display.model_validate(value or {})
        elif section == "savings":
            new.savings = d.SavingsInputs.model_validate(value) if value else None
        elif section == "notes_free":
            new.notes_free = list(value or [])
        elif section == "cover_highlight":
            new.cover_highlight = value or None
        elif section == "validity_days":
            new.validity_days = int(value)
        return d.StudioDocument.model_validate(d.dump(new))
    except StudioError:
        raise
    except (ValueError, TypeError) as exc:
        raise StudioError("invalid_value", d._first_error(exc) if isinstance(exc, ValueError) else str(exc), 422,
                          section) from exc


def _check_item_makes(db: Session, doc: d.StudioDocument) -> None:
    allowed = d.allowed_makes(db)
    for i, item in enumerate(doc.items):
        unknown = [x.strip() for x in item.make.split("/") if x.strip() and x.strip().lower() not in allowed]
        if unknown:
            raise StudioError("make_not_in_catalogue", f"Unknown make(s) {unknown}: use the equipment catalogue's or "
                                                       "the price list's makes, or 'Equivalent'.", 422,
                              f"items[{i}].make")


def edit(db: Session, p: StudioProposal, *, section: str, value: Any, base_revision: int, actor: str,
         can_price: bool) -> dict:
    """One section, validated. Returns {changes, regenerated_sections, approval_cleared}.
    Raises StudioError (403 not_permitted / 409 proposal_locked, stale_version / 422 …)."""
    if section not in SECTIONS:
        raise StudioError("field_not_editable", f"'{section}' cannot be changed in the Studio.", 403, section)
    if section == "price" and not can_price:
        raise StudioError("not_permitted", "Your role cannot set prices.", 403, section)
    v = version_of(db, p)
    _require_editable(v)
    doc = doc_of(v)
    if base_revision != doc.revision:
        raise StudioError("stale_version", "Someone changed this proposal after you opened it. Reload and apply your "
                                           "change again.", extra={"current_revision": doc.revision})
    before = d.dump(doc)
    before_total = d.figures(doc)["total"]
    regenerated: list[str] = []
    if section in BASE_SECTIONS:
        try:
            new, _, regenerated = d.apply_edit(db, doc, section, value)
        except d.DocumentError as exc:
            raise StudioError(exc.code, exc.message, exc.status_code if exc.status_code != 403 else 422,
                              exc.field) from exc
        d.apply_project_type(new)
    elif section == "price":
        new = _priced(doc, value)
    else:
        new = _section_value(doc, section, value)
        if section == "items":
            _check_item_makes(db, new)
        d.requote(new)
        regenerated = d.refresh_defaults(new)
        if section not in new.edited:
            new.edited.append(section)
    new.revision = doc.revision + 1
    new = d.StudioDocument.model_validate(d.dump(new))
    figs = _store(v, new)
    changes = [c for c in d.diff(before, d.dump(new), "") if c["field"] != "revision"
               and not c["field"].startswith(("defaults.", "edited"))]
    cleared = _back_to_draft(db, p, v, actor)
    customer = db.get(StudioCustomer, p.customer_id)
    p.title = title_for(new, customer)
    p.project_type = new.project.project_type
    p.updated_at = now()
    kind = "priced" if section == "price" else "edited"
    event(db, p, kind, version=v.version, actor=actor,
          detail={"section": section, "revision": new.revision, "changes": changes[:60],
                  "total_before": before_total, "total_after": figs["total"], "approval_cleared": cleared,
                  "regenerated_sections": regenerated})
    db.flush()
    return {"changes": changes, "regenerated_sections": regenerated, "approval_cleared": cleared}


def _money_or_none(value, field: str) -> Optional[Decimal]:
    if value in (None, ""):
        return None
    try:
        out = d.money(value)
    except Exception:
        raise StudioError("invalid_value", f"{field} must be an amount in rupees.", 422, f"price.{field}")
    if out <= 0:
        raise StudioError("invalid_value", f"{field} must be positive.", 422, f"price.{field}")
    return out


def _priced(doc: d.StudioDocument, value: Any) -> d.StudioDocument:
    """The price section: mode price_list | staff_basic (basic or rate_per_kwp) | staff_total |
    none, an optional discount {amount, reason} off the basic price, and the reason when the
    offered price is below the price master. Every figure is recomputed, never stored typed."""
    if not isinstance(value, dict):
        raise StudioError("invalid_value", "price must be an object.", 422, "price")
    if d.layout(doc) == "itemised":
        raise StudioError("invalid_value", "An itemised proposal is priced by its line items.", 422, "price")
    mode = value.get("mode")
    new = doc.model_copy(deep=True)
    discount = value.get("discount")
    try:
        disc = d.Discount.model_validate(discount) if discount else None
    except ValueError as exc:
        raise StudioError("invalid_value", d._first_error(exc), 422, "price.discount") from exc
    reason = " ".join(str(value.get("reason") or "").split()) or None
    if reason and len(reason) > 200:
        raise StudioError("invalid_value", "The reason may be at most 200 characters.", 422, "price.reason")
    if mode == "price_list":
        q = d.master_quote(doc)
        if q is None:
            raise StudioError("not_list_priceable", "The price master does not price this package, phase and capacity; "
                                                    "enter a price.", 409, "price.mode")
        new.price = d.StudioPrice(mode="price_list", basic=q.basic, price_list_version=q.price_list_version,
                                  discount=disc, reason=reason)
    elif mode == "staff_basic":
        basic, rate = _money_or_none(value.get("basic"), "basic"), _money_or_none(value.get("rate_per_kwp"),
                                                                                   "rate_per_kwp")
        if (basic is None) == (rate is None):
            raise StudioError("invalid_value", "Give either the basic price or a rate per kWp.", 422, "price")
        if rate is not None and not doc.plant.capacity_kwp:
            raise StudioError("missing_required", "A rate per kWp needs the plant capacity first.", 409,
                              "plant.capacity_kwp")
        new.price = d.StudioPrice(mode="staff_basic", basic=basic, rate_per_kwp=rate, discount=disc, reason=reason)
    elif mode == "staff_total":
        if disc is not None:
            raise StudioError("invalid_value", "A discount applies to a basic price, not to a total.", 422,
                              "price.discount")
        new.price = d.StudioPrice(mode="staff_total", total=_money_or_none(value.get("total"), "total"), reason=reason)
        if new.price.total is None:
            raise StudioError("invalid_value", "Give the total including GST.", 422, "price.total")
    elif mode == "none":
        new.price = d.StudioPrice(mode="none")
    else:
        raise StudioError("invalid_value", "mode must be price_list, staff_basic, staff_total or none.", 422,
                          "price.mode")
    d.requote(new)
    if disc is not None:
        before = d.basic_before_discount(new.price, new.plant.capacity_kwp)
        if before is None or disc.amount >= before:
            raise StudioError("invalid_value", "The discount must be smaller than the basic price.", 422,
                              "price.discount")
    if "price" not in new.edited:
        new.edited.append("price")
    figs = d.figures(new)
    if figs.get("below_price_master") and not (reason or disc):
        raise StudioError("reason_required", f"The price is {figs['below_price_master']['percent']}% below the price "
                                             "master: give the reason.", 422, "price.reason",
                          extra={"below_price_master": figs["below_price_master"]})
    return new


def acknowledge(db: Session, p: StudioProposal, fingerprints: list[str], actor: str) -> list[str]:
    """Record "I have checked this" for the given current warnings."""
    v = version_of(db, p)
    _require_editable(v)
    current = {c["fingerprint"]: c for c in run_checks(db, p, v) if c["level"] == proof.WARNING}
    unknown = [f for f in fingerprints if f not in current]
    if unknown:
        raise StudioError("unknown_warning", "Some of these warnings no longer apply; reload.", 409,
                          extra={"unknown": unknown})
    v.acknowledged = sorted(set(v.acknowledged or []) | set(fingerprints))
    event(db, p, "warnings_acknowledged", version=v.version, actor=actor,
          detail={"warnings": [{"id": current[f]["id"], "field": current[f]["field"], "message": current[f]["message"]}
                               for f in fingerprints]})
    db.flush()
    return v.acknowledged


def ignore_word(db: Session, p: StudioProposal, word: str, actor: str) -> list[str]:
    v = version_of(db, p)
    _require_editable(v)
    word = word.strip().lower()
    if not re.fullmatch(r"[a-z][a-z'’-]{1,40}", word):
        raise StudioError("invalid_value", "Give one word.", 422, "word")
    v.spell_ignore = sorted(set(v.spell_ignore or []) | {word})
    event(db, p, "spelling_ignored", version=v.version, actor=actor, detail={"word": word})
    db.flush()
    return v.spell_ignore


# ── Submit, approve, reject ─────────────────────────────────────────────────────

def _gate(db: Session, p: StudioProposal, v: StudioProposalVersion) -> tuple[d.StudioDocument, dict, list]:
    doc = doc_of(v)
    figs = d.figures(doc)
    items = run_checks(db, p, v, doc, figs)
    blockers = d.content_blockers(doc)
    if blockers:
        raise StudioError("content_not_ready", "Grid Nova's text or rates for this proposal type are still missing: "
                                               + ", ".join(b["registry"] for b in blockers) + ".",
                          extra={"registry": [b["registry"] for b in blockers], "blockers": blockers})
    errs = errors_of(items)
    if errs:
        raise StudioError("proof_check_failed", "The proof-check found errors; fix them first.",
                          extra={"checks": errs})
    pending = unacknowledged(v, items)
    if pending:
        raise StudioError("warnings_unacknowledged", "Confirm you have checked each warning first.",
                          extra={"warnings": pending})
    return doc, figs, items


def submit(db: Session, p: StudioProposal, actor: str) -> str:
    """draft → pending_approval (a second person must approve) or → ready."""
    v = version_of(db, p)
    if StudioStatus(v.status) != StudioStatus.draft or v.locked:
        raise StudioError("invalid_status", f"Version {v.version} is {StudioStatus(v.status).value}.")
    doc, figs, _ = _gate(db, p, v)
    reasons = approval_reasons(doc, figs)
    v.submitted_by, v.submitted_at = actor, now()
    if reasons:
        v.status = p.status = StudioStatus.pending_approval
        event(db, p, "submitted_for_approval", version=v.version, actor=actor, detail={"reasons": reasons,
                                                                                         "total": figs["total"]})
        _notify_after_commit(db, "studio_approval_requested",
                             {"proposal_id": p.id, "code": p.code, "title": p.title, "total": figs["total"],
                              "reasons": reasons, "requested_by": actor})
    else:
        v.status = p.status = StudioStatus.ready
        event(db, p, "ready", version=v.version, actor=actor, detail={"total": figs["total"]})
    p.updated_at = now()
    db.flush()
    return StudioStatus(v.status).value


def approve(db: Session, p: StudioProposal, actor: str, comment: Optional[str]) -> None:
    v = version_of(db, p)
    if StudioStatus(v.status) != StudioStatus.pending_approval:
        raise StudioError("invalid_status", "This version is not waiting for approval.")
    if actor in preparers(db, p, v.version):
        raise StudioError("maker_checker", "You prepared this version; someone else must approve it.", 403)
    doc, figs, _ = _gate(db, p, v)
    v.status = p.status = StudioStatus.ready
    v.approved_by, v.approved_at, v.approval_revision = actor, now(), doc.revision
    v.approval_comment = (comment or "").strip()[:2000] or None
    p.updated_at = now()
    event(db, p, "approved", version=v.version, actor=actor, detail={"comment": v.approval_comment,
                                                                     "revision": doc.revision, "total": figs["total"]})
    db.flush()


def reject(db: Session, p: StudioProposal, actor: str, comment: str) -> None:
    v = version_of(db, p)
    if StudioStatus(v.status) != StudioStatus.pending_approval:
        raise StudioError("invalid_status", "This version is not waiting for approval.")
    if actor in preparers(db, p, v.version):
        raise StudioError("maker_checker", "You prepared this version; someone else must decide on it.", 403)
    comment = (comment or "").strip()
    if not comment:
        raise StudioError("invalid_value", "Say why it is rejected.", 422, "comment")
    v.status = p.status = StudioStatus.draft
    v.submitted_by = v.submitted_at = None
    v.approval_comment = comment[:2000]
    p.updated_at = now()
    event(db, p, "rejected", version=v.version, actor=actor, detail={"comment": comment[:2000]})
    db.flush()


# ── Issue ───────────────────────────────────────────────────────────────────────

def _reference(db: Session, p: StudioProposal, v: StudioProposalVersion, day: date) -> str:
    """The first issued version takes the next GN/DD/MM/YYYY/NN from the counter lead
    proposals use; a later version its base + -R{n}."""
    from app.proposals import reference as refs
    earlier = [x.document_reference for x in versions(db, p) if x.version < v.version and x.document_reference]
    if earlier:
        base = earlier[0].split("-R")[0]
        n = max(int(r.split("-R")[1]) if "-R" in r else 0 for r in earlier) + 1
        return f"{base}-R{n}"
    return refs.format_reference(day, refs.next_sequence(db, day))


def pdf_filename(reference: str, doc: d.StudioDocument, customer_name: str) -> str:
    cap = d.kw_text(doc.plant.capacity_kwp)
    name = re.sub(r"[^A-Za-z0-9]+", "", customer_name.title())[:60] or "Customer"
    middle = f"_{cap}kWp" if cap else ""
    return f"GridNova_Proposal_{reference.replace('/', '-')}{middle}_{name}.pdf"


def link_token(v: StudioProposalVersion) -> Optional[str]:
    from app.security import encryption
    return encryption.decrypt(v.token_encrypted) if v.token_encrypted else None


def public_url(token: str) -> str:
    from app.config import settings
    return f"{settings.frontend_base_url.rstrip('/')}{studio_config.load()['render']['public_page_path']}{token}"


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def issue(db: Session, p: StudioProposal, actor: str) -> dict:
    """Allocate the reference and date, render the final PDF/A-3b, check it, store it with
    its SHA-256, lock the version, create the customer's link. Returns {"url", "token"}.
    Everything happens in the caller's transaction: a failure leaves nothing behind."""
    from app.proposals import document_render
    from app.security import encryption
    from app.storage import r2
    v = version_of(db, p)
    status = StudioStatus(v.status)
    if v.locked or status not in (StudioStatus.draft, StudioStatus.ready):
        raise StudioError("invalid_status", f"Version {v.version} is {status.value}; it cannot be issued.")
    doc, figs, _ = _gate(db, p, v)
    reasons = approval_reasons(doc, figs)
    if reasons and not (status == StudioStatus.ready and v.approved_at and v.approval_revision == doc.revision):
        raise StudioError("approval_required", "This version needs approval by someone other than its preparer "
                                               "before it is issued.", extra={"reasons": reasons})
    day = today_ist()
    v.document_reference = _reference(db, p, v, day)
    v.document_date = day
    v.valid_until = day + timedelta(days=doc.validity_days)
    try:
        pdf = document_render.render(doc, reference=v.document_reference, doc_date=day, draft=False,
                                     verify_url=verify_url(v), pdf_a=True)
    except document_render.DocumentRenderError as exc:
        raise StudioError("render_failed", str(exc)) from exc
    final = proof.pdf_checks(doc, pdf, qr_size=document_render.qr_png_size(verify_url(v)))
    if errors_of(final):
        raise StudioError("proof_check_failed", "The issued PDF failed the proof-check.", extra={"checks": final})
    customer = db.get(StudioCustomer, p.customer_id)
    v.pdf_sha256 = hashlib.sha256(pdf).hexdigest()
    v.pdf_filename = pdf_filename(v.document_reference, doc, customer.name)
    key = f"studio/{p.id}/v{v.version}/{v.pdf_sha256[:16]}.pdf"
    try:
        r2.upload_bytes(key, pdf, "application/pdf")
    except r2.StorageNotConfiguredError as exc:
        raise StudioError("storage_unavailable", "File storage is not configured; the proposal cannot be issued.",
                          503) from exc
    v.pdf_storage_key = key
    token = secrets.token_urlsafe(32)
    v.token_hash = token_hash(token)
    try:
        v.token_encrypted = encryption.encrypt(token)
    except encryption.EncryptionKeyMissingError as exc:
        raise StudioError("encryption_unavailable", "FERNET_ENCRYPTION_KEY is not set; the link cannot be stored.",
                          503) from exc
    v.figures = figs
    v.issued_by, v.issued_at, v.locked = actor, now(), True
    v.status = p.status = StudioStatus.issued
    previous = [x for x in versions(db, p) if x.version < v.version and x.issued_at is not None]
    if previous:
        last = previous[-1]
        v.change_summary = [c for c in d.diff(last.document, v.document, "") if c["field"] != "revision"
                            and not c["field"].startswith(("defaults.", "edited"))][:200]
    for x in previous:
        if StudioStatus(x.status) in (StudioStatus.issued, StudioStatus.viewed, StudioStatus.expired,
                                      StudioStatus.declined):
            x.status = StudioStatus.superseded
            event(db, p, "superseded", version=x.version, actor_type="system", detail={"by_version": v.version})
    p.updated_at = now()
    event(db, p, "issued", version=v.version, actor=actor,
          detail={"reference": v.document_reference, "pdf_sha256": v.pdf_sha256, "total": figs["total"],
                  "valid_until": v.valid_until.isoformat(), "approved_by": v.approved_by})
    db.flush()
    return {"token": token, "url": public_url(token)}


# ── Revise, duplicate ───────────────────────────────────────────────────────────

def revise(db: Session, p: StudioProposal, actor: str) -> StudioProposalVersion:
    v = version_of(db, p)
    if not v.locked:
        raise StudioError("invalid_status", "The current version is still a draft; edit it instead.")
    if StudioStatus(v.status) == StudioStatus.accepted or StudioStatus(p.status) in (StudioStatus.converted,
                                                                                     StudioStatus.lost):
        raise StudioError("invalid_status", f"This proposal is {StudioStatus(p.status).value}; it cannot be revised.")
    doc = doc_of(v)
    doc.revision = 0
    new = _new_version(db, p, v.version + 1, doc, actor, supersedes=v.version)
    p.current_version = new.version
    p.status = StudioStatus.draft
    p.updated_at = now()
    event(db, p, "revised", version=new.version, actor=actor, detail={"from_version": v.version})
    _store(new, doc)
    db.flush()
    return new


def duplicate(db: Session, p: StudioProposal, *, version: Optional[int], customer: Optional[StudioCustomer],
              owner_id: str, actor: str) -> StudioProposal:
    src = version_of(db, p, version)
    doc = doc_of(src)
    target = customer or db.get(StudioCustomer, p.customer_id)
    doc.revision = 0
    doc.addressee = addressee_for(target) if customer is not None else doc.addressee
    if customer is not None:
        doc.site.address_lines = list(target.address_lines or [])
    doc = d.StudioDocument.model_validate(d.dump(doc))
    q = StudioProposal(customer_id=target.id, owner_id=owner_id, project_type=doc.project.project_type,
                       status=StudioStatus.draft, current_version=1, title=title_for(doc, target),
                       created_at=now(), updated_at=now())
    db.add(q)
    db.flush()
    q.code = f"SP-{q.id:06d}"
    v = _new_version(db, q, 1, doc, actor)
    _store(v, doc)
    event(db, q, "duplicated", version=1, actor=actor, detail={"from_proposal": p.id, "from_version": src.version})
    event(db, q, "created", version=1, actor=actor, detail={"project_type": q.project_type, "duplicate_of": p.code})
    db.flush()
    return q


# ── Accepted (in person), lost ───────────────────────────────────────────────────

def is_expired(v: StudioProposalVersion, today: Optional[date] = None) -> bool:
    return v.valid_until is not None and (today or today_ist()) > v.valid_until


def mark_accepted_in_person(db: Session, p: StudioProposal, *, accepted_on: date, signed_copy: bytes,
                            content_type: str, filename: str, actor: str) -> None:
    from app.storage import r2
    v = version_of(db, p)
    if StudioStatus(v.status) not in OPEN_FOR_CUSTOMER:
        raise StudioError("invalid_status", f"Version {v.version} is {StudioStatus(v.status).value}; only an issued "
                                            "proposal can be accepted.")
    if accepted_on > today_ist() or (v.document_date and accepted_on < v.document_date):
        raise StudioError("invalid_value", "The acceptance date must be between the proposal date and today.", 422,
                          "accepted_on")
    if v.valid_until and accepted_on > v.valid_until:
        raise StudioError("proposal_expired", "That date is after the proposal's validity.", 409, "accepted_on")
    digest = hashlib.sha256(signed_copy).hexdigest()
    ext = {"application/pdf": "pdf", "image/jpeg": "jpg", "image/png": "png"}[content_type]
    key = f"studio/{p.id}/v{v.version}/signed-{digest[:16]}.{ext}"
    try:
        r2.upload_bytes(key, signed_copy, content_type)
    except r2.StorageNotConfiguredError as exc:
        raise StudioError("storage_unavailable", "File storage is not configured.", 503) from exc
    v.signed_copy_key, v.signed_copy_sha256 = key, digest
    when = datetime.combine(accepted_on, datetime.min.time(), tzinfo=timezone.utc)
    _accepted(db, p, v, when=when, via="in_person", name=None, actor_type="employee", actor=actor,
              detail={"accepted_on": accepted_on.isoformat(), "signed_copy_sha256": digest, "filename": filename[:200]})


def _accepted(db: Session, p: StudioProposal, v: StudioProposalVersion, *, when: datetime, via: str,
              name: Optional[str], actor_type: str, actor: Optional[str], detail: dict, ip: Optional[str] = None,
              user_agent: Optional[str] = None) -> None:
    v.accepted_at, v.accepted_via, v.accepted_name = when, via, name
    v.accepted_pdf_sha256 = v.pdf_sha256
    v.accepted_ip, v.accepted_user_agent = ip, (user_agent or "")[:400] or None
    v.status = StudioStatus.accepted
    if StudioStatus(p.status) != StudioStatus.converted:
        p.status = StudioStatus.accepted
    p.updated_at = now()
    event(db, p, "accepted", version=v.version, actor_type=actor_type, actor=actor,
          detail={**detail, "via": via, "pdf_sha256": v.pdf_sha256, "reference": v.document_reference},
          ip=ip, user_agent=user_agent)
    _notify_after_commit(db, "studio_accepted", {"proposal_id": p.id, "owner_id": p.owner_id, "code": p.code,
                                                 "title": p.title, "reference": v.document_reference, "via": via,
                                                 "total": str(v.total)})
    if p.lead_id is not None:
        from app.studio import convert
        convert.mark_lead_accepted(db, p, v)
    db.flush()


def mark_lost(db: Session, p: StudioProposal, reason: str, actor: str) -> None:
    reason = " ".join((reason or "").split())
    if not 3 <= len(reason) <= 500:
        raise StudioError("invalid_value", "Give the reason the proposal was lost (3–500 characters).", 422, "reason")
    if StudioStatus(p.status) in (StudioStatus.accepted, StudioStatus.converted, StudioStatus.lost):
        raise StudioError("invalid_status", f"This proposal is {StudioStatus(p.status).value}.")
    p.status, p.lost_reason, p.updated_at = StudioStatus.lost, reason, now()
    event(db, p, "lost", version=p.current_version, actor=actor, detail={"reason": reason})
    db.flush()


# ── Diff, library, summary ──────────────────────────────────────────────────────

def version_diff(db: Session, p: StudioProposal, a: int, b: int) -> list[dict]:
    va, vb = version_of(db, p, a), version_of(db, p, b)
    return [c for c in d.diff(va.document, vb.document, "") if c["field"] != "revision"
            and not c["field"].startswith(("defaults.", "edited"))]


def save_as_template(db: Session, p: StudioProposal, name: str, actor: str) -> StudioLibraryItem:
    """A whole document minus customer data."""
    doc = doc_of(version_of(db, p))
    doc.addressee = d.Addressee()
    doc.site = d.Site()
    doc.connection = doc.connection.model_copy(update={"consumer_number": None, "sub_division": None})
    doc.pm_surya_ghar = d.PmSuryaGhar(subsidy_opted=doc.pm_surya_ghar.subsidy_opted)
    doc.savings = None
    doc.revision = 0
    item = StudioLibraryItem(kind="template", name=name.strip()[:120], project_type=doc.project.project_type,
                             body={"document": d.dump(doc)}, created_by=actor, created_at=now())
    db.add(item)
    db.flush()
    return item


def _notify_after_commit(db: Session, event_type: str, payload: dict) -> None:
    from app.memory.memory_manager import MemoryManager
    from app.notifications.notifier import notifier
    MemoryManager(db).notify_after_commit(lambda: notifier.notify(event_type, payload), event_type)


def owner_email(db: Session, p: StudioProposal) -> Optional[str]:
    emp = db.get(Employee, p.owner_id)
    from app.notifications.alerts import _usable
    return emp.email if emp is not None and _usable(emp.email) else None
