"""The Engineer's feasibility review of a site survey (S1 Step 5), and the gate it puts
on the formal proposal.

    outcome                     needs
    feasible                    —
    feasible_with_conditions    conditions (non-empty list of strings)
    revisit_needed              notes, revisit_items ([{section, field?, note}] naming
                                real survey sections and fields)
    not_feasible                notes

Only a complete, signed survey of a submitted or verified customer is reviewed. A new
review overwrites the columns (the audit row keeps before/after) and clears
`survey_revised_after_review`. Any later change to a survey field — the associate's own
edit, or an applied correction — sets it again, so the survey re-enters the review
queue (mark_revised).

How the associate acts on revisit_needed follows the existing edit rules
(app/onboarding/service.py): a submitted customer is edited directly (the edit returns
it to draft, the survey is signed again, then submitted again); a verified customer
can no longer be edited, so the change goes through a correction request
(app/survey/corrections.py).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.associates.service import AssociateError
from app.memory.memory_manager import MemoryManager
from app.memory.models import (
    CustomerOnboarding, FeasibilityOutcome, OnboardingStatus, ProposalInputs, TimelineEventType,
)
from app.survey import config, status

REVIEWABLE_STATUSES = (OnboardingStatus.submitted, OnboardingStatus.verified)
PASSING = (FeasibilityOutcome.feasible, FeasibilityOutcome.feasible_with_conditions)
NOTES_REQUIRED = (FeasibilityOutcome.revisit_needed, FeasibilityOutcome.not_feasible)


def _refuse(message: str, code: str, status_code: int = 422, field: Optional[str] = None, **extra) -> AssociateError:
    return AssociateError(message, code=code, status_code=status_code, field=field, extra=extra or None)


def section_fields() -> dict[str, list[str]]:
    """{section key: every field it can hold} — across variants and conditional fields."""
    cfg = config.load()
    out = {s["key"]: list(s["fields"]) for s in cfg["sections"]}
    for extra in cfg["variants"].values():
        for key, fields in (extra or {}).items():
            out[key] += [f for f in fields if f not in out[key]]
    for rule in cfg.get("conditional") or []:
        out[rule["section"]] += [f for f in rule["require"] if f not in out[rule["section"]]]
    return out


def _clean_text(value: Optional[str]) -> Optional[str]:
    value = (value or "").strip()
    return value or None


def validate(outcome: FeasibilityOutcome, notes: Optional[str], conditions: Optional[list[str]],
             revisit_items: Optional[list[dict]]) -> tuple[Optional[str], list[str], list[dict]]:
    """(notes, conditions, revisit_items) as stored, or a 422."""
    notes = _clean_text(notes)
    conditions = [c.strip() for c in (conditions or []) if c and c.strip()]
    items = revisit_items or []
    if outcome in NOTES_REQUIRED and not notes:
        raise _refuse(f"Notes are required when the outcome is {outcome.value}.", "notes_required", field="notes")
    if outcome == FeasibilityOutcome.feasible_with_conditions and not conditions:
        raise _refuse("List at least one condition.", "conditions_required", field="conditions")
    if outcome != FeasibilityOutcome.feasible_with_conditions and conditions:
        raise _refuse("Conditions are only recorded with feasible_with_conditions.", "not_applicable",
                      field="conditions")
    if outcome == FeasibilityOutcome.revisit_needed and not items:
        raise _refuse("Say which parts of the survey need a revisit.", "revisit_items_required",
                      field="revisit_items")
    if outcome != FeasibilityOutcome.revisit_needed and items:
        raise _refuse("Revisit items are only recorded with revisit_needed.", "not_applicable",
                      field="revisit_items")
    known = section_fields()
    stored = []
    for i, item in enumerate(items):
        section, field, note = item.get("section"), item.get("field"), _clean_text(item.get("note"))
        if section not in known:
            raise _refuse(f"Revisit item {i + 1}: {section} is not a survey section.", "unknown_section",
                          field="revisit_items", index=i, sections=sorted(known))
        if field is not None and field not in known[section]:
            raise _refuse(f"Revisit item {i + 1}: {field} is not a field of the {section} section.",
                          "unknown_field", field="revisit_items", index=i, fields=known[section])
        if not note:
            raise _refuse(f"Revisit item {i + 1} needs a note.", "note_required", field="revisit_items", index=i)
        stored.append({"section": section, "field": field, "note": note})
    return notes, conditions, stored


def _snapshot(inputs: ProposalInputs) -> dict:
    return {"outcome": inputs.feasibility_outcome.value if inputs.feasibility_outcome else None,
            "notes": inputs.feasibility_notes, "conditions": inputs.feasibility_conditions,
            "revisit_items": inputs.revisit_items, "reviewed_by": inputs.feasibility_reviewed_by_id,
            "reviewed_at": inputs.feasibility_reviewed_at.isoformat() if inputs.feasibility_reviewed_at else None,
            "revised_after_review": bool(inputs.survey_revised_after_review)}


def reviewable(onboarding: CustomerOnboarding, inputs: ProposalInputs) -> None:
    """409 unless the customer is submitted/verified and the survey complete and signed."""
    if onboarding.status not in REVIEWABLE_STATUSES:
        raise _refuse(f"Only a submitted or verified customer's survey can be reviewed (this one is "
                      f"{onboarding.status.value}).", "survey_not_submitted", status_code=409)
    missing = [s["key"] for s in status.sections(inputs) if not s["complete"]]
    unsigned = status.unsigned(inputs)
    if missing or unsigned:
        raise _refuse("The survey is not complete and signed yet.", "survey_incomplete", status_code=409,
                      missing_survey_sections=missing, survey_unsigned=unsigned)


def record(db: Session, onboarding: CustomerOnboarding, employee_id: str, *, outcome: FeasibilityOutcome,
           notes: Optional[str] = None, conditions: Optional[list[str]] = None,
           revisit_items: Optional[list[dict]] = None) -> ProposalInputs:
    """Record (or replace) the review. Caller commits."""
    from app.onboarding import service

    inputs = db.get(ProposalInputs, onboarding.proposal_inputs_id)
    reviewable(onboarding, inputs)
    notes, conditions, items = validate(outcome, notes, conditions, revisit_items)
    before = _snapshot(inputs)
    inputs.feasibility_outcome = outcome
    inputs.feasibility_notes = notes
    inputs.feasibility_conditions = conditions or None
    inputs.revisit_items = items or None
    inputs.feasibility_reviewed_by_id = employee_id
    inputs.feasibility_reviewed_at = datetime.now(timezone.utc)
    inputs.survey_revised_after_review = False
    db.flush()
    service._audit(db, onboarding.id, "survey_reviewed", employee_id, before=before, after=_snapshot(inputs))
    MemoryManager(db).add_timeline_event(
        lead_id=onboarding.lead_id, event_type=TimelineEventType.survey_reviewed,
        description=f"Site survey reviewed: {outcome.value.replace('_', ' ')}", created_by=employee_id)
    from app.notifications import associate_emails
    associate_emails.survey_reviewed(db, onboarding, outcome)
    return inputs


def has_current_review(inputs: ProposalInputs) -> bool:
    return inputs.feasibility_outcome is not None and not inputs.survey_revised_after_review


def mark_revised(inputs: ProposalInputs) -> tuple[dict, dict]:
    """A survey field changed after a review: the survey re-enters the review queue.
    (before, after) audit fragments, or ({}, {}) when nothing changes. Caller audits."""
    if inputs.feasibility_outcome is None or inputs.survey_revised_after_review:
        return {}, {}
    inputs.survey_revised_after_review = True
    return {"survey_revised_after_review": False}, {"survey_revised_after_review": True}


def formal_proposal_problem(db: Session, onboarding: CustomerOnboarding) -> Optional[AssociateError]:
    """Why the formal proposal cannot be requested or drafted yet because of the
    survey review, or None. not_feasible always blocks; otherwise the review is needed
    only when config says so and the customer's survey is required (not grandfathered)."""
    inputs = db.get(ProposalInputs, onboarding.proposal_inputs_id)
    if inputs.feasibility_outcome == FeasibilityOutcome.not_feasible:
        return _refuse("An Engineer reviewed the site survey and found the installation not feasible.",
                       "survey_not_feasible", status_code=409)
    if not (config.load()["review_required_for_formal_proposal"] and status.required_to_submit(onboarding)):
        return None
    if inputs.feasibility_outcome in PASSING and not inputs.survey_revised_after_review:
        return None
    if inputs.feasibility_outcome is None:
        message = "An Engineer has not reviewed the site survey yet."
    elif inputs.survey_revised_after_review:
        message = "The site survey changed after the Engineer's review; it needs reviewing again."
    else:
        message = "The Engineer asked for parts of the site survey to be revisited."
    return _refuse(message, "survey_review_required", status_code=409,
                   review_outcome=inputs.feasibility_outcome.value if inputs.feasibility_outcome else None,
                   revised_after_review=bool(inputs.survey_revised_after_review))


def require_formal_proposal_allowed(db: Session, onboarding: CustomerOnboarding) -> None:
    problem = formal_proposal_problem(db, onboarding)
    if problem is not None:
        raise problem
