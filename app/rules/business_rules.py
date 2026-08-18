"""
Deterministic business rules — zero LLM calls anywhere in this module.
All thresholds are read from settings; no magic numbers.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from app.config import settings

REQUIRED_FIELDS = ["name", "email", "project_type", "location", "capacity_kw"]

SCORED_FIELDS = ["capacity_kw", "budget", "timeline", "monthly_electricity_bill"]


def check_missing_fields(collected: dict) -> list[str]:
    return [f for f in REQUIRED_FIELDS if not collected.get(f)]


def evaluate_proposal_value(value: Optional[float]) -> bool:
    """True when the value exceeds the manager-approval threshold."""
    if value is None:
        return False
    return value > settings.proposal_value_manager_approval_threshold


def evaluate_capacity(capacity_kw: Optional[float]) -> bool:
    """True when capacity exceeds the director-notification threshold (MW → kW)."""
    if capacity_kw is None:
        return False
    threshold_kw = settings.capacity_director_notify_threshold_mw * 1_000
    return capacity_kw > threshold_kw


def evaluate_existing_customer(is_existing: bool) -> bool:
    return is_existing


def _score_completeness(collected: dict) -> str:
    present = sum(1 for f in SCORED_FIELDS if collected.get(f))
    if present == len(SCORED_FIELDS):
        return "high"
    if present >= 2:
        return "medium"
    return "low"


@dataclass
class LeadEvaluation:
    missing_fields: list[str] = field(default_factory=list)
    lead_score: str = "low"
    is_ready_for_proposal: bool = False
    requires_manager_signoff: bool = False
    requires_director_notify: bool = False
    is_existing_customer: bool = False


def evaluate_lead(
    collected: dict,
    total_value: Optional[float] = None,
    is_existing_customer: bool = False,
) -> LeadEvaluation:
    missing = check_missing_fields(collected)
    score = _score_completeness(collected)
    capacity_kw = collected.get("capacity_kw")
    if isinstance(capacity_kw, str):
        try:
            capacity_kw = float(capacity_kw)
        except (ValueError, TypeError):
            capacity_kw = None

    requires_signoff = evaluate_proposal_value(total_value) or evaluate_capacity(capacity_kw)
    requires_director = evaluate_capacity(capacity_kw)

    return LeadEvaluation(
        missing_fields=missing,
        lead_score=score,
        is_ready_for_proposal=len(missing) == 0,
        requires_manager_signoff=requires_signoff,
        requires_director_notify=requires_director,
        is_existing_customer=is_existing_customer,
    )
