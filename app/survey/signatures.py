"""On-screen signatures on the survey: the customer's (with a typed declaration name and
an explicit confirmation of the data) and the associate's. PNG only, typed by its
bytes, kept in private storage; the server records when it was signed. A new signature
replaces the reference but never deletes the earlier file (the audit keeps its key).

When `resign_after_edit` is on, a change to any survey field after signing clears both
signatures — the customer signed what was on screen then."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.associates.service import AssociateError
from app.memory.memory_manager import MemoryManager
from app.memory.models import CustomerOnboarding, OnboardingStatus, ProposalInputs, TimelineEventType
from app.survey import config

PNG = "image/png"


def max_bytes() -> int:
    return int(config.load()["signature_max_kb"]) * 1024


def survey_fields() -> set[str]:
    """Every field the customer signs for: the survey sections (all variants and
    conditional fields) plus the survey-only fields."""
    from app.onboarding.service import SURVEY_FIELDS

    cfg = config.load()
    fields = set(SURVEY_FIELDS) | {"consumption_history", "avg_monthly_units_kwh", "avg_monthly_bill_inr"}
    for s in cfg["sections"]:
        fields |= set(s["fields"])
    for extra in cfg["variants"].values():
        for more in (extra or {}).values():
            fields |= set(more)
    for rule in cfg.get("conditional") or []:
        fields |= set(rule["require"])
    return fields


def _ref_summary(ref: Optional[dict]) -> Optional[dict]:
    return {"storage_key": ref.get("storage_key"), "signed_at": ref.get("signed_at")} if ref else None


def clear_after_edit(inputs: ProposalInputs, changed: set[str]) -> tuple[dict, dict]:
    """(before, after) audit fragments when an edit to a signed survey clears the
    signatures; ({}, {}) otherwise. Caller audits."""
    if not config.load()["resign_after_edit"] or not (changed & survey_fields()):
        return {}, {}
    if not (inputs.customer_signature_ref or inputs.associate_signature_ref):
        return {}, {}
    before = {"signatures": {"customer": _ref_summary(inputs.customer_signature_ref),
                             "associate": _ref_summary(inputs.associate_signature_ref),
                             "declaration_name": inputs.customer_declaration_name}}
    inputs.customer_signature_ref = None
    inputs.associate_signature_ref = None
    inputs.customer_declaration_name = None
    inputs.customer_declared_at = None
    return before, {"signatures": "cleared_after_edit"}


def record(db: Session, onboarding: CustomerOnboarding, party: str, *, content: bytes, performed_by: str,
           declaration_name: Optional[str] = None) -> ProposalInputs:
    """Store one signature (bytes already checked to be a PNG within the limit). The
    customer's needs the declaration name. Draft or submitted; a submitted customer
    returns to draft, like any edit. A verified customer is signed again only where an
    applied survey correction cleared that signature (app/survey/corrections.py) — it
    stays verified. Caller commits."""
    from app.onboarding import service
    from app.storage import r2

    inputs = db.get(ProposalInputs, onboarding.proposal_inputs_id)
    attr = "customer_signature_ref" if party == "customer" else "associate_signature_ref"
    resign = onboarding.status == OnboardingStatus.verified and not (getattr(inputs, attr) or {}).get("storage_key")
    if onboarding.status not in service.EDITABLE_STATUSES and not resign:
        raise AssociateError(f"This customer is {onboarding.status.value} and can no longer be signed here.",
                             code="not_editable", status_code=409)
    now = datetime.now(timezone.utc)
    key = f"proposal-inputs/{inputs.id}/signature/{party}_{uuid.uuid4().hex}.png"
    r2.upload_bytes(key, content, PNG)
    ref = {"filename": f"{party}_signature.png", "storage_key": key, "signed_at": now.isoformat(),
           "content_type": PNG, "size_bytes": len(content), "signed_by": performed_by}
    before = {party: _ref_summary(getattr(inputs, attr))}
    after = {party: _ref_summary(ref)}
    setattr(inputs, attr, ref)
    if party == "customer":
        before["declaration_name"] = inputs.customer_declaration_name
        inputs.customer_declaration_name = declaration_name
        inputs.customer_declared_at = now
        after.update(declaration_name=declaration_name, customer_confirms_data=True)
    if onboarding.status == OnboardingStatus.submitted:
        before["status"], after["status"] = "submitted", "draft"
        onboarding.status = OnboardingStatus.draft
        onboarding.submitted_at = None
    db.flush()
    service._audit(db, onboarding.id, "survey_signed", performed_by, before=before, after=after)
    MemoryManager(db).add_timeline_event(
        lead_id=onboarding.lead_id, event_type=TimelineEventType.survey_signed,
        description=f"Site survey signed by the {party}", created_by=performed_by)
    return inputs
