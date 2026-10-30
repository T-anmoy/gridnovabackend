"""Customer acceptance of a proposal — the rules shared by the online accept link
(app/api/routes/public_proposals.py) and in-person acceptance (app/onboarding/formal.py).

A proposal can be accepted only while it is the one on offer:

  * its status is `sent` (staff approved it and it went to the customer);
  * no newer version exists on the lead, and no other proposal on the lead has been
    accepted (otherwise: proposal_superseded);
  * it is still within the validity period (config/sales_policy.yaml
    proposal_validity_days — a registered, unconfirmed default);
  * it is not waiting for re-verification after a survey change (app/survey/changes.py
    — otherwise: needs_reverification; the customer is not told which fields);
  * Grid Nova's wording in it is still approved exactly as sent (BE-22: an Admin may
    revoke an approval after sending — otherwise: proposal_not_available, the same
    neutral answer, so the customer is never shown the unapproved text).

Acceptance locks the lead row and then the proposal row (SELECT … FOR UPDATE on
PostgreSQL; SQLite has no row locks and serialises writers instead) and re-reads
`accepted_at` under the lock, so two simultaneous accepts record one acceptance.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.memory.models import Lead, Proposal, ProposalStatus

SUPERSEDED = "proposal_superseded"
NOT_AVAILABLE = "proposal_not_available"
EXPIRED = "proposal_expired"
REVERIFICATION = "needs_reverification"


def validity_days() -> int:
    from app.rules import sales_policy
    return sales_policy.proposal_validity_days()


def valid_until(proposal: Proposal) -> Optional[datetime]:
    if not proposal.date_sent:
        return None
    sent = proposal.date_sent if proposal.date_sent.tzinfo else proposal.date_sent.replace(tzinfo=timezone.utc)
    return sent + timedelta(days=validity_days())


def is_expired(proposal: Proposal, now: Optional[datetime] = None) -> bool:
    until = valid_until(proposal)
    return until is not None and (now or datetime.now(timezone.utc)) > until


def superseded(db: Session, proposal: Proposal) -> bool:
    """A newer version exists on the lead, or another proposal on it was accepted."""
    newer = db.execute(select(Proposal.id).where(
        Proposal.lead_id == proposal.lead_id, Proposal.version > proposal.version).limit(1)).first()
    other_accepted = db.execute(select(Proposal.id).where(
        Proposal.lead_id == proposal.lead_id, Proposal.id != proposal.id,
        Proposal.status == ProposalStatus.accepted).limit(1)).first()
    return newer is not None or other_accepted is not None


def lock(db: Session, proposal_id: int) -> Proposal:
    """The proposal, re-read under a lock on its lead and then itself."""
    lead_id = db.execute(select(Proposal.lead_id).where(Proposal.id == proposal_id)).scalar_one()
    db.execute(select(Lead.id).where(Lead.id == lead_id).with_for_update())
    return db.execute(select(Proposal).where(Proposal.id == proposal_id).with_for_update()
                      .execution_options(populate_existing=True)).scalar_one()


def refusal(db: Session, proposal: Proposal) -> Optional[tuple[str, str]]:
    """(code, message) when this proposal cannot be accepted or shown as on offer now,
    or None. An accepted proposal is not refused here — callers treat it as done."""
    if proposal.status == ProposalStatus.accepted:
        return None
    if superseded(db, proposal):
        return SUPERSEDED, "This proposal has been replaced by a newer one. Please use the latest proposal from Grid Nova."
    if proposal.status != ProposalStatus.sent:
        return NOT_AVAILABLE, "This proposal is not open for acceptance right now. Please contact Grid Nova."
    if proposal.needs_reverification:
        return REVERIFICATION, ("Grid Nova is checking this proposal again after a change to the site survey. "
                                "Please wait for Grid Nova to contact you.")
    from app.proposals import content_engine
    if content_engine.unapproved_sections(proposal.content, db):
        return NOT_AVAILABLE, "This proposal is not open for acceptance right now. Please contact Grid Nova."
    return None
