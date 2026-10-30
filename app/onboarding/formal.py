"""The FORMAL proposal for an associate-sourced customer — a separate, deliberate step
after the site assessment, never an automatic consequence of onboarding.

  request_formal_proposal   associate (owner) or staff ask Grid Nova to prepare it.
                            Records `proposal_requested` with whether the lead is ready;
                            creates no draft.

Both requesting and drafting need the Engineer's survey review to allow it
(app/survey/review.py formal_proposal_problem): not_feasible always blocks.
  prepare_formal_proposal   staff (Admin/Sales) draft it through the same Proposal Agent
                            entry point as every other draft — approval tiers, manager
                            sign-off and the send-guard all apply unchanged.
  record_in_person_acceptance
                            the associate (owner) or staff record that the approved
                            formal proposal was handed over and accepted in person — the
                            alternative to the emailed accept link. Both paths reach the
                            same `accepted` stage; the evidence says which one was used.

The indicative estimate shown during onboarding (app/onboarding/estimate.py) is
unrelated to all of this.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.associates.service import AssociateError
from app.memory.memory_manager import MemoryManager
from app.memory.models import (
    CustomerOnboarding, Lead, PipelineStage, ProjectStage, Proposal, ProposalStatus, TimelineEventType,
)
from app.onboarding import progress
from app.rules.business_rules import check_missing_fields

logger = logging.getLogger(__name__)


# How each missing lead field reads to a person (staff email, blocked marker).
MISSING_FIELD_LABELS = {
    "email": "no email on file",
    "name": "no customer name",
    "project_type": "no project type",
    "location": "no site location",
    "capacity_kw": "no requested capacity (kW)",
}


def readiness(db: Session, onboarding: CustomerOnboarding) -> list[str]:
    """The lead fields a formal draft still needs (empty = ready) — the same check the
    Proposal Agent's gate applies (app/rules/business_rules.REQUIRED_FIELDS)."""
    return check_missing_fields(db.get(Lead, onboarding.lead_id).collected_fields or {})


def _require_assessed(db: Session, onboarding: CustomerOnboarding) -> dict:
    done = progress.recorded(db, onboarding.project_id)
    if ProjectStage.site_assessed not in done:
        raise AssociateError("The site assessment has not been approved by Grid Nova yet.",
                             code="not_assessed", status_code=409)
    return done


def blocked(db: Session, onboarding: CustomerOnboarding) -> Optional[dict]:
    """`formal_proposal_blocked`: the formal proposal was requested but cannot be
    prepared, because the customer record lacks what a draft needs. Computed from the
    record as it is NOW, so it clears the moment the gap is filled. None otherwise."""
    done = progress.recorded(db, onboarding.project_id)
    if ProjectStage.proposal_prepared in done:
        return None
    request = done.get(ProjectStage.proposal_requested)
    requested = {"requested_at": request.recorded_at.isoformat() if request else None,
                 "requested_by": request.recorded_by if request else None}
    missing = readiness(db, onboarding)
    if request is not None and missing:
        return {"reason": "missing_information", "missing_fields": missing,
                "missing": [MISSING_FIELD_LABELS.get(f, f) for f in missing],
                "draft_error": None, "draft_failed_at": None, **requested}
    if onboarding.formal_draft_failed_at is not None:
        # The record is complete (or was completed in the same call), but the draft
        # itself failed — a different problem, and it needs a retry, not more data.
        return {"reason": "draft_failed", "missing_fields": missing,
                "missing": [MISSING_FIELD_LABELS.get(f, f) for f in missing],
                "draft_error": onboarding.formal_draft_failure,
                "draft_failed_at": onboarding.formal_draft_failed_at.isoformat(), **requested}
    return None


def request_formal_proposal(db: Session, onboarding: CustomerOnboarding, requested_by: str) -> dict:
    """Record the request and email staff (after commit): which customer, and — if the
    record is not ready — exactly what is missing. Caller commits. Returns
    {ready, missing_fields}."""
    import app.notifications.notifier as notifier_module
    from app.memory.models import Customer, Project

    done = _require_assessed(db, onboarding)
    from app.survey import review as survey_review
    survey_review.require_formal_proposal_allowed(db, onboarding)
    if ProjectStage.proposal_requested in done:
        raise AssociateError("The formal proposal has already been requested for this customer.",
                             code="already_requested", status_code=409)
    missing = readiness(db, onboarding)
    state = {"ready": not missing, "missing_fields": missing}
    progress.record(db, onboarding, ProjectStage.proposal_requested, requested_by,
                    evidence={"requested_by": requested_by, **state})
    payload = {"onboarding_id": onboarding.id, "lead_id": onboarding.lead_id, "requested_by": requested_by,
               "customer_name": db.get(Customer, onboarding.customer_id).name,
               "project_reference_id": db.get(Project, onboarding.project_id).reference_id,
               "missing": [MISSING_FIELD_LABELS.get(f, f) for f in missing], **state}
    MemoryManager(db).notify_after_commit(
        lambda: notifier_module.notifier.notify("formal_proposal_requested", payload),
        f"formal proposal requested onboarding {onboarding.id}")
    return state


def set_customer_email(db: Session, onboarding: CustomerOnboarding, email: str, employee_id: str) -> bool:
    """Give an associate-sourced customer the email they are missing (staff). Only fills
    a gap: a customer who already has a real email keeps it, and an address already
    belonging to another customer is refused rather than merging two customers.
    Returns whether anything changed. Caller commits."""
    from app.memory.lead_fields import apply_lead_fields
    from app.memory.memory_manager import is_placeholder_email
    from app.memory.models import AuditLog, Customer

    memory = MemoryManager(db)
    customer = db.get(Customer, onboarding.customer_id)
    if not is_placeholder_email(customer.email):
        if customer.email == email:
            return False
        raise AssociateError("This customer already has an email on file; it is not replaced here.",
                             code="email_already_set", status_code=409, field="customer_email")
    other = db.execute(select(Customer).where(func.lower(Customer.email) == email,
                                              Customer.id != customer.id)).scalar_one_or_none()
    if other is not None:
        raise AssociateError("That email belongs to another customer record — check it with the associate.",
                             code="email_in_use", status_code=409, field="customer_email")
    previous = customer.email
    memory.update_customer(customer.id, performed_by=employee_id, email=email)
    apply_lead_fields(memory, db.get(Lead, onboarding.lead_id), {"email": email}, performed_by=employee_id,
                      via="staff fix-in-place on an associate-sourced customer")
    db.add(AuditLog(entity_type="customer_onboarding", entity_id=str(onboarding.id), action="customer_email_set",
                    performed_by=employee_id, before_value={"email": previous}, after_value={"email": email}))
    return True


def _record_draft_failure(db: Session, onboarding_id: int, reason: str, employee_id: str) -> None:
    from app.memory.models import AuditLog

    onboarding = db.get(CustomerOnboarding, onboarding_id)
    onboarding.formal_draft_failed_at = datetime.now(timezone.utc)
    onboarding.formal_draft_failure = reason[:1000]
    db.add(AuditLog(entity_type="customer_onboarding", entity_id=str(onboarding_id),
                    action="formal_draft_failed", performed_by=employee_id, after_value={"error": reason}))


def prepare_formal_proposal(db: Session, onboarding: CustomerOnboarding, employee_id: str,
                            customer_email: Optional[str] = None) -> dict:
    """Draft the formal proposal (staff) — optionally first filling in the customer's
    missing email, in the same call (the fix-in-place for a blocked request). Commits:
    the Proposal Agent writes through its own session. Returns {proposal_id, version,
    status, …}."""
    from app.orchestrator.orchestrator import orchestrator

    done = _require_assessed(db, onboarding)
    if ProjectStage.proposal_prepared in done:
        raise AssociateError("The formal proposal is already prepared; revise it from the proposal itself.",
                             code="already_prepared", status_code=409)
    from app.survey import review as survey_review
    survey_review.require_formal_proposal_allowed(db, onboarding)
    if customer_email:
        set_customer_email(db, onboarding, customer_email, employee_id)
    missing = readiness(db, onboarding)
    if missing:
        raise AssociateError("The customer record is missing what a formal proposal needs: "
                             + "; ".join(MISSING_FIELD_LABELS.get(f, f) for f in missing) + ".",
                             code="formal_proposal_not_ready", status_code=409)
    lead_id, customer_id, onboarding_id = onboarding.lead_id, onboarding.customer_id, onboarding.id
    db.commit()  # the draft below runs in its own session
    try:
        outcome = orchestrator.request_proposal_draft(lead_id=lead_id, customer_id=customer_id,
                                                      performed_by=employee_id)
    except Exception as exc:  # recorded below, never a bare 500 after the email was saved
        logger.exception("Formal proposal draft raised for onboarding %s", onboarding_id)
        outcome = {"error": f"The draft failed unexpectedly ({type(exc).__name__})."}
    db.expire_all()
    if not outcome.get("proposal_id"):
        reason = outcome.get("error") or "The formal proposal could not be drafted."
        _record_draft_failure(db, onboarding_id, reason, employee_id)
        db.commit()
        raise AssociateError(reason + " Nothing else changed; retry once it is resolved.",
                             code="draft_failed", status_code=409)
    from app.notifications.notifier import announce_proposal_ready
    announce_proposal_ready(db, outcome["proposal_id"], lead_id, employee_id, onboarding_id=onboarding_id)
    status = outcome.get("proposal_status")
    return {"proposal_id": outcome["proposal_id"], "version": outcome.get("proposal_version"),
            "status": status.value if hasattr(status, "value") else status,
            "requires_manager_signoff": outcome.get("requires_manager_signoff"),
            "onboarding_id": onboarding_id}


def record_in_person_acceptance(db: Session, onboarding: CustomerOnboarding, recorded_by: str, *,
                                proposal_id: int, delivered_on: date, note: str, reference: str,
                                today: date) -> Proposal:
    """The formal proposal was handed to the customer and accepted in person. Caller
    commits. Applies every guard the online path has: staff approval (incl. manager
    sign-off where required), the send-guard (placeholder content and re-verification
    after a survey change), the latest version only, and validity."""
    import app.notifications.notifier as notifier_module
    from app.memory import reflection
    from app.proposals import acceptance
    from app.proposals.send_guard import placeholder_problem, reverification_problem

    note, reference = (note or "").strip(), (reference or "").strip()
    if not note:
        raise AssociateError("Say what happened when the proposal was delivered.", code="note_required",
                             status_code=422, field="note")
    if not reference:
        raise AssociateError("Give a reference for the in-person delivery (for example what was handed over, "
                             "or 'verbal acknowledgement').", code="reference_required", status_code=422,
                             field="reference")
    if delivered_on > today:
        raise AssociateError("delivered_on cannot be in the future.", code="invalid_delivered_on",
                             status_code=422, field="delivered_on")
    proposal = db.get(Proposal, proposal_id)
    if proposal is None or proposal.lead_id != onboarding.lead_id:
        raise AssociateError("That proposal is not this customer's.", code="proposal_not_found", status_code=404,
                             field="proposal_id")
    # Same lock as the online accept link (app/proposals/acceptance.py): everything
    # below is decided on what is true under it.
    proposal = acceptance.lock(db, proposal.id)
    latest = db.execute(select(func.max(Proposal.version)).where(Proposal.lead_id == onboarding.lead_id)).scalar_one()
    if proposal.version != latest:
        raise AssociateError(f"Only the latest version (v{latest}) can be accepted.", code="not_latest_version",
                             status_code=409, field="proposal_id")
    if proposal.accepted_at is not None:
        raise AssociateError("This proposal has already been accepted.", code="already_accepted", status_code=409)
    if acceptance.superseded(db, proposal):
        raise AssociateError("Another proposal for this customer has already been accepted.",
                             code=acceptance.SUPERSEDED, status_code=409)
    if proposal.status not in (ProposalStatus.approved, ProposalStatus.sent):
        raise AssociateError(f"Only a proposal approved by Grid Nova can be handed over (this one is "
                             f"{proposal.status.value}).", code="not_approved", status_code=409)
    problem = placeholder_problem(proposal, db)
    if problem:
        extra = {k: problem[k] for k in ("missing", "unapproved_sections") if problem.get(k)}
        raise AssociateError(problem["message"], code=problem["code"], status_code=409, extra=extra or None)
    problem = reverification_problem(proposal)
    if problem:
        raise AssociateError(problem["message"], code=problem["code"], status_code=409,
                             extra={"fields": problem["fields"]})

    memory = MemoryManager(db)
    handed_over = datetime.combine(delivered_on, time(0, 0), tzinfo=timezone.utc)
    issued = proposal.date_sent or handed_over
    issued = issued if issued.tzinfo else issued.replace(tzinfo=timezone.utc)
    days = acceptance.validity_days()
    if (datetime.combine(today, time(0, 0), tzinfo=timezone.utc) - issued).days > days:
        raise AssociateError(f"This proposal is more than {days} days old and has expired.",
                             code="expired", status_code=409)
    details = {"delivered_on": delivered_on.isoformat(), "reference": reference}
    if proposal.status == ProposalStatus.approved:
        memory.transition_proposal_status(
            proposal_id=proposal.id, new_status=ProposalStatus.sent, performed_by=recorded_by,
            date_sent=handed_over, issued_via="handed over in person", issue_details=details,
        )
        memory.add_timeline_event(lead_id=proposal.lead_id, event_type=TimelineEventType.proposal_sent,
                                  description=f"Handed over in person ({reference})", created_by=recorded_by)
        memory.advance_lead_stage(proposal.lead_id, PipelineStage.proposal_sent, performed_by=recorded_by,
                                  description=f"Proposal v{proposal.version} handed over in person")
    memory.mark_customer_accepted(proposal.id, via="in person", recorded_by=recorded_by,
                                  details={**details, "note": note})
    memory.add_timeline_event(lead_id=proposal.lead_id, event_type=TimelineEventType.customer_accepted,
                              description=f"Customer accepted the proposal in person — {note}",
                              created_by=recorded_by)
    memory.advance_lead_stage(proposal.lead_id, PipelineStage.won, performed_by=recorded_by,
                              description=f"Customer accepted proposal v{proposal.version} in person")
    reflection.record_proposal_outcome(memory, proposal.id, reflection.OUTCOME_ACCEPTED, performed_by=recorded_by)
    customer = proposal.lead.customer if proposal.lead else None
    accepted = {"proposal_id": proposal.id, "lead_id": proposal.lead_id, "version": proposal.version,
                "customer_name": customer.name if customer else None}
    memory.notify_after_commit(lambda: notifier_module.notifier.customer_accepted(**accepted), "customer_accepted")
    return proposal



def serialize_latest(db: Session, onboarding: CustomerOnboarding, *, with_pdf: bool = True) -> dict:
    """The latest formal proposal (or {"prepared": false}). A download link is included
    once Grid Nova has approved it — never for a draft — and only when `with_pdf`."""
    proposal = db.execute(select(Proposal).where(Proposal.lead_id == onboarding.lead_id)
                          .order_by(Proposal.version.desc())).scalars().first()
    if proposal is None:
        return {"prepared": False, "missing_fields": readiness(db, onboarding)}
    handed_out = proposal.status in (ProposalStatus.approved, ProposalStatus.sent, ProposalStatus.accepted)
    data = {"prepared": True, "proposal_id": proposal.id, "version": proposal.version,
            "status": proposal.status.value, "total_value": proposal.total_value if handed_out else None,
            "approved_at": proposal.approved_at.isoformat() if proposal.approved_at else None,
            "date_sent": proposal.date_sent.isoformat() if proposal.date_sent else None,
            "accepted_at": proposal.accepted_at.isoformat() if proposal.accepted_at else None,
            "ready_to_hand_over": proposal.status in (ProposalStatus.approved, ProposalStatus.sent)
                                  and proposal.accepted_at is None,
            "pdf": None}
    if handed_out and with_pdf:
        key = MemoryManager.proposal_pdf_key(proposal)
        if key:
            from app.storage import r2
            try:
                url, expires = r2.signed_get_url(key, filename=MemoryManager.proposal_pdf_filename(proposal))
                data["pdf"] = {"url": url, "expires_at": expires.isoformat()}
            except r2.StorageNotConfiguredError:
                pass
    return data
