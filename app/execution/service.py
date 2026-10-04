"""Project execution for associate-sourced projects — the stages after the agreement.

    agreement_signed ─► project_executions row (execution_mode fixed at that moment)

    execution_mode = associate  when the owning associate is a Preferred Associate or an
                                Associate with capability `execution`: the associate
                                SUBMITS each stage with its evidence (stage_submissions),
                                and Operations (or Admin) verifies — which records the
                                stage — or rejects it with a note (resubmit as a new row).
    execution_mode = grid_nova  otherwise: Operations/Admin record each stage directly,
                                under the same evidence rules (evidence says `via: direct`).

Recording always goes through app/onboarding/progress.record — the one path that
enforces order and applicability, writes the append-only ProjectStageEvent, the audit
row and the timeline event. Evidence per stage, photo slots, document types and the
stuck thresholds come from config/execution_payments.yaml (placeholders E2–E5).

Nothing here exposes money: the payment gate (E6, off by default) answers only
"blocked or not", so Operations never sees an amount.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.associates.service import AssociateError
from app.execution import config, register
from app.memory.models import (
    Associate, AssociateCapability, AssociateType, AuditLog, CustomerOnboarding, ExecutionMode, ProjectExecution,
    ProjectStage, ProjectStageEvent, StageSubmission, StageSubmissionStatus, TimelineEventType,
)

EXECUTION_STAGES = config.EXECUTION_STAGES


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def mode_for(owner: Associate) -> ExecutionMode:
    if owner.associate_type == AssociateType.preferred_associate or owner.capability == AssociateCapability.execution:
        return ExecutionMode.associate
    return ExecutionMode.grid_nova


def get(db: Session, onboarding: CustomerOnboarding) -> Optional[ProjectExecution]:
    from app.onboarding.preload import current
    pre = current(db)
    if pre is not None and onboarding.id in pre.executions:
        return pre.executions[onboarding.id]
    return db.execute(select(ProjectExecution).where(
        ProjectExecution.onboarding_id == onboarding.id)).scalar_one_or_none()


def start(db: Session, onboarding: CustomerOnboarding, performed_by: str) -> ProjectExecution:
    """Create the execution row when agreement_signed is recorded. Idempotent. The mode
    is fixed now: a later change to the associate does not move a running project."""
    existing = get(db, onboarding)
    if existing is not None:
        return existing
    mode = mode_for(db.get(Associate, onboarding.owner_associate_id))
    execution = ProjectExecution(project_id=onboarding.project_id, onboarding_id=onboarding.id, execution_mode=mode,
                                 started_at=_now(), files={}, created_at=_now())
    db.add(execution)
    db.flush()
    db.add(AuditLog(entity_type="project_execution", entity_id=str(execution.id), action="started",
                    performed_by=performed_by, after_value={"onboarding_id": onboarding.id,
                                                            "execution_mode": mode.value}))
    return execution


def require(db: Session, onboarding: CustomerOnboarding) -> ProjectExecution:
    execution = get(db, onboarding)
    if execution is None:
        raise AssociateError("Execution starts once the signed agreement is recorded.", code="execution_not_started",
                             status_code=409)
    return execution


def _stage(value: str) -> ProjectStage:
    try:
        stage = ProjectStage(value)
    except ValueError:
        stage = None
    if stage not in EXECUTION_STAGES:
        raise AssociateError(f"'{value}' is not an execution stage.", code="not_an_execution_stage", status_code=422,
                             field="stage")
    return stage


# ── Files ────────────────────────────────────────────────────────────────────────

def attach_file(db: Session, execution: ProjectExecution, *, kind: str, name: str, storage_key: str,
                filename: str, content_type: str, uploaded_by: str, extra: dict) -> dict:
    """Record one uploaded execution file: the latest photo per slot / document per
    type. Earlier files stay referenced by any submission that snapshotted them."""
    item = {"file_ref": uuid.uuid4().hex, "filename": filename, "storage_key": storage_key,
            "content_type": content_type, "received_at": _now().isoformat(), "uploaded_by": uploaded_by, **extra}
    files = {"photos": dict((execution.files or {}).get("photos") or {}),
             "documents": dict((execution.files or {}).get("documents") or {})}
    files["photos" if kind == "photo" else "documents"][name] = item
    execution.files = files
    db.add(AuditLog(entity_type="project_execution", entity_id=str(execution.id), action="file_uploaded",
                    performed_by=uploaded_by, after_value={"kind": kind, "name": name, "filename": filename}))
    db.flush()
    return item


def find_file(execution: ProjectExecution, file_ref: str) -> Optional[dict]:
    files = execution.files or {}
    for group in ("photos", "documents"):
        for item in (files.get(group) or {}).values():
            if item.get("file_ref") == file_ref:
                return item
    return None


def file_from_submissions(db: Session, execution: ProjectExecution, file_ref: str) -> Optional[dict]:
    """A file a submission snapshotted that has since been replaced by a newer upload."""
    for sub in db.execute(select(StageSubmission).where(StageSubmission.project_id == execution.project_id)).scalars():
        for group in ("photos", "documents"):
            for item in ((sub.evidence or {}).get(group) or {}).values():
                if item.get("file_ref") == file_ref:
                    return item
    return None


def _public_file(item: dict) -> dict:
    from app.execution.photos import public_fields
    out = {k: item.get(k) for k in ("file_ref", "filename", "received_at", "uploaded_by")}
    if "sha256" in item or "capture_method" in item:
        out.update(public_fields(item), latitude=item.get("latitude"), longitude=item.get("longitude"),
                   client_captured_at=item.get("client_captured_at"), exif_taken_at=item.get("exif_taken_at"))
    return out


# ── Evidence ─────────────────────────────────────────────────────────────────────

def evidence_for(db: Session, execution: ProjectExecution, stage: ProjectStage, text: Optional[dict]) -> dict:
    """Check the stage's evidence rule (E3) against what was sent and uploaded, and
    return the snapshot to keep. 422 evidence_missing lists everything absent."""
    from app.execution.photos import counts_as_evidence

    rule = config.evidence_rule(stage)
    text = {k: (v or "").strip() for k, v in (text or {}).items() if isinstance(v, str)}
    files = execution.files or {}
    photos = files.get("photos") or {}
    documents = files.get("documents") or {}
    missing = [f"text:{k}" for k in rule.get("text") or [] if not text.get(k)]
    missing += [f"document:{d}" for d in rule.get("documents") or [] if d not in documents]
    missing += [f"photo:{p}" for p in rule.get("photos") or [] if not counts_as_evidence(photos.get(p) or {})]
    if rule.get("register_verified") and execution.register_verified_at is None:
        missing.append("register:verified")
    if missing:
        raise AssociateError("Some evidence for this stage is missing: " + ", ".join(missing) + ".",
                             code="evidence_missing", status_code=422, extra={"missing": missing})
    wanted_docs = (rule.get("documents") or []) + (rule.get("optional_documents") or [])
    snapshot = {"text": {k: text[k] for k in rule.get("text") or []},
                "documents": {d: documents[d] for d in wanted_docs if d in documents},
                "photos": {p: photos[p] for p in rule.get("photos") or []}}
    if rule.get("register_verified"):
        snapshot["register_verified_at"] = _aware(execution.register_verified_at).isoformat()
    return snapshot


# ── Blockers (payment gate; Step 4 adds the DCR mismatch) ───────────────────────

def payment_gate_blocked(db: Session, onboarding: CustomerOnboarding) -> bool:
    """E6: material_dispatched waits for every milestone up to `through_milestone` to be
    fully verified — when enabled (off by default). Answers yes/no only."""
    gate = config.execution()["gates"]["material_dispatched"]
    if not gate["enabled"]:
        return False
    from app.payments import service as payments
    return not payments.milestones_verified_through(db, onboarding.project_id, gate["through_milestone"])


def commissioning_gate_blocked(db: Session, onboarding: CustomerOnboarding) -> bool:
    """P10: commissioned waits for every milestone up to `through_milestone` ("Before
    Commissioning") to be fully verified — when enabled (off by default)."""
    gate = config.execution()["gates"]["commissioned"]
    if not gate["enabled"]:
        return False
    from app.payments import service as payments
    return not payments.milestones_verified_through(db, onboarding.project_id, gate["through_milestone"])


def stage_blockers(db: Session, onboarding: CustomerOnboarding, stage: ProjectStage) -> list[dict]:
    blockers = []
    if stage == ProjectStage.material_dispatched and payment_gate_blocked(db, onboarding):
        blockers.append({"code": "payment_gate",
                         "message": "Material cannot be dispatched until the customer's pre-dispatch payment is "
                                    "confirmed by Accounts."})
    if stage == ProjectStage.commissioned and commissioning_gate_blocked(db, onboarding):
        blockers.append({"code": "commissioning_payment_gate",
                         "message": "The system cannot be commissioned until the customer's \"Before "
                                    "Commissioning\" payment is confirmed by Accounts."})
    return blockers + register.stage_blockers(db, onboarding, stage)


def project_flags(db: Session, onboarding: CustomerOnboarding, execution: ProjectExecution,
                  staff: bool = False) -> list[str]:
    """Visible, non-blocking project-level flags (dcr_mismatch, capacity_mismatch; for
    staff also previously_voided)."""
    return register.project_flags(db, onboarding, execution, staff)


def _raise_blockers(blockers: list[dict]) -> None:
    if blockers:
        first = blockers[0]
        raise AssociateError(first["message"], code=first["code"], status_code=409)


# ── Two-step movement ────────────────────────────────────────────────────────────

def _check_can_move(db: Session, onboarding: CustomerOnboarding, stage: ProjectStage) -> None:
    """Order and applicability now, so a submission is refused up front instead of
    being accepted and failing at verification."""
    from app.onboarding import progress

    stages = progress.applicable(db, onboarding)
    if stage not in stages:
        raise AssociateError(f"'{stage.value}' does not apply to this project.", code="stage_not_applicable",
                             status_code=409)
    done = progress.recorded(db, onboarding.project_id)
    if stage in done:
        raise AssociateError(f"'{stage.value}' is already recorded.", code="stage_already_recorded", status_code=409)
    previous = stages[stages.index(stage) - 1]
    if previous not in done:
        raise AssociateError(f"'{stage.value}' cannot be recorded before '{previous.value}'.",
                             code="stage_out_of_order", status_code=409)


def pending_for(db: Session, project_id: int, stage: Optional[ProjectStage] = None) -> list[StageSubmission]:
    q = select(StageSubmission).where(StageSubmission.project_id == project_id,
                                      StageSubmission.status == StageSubmissionStatus.pending)
    if stage is not None:
        q = q.where(StageSubmission.stage == stage)
    return list(db.execute(q).scalars())


def submit(db: Session, onboarding: CustomerOnboarding, submitted_by: str, stage_value: str,
           text: Optional[dict], note: Optional[str] = None) -> StageSubmission:
    """The owning associate submits an execution stage (associate mode only)."""
    from app.memory.memory_manager import MemoryManager
    from app.notifications import notifier as notifier_module
    from app.onboarding import progress

    execution = require(db, onboarding)
    if execution.execution_mode != ExecutionMode.associate:
        raise AssociateError("Grid Nova's team records this project's execution.", code="execution_mode_grid_nova",
                             status_code=409)
    stage = _stage(stage_value)
    _check_can_move(db, onboarding, stage)
    if pending_for(db, onboarding.project_id, stage):
        raise AssociateError("This stage is already waiting for Grid Nova to check it.", code="submission_pending",
                             status_code=409)
    evidence = evidence_for(db, execution, stage, text)
    if note and note.strip():
        evidence["note"] = note.strip()
    sub = StageSubmission(project_id=onboarding.project_id, onboarding_id=onboarding.id, stage=stage,
                          submitted_by=submitted_by, submitted_at=_now(), evidence=evidence,
                          status=StageSubmissionStatus.pending)
    db.add(sub)
    db.flush()
    db.add(AuditLog(entity_type="stage_submission", entity_id=str(sub.id), action="submitted",
                    performed_by=submitted_by, after_value={"onboarding_id": onboarding.id, "stage": stage.value}))
    memory = MemoryManager(db)
    memory.add_timeline_event(lead_id=onboarding.lead_id, event_type=TimelineEventType.execution_stage_submitted,
                              description=f"Submitted for checking: {progress.LABELS[stage]}", created_by=submitted_by)
    payload = {"submission_id": sub.id, "onboarding_id": onboarding.id, "stage": stage.value,
               "stage_label": progress.LABELS[stage], "submitted_by": submitted_by,
               "customer_name": _customer_name(db, onboarding)}
    memory.notify_after_commit(lambda: notifier_module.notifier.notify("stage_submitted", payload),
                               "stage_submitted")
    return sub


def _customer_name(db: Session, onboarding: CustomerOnboarding) -> Optional[str]:
    from app.memory.models import Customer
    customer = db.get(Customer, onboarding.customer_id)
    return customer.name if customer else None


def _record(db: Session, onboarding: CustomerOnboarding, stage: ProjectStage, recorded_by: str, evidence: dict,
            note: Optional[str]) -> ProjectStageEvent:
    from app.onboarding import progress

    _raise_blockers(stage_blockers(db, onboarding, stage))
    return progress.record(db, onboarding, stage, recorded_by, evidence=evidence, note=note)


def verify(db: Session, sub: StageSubmission, reviewed_by: str, note: Optional[str] = None) -> ProjectStageEvent:
    if sub.status != StageSubmissionStatus.pending:
        raise AssociateError(f"This submission is already {sub.status.value}.", code="submission_not_pending",
                             status_code=409)
    onboarding = db.get(CustomerOnboarding, sub.onboarding_id)
    evidence = {**(sub.evidence or {}), "via": "associate_submission", "submission_id": sub.id,
                "submitted_by": sub.submitted_by, "verified_by": reviewed_by}
    event = _record(db, onboarding, sub.stage, reviewed_by, evidence, note.strip() if note else None)
    sub.status = StageSubmissionStatus.verified
    sub.reviewed_by, sub.reviewed_at = reviewed_by, _now()
    sub.review_note = note.strip() if note and note.strip() else None
    db.add(AuditLog(entity_type="stage_submission", entity_id=str(sub.id), action="verified",
                    performed_by=reviewed_by, before_value={"status": "pending"},
                    after_value={"status": "verified", "stage": sub.stage.value, "note": sub.review_note}))
    db.flush()
    return event


def reject(db: Session, sub: StageSubmission, reviewed_by: str, note: str) -> StageSubmission:
    from app.memory.memory_manager import MemoryManager
    from app.onboarding import progress

    if sub.status != StageSubmissionStatus.pending:
        raise AssociateError(f"This submission is already {sub.status.value}.", code="submission_not_pending",
                             status_code=409)
    note = (note or "").strip()
    if not note:
        raise AssociateError("Say what is wrong so the associate can fix it.", code="note_required", status_code=422,
                             field="note")
    sub.status = StageSubmissionStatus.rejected
    sub.reviewed_by, sub.reviewed_at, sub.review_note = reviewed_by, _now(), note
    db.add(AuditLog(entity_type="stage_submission", entity_id=str(sub.id), action="rejected",
                    performed_by=reviewed_by, before_value={"status": "pending"},
                    after_value={"status": "rejected", "stage": sub.stage.value, "note": note}))
    onboarding = db.get(CustomerOnboarding, sub.onboarding_id)
    MemoryManager(db).add_timeline_event(
        lead_id=onboarding.lead_id, event_type=TimelineEventType.execution_stage_rejected,
        description=f"Sent back: {progress.LABELS[sub.stage]} — {note}", created_by=reviewed_by)
    db.flush()
    _after_reject_hooks(db, onboarding, sub)
    return sub


def _after_reject_hooks(db: Session, onboarding: CustomerOnboarding, sub: StageSubmission) -> None:
    from app.notifications import associate_emails
    associate_emails.stage_rejected(db, onboarding, sub)


def record_direct(db: Session, onboarding: CustomerOnboarding, recorded_by: str, stage_value: str,
                  text: Optional[dict], note: Optional[str] = None, *, is_admin: bool = False,
                  admin_override_reason: Optional[str] = None) -> ProjectStageEvent:
    """Grid Nova's own team records a stage, same evidence rules.

    On a grid_nova project that is ordinary. On an associate-mode project only an Admin
    may, with a mandatory reason: the stage is recorded with `via: admin_override`,
    audited as `admin_override`, and any submission the associate still has pending for
    that stage is closed as superseded."""
    execution = require(db, onboarding)
    override = None
    if execution.execution_mode != ExecutionMode.grid_nova:
        if not is_admin:
            raise AssociateError("The associate submits this project's stages; verify their submission instead.",
                                 code="execution_mode_associate", status_code=409)
        override = (admin_override_reason or "").strip()
        if not override:
            raise AssociateError("This project's stages are submitted by the associate. To record one yourself, "
                                 "give admin_override_reason.", code="reason_required", status_code=422,
                                 field="admin_override_reason")
    stage = _stage(stage_value)
    _check_can_move(db, onboarding, stage)
    evidence = {**evidence_for(db, execution, stage, text), "via": "admin_override" if override else "direct"}
    if override:
        evidence["admin_override_reason"] = override
    event = _record(db, onboarding, stage, recorded_by, evidence, note.strip() if note and note.strip() else None)
    if override:
        for sub in pending_for(db, onboarding.project_id, stage):
            sub.status, sub.reviewed_by, sub.reviewed_at = StageSubmissionStatus.rejected, recorded_by, _now()
            sub.review_note = f"Superseded: an admin recorded this stage directly ({override})"
        db.add(AuditLog(entity_type="project_execution", entity_id=str(execution.id), action="admin_override",
                        performed_by=recorded_by, after_value={"stage": stage.value, "reason": override,
                                                               "execution_mode": execution.execution_mode.value}))
        db.flush()
    return event


def switch_to_grid_nova(db: Session, onboarding: CustomerOnboarding, admin_id: str, reason: str) -> ProjectExecution:
    """An Admin takes execution over from the associate (mandatory reason, audited with
    before/after). Stages already recorded, and any pending submission, are untouched."""
    execution = require(db, onboarding)
    reason = (reason or "").strip()
    if not reason:
        raise AssociateError("Say why Grid Nova is taking over this project's execution.", code="reason_required",
                             status_code=422, field="reason")
    if execution.execution_mode == ExecutionMode.grid_nova:
        raise AssociateError("Grid Nova already executes this project.", code="already_grid_nova", status_code=409)
    before = execution.execution_mode.value
    execution.execution_mode = ExecutionMode.grid_nova
    db.add(AuditLog(entity_type="project_execution", entity_id=str(execution.id), action="mode_changed",
                    performed_by=admin_id, before_value={"execution_mode": before},
                    after_value={"execution_mode": "grid_nova", "reason": reason}))
    db.flush()
    return execution


# ── Aging and status ─────────────────────────────────────────────────────────────

def aging(stages: list[ProjectStage], done: dict, now: Optional[datetime] = None) -> dict:
    """Days since the last recorded stage, the threshold for the next one (E2), and
    whether that makes the project stuck. Only execution stages have thresholds."""
    now = now or _now()
    current = next((s for s in reversed(stages) if s in done), None)
    last = _aware(done[current].recorded_at) if current is not None else None
    nxt = next((s for s in stages if s not in done), None)
    threshold = config.execution()["stuck_threshold_days"].get(nxt.value) if nxt is not None else None
    days = (now - last).days if last is not None else None
    return {"days_in_stage": days, "stuck_threshold_days": threshold,
            "is_stuck": bool(threshold is not None and days is not None and days > threshold)}


def progress_extras(db: Session, onboarding: CustomerOnboarding, stages: list[ProjectStage], done: dict) -> dict:
    execution = get(db, onboarding)
    return {"execution_mode": execution.execution_mode.value if execution else None,
            "execution_started_at": _aware(execution.started_at).isoformat() if execution else None,
            "aging": aging(stages, done) if execution else None}


def submission_json(sub: StageSubmission) -> dict:
    from app.onboarding import progress
    ev = sub.evidence or {}
    return {"submission_id": sub.id, "onboarding_id": sub.onboarding_id, "stage": sub.stage.value,
            "stage_label": progress.LABELS[sub.stage], "status": sub.status.value, "submitted_by": sub.submitted_by,
            "submitted_at": _aware(sub.submitted_at).isoformat(),
            "evidence": {"text": ev.get("text") or {}, "note": ev.get("note"),
                         "documents": {k: _public_file(v) for k, v in (ev.get("documents") or {}).items()},
                         "photos": {k: _public_file(v) for k, v in (ev.get("photos") or {}).items()},
                         "register_verified_at": ev.get("register_verified_at")},
            "reviewed_by": sub.reviewed_by, "reviewed_at": _aware(sub.reviewed_at).isoformat() if sub.reviewed_at
            else None, "review_note": sub.review_note}


def status(db: Session, onboarding: CustomerOnboarding, staff: bool = False) -> dict:
    """The execution picture for one customer — identical for every audience allowed
    to see it (it carries no money). 409 execution_not_started before the agreement."""
    from app.onboarding import progress

    execution = require(db, onboarding)
    stages = progress.applicable(db, onboarding)
    done = progress.recorded(db, onboarding.project_id)
    subs = list(db.execute(select(StageSubmission).where(StageSubmission.project_id == onboarding.project_id)
                           .order_by(StageSubmission.submitted_at, StageSubmission.id)).scalars())
    latest = {}
    for sub in subs:
        latest[sub.stage] = sub
    items = []
    for stage in [s for s in stages if s in EXECUTION_STAGES]:
        event = done.get(stage)
        sub = latest.get(stage)
        rule = config.evidence_rule(stage)
        items.append({
            "stage": stage.value, "label": progress.LABELS[stage], "recorded": event is not None,
            "recorded_at": _aware(event.recorded_at).isoformat() if event else None,
            "recorded_by": event.recorded_by if event else None,
            "via": (event.evidence or {}).get("via") if event else None,
            "submission": submission_json(sub) if sub is not None and event is None else None,
            "evidence_required": {"text": rule.get("text") or [], "documents": rule.get("documents") or [],
                                  "optional_documents": rule.get("optional_documents") or [],
                                  "photos": rule.get("photos") or [],
                                  "register_verified": bool(rule.get("register_verified"))},
            "blockers": stage_blockers(db, onboarding, stage) if event is None else [],
        })
    files = execution.files or {}
    nxt = next((s for s in stages if s not in done), None)
    pending = [s for s in subs if s.status == StageSubmissionStatus.pending]
    extras = register.summary(db, onboarding, execution, staff)
    return {
        "onboarding_id": onboarding.id, "project_id": onboarding.project_id,
        "execution_mode": execution.execution_mode.value,
        "started_at": _aware(execution.started_at).isoformat(),
        "register_verified_at": _aware(execution.register_verified_at).isoformat()
        if execution.register_verified_at else None,
        "current_stage": next((s.value for s in reversed(stages) if s in done), None),
        "next_stage": nxt.value if nxt else None,
        "next_stage_is_execution": nxt in EXECUTION_STAGES if nxt else False,
        "aging": aging(stages, done),
        "pending_submission": submission_json(pending[0]) if pending else None,
        "stages": items,
        "photo_slots": config.photo_slots(), "document_types": config.document_types(),
        "files": {"photos": {k: _public_file(v) for k, v in (files.get("photos") or {}).items()},
                  "documents": {k: _public_file(v) for k, v in (files.get("documents") or {}).items()}},
        "flags": extras["register"]["flags"],
        **extras,
    }
