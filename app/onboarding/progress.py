"""Per-project progress for associate-sourced customers: the sales stages below, then the
execution stages (app/execution/ — submitted by the associate and verified by Operations,
or recorded directly by Grid Nova's team; which of them apply comes from
config/execution_payments.yaml).

    registered → verified → site assessed → proposal requested → proposal prepared →
    proposal issued → accepted → agreement signed → customer approved the agreement on
    the PM Surya Ghar portal (PM Surya Ghar-linked projects only)

The FORMAL proposal is a separate, deliberate step after the site assessment — never an
automatic consequence of onboarding. What the associate shows the customer during
onboarding is an indicative estimate (app/onboarding/estimate.py), which is not a stage.

Each stage is recorded once, in order, with who recorded it, when, and the evidence
behind it — an append-only log (project_stage_events), never edited or deleted.

Most stages are recorded automatically from the action that proves them:
    registered          onboarding created                      (app/onboarding/service.py)
    verified            staff verification call                 (app/onboarding/service.py)
    site_assessed       site assessment approved_for_proposal   (app/api/routes/proposal_inputs.py)
    proposal_requested  associate or staff asks for the formal proposal (app/onboarding/formal.py);
                        also recorded, naming the staff member, when staff draft without a request
    proposal_prepared   staff draft the formal proposal         (MemoryManager.create_proposal_draft)
    proposal_issued     emailed with the accept link, OR handed over in person
                        (MemoryManager.transition_proposal_status; evidence says which)
    accepted            accepted online via the link, OR in person (MemoryManager.mark_customer_accepted;
                        evidence says which)
    agreement_signed    signed agreement + stamp paper uploaded (app/onboarding/agreement.py)
Staff record `portal_agreement_approved` by hand with a note.

Automatic recording never breaks the action it rides on: if the project has no
onboarding, or an earlier stage is missing, it records nothing and says why in the log.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.associates.service import AssociateError
from app.memory.models import (
    AuditLog, CustomerOnboarding, GovernmentScheme, ProjectStage, ProjectStageEvent, ProposalInputs,
    TimelineEventType,
)

logger = logging.getLogger(__name__)

ORDER = (
    ProjectStage.registered, ProjectStage.verified, ProjectStage.site_assessed, ProjectStage.proposal_requested,
    ProjectStage.proposal_prepared, ProjectStage.proposal_issued, ProjectStage.accepted, ProjectStage.agreement_signed,
    ProjectStage.portal_agreement_approved,
    ProjectStage.feasibility_submitted, ProjectStage.material_dispatched, ProjectStage.installed,
    ProjectStage.inspection_net_meter, ProjectStage.commissioned, ProjectStage.subsidy_claimed,
    ProjectStage.subsidy_received, ProjectStage.handover_complete,
)
LABELS = {
    ProjectStage.registered: "Registered",
    ProjectStage.verified: "Verified",
    ProjectStage.site_assessed: "Site assessed",
    ProjectStage.proposal_requested: "Proposal requested",
    ProjectStage.proposal_prepared: "Proposal prepared",
    ProjectStage.proposal_issued: "Proposal issued",
    ProjectStage.accepted: "Accepted",
    ProjectStage.agreement_signed: "Agreement signed",
    ProjectStage.portal_agreement_approved: "Customer approved the agreement on the PM Surya Ghar portal",
    ProjectStage.feasibility_submitted: "DISCOM feasibility submitted",
    ProjectStage.material_dispatched: "Material dispatched",
    ProjectStage.installed: "Installed",
    ProjectStage.inspection_net_meter: "Inspection and net meter",
    ProjectStage.commissioned: "Commissioned",
    ProjectStage.subsidy_claimed: "Subsidy claimed",
    ProjectStage.subsidy_received: "Subsidy received",
    ProjectStage.handover_complete: "Handover complete",
}
# Stages staff may record by hand (everything else is recorded by its own action —
# including in-person acceptance, which has its own endpoint so the Proposal itself
# moves to accepted too).
MANUAL_STAGES = (ProjectStage.portal_agreement_approved,)


def applicable(db: Session, onboarding: CustomerOnboarding) -> list[ProjectStage]:
    """The stages this project goes through. Execution stages follow
    execution.stages.applicability (placeholder E1); where the project's details are
    not known yet, the stage is included."""
    from app.execution import config as execution_config

    inputs = db.get(ProposalInputs, onboarding.proposal_inputs_id)
    rules = execution_config.execution()["stages"]["applicability"]
    pm_surya_ghar = inputs.scheme_name == GovernmentScheme.pm_surya_ghar
    scheme_known = inputs.scheme_name is not None
    connection = inputs.connection_type.value if inputs.connection_type is not None else None
    stages = []
    for stage in ORDER:
        if stage == ProjectStage.portal_agreement_approved and not pm_surya_ghar:
            continue
        if stage.value in rules.get("pm_surya_ghar_only", []) and scheme_known and not pm_surya_ghar:
            continue
        if (stage.value in rules.get("grid_connected_only", []) and connection is not None
                and connection not in rules.get("grid_connected_types", [])):
            continue
        stages.append(stage)
    return stages


def recorded(db: Session, project_id: int) -> dict[ProjectStage, ProjectStageEvent]:
    from app.onboarding.preload import current
    pre = current(db)
    if pre is not None and project_id in pre.stage_events:
        return dict(pre.stage_events[project_id])
    rows = db.execute(select(ProjectStageEvent).where(ProjectStageEvent.project_id == project_id)).scalars()
    return {r.stage: r for r in rows}


def record(db: Session, onboarding: CustomerOnboarding, stage: ProjectStage, recorded_by: str,
           evidence: Optional[dict] = None, note: Optional[str] = None) -> ProjectStageEvent:
    """Record one stage. Raises AssociateError (409) when it is out of order, already
    recorded, or does not apply to this project. Caller commits."""
    from app.memory.memory_manager import MemoryManager

    stages = applicable(db, onboarding)
    if stage not in stages:
        raise AssociateError(f"'{stage.value}' does not apply to this project.", code="stage_not_applicable",
                             status_code=409)
    done = recorded(db, onboarding.project_id)
    if stage in done:
        raise AssociateError(f"'{stage.value}' is already recorded.", code="stage_already_recorded", status_code=409)
    previous = stages[stages.index(stage) - 1] if stages.index(stage) > 0 else None
    if previous is not None and previous not in done:
        raise AssociateError(f"'{stage.value}' cannot be recorded before '{previous.value}'.",
                             code="stage_out_of_order", status_code=409)
    event = ProjectStageEvent(project_id=onboarding.project_id, stage=stage, recorded_by=recorded_by,
                              recorded_at=datetime.now(timezone.utc), evidence=evidence or {}, note=note)
    db.add(event)
    db.flush()
    db.add(AuditLog(entity_type="project", entity_id=str(onboarding.project_id), action=f"stage_{stage.value}",
                    performed_by=recorded_by, after_value={"stage": stage.value, "evidence": evidence or {},
                                                           "note": note}))
    MemoryManager(db).add_timeline_event(lead_id=onboarding.lead_id, event_type=TimelineEventType.project_stage_recorded,
                                         description=f"Progress: {LABELS[stage]}", created_by=recorded_by)
    if stage == ProjectStage.agreement_signed:
        from app.execution import service as execution
        execution.start(db, onboarding, recorded_by)
        # The agreement date is the commission rate date (policy 4.1).
        from app.commissions import service as commissions
        commissions.on_agreement_signed(db, onboarding, recorded_by)
    if stage == ProjectStage.handover_complete:
        # Warranty register, handover pack and the customer's link (BE-5).
        from app.handover import service as handover
        handover.on_handover_complete(db, onboarding, recorded_by, event)
    if stage == ProjectStage.accepted:
        # The payment schedule is made against the accepted proposal's total (nothing
        # happens, and nothing fails, while there is no total yet).
        from app.payments import service as payments
        payments.ensure_schedule(db, onboarding, recorded_by)
    return event


def record_automatically(db: Session, *, lead_id: int, stage: ProjectStage, recorded_by: str,
                         evidence: Optional[dict] = None) -> Optional[ProjectStageEvent]:
    """For hooks inside existing actions: never raises. Returns the event, or None
    (and logs why) when the lead has no onboarding or the stage cannot be recorded."""
    onboarding = db.execute(select(CustomerOnboarding).where(CustomerOnboarding.lead_id == lead_id)).scalar_one_or_none()
    if onboarding is None:
        return None
    try:
        return record(db, onboarding, stage, recorded_by, evidence)
    except AssociateError as exc:
        logger.info("Progress stage %s not recorded for lead %s: %s", stage.value, lead_id, exc.message)
        return None


def timeline(db: Session, onboarding: CustomerOnboarding) -> dict:
    done = recorded(db, onboarding.project_id)
    stages = applicable(db, onboarding)
    items = []
    for stage in stages:
        e = done.get(stage)
        items.append({"stage": stage.value, "label": LABELS[stage], "recorded": e is not None,
                      "recorded_at": e.recorded_at.isoformat() if e else None,
                      "recorded_by": e.recorded_by if e else None,
                      "evidence": e.evidence if e else None, "note": e.note if e else None})
    current = next((s.value for s in reversed(stages) if s in done), None)
    nxt = next((s.value for s in stages if s not in done), None)
    from app.execution import service as execution
    not_applicable = [{"stage": s.value, "label": LABELS[s]} for s in ORDER if s not in stages]
    return {"project_id": onboarding.project_id, "current_stage": current, "next_stage": nxt, "stages": items,
            "not_applicable": not_applicable, **execution.progress_extras(db, onboarding, stages, done)}


def record_formal_draft(db: Session, *, lead_id: int, proposal_id: int, version: int,
                        performed_by: str) -> None:
    """Hook for every formal draft (MemoryManager.create_proposal_draft). For an
    associate-sourced project whose site assessment is approved: records
    `proposal_requested` first if nobody had asked (evidence names the staff member who
    drafted — they are the one who asked), then `proposal_prepared`. Revisions (later
    versions) find both already recorded and change nothing. Never raises."""
    onboarding = db.execute(select(CustomerOnboarding).where(CustomerOnboarding.lead_id == lead_id)).scalar_one_or_none()
    if onboarding is None:
        return
    done = recorded(db, onboarding.project_id)
    if ProjectStage.proposal_requested not in done:
        record_automatically(db, lead_id=lead_id, stage=ProjectStage.proposal_requested, recorded_by=performed_by,
                             evidence={"requested_by": performed_by, "via": "staff drafted the formal proposal"})
    record_automatically(db, lead_id=lead_id, stage=ProjectStage.proposal_prepared, recorded_by=performed_by,
                         evidence={"proposal_id": proposal_id, "version": version, "drafted_by": performed_by})
    if onboarding.formal_draft_failed_at is not None:
        onboarding.formal_draft_failed_at = None
        onboarding.formal_draft_failure = None
