"""Bulk-load everything a LIST of onboardings needs, so serializing N customers costs a
fixed number of queries instead of ~10 per customer.

    with preloaded(db, onboardings):
        items = [serialize.onboarding(db, o, ...) for o in onboardings]

Two effects while the block is open:
  * related rows (inputs, customers, projects, leads, owners, territories) are loaded
    with one IN query each and held by strong references, so every `db.get()` the
    serializer makes is answered from the session's identity map (which on its own
    holds rows only weakly);
  * the per-customer SELECTs that are not `db.get()` — stage events, open attribution
    flags, the execution row, stage submissions, the agreement, payment milestones, the
    verified ledger total, the accepted proposal and the indicative estimates — consult
    the maps here first (see `current()`).

Read-only by design: the cache lives only for the block, and list endpoints do not
write inside it.
"""
from __future__ import annotations

from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.memory.models import (
    Associate, AttributionFlagStatus, Customer, CustomerAgreement, CustomerOnboarding, IndicativeEstimate, Lead,
    OnboardingAttributionFlag, PaymentLedgerEntry, PaymentMilestone, Project, ProjectExecution, ProjectStageEvent,
    Proposal, ProposalInputs, ProposalStatus, StageSubmission, TerritoryBlock, TerritoryDistrict, TerritoryState,
)

_KEY = "gridnova_onboarding_preload"


@dataclass
class Preload:
    stage_events: dict = field(default_factory=dict)      # project_id → {ProjectStage: event}
    open_flags: dict = field(default_factory=dict)        # onboarding_id → [flag]
    executions: dict = field(default_factory=dict)        # onboarding_id → ProjectExecution | None
    submissions: dict = field(default_factory=dict)       # project_id → [StageSubmission] (oldest first)
    agreements: dict = field(default_factory=dict)        # onboarding_id → CustomerAgreement | None
    milestones: dict = field(default_factory=dict)        # project_id → [PaymentMilestone]
    ledger_totals: dict = field(default_factory=dict)     # project_id → Decimal
    accepted_proposals: dict = field(default_factory=dict)  # lead_id → Proposal | None (BE-10)
    estimates: dict = field(default_factory=dict)         # onboarding_id → [IndicativeEstimate] (newest first)
    keep: list = field(default_factory=list)              # strong references for the identity map


def current(db: Session) -> Optional[Preload]:
    return db.info.get(_KEY)


def _by_id(db: Session, model, ids) -> list:
    ids = {i for i in ids if i is not None}
    return list(db.execute(select(model).where(model.id.in_(ids))).scalars()) if ids else []


@contextmanager
def preloaded(db: Session, onboardings: list[CustomerOnboarding]):
    if current(db) is not None or not onboardings:   # nested or empty: nothing to add
        yield current(db)
        return
    pre = Preload()
    ids = [o.id for o in onboardings]
    projects = [o.project_id for o in onboardings]
    pre.keep += onboardings
    pre.keep += _by_id(db, ProposalInputs, (o.proposal_inputs_id for o in onboardings))
    pre.keep += _by_id(db, Customer, (o.customer_id for o in onboardings))
    pre.keep += _by_id(db, Project, projects)
    pre.keep += _by_id(db, Lead, (o.lead_id for o in onboardings))
    pre.keep += _by_id(db, Associate, (o.owner_associate_id for o in onboardings))
    pre.keep += _by_id(db, TerritoryState, (o.state_id for o in onboardings))
    pre.keep += _by_id(db, TerritoryDistrict, (o.district_id for o in onboardings))
    pre.keep += _by_id(db, TerritoryBlock, (o.block_id for o in onboardings))

    pre.stage_events = {p: {} for p in projects}
    for e in db.execute(select(ProjectStageEvent).where(ProjectStageEvent.project_id.in_(projects))).scalars():
        pre.stage_events[e.project_id][e.stage] = e
    pre.open_flags = defaultdict(list)
    for f in db.execute(select(OnboardingAttributionFlag).where(
            OnboardingAttributionFlag.status == AttributionFlagStatus.open,
            or_(OnboardingAttributionFlag.onboarding_id.in_(ids),
                OnboardingAttributionFlag.existing_onboarding_id.in_(ids)))).scalars():
        for oid in {f.onboarding_id, f.existing_onboarding_id} & set(ids):
            pre.open_flags[oid].append(f)
    pre.open_flags = {oid: pre.open_flags.get(oid, []) for oid in ids}
    pre.executions = {oid: None for oid in ids}
    for x in db.execute(select(ProjectExecution).where(ProjectExecution.onboarding_id.in_(ids))).scalars():
        pre.executions[x.onboarding_id] = x
    pre.submissions = {p: [] for p in projects}
    for sub in db.execute(select(StageSubmission).where(StageSubmission.project_id.in_(projects))
                          .order_by(StageSubmission.submitted_at, StageSubmission.id)).scalars():
        pre.submissions[sub.project_id].append(sub)
    pre.agreements = {oid: None for oid in ids}
    for a in db.execute(select(CustomerAgreement).where(CustomerAgreement.onboarding_id.in_(ids))).scalars():
        pre.agreements[a.onboarding_id] = a
    pre.milestones = {p: [] for p in projects}
    for m in db.execute(select(PaymentMilestone).where(PaymentMilestone.project_id.in_(projects))
                        .order_by(PaymentMilestone.number)).scalars():
        pre.milestones[m.project_id].append(m)
    pre.ledger_totals = {p: Decimal(0) for p in projects}
    for project_id, total in db.execute(select(PaymentLedgerEntry.project_id, func.sum(PaymentLedgerEntry.amount))
                                        .where(PaymentLedgerEntry.project_id.in_(projects))
                                        .group_by(PaymentLedgerEntry.project_id)).all():
        pre.ledger_totals[project_id] = Decimal(str(total or 0))
    leads = [o.lead_id for o in onboardings]
    pre.accepted_proposals = {lead: None for lead in leads}
    for p in db.execute(select(Proposal).where(Proposal.lead_id.in_(leads), Proposal.status == ProposalStatus.accepted)
                        .order_by(Proposal.version.desc(), Proposal.id.desc())).scalars():
        if pre.accepted_proposals[p.lead_id] is None:   # payments.accepted_proposal's order
            pre.accepted_proposals[p.lead_id] = p
    pre.estimates = {oid: [] for oid in ids}
    for e in db.execute(select(IndicativeEstimate).where(IndicativeEstimate.onboarding_id.in_(ids))
                        .order_by(IndicativeEstimate.id.desc())).scalars():
        pre.estimates[e.onboarding_id].append(e)
    pre.keep += [x for x in pre.executions.values() if x] + [a for a in pre.agreements.values() if a]
    pre.keep += [p for p in pre.accepted_proposals.values() if p] + [e for v in pre.estimates.values() for e in v]
    db.info[_KEY] = pre
    try:
        yield pre
    finally:
        db.info.pop(_KEY, None)
