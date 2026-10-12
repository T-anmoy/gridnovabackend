"""Re-verification after a survey change (S1 Step 6).

A change to a field that decides the system's size, price or design
(config/site_survey.yaml reverification_fields) after a proposal was drafted means
that proposal may no longer fit the site. Every open proposal on the customer's lead —
drafted, awaiting pricing or approval, approved, or sent but not yet accepted — is
flagged `needs_reverification`, with the fields and what changed them:

    reverification_reason = {"fields": [...], "source": "correction" | "revisit" | "edit",
                             "correction_id": int | None, "at": ISO time}

A flagged proposal cannot reach the customer: app/proposals/send_guard.py refuses the
staff send, the online acceptance and the in-person acceptance, and
MemoryManager.transition_proposal_status / mark_customer_accepted refuse it as a last
line. Staff clear the flag with a note (clear()) — never the person who applied the
correction that raised it. An accepted proposal is history and is never flagged.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.associates.service import AssociateError
from app.memory.models import AuditLog, CustomerOnboarding, Proposal, ProposalStatus
from app.survey import config

OPEN_STATUSES = (ProposalStatus.draft, ProposalStatus.pending_pricing_validation, ProposalStatus.pending_approval,
                 ProposalStatus.approved, ProposalStatus.sent)
CODE = "needs_reverification"


def affecting(fields: Iterable[str]) -> list[str]:
    """The changed fields that affect size, price or design, in config order."""
    changed = set(fields)
    return [f for f in config.load()["reverification_fields"] if f in changed]


def _audit(db: Session, proposal_id: int, action: str, performed_by: str, before=None, after=None) -> None:
    db.add(AuditLog(entity_type="proposal", entity_id=str(proposal_id), action=action, performed_by=performed_by,
                    before_value=before, after_value=after))


def flag(db: Session, onboarding: CustomerOnboarding, changed: Iterable[str], *, source: str, performed_by: str,
         correction_id: Optional[int] = None) -> list[int]:
    """Flag every open proposal on the customer's lead when `changed` touches a
    reverification field. Returns the flagged proposal ids. Caller commits."""
    fields = affecting(changed)
    if not fields:
        return []
    proposals = list(db.execute(select(Proposal).where(
        Proposal.lead_id == onboarding.lead_id, Proposal.status.in_(OPEN_STATUSES),
        Proposal.accepted_at.is_(None))).scalars())
    now = datetime.now(timezone.utc)
    for proposal in proposals:
        before = {"needs_reverification": bool(proposal.needs_reverification),
                  "reverification_reason": proposal.reverification_reason}
        previous = (proposal.reverification_reason or {}).get("fields", []) if proposal.needs_reverification else []
        proposal.needs_reverification = True
        proposal.reverification_reason = {"fields": previous + [f for f in fields if f not in previous],
                                          "source": source, "correction_id": correction_id, "at": now.isoformat()}
        proposal.reverification_flagged_at = now
        proposal.reverification_cleared_by = None
        proposal.reverification_cleared_at = None
        _audit(db, proposal.id, "reverification_flagged", performed_by, before=before,
               after={"needs_reverification": True, "reverification_reason": proposal.reverification_reason})
    db.flush()
    return [p.id for p in proposals]


def problem(proposal: Proposal) -> Optional[dict]:
    """None when the proposal is not waiting for re-verification; otherwise the 409 body."""
    if not proposal.needs_reverification:
        return None
    fields = (proposal.reverification_reason or {}).get("fields", [])
    return {"code": CODE,
            "message": "The site survey changed after this proposal was drafted (" + ", ".join(fields)
                       + "). Grid Nova must re-verify the proposal before it reaches the customer.",
            "fields": fields}


def serialize(proposal: Proposal) -> dict:
    """What the dashboard's proposal responses carry."""
    return {"needs_reverification": bool(proposal.needs_reverification),
            "reverification_reason": proposal.reverification_reason}


def clear(db: Session, proposal: Proposal, employee_id: str, note: str) -> Proposal:
    """Staff confirm the proposal still fits the changed survey. Caller commits."""
    from app.memory.models import SurveyCorrectionRequest

    note = (note or "").strip()
    if not note:
        raise AssociateError("Say what was checked.", code="note_required", status_code=422, field="note")
    proposal = db.execute(select(Proposal).where(Proposal.id == proposal.id).with_for_update()
                          .execution_options(populate_existing=True)).scalar_one()
    if not proposal.needs_reverification:
        raise AssociateError("This proposal is not waiting for re-verification.", code="not_flagged",
                             status_code=409)
    correction_id = (proposal.reverification_reason or {}).get("correction_id")
    if correction_id is not None:
        correction = db.get(SurveyCorrectionRequest, correction_id)
        if correction is not None and correction.reviewed_by == employee_id:
            raise AssociateError("The person who applied the survey correction cannot also clear the proposal "
                                 "it flagged.", code="segregation_of_duties", status_code=403)
    before = {"needs_reverification": True, "reverification_reason": proposal.reverification_reason}
    proposal.needs_reverification = False
    proposal.reverification_cleared_by = employee_id
    proposal.reverification_cleared_at = datetime.now(timezone.utc)
    db.flush()
    _audit(db, proposal.id, "reverification_cleared", employee_id, before=before,
           after={"needs_reverification": False, "note": note,
                  "cleared_at": proposal.reverification_cleared_at.isoformat()})
    return proposal
