"""Overdue survey-review alerts (Cloud Scheduler → POST
/api/v1/dashboard/internal/check-survey-reviews).

A survey waiting for the Engineer's review longer than config/site_survey.yaml
review_target_hours is overdue (app/survey/queue.py). Staff get one email listing the
surveys that became overdue since the last run.

Each survey is claimed before it is reported, with one conditional UPDATE:
proposal_inputs.survey_review_alerted_for is set to the moment the survey started
waiting, only where it is not that moment already. Two overlapping runs therefore never
report the same wait twice (on PostgreSQL the second UPDATE waits for the first run's
row lock and then finds the row already claimed); a survey that re-enters the queue
waits from a new moment and is reported again. Idempotent.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import or_, update
from sqlalchemy.orm import Session

from app.memory.memory_manager import MemoryManager
from app.memory.models import ProposalInputs
from app.survey import queue


def check_survey_reviews(db: Session, now: datetime) -> dict:
    from app.notifications import notifier as notifier_module

    overdue = [r for r in queue.rows(db, None, now) if r["overdue"]]
    newly = []
    for row in overdue:
        claimed = db.execute(
            update(ProposalInputs)
            .where(ProposalInputs.id == row["_inputs_id"],
                   or_(ProposalInputs.survey_review_alerted_for.is_(None),
                       ProposalInputs.survey_review_alerted_for != row["_since"]))
            .values(survey_review_alerted_for=row["_since"])
            .execution_options(synchronize_session=False)).rowcount
        if claimed:
            newly.append({"onboarding_id": row["onboarding_id"], "client_code": row["client_code"],
                          "customer_name": row["customer_name"], "hours_waiting": row["hours_waiting"],
                          "revised_after_review": row["revised_after_review"]})
    if newly:
        payload = {"surveys": newly, "review_target_hours": queue.target_hours()}
        MemoryManager(db).notify_after_commit(
            lambda: notifier_module.notifier.notify("survey_review_overdue", payload), "survey_review_overdue")
    db.commit()
    return {"overdue": len(overdue), "newly_alerted": len(newly)}
