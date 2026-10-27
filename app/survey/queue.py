"""The Engineer's survey review queue (S1 Step 8).

A survey is in the queue once it is complete and signed and its customer is submitted
or verified. It waits for a review from `waiting_since` — the latest of the submission,
the two signatures and (for a survey changed after its review) that review — and is
overdue after config/site_survey.yaml review_target_hours.

    state      which surveys
    awaiting   no review yet, or changed after the review (includes overdue and revised)
    overdue    awaiting for review_target_hours or more
    reviewed   reviewed, and unchanged since
    revised    changed after the review (awaiting again)
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.memory.models import (
    Associate, Customer, CustomerOnboarding, Lead, OnboardingStatus, Project, ProposalInputs,
)
from app.survey import config, status

STATES = ("awaiting", "overdue", "reviewed", "revised")


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _signed_at(ref: Optional[dict]) -> Optional[datetime]:
    try:
        return _aware(datetime.fromisoformat(ref["signed_at"])) if ref and ref.get("signed_at") else None
    except ValueError:
        return None


def target_hours() -> float:
    return float(config.load()["review_target_hours"])


def _row(onboarding: CustomerOnboarding, inputs: ProposalInputs, now: datetime) -> dict:
    signed = [t for t in (_signed_at(inputs.customer_signature_ref), _signed_at(inputs.associate_signature_ref)) if t]
    revised = bool(inputs.feasibility_outcome is not None and inputs.survey_revised_after_review)
    awaiting = inputs.feasibility_outcome is None or revised
    marks = [_aware(onboarding.submitted_at), *signed]
    if revised:
        marks.append(_aware(inputs.feasibility_reviewed_at))
    since = max((m for m in marks if m is not None), default=None)
    hours = int((now - since).total_seconds() // 3600) if awaiting and since is not None else None
    return {"onboarding_id": onboarding.id, "onboarding_status": onboarding.status.value,
            "submitted_at": _aware(onboarding.submitted_at).isoformat() if onboarding.submitted_at else None,
            "signed_at": max(signed).isoformat() if signed else None,
            "waiting_since": since.isoformat() if since else None, "hours_waiting": hours,
            "awaiting": awaiting, "overdue": awaiting and hours is not None and hours >= target_hours(),
            "revised_after_review": revised,
            "review": {"outcome": inputs.feasibility_outcome.value if inputs.feasibility_outcome else None,
                       "reviewed_at": _aware(inputs.feasibility_reviewed_at).isoformat()
                       if inputs.feasibility_reviewed_at else None,
                       "reviewed_by": inputs.feasibility_reviewed_by_id},
            "_inputs_id": inputs.id, "_since": since}


def rows(db: Session, employee=None, now: Optional[datetime] = None) -> list[dict]:
    """Every survey in the queue the caller may see (all of them when `employee` is
    None — the alert job), newest wait first."""
    from app.auth.scoping import lead_scope, narrowed_to_assigned

    now = now or datetime.now(timezone.utc)
    q = (select(CustomerOnboarding, ProposalInputs)
         .join(ProposalInputs, ProposalInputs.id == CustomerOnboarding.proposal_inputs_id)
         .where(CustomerOnboarding.status.in_((OnboardingStatus.submitted, OnboardingStatus.verified))))
    if employee is not None and narrowed_to_assigned(employee):
        q = q.join(Lead, Lead.id == CustomerOnboarding.lead_id).where(lead_scope(employee).condition())
    out = []
    for onboarding, inputs in db.execute(q).all():
        if status.unsigned(inputs) or not all(s["complete"] for s in status.sections(inputs)):
            continue
        out.append((onboarding, _row(onboarding, inputs, now)))
    if out:
        customers = {c.id: c for c in db.execute(select(Customer).where(
            Customer.id.in_({o.customer_id for o, _ in out}))).scalars()}
        associates = {a.id: a for a in db.execute(select(Associate).where(
            Associate.id.in_({o.owner_associate_id for o, _ in out}))).scalars()}
        projects = {p.id: p for p in db.execute(select(Project).where(
            Project.id.in_({o.project_id for o, _ in out}))).scalars()}
        for onboarding, row in out:
            customer, associate = customers.get(onboarding.customer_id), associates.get(onboarding.owner_associate_id)
            row.update(client_code=projects[onboarding.project_id].reference_id,
                       customer_name=customer.name if customer else None,
                       associate={"associate_id": associate.associate_id, "name": associate.full_name}
                       if associate else None)
    result = [row for _, row in out]
    result.sort(key=lambda r: (r["_since"] or datetime.min.replace(tzinfo=timezone.utc), r["onboarding_id"]),
                reverse=True)
    return result


def matches(row: dict, state: str) -> bool:
    if state == "awaiting":
        return row["awaiting"]
    if state == "overdue":
        return row["overdue"]
    if state == "reviewed":
        return row["review"]["outcome"] is not None and not row["revised_after_review"]
    return row["revised_after_review"]


def public(row: dict) -> dict:
    return {k: v for k, v in row.items() if not k.startswith("_") and k != "awaiting"}


def counts(db: Session, employee=None, now: Optional[datetime] = None) -> dict:
    all_rows = rows(db, employee, now)
    return {"awaiting": sum(1 for r in all_rows if r["awaiting"]), "overdue": sum(1 for r in all_rows if r["overdue"])}
