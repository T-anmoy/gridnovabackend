"""The customer's side of a Studio proposal (BE-18 §9): the online proposal page, the PDF
download, accepting and declining online, and the public verification of a printed QR.
Delivery by email and the WhatsApp link live here too.

A customer reaches a version only through its access token (≥ 32 random bytes, stored as a
SHA-256 hash and, for re-sending the link, Fernet-encrypted). Every view and download is an
event with the IP and user agent; the first view alerts the owner. Accepting needs the full
name typed, the last four digits of the mobile on the proposal and the terms checkbox; it
records the PDF hash the customer accepted.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.memory.models import StudioCustomer, StudioProposal, StudioProposalVersion, StudioStatus
from app.proposals import document as d
from app.studio import config as studio_config
from app.studio import service
from app.studio.service import StudioError


def by_token(db: Session, token: str, *, lock: bool = False) -> Optional[StudioProposalVersion]:
    if not token or len(token) < 32 or len(token) > 128:
        return None
    q = select(StudioProposalVersion).where(StudioProposalVersion.token_hash == service.token_hash(token))
    if lock:
        q = q.with_for_update().execution_options(populate_existing=True)
    return db.execute(q).scalar_one_or_none()


def _state(db: Session, v: StudioProposalVersion) -> str:
    """accepted | declined | superseded | expired | open (as the customer sees it)."""
    status = StudioStatus(v.status)
    if status == StudioStatus.accepted:
        return "accepted"
    if status == StudioStatus.declined:
        return "declined"
    if status == StudioStatus.superseded:
        return "superseded"
    if status == StudioStatus.expired or service.is_expired(v):
        return "expired"
    return "open"


def summary(db: Session, v: StudioProposalVersion) -> dict:
    """What the online page shows: the figures the PDF prints, never more."""
    p = db.get(StudioProposal, v.proposal_id)
    figs = v.figures or d.figures(service.doc_of(v))
    doc = service.doc_of(v)
    customer = db.get(StudioCustomer, p.customer_id)
    return {
        "reference": v.document_reference, "date": v.document_date.isoformat() if v.document_date else None,
        "title": p.title, "customer_name": customer.name if customer else None,
        "capacity_kwp": figs.get("capacity_kwp"), "layout": figs["layout"],
        "basic": figs["basic"], "gst": figs["gst"], "total": figs["total"], "total_in_words": figs["words"]["total"],
        "discount": figs.get("discount") if doc.display.show_discount_line else None,
        "payments": figs["payments"], "subsidy": figs["subsidy"] if figs["subsidy"].get("applies") else None,
        "valid_until": v.valid_until.isoformat() if v.valid_until else None,
        "state": _state(db, v), "accepted_at": service._aware(v.accepted_at).isoformat() if v.accepted_at else None,
        "pdf_sha256": v.pdf_sha256, "pdf_filename": v.pdf_filename,
        "mobile_hint": f"XXXXXX{doc.addressee.mobile[-4:]}" if doc.addressee.mobile else None,
    }


def record_view(db: Session, v: StudioProposalVersion, ip: Optional[str], user_agent: Optional[str]) -> None:
    p = db.get(StudioProposal, v.proposal_id)
    first = v.first_viewed_at is None
    v.view_count = (v.view_count or 0) + 1
    if first:
        v.first_viewed_at = service.now()
        if StudioStatus(v.status) == StudioStatus.issued:
            v.status = StudioStatus.viewed
            if StudioStatus(p.status) == StudioStatus.issued and p.current_version == v.version:
                p.status = StudioStatus.viewed
        service._notify_after_commit(db, "studio_viewed", {"proposal_id": p.id, "owner_id": p.owner_id,
                                                           "code": p.code, "title": p.title,
                                                           "reference": v.document_reference})
    service.event(db, p, "viewed", version=v.version, actor_type="customer", detail={"first": first,
                                                                                     "count": v.view_count},
                  ip=ip, user_agent=user_agent)


def record_download(db: Session, v: StudioProposalVersion, ip: Optional[str], user_agent: Optional[str]) -> None:
    p = db.get(StudioProposal, v.proposal_id)
    service.event(db, p, "downloaded", version=v.version, actor_type="customer", ip=ip, user_agent=user_agent)


def _open_or_refuse(db: Session, v: StudioProposalVersion) -> None:
    state = _state(db, v)
    if state == "superseded":
        raise StudioError("proposal_superseded", "This proposal has been replaced by a newer one. Please use the "
                                                 "latest proposal Grid Nova sent you.")
    if state == "expired":
        raise StudioError("proposal_expired", "This proposal has expired. Please contact Grid Nova for a fresh one.")
    if state == "declined":
        raise StudioError("proposal_declined", "This proposal was declined. Please contact Grid Nova.")


def accept(db: Session, token: str, *, full_name: str, mobile_last4: str, accept_terms: bool, ip: Optional[str],
           user_agent: Optional[str]) -> dict:
    v = by_token(db, token, lock=True)
    if v is None:
        raise StudioError("not_found", "Proposal not found.", 404)
    if v.accepted_at is not None:
        return {"status": "accepted", "accepted_at": service._aware(v.accepted_at).isoformat(),
                "already_accepted": True}
    _open_or_refuse(db, v)
    name = " ".join((full_name or "").split())
    if not 3 <= len(name) <= 120:
        raise StudioError("invalid_value", "Type your full name.", 422, "full_name")
    if not accept_terms:
        raise StudioError("terms_not_accepted", "Tick the box to accept the proposal and its terms.", 422,
                          "accept_terms")
    doc = service.doc_of(v)
    if not doc.addressee.mobile or (mobile_last4 or "").strip() != doc.addressee.mobile[-4:]:
        raise StudioError("mobile_mismatch", "The last four digits do not match the mobile number on the proposal.",
                          422, "mobile_last4")
    p = db.get(StudioProposal, v.proposal_id)
    service._accepted(db, p, v, when=service.now(), via="online", name=name, actor_type="customer", actor=name,
                      detail={"name": name}, ip=ip, user_agent=user_agent)
    _send_acceptance_emails(db, p, v, name)
    return {"status": "accepted", "accepted_at": service._aware(v.accepted_at).isoformat(), "already_accepted": False}


def decline(db: Session, token: str, *, reason: Optional[str], ip: Optional[str], user_agent: Optional[str]) -> dict:
    v = by_token(db, token, lock=True)
    if v is None:
        raise StudioError("not_found", "Proposal not found.", 404)
    if StudioStatus(v.status) == StudioStatus.declined:
        return {"status": "declined", "already_declined": True}
    if v.accepted_at is not None:
        raise StudioError("proposal_accepted", "This proposal has already been accepted.")
    _open_or_refuse(db, v)
    p = db.get(StudioProposal, v.proposal_id)
    v.status = StudioStatus.declined
    v.declined_at = service.now()
    v.decline_reason = " ".join((reason or "").split())[:1000] or None
    if p.current_version == v.version and StudioStatus(p.status) not in (StudioStatus.converted, StudioStatus.lost):
        p.status = StudioStatus.declined
    p.updated_at = service.now()
    service.event(db, p, "declined", version=v.version, actor_type="customer", detail={"reason": v.decline_reason},
                  ip=ip, user_agent=user_agent)
    service._notify_after_commit(db, "studio_declined", {"proposal_id": p.id, "owner_id": p.owner_id, "code": p.code,
                                                         "title": p.title, "reference": v.document_reference,
                                                         "reason": v.decline_reason})
    db.flush()
    return {"status": "declined", "already_declined": False}


def _mask(name: Optional[str]) -> Optional[str]:
    parts = (name or "").split()
    if not parts:
        return None
    return parts[0] if len(parts) == 1 else f"{parts[0]} {parts[1][0]}."


def verify(db: Session, code: str) -> Optional[dict]:
    """The public check a printed QR opens: is this a genuine, current Grid Nova proposal?"""
    code = (code or "").strip().upper()
    if not 8 <= len(code) <= 16:
        return None
    v = db.execute(select(StudioProposalVersion).where(StudioProposalVersion.verification_code == code)
                   ).scalar_one_or_none()
    if v is None or v.issued_at is None:
        return None
    p = db.get(StudioProposal, v.proposal_id)
    customer = db.get(StudioCustomer, p.customer_id)
    state = _state(db, v)
    status = {"open": "valid"}.get(state, state)
    figs = v.figures or {}
    return {"reference": v.document_reference, "date": v.document_date.isoformat() if v.document_date else None,
            "capacity_kwp": figs.get("capacity_kwp"), "title": p.title.split(" — ")[0],
            "total_incl_gst": str(v.total) if v.total is not None else None, "status": status,
            "valid_until": v.valid_until.isoformat() if v.valid_until else None,
            "customer": _mask(customer.name if customer else None), "issuer": "Grid Nova Energy"}


# ── Delivery ────────────────────────────────────────────────────────────────────

def _message_ctx(db: Session, p: StudioProposal, v: StudioProposalVersion, link: str) -> dict:
    customer = db.get(StudioCustomer, p.customer_id)
    doc = service.doc_of(v)
    name = " ".join(x for x in (doc.addressee.title, doc.addressee.name) if x) or (customer.name if customer else "")
    return {"name": name, "reference": v.document_reference, "title": p.title, "link": link,
            "valid_until": v.valid_until.strftime("%d.%m.%Y") if v.valid_until else ""}


def _fill(text: str, ctx: dict) -> str:
    for k, val in ctx.items():
        text = text.replace("{" + k + "}", str(val))
    return text


def _issued_version(db: Session, p: StudioProposal) -> StudioProposalVersion:
    v = service.version_of(db, p)
    if StudioStatus(v.status) not in service.OPEN_FOR_CUSTOMER:
        raise StudioError("invalid_status", f"Version {v.version} is {StudioStatus(v.status).value}; only an open, "
                                            "issued proposal can be sent.")
    return v


def whatsapp_link(db: Session, p: StudioProposal, actor: str) -> dict:
    v = _issued_version(db, p)
    link = service.public_url(service.link_token(v))
    ctx = _message_ctx(db, p, v, link)
    text = _fill(studio_config.load()["messages"]["whatsapp"], ctx)
    mobile = service.doc_of(v).addressee.mobile
    if not mobile:
        raise StudioError("missing_required", "The proposal has no mobile number.", 409, "addressee.mobile")
    url = f"https://wa.me/91{mobile}?text={quote(text)}"
    service.event(db, p, "whatsapp_link_created", version=v.version, actor=actor, detail={"mobile": mobile[-4:]})
    db.flush()
    return {"url": url, "text": text, "link": link}


def email(db: Session, p: StudioProposal, actor: str, to: Optional[str]) -> dict:
    """Email the customer the PDF and the online link from NOTIFY_FROM_EMAIL, reply-to the owner."""
    from app.notifications.notifier import notifier
    from app.storage import r2
    v = _issued_version(db, p)
    customer = db.get(StudioCustomer, p.customer_id)
    address = (to or (customer.email if customer else None) or "").strip()
    if not address:
        raise StudioError("no_customer_email", "The customer has no email address; add one or give `to`.", 422, "to")
    link = service.public_url(service.link_token(v))
    ctx = _message_ctx(db, p, v, link)
    msgs = studio_config.load()["messages"]
    try:
        pdf = r2.download_bytes(v.pdf_storage_key)
    except r2.StorageNotConfiguredError as exc:
        raise StudioError("storage_unavailable", "File storage is not configured.", 503) from exc
    sent, reason = notifier.send_studio_email(
        to=address, subject=_fill(msgs["email_subject"], ctx), lines=[_fill(x, ctx) for x in msgs["email_lines"]],
        link_label="Read and accept the proposal", link_url=link, reply_to=service.owner_email(db, p),
        attachment=(v.pdf_filename, base64.b64encode(pdf).decode()), event_type="studio_proposal_email")
    if not sent:
        raise StudioError(reason or "send_failed", "The email could not be sent: " + (reason or "send failed") + ".",
                          503 if reason == "email_not_configured" else 502)
    service.event(db, p, "emailed", version=v.version, actor=actor, detail={"to": address})
    db.flush()
    return {"sent": True, "to": address}


def _send_acceptance_emails(db: Session, p: StudioProposal, v: StudioProposalVersion, name: str) -> None:
    from app.notifications.notifier import notifier
    customer = db.get(StudioCustomer, p.customer_id)
    if not (customer and customer.email):
        return
    token = service.link_token(v)
    ctx = _message_ctx(db, p, v, service.public_url(token) if token else "")
    msgs = studio_config.load()["messages"]
    to, subject, lines = customer.email, _fill(msgs["accepted_subject"], ctx), [_fill(x, ctx)
                                                                               for x in msgs["accepted_lines"]]
    reply_to = service.owner_email(db, p)
    from app.memory.memory_manager import MemoryManager
    MemoryManager(db).notify_after_commit(
        lambda: notifier.send_studio_email(to=to, subject=subject, lines=lines, link_label=None, link_url=None,
                                           reply_to=reply_to, attachment=None, event_type="studio_accepted_customer"),
        "studio_accepted_customer")


def expiry_email(db: Session, p: StudioProposal, v: StudioProposalVersion) -> bool:
    from app.notifications.notifier import notifier
    customer = db.get(StudioCustomer, p.customer_id)
    token = service.link_token(v)
    if not (customer and customer.email and token):
        return False
    ctx = _message_ctx(db, p, v, service.public_url(token))
    msgs = studio_config.load()["messages"]
    sent, _ = notifier.send_studio_email(to=customer.email, subject=_fill(msgs["expiry_subject"], ctx),
                                         lines=[_fill(x, ctx) for x in msgs["expiry_lines"]],
                                         link_label="Read and accept the proposal", link_url=ctx["link"],
                                         reply_to=service.owner_email(db, p), attachment=None,
                                         event_type="studio_expiry_customer")
    return sent


def now() -> datetime:
    return datetime.now(timezone.utc)
