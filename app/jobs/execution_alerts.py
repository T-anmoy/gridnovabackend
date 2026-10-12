"""Stuck-project alerts for execution (Cloud Scheduler → POST
/api/v1/dashboard/internal/check-execution-alerts).

A project is stuck when it has waited for its next execution stage longer than that
stage's threshold (config/execution_payments.yaml execution.stuck_threshold_days,
placeholder E2). Operations get one email listing the projects that became stuck since
the last run; project_executions.stuck_alerted_stage remembers which stage was alerted,
so the same stall is never emailed twice, and a project that moves on and stalls again
is emailed again. Idempotent.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from app.execution import service as execution
from app.memory.memory_manager import MemoryManager
from app.memory.models import Customer, CustomerOnboarding, ProjectExecution, ProjectStage, ProjectStageEvent


def check_execution_alerts(db: Session, now: datetime) -> dict:
    from app.notifications import notifier as notifier_module
    from app.onboarding import progress

    newly_stuck = []
    stuck_total = 0
    # Only projects still in execution: handover_complete is every project's last stage
    # (app/onboarding/progress.py ORDER, always applicable), and a handed-over project
    # can never be stuck, so it is not loaded on every run any more.
    handed_over = exists().where(ProjectStageEvent.project_id == ProjectExecution.project_id,
                                 ProjectStageEvent.stage == ProjectStage.handover_complete)
    for record in db.execute(select(ProjectExecution).where(~handed_over).order_by(ProjectExecution.id)).scalars():
        onboarding = db.get(CustomerOnboarding, record.onboarding_id)
        stages = progress.applicable(db, onboarding)
        done = progress.recorded(db, onboarding.project_id)
        age = execution.aging(stages, done, now)
        if not age["is_stuck"]:
            continue
        stuck_total += 1
        nxt = next(s for s in stages if s not in done)
        if record.stuck_alerted_stage == nxt.value:
            continue
        record.stuck_alerted_stage = nxt.value
        customer = db.get(Customer, onboarding.customer_id)
        newly_stuck.append({"onboarding_id": onboarding.id, "customer_name": customer.name if customer else None,
                            "next_stage": nxt.value, "next_stage_label": progress.LABELS[nxt],
                            "days_in_stage": age["days_in_stage"],
                            "stuck_threshold_days": age["stuck_threshold_days"]})
    if newly_stuck:
        payload = {"projects": newly_stuck}
        MemoryManager(db).notify_after_commit(lambda: notifier_module.notifier.notify("execution_stuck", payload),
                                              "execution_stuck")
    db.commit()
    return {"stuck": stuck_total, "newly_alerted": len(newly_stuck)}
