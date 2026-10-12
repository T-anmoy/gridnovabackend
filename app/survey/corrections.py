"""Survey corrections after verification (S1 Step 6, change control).

A verified customer can no longer be edited (app/onboarding/service.py), so the owning
associate asks for a change instead: {changes: {field: value, …}, reason}. Staff
(Engineer, Admin) apply or reject it; the requester can never be the one who applies it.

  * `changes` may name survey fields only (signatures.survey_fields()), never the
    consumer number: it is part of the customer's identity for attribution, which a
    verified customer has locked. Values go through the same request schema and the
    same survey validation (app/survey/validation.py) as an ordinary edit — when the
    request is made, and again on the survey as it stands when it is applied.
  * One pending request per onboarding (a partial unique index); a second is
    409 correction_pending.
  * Apply writes the change with an audit before/after, the derived values (shadow-free
    area, bill averages) as an edit would, clears the signatures when
    resign_after_edit is on, sends a reviewed survey back to the Engineer
    (survey_revised_after_review), records `survey_corrected` and flags open proposals
    for re-verification (app/survey/changes.py).
  * Apply and reject lock the request row and re-check it is still pending, so two
    reviewers can never both act on it. Requests are never deleted.

Draft and submitted customers are edited directly; a closed one is history.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.associates.service import AssociateError
from app.memory.memory_manager import MemoryManager
from app.memory.models import (
    CustomerOnboarding, OnboardingStatus, ProposalInputs, SurveyCorrectionRequest, SurveyCorrectionStatus,
    TimelineEventType,
)

NOT_CORRECTABLE = frozenset({"consumer_number"})
REASON_MAX = 1000


def correctable_fields() -> set[str]:
    from app.survey import signatures
    return signatures.survey_fields() - NOT_CORRECTABLE


def _jsonable(value: Any) -> Any:
    if hasattr(value, "value"):
        return value.value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    return value


def parse(changes: dict) -> dict:
    """The requested changes checked by the same request schema as PATCH
    /associate/onboardings/{id}, as service-ready values. 422 on anything else."""
    from pydantic import ValidationError

    from app.api.routes.associate_onboarding import OnboardingUpdate

    if not isinstance(changes, dict) or not changes:
        raise AssociateError("Say what should change.", code="no_changes", status_code=422, field="changes")
    allowed = correctable_fields()
    unknown = sorted(set(changes) - allowed)
    if unknown:
        code = "field_not_correctable" if set(unknown) & NOT_CORRECTABLE else "not_a_survey_field"
        raise AssociateError("Only survey fields can be corrected here: " + ", ".join(unknown) + ".",
                             code=code, status_code=422, field="changes", extra={"fields": unknown})
    try:
        body = OnboardingUpdate.model_validate(changes)
    except ValidationError as exc:
        first = exc.errors()[0]
        field = ".".join(str(p) for p in first.get("loc", ()))
        raise AssociateError(f"{field}: {first.get('msg')}", code="invalid_value", status_code=422,
                             field="changes", extra={"changes_field": field})
    return body.to_service(set(body.model_fields_set))


def _correctable_status(onboarding: CustomerOnboarding) -> None:
    if onboarding.status in (OnboardingStatus.draft, OnboardingStatus.submitted):
        raise AssociateError("This customer can still be edited directly; change the survey there.",
                             code="edit_directly", status_code=409)
    if onboarding.status != OnboardingStatus.verified:
        raise AssociateError(f"This customer is {onboarding.status.value}; its survey can no longer be corrected.",
                             code="not_editable", status_code=409)


def create(db: Session, onboarding: CustomerOnboarding, requested_by: str, changes: dict,
           reason: str) -> SurveyCorrectionRequest:
    """Record a correction request. Caller commits."""
    from app.survey import validation

    _correctable_status(onboarding)
    reason = (reason or "").strip()
    if not reason:
        raise AssociateError("Say why the survey needs correcting.", code="reason_required", status_code=422,
                             field="reason")
    parsed = parse(changes)
    validation.prepare(parsed, db.get(ProposalInputs, onboarding.proposal_inputs_id))
    stored = _jsonable({k: v for k, v in changes.items()})
    request = SurveyCorrectionRequest(onboarding_id=onboarding.id, requested_by=requested_by, changes=stored,
                                      reason=reason, status=SurveyCorrectionStatus.pending)
    try:
        with db.begin_nested():
            db.add(request)
            db.flush()
    except IntegrityError:
        raise AssociateError("A correction for this customer is already waiting for Grid Nova.",
                             code="correction_pending", status_code=409)
    from app.onboarding import service
    service._audit(db, onboarding.id, "survey_correction_requested", requested_by,
                   after={"correction_id": request.id, "changes": stored, "reason": reason})
    MemoryManager(db).add_timeline_event(
        lead_id=onboarding.lead_id, event_type=TimelineEventType.survey_correction_requested,
        description="Survey correction requested: " + ", ".join(sorted(stored)), created_by=requested_by)
    return request


def serialize(request: SurveyCorrectionRequest, *, staff: bool = False) -> dict:
    data = {"correction_id": request.id, "onboarding_id": request.onboarding_id, "changes": request.changes,
            "reason": request.reason, "status": request.status.value,
            "requested_at": request.created_at.isoformat() if request.created_at else None,
            "reviewed_at": request.reviewed_at.isoformat() if request.reviewed_at else None,
            "review_note": request.review_note}
    if staff:
        data.update(requested_by=request.requested_by, reviewed_by=request.reviewed_by)
    return data


def for_onboarding(db: Session, onboarding_id: int) -> list[SurveyCorrectionRequest]:
    return list(db.execute(select(SurveyCorrectionRequest).where(
        SurveyCorrectionRequest.onboarding_id == onboarding_id)
        .order_by(SurveyCorrectionRequest.created_at.desc(), SurveyCorrectionRequest.id.desc())).scalars())


def _locked_pending(db: Session, correction_id: int, employee_id: str) -> SurveyCorrectionRequest:
    request = db.execute(select(SurveyCorrectionRequest).where(SurveyCorrectionRequest.id == correction_id)
                         .with_for_update().execution_options(populate_existing=True)).scalar_one_or_none()
    if request is None:
        raise AssociateError("Correction not found.", code="correction_not_found", status_code=404)
    if request.status != SurveyCorrectionStatus.pending:
        raise AssociateError(f"This correction was already {request.status.value}.", code="correction_not_pending",
                             status_code=409)
    if request.requested_by == employee_id:
        raise AssociateError("The person who asked for a correction cannot also decide it.",
                             code="segregation_of_duties", status_code=403)
    return request


def apply(db: Session, correction_id: int, employee_id: str, note: Optional[str] = None) -> dict:
    """Write the requested change. Returns {correction, flagged_proposal_ids}. Caller commits."""
    from app.onboarding import service
    from app.survey import changes as reverification, review, signatures, validation

    request = _locked_pending(db, correction_id, employee_id)
    onboarding = db.get(CustomerOnboarding, request.onboarding_id)
    _correctable_status(onboarding)
    inputs = db.get(ProposalInputs, onboarding.proposal_inputs_id)
    prepared = validation.prepare(parse(request.changes), inputs)
    before: dict = {}
    after: dict = {}
    for field, value in prepared.items():
        if getattr(inputs, field) != value:
            before[field], after[field] = getattr(inputs, field), value
            setattr(inputs, field, value)
    if after:
        cleared_before, cleared_after = signatures.clear_after_edit(inputs, set(after))
        before.update(cleared_before)
        after.update(cleared_after)
        revised_before, revised_after = review.mark_revised(inputs)
        before.update(revised_before)
        after.update(revised_after)
    now = datetime.now(timezone.utc)
    request.status = SurveyCorrectionStatus.applied
    request.reviewed_by = employee_id
    request.reviewed_at = now
    request.review_note = (note or "").strip() or None
    db.flush()
    service._audit(db, onboarding.id, "survey_corrected", employee_id,
                   before=service._jsonable(before), after={**service._jsonable(after), "correction_id": request.id})
    MemoryManager(db).add_timeline_event(
        lead_id=onboarding.lead_id, event_type=TimelineEventType.survey_corrected,
        description="Survey correction applied: " + (", ".join(sorted(f for f in after if f in prepared))
                                                     or "no change"), created_by=employee_id)
    flagged = reverification.flag(db, onboarding, set(after) & set(prepared), source="correction",
                                  performed_by=employee_id, correction_id=request.id)
    return {"correction": serialize(request, staff=True), "flagged_proposal_ids": flagged}


def reject(db: Session, correction_id: int, employee_id: str, note: str) -> dict:
    from app.onboarding import service

    note = (note or "").strip()
    if not note:
        raise AssociateError("Say why the correction is rejected.", code="note_required", status_code=422,
                             field="note")
    request = _locked_pending(db, correction_id, employee_id)
    request.status = SurveyCorrectionStatus.rejected
    request.reviewed_by = employee_id
    request.reviewed_at = datetime.now(timezone.utc)
    request.review_note = note
    db.flush()
    service._audit(db, request.onboarding_id, "survey_correction_rejected", employee_id,
                   before={"correction_id": request.id, "status": "pending"},
                   after={"correction_id": request.id, "status": "rejected", "note": note})
    return {"correction": serialize(request, staff=True)}
