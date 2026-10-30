"""Handover orchestration: what happens when `handover_complete` is recorded, rendering and
emailing the pack, and the JSON shape staff / associates / the customer see.

    progress.record(handover_complete) ─► on_handover_complete (same transaction)
        warranty register written · pack ensured · customer link issued
        after commit, off the request path: render the PDF, then email the link
        (held while launch-blocking text is a dummy; skipped without a customer email)
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.associates.service import AssociateError
from app.handover import link, pack as pack_module, pdf, warranty as warranty_register
from app.memory.models import AuditLog, Customer, CustomerOnboarding, HandoverPack, Project, ProjectStageEvent

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def on_handover_complete(db: Session, onboarding: CustomerOnboarding, actor: str, event: ProjectStageEvent) -> None:
    """Inside the stage-recording transaction. Never sends anything before the commit."""
    from app.jobs import runner
    from app.memory.memory_manager import MemoryManager

    warranty_register.create(db, onboarding, actor, event)
    pack = pack_module.ensure(db, onboarding)
    token = link.issue(db, pack, actor)
    pack_id = pack.id
    MemoryManager(db).notify_after_commit(
        lambda: runner.submit(lambda: generate_and_deliver(pack_id, token), "handover_pack"), "handover_pack")


def generate_and_deliver(pack_id: int, token: Optional[str], *, regenerate: bool = False) -> None:
    """After the commit, in its own session: render (unless held) and email the customer
    the link (when a token is given). Never raises."""
    from app.memory.db import SessionLocal

    db = SessionLocal()
    try:
        pack = db.get(HandoverPack, pack_id)
        if pack is None:
            return
        reason = pdf.hold_reason(db)
        if reason:
            pack.held_reason, pack.email_status = reason, "held"
            db.commit()
            _blocked(db, pack, "held")
            return
        pack.held_reason = None
        if (pack.pdf_key is None or regenerate) and not _render(db, pack):
            db.commit()
            _blocked(db, pack, "render_failed")
            return
        if token:
            _deliver(db, pack, token)
        db.commit()
    except Exception:
        logger.exception("Handover pack %s: generate/deliver failed", pack_id)
        db.rollback()
    finally:
        db.close()


def _render(db: Session, pack: HandoverPack) -> bool:
    from app.associates import documents
    from app.storage import r2

    onboarding = db.get(CustomerOnboarding, pack.onboarding_id)
    try:
        content = documents.render_pdf(pdf.build_html(db, onboarding, pack))
        key = f"handover/{pack.project_id}/handover-pack-{uuid.uuid4().hex}.pdf"
        r2.upload_bytes(key, content, "application/pdf")
    except Exception as exc:
        pack.render_error = f"{type(exc).__name__}: {exc}"[:500]
        return False
    pack.pdf_key, pack.pdf_generated_at, pack.render_error = key, _now(), None
    db.flush()
    return True


def _deliver(db: Session, pack: HandoverPack, token: str) -> None:
    from app.memory.memory_manager import is_placeholder_email
    from app.notifications import notifier as notifier_module

    customer = db.get(Customer, db.get(Project, pack.project_id).customer_id)
    email = customer.email if customer else None
    if not email or is_placeholder_email(email):
        pack.email_status, pack.email_skipped_reason = "skipped", "no_customer_email"
        db.commit()
        _blocked(db, pack, "no_customer_email")
        return
    url = link.public_pdf_url(token)
    if url is None:
        pack.email_status, pack.email_skipped_reason = "failed", "no_public_api_base_url"
        db.commit()
        _blocked(db, pack, "no_public_api_base_url")
        return
    sent, why = notifier_module.notifier.send_handover_to_customer(
        customer_email=email, customer_name=customer.name, project_reference=db.get(Project, pack.project_id).reference_id,
        pack_url=url)
    if sent:
        pack.email_status, pack.emailed_at, pack.email_skipped_reason = "sent", _now(), None
    else:
        pack.email_status, pack.email_skipped_reason = "failed", why
    db.commit()
    if not sent:
        _blocked(db, pack, why or "send_failed")


def _blocked(db: Session, pack: HandoverPack, reason: str) -> None:
    from app.notifications import notifier as notifier_module
    try:
        customer = db.get(Customer, db.get(Project, pack.project_id).customer_id)
        notifier_module.notifier.notify("handover_blocked", {
            "onboarding_id": pack.onboarding_id, "reason": reason, "customer_name": customer.name if customer else None,
            "project_reference_id": db.get(Project, pack.project_id).reference_id})
    except Exception:
        logger.exception("handover_blocked notification failed")


def send_link(db: Session, onboarding: CustomerOnboarding, actor: str, *, send_email: bool = True) -> dict:
    """Staff: issue a NEW customer link (the old one stops working), render the PDF if it
    does not exist, and email it. 409 handover_not_recorded before the handover; 409
    handover_pdf_held while launch-blocking text is a dummy. The link is returned once."""
    from app.jobs import runner
    from app.memory.memory_manager import MemoryManager

    if not pack_module.handed_over(db, onboarding):
        raise AssociateError("The link exists once the project is handed over.", code="handover_not_recorded",
                             status_code=409)
    reason = pdf.hold_reason(db)
    if reason:
        raise AssociateError("The handover pack cannot be sent while its text is a placeholder or not yet "
                             "approved by an Admin.",
                             code="handover_pdf_held", status_code=409, extra={"held_reason": reason})
    warranty_register.ensure(db, onboarding, actor)
    pack = pack_module.ensure(db, onboarding)
    token = link.issue(db, pack, actor)
    pack.email_status = pack.email_skipped_reason = None
    pack_id = pack.id
    MemoryManager(db).notify_after_commit(
        lambda: runner.submit(lambda: generate_and_deliver(pack_id, token if send_email else None), "handover_pack"),
        "handover_pack")
    return {"url": link.public_pdf_url(token), "token": token, "expires_at": pack.link_expires_at.isoformat()
            if pack.link_expires_at else None}


def regenerate_pdf(db: Session, onboarding: CustomerOnboarding, actor: str) -> HandoverPack:
    from app.jobs import runner
    from app.memory.memory_manager import MemoryManager

    if not pack_module.handed_over(db, onboarding):
        raise AssociateError("The pack is rendered once the project is handed over.", code="handover_not_recorded",
                             status_code=409)
    if pdf.hold_reason(db):
        raise AssociateError("The handover pack cannot be rendered while its text is a placeholder or not yet "
                             "approved by an Admin.",
                             code="handover_pdf_held", status_code=409, extra={"held_reason": pdf.hold_reason(db)})
    warranty_register.ensure(db, onboarding, actor)
    pack = pack_module.ensure(db, onboarding)
    db.add(AuditLog(entity_type="handover_pack", entity_id=str(pack.id), action="pdf_regenerate_requested",
                    performed_by=actor))
    pack_id = pack.id
    MemoryManager(db).notify_after_commit(
        lambda: runner.submit(lambda: generate_and_deliver(pack_id, None, regenerate=True), "handover_pack"),
        "handover_pack")
    return pack


# ── JSON ─────────────────────────────────────────────────────────────────────────

def pdf_status(pack: Optional[HandoverPack], held: Optional[str]) -> str:
    if pack is None or not pack.pdf_key:
        return "held" if held else ("failed" if pack is not None and pack.render_error else "not_generated")
    return "ready"


def detail(db: Session, onboarding: CustomerOnboarding, *, staff: bool) -> dict:
    """The pack for one customer. Identical for staff and associates except `link` and
    `email` (staff only). No money."""
    pack = pack_module.find(db, onboarding.project_id)
    rows = pack_module.items(db, onboarding, pack)
    recorded = pack_module.handed_over(db, onboarding)
    warranty = warranty_register.get(db, onboarding.project_id) if recorded else None
    held = pdf.hold_reason(db)
    from app.handover import config
    body = {"onboarding_id": onboarding.id, "project_id": onboarding.project_id,
            "handover_recorded": recorded, "pack_required": config.handover()["pack_required"],
            "items": rows, **pack_module.summary(db, onboarding, rows),
            "warranty": warranty_register.serialize(db, warranty),
            "pdf": {"status": pdf_status(pack, held if recorded else None), "held_reason": held if recorded else None,
                    "generated_at": pack.pdf_generated_at.isoformat() if pack and pack.pdf_generated_at else None}}
    if staff:
        body["link"] = {"state": link.state(pack) if pack else "none",
                        "issued_at": pack.link_issued_at.isoformat() if pack and pack.link_issued_at else None,
                        "issued_by": pack.link_issued_by if pack else None,
                        "expires_at": pack.link_expires_at.isoformat() if pack and pack.link_expires_at else None,
                        "revoked_at": pack.link_revoked_at.isoformat() if pack and pack.link_revoked_at else None}
        body["email"] = {"status": pack.email_status if pack else None,
                         "skipped_reason": pack.email_skipped_reason if pack else None,
                         "emailed_at": pack.emailed_at.isoformat() if pack and pack.emailed_at else None}
    return body
