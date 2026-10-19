"""The handover pack: a checklist computed from real records wherever there is one, plus
three manual items (orientation, customer acknowledgement, O&M contact details given)
that staff tick — each tick audited with who, when and the before/after.

    commissioning_report   the document is on the project's execution files
    net_meter_report       the same, only where the net-meter stage applies to the project
    register_verified      Operations verified the serial register and every unit is verified
    final_payment          every payment milestone up to handover.final_payment_through_milestone
                           is verified (a yes/no only — Operations never sees an amount)
    orientation / acknowledgement / om_contact   manual

`gate` is the check `handover_complete` must pass (config handover.pack_required); it lists
what is missing in a structured 422, like the onboarding submit gate. Projects already at
handover_complete are never re-checked.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.associates.service import AssociateError
from app.handover import config
from app.memory.models import (
    AuditLog, CustomerOnboarding, HandoverPack, InstalledUnit, InstalledUnitStatus, ProjectExecution, ProjectStage,
)

ITEMS = ("commissioning_report", "net_meter_report", "register_verified", "final_payment", "orientation",
         "acknowledgement", "om_contact")
LABELS = {
    "commissioning_report": "Commissioning report uploaded",
    "net_meter_report": "Net-meter report uploaded",
    "register_verified": "Installed equipment register verified",
    "final_payment": "Final payment milestone verified",
    "orientation": "Customer orientation done",
    "acknowledgement": "Customer acknowledgement recorded",
    "om_contact": "O&M contact details given to the customer",
}
MANUAL = ("orientation", "acknowledgement", "om_contact")
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).isoformat()


def find(db: Session, project_id: int) -> Optional[HandoverPack]:
    return db.execute(select(HandoverPack).where(HandoverPack.project_id == project_id)).scalar_one_or_none()


def ensure(db: Session, onboarding: CustomerOnboarding) -> HandoverPack:
    pack = find(db, onboarding.project_id)
    if pack is None:
        pack = HandoverPack(project_id=onboarding.project_id, onboarding_id=onboarding.id, created_at=_now())
        db.add(pack)
        db.flush()
    return pack


def handed_over(db: Session, onboarding: CustomerOnboarding) -> bool:
    from app.onboarding import progress
    return ProjectStage.handover_complete in progress.recorded(db, onboarding.project_id)


def _net_meter_applies(db: Session, onboarding: CustomerOnboarding) -> bool:
    from app.onboarding import progress
    return ProjectStage.inspection_net_meter in progress.applicable(db, onboarding)


def _register_ok(db: Session, execution: Optional[ProjectExecution], onboarding: CustomerOnboarding) -> bool:
    if execution is None or execution.register_verified_at is None:
        return False
    statuses = list(db.execute(select(InstalledUnit.status).where(
        InstalledUnit.project_id == onboarding.project_id, InstalledUnit.status != InstalledUnitStatus.voided)).scalars())
    return bool(statuses) and all(s == InstalledUnitStatus.verified for s in statuses)


def items(db: Session, onboarding: CustomerOnboarding, pack: Optional[HandoverPack] = None) -> list[dict]:
    """Every checklist item with its state. Items that do not apply to the project are
    left out."""
    from app.execution import service as execution_service
    from app.payments import service as payments

    execution = execution_service.get(db, onboarding)
    documents = (execution.files or {}).get("documents") or {} if execution else {}
    pack = pack or find(db, onboarding.project_id)
    out = []

    def add(key: str, done: bool, source: str, **extra):
        out.append({"key": key, "label": LABELS[key], "message_key": f"handover.item.{key}", "done": bool(done),
                    "source": source, "done_at": None, "done_by": None, "note": None, **extra})

    add("commissioning_report", "commissioning_report" in documents, "record")
    if _net_meter_applies(db, onboarding):
        add("net_meter_report", "net_meter_report" in documents, "record")
    add("register_verified", _register_ok(db, execution, onboarding), "record")
    through = config.handover()["final_payment_through_milestone"]
    add("final_payment", payments.milestones_verified_through(db, onboarding.project_id, through), "record",
        through_milestone=through)
    add("orientation", pack is not None and pack.orientation_done_at is not None, "manual",
        done_at=_iso(pack.orientation_done_at) if pack else None, done_by=pack.orientation_done_by if pack else None,
        note=pack.orientation_note if pack else None)
    add("acknowledgement", pack is not None and pack.acknowledged_at is not None, "manual",
        done_at=_iso(pack.acknowledged_at) if pack else None, done_by=pack.acknowledged_by if pack else None,
        note=pack.ack_note if pack else None, method=pack.ack_method if pack else None,
        customer_name=pack.ack_customer_name if pack else None)
    add("om_contact", pack is not None and pack.om_contact_given_at is not None, "manual",
        done_at=_iso(pack.om_contact_given_at) if pack else None, done_by=pack.om_contact_given_by if pack else None,
        note=pack.om_contact_note if pack else None)
    return out


def missing(rows: list[dict]) -> list[str]:
    return [r["key"] for r in rows if not r["done"]]


def gate(db: Session, onboarding: CustomerOnboarding) -> None:
    """422 handover_pack_incomplete when the pack is required and not complete."""
    if not config.handover()["pack_required"]:
        return
    todo = missing(items(db, onboarding))
    if todo:
        raise AssociateError("The handover pack is not complete: " + ", ".join(LABELS[k] for k in todo) + ".",
                             code="handover_pack_incomplete", status_code=422,
                             extra={"missing": todo, "missing_labels": [LABELS[k] for k in todo]})


def summary(db: Session, onboarding: CustomerOnboarding, rows: Optional[list[dict]] = None) -> dict:
    rows = rows if rows is not None else items(db, onboarding)
    todo = missing(rows)
    return {"complete": not todo, "missing": todo, "done": len(rows) - len(todo), "total": len(rows)}


# ── Manual items ─────────────────────────────────────────────────────────────────

def _editable(db: Session, onboarding: CustomerOnboarding) -> HandoverPack:
    from app.execution import service as execution_service

    execution_service.require(db, onboarding)
    if handed_over(db, onboarding):
        raise AssociateError("This project has been handed over; its pack can no longer change.",
                             code="handover_recorded", status_code=409)
    return ensure(db, onboarding)


def _clean(note: Optional[str]) -> Optional[str]:
    return note.strip() or None if note else None


def set_tick(db: Session, onboarding: CustomerOnboarding, item: str, done: bool, actor: str,
             note: Optional[str] = None) -> HandoverPack:
    """Tick (or untick) `orientation` or `om_contact`."""
    prefix = {"orientation": "orientation_done", "om_contact": "om_contact_given"}[item]
    pack = _editable(db, onboarding)
    before = {"done": getattr(pack, f"{prefix}_at") is not None, "by": getattr(pack, f"{prefix}_by"),
              "note": getattr(pack, f"{item}_note")}
    setattr(pack, f"{prefix}_at", _now() if done else None)
    setattr(pack, f"{prefix}_by", actor if done else None)
    setattr(pack, f"{item}_note", _clean(note) if done else None)
    db.add(AuditLog(entity_type="handover_pack", entity_id=str(pack.id), action=f"{item}_{'ticked' if done else 'unticked'}",
                    performed_by=actor, before_value=before,
                    after_value={"done": done, "by": actor if done else None, "note": _clean(note) if done else None}))
    db.flush()
    return pack


def acknowledge(db: Session, onboarding: CustomerOnboarding, actor: str, *, method: str, customer_name: str,
                note: Optional[str] = None, signature: Optional[bytes] = None) -> HandoverPack:
    """The customer's acknowledgement: a signature image (PNG) or an in-person tick."""
    from app.storage import r2

    if method not in config.handover()["acknowledgement_methods"]:
        raise AssociateError("Choose how the customer acknowledged.", code="invalid_method", status_code=422,
                             field="method")
    customer_name = (customer_name or "").strip()
    if not customer_name:
        raise AssociateError("Enter the name of the person who acknowledged.", code="name_required", status_code=422,
                             field="customer_name")
    ref = None
    if method == "signature":
        limit = config.handover()["signature_max_kb"] * 1024
        if not signature or not signature.startswith(PNG_MAGIC):
            raise AssociateError("The signature must be a PNG image.", code="invalid_file", status_code=422,
                                 field="file")
        if len(signature) > limit:
            raise AssociateError(f"The signature image is larger than {limit // 1024} KB.", code="file_too_large",
                                 status_code=413, field="file")
    pack = _editable(db, onboarding)
    now = _now()
    if method == "signature":
        key = f"handover/{onboarding.project_id}/acknowledgement_{uuid.uuid4().hex}.png"
        r2.upload_bytes(key, signature, "image/png")
        ref = {"storage_key": key, "content_type": "image/png", "size_bytes": len(signature),
               "signed_at": now.isoformat(), "recorded_by": actor}
    before = {"done": pack.acknowledged_at is not None, "method": pack.ack_method,
              "signature_key": (pack.ack_signature_ref or {}).get("storage_key")}
    pack.ack_method, pack.acknowledged_at, pack.acknowledged_by = method, now, actor
    pack.ack_customer_name, pack.ack_signature_ref, pack.ack_note = customer_name, ref, _clean(note)
    db.add(AuditLog(entity_type="handover_pack", entity_id=str(pack.id), action="acknowledged", performed_by=actor,
                    before_value=before, after_value={"method": method, "customer_name": customer_name,
                                                      "signature_key": ref["storage_key"] if ref else None}))
    db.flush()
    return pack
