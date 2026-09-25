"""Staff- and form-driven updates to a lead's collected fields, re-evaluating
qualification exactly the way a chat turn does."""
from __future__ import annotations

from typing import TYPE_CHECKING

from app.memory.models import PipelineStage, TimelineEventType
from app.rules.business_rules import REQUIRED_FIELDS, evaluate_lead
from app.rules.project_types import project_type_for_family

if TYPE_CHECKING:
    from app.memory.memory_manager import MemoryManager
    from app.memory.models import Lead, ProposalInputs


def apply_lead_fields(memory: "MemoryManager", lead: "Lead", fields: dict, performed_by: str, via: str):
    """Merge fields into a lead and re-evaluate qualification (score, stage, and the
    qualification_completed timeline event). Returns (updated_lead, evaluation)."""
    from app.agents.sales_agent import _score_from_fields

    merged = {**dict(lead.collected_fields or {}), **fields}
    score = _score_from_fields(merged)
    eval_result = evaluate_lead(merged)

    stage = None
    if eval_result.is_ready_for_proposal:
        stage = PipelineStage.qualified
    elif merged:
        stage = PipelineStage.qualifying

    updated = memory.update_lead_fields(
        lead_id=lead.id,
        new_fields=fields,
        performed_by=performed_by,
        stage=stage,
        score=score,
    )
    if eval_result.is_ready_for_proposal:
        memory.add_timeline_event(
            lead_id=lead.id,
            event_type=TimelineEventType.qualification_completed,
            description=f"All required fields collected via {via} by {performed_by}",
            created_by=performed_by,
        )
    return updated, eval_result


def required_fields_from_inputs(inputs: "ProposalInputs") -> dict:
    """The REQUIRED_FIELDS a site assessment can honestly supply. name and email are
    not on the form; capacity is only what the customer asked for (desired_output_kw),
    never derived from the bill; project_type only where the family has an exact
    equivalent (app/rules/project_types.py)."""
    candidates = {
        "project_type": project_type_for_family(inputs.project_family),
        "capacity_kw": inputs.desired_output_kw if inputs.desired_output_kw and inputs.desired_output_kw > 0 else None,
        "location": ", ".join(p.strip() for p in (inputs.district, inputs.state) if p and p.strip()) or None,
    }
    return {k: v for k, v in candidates.items() if k in REQUIRED_FIELDS and v is not None}


def fill_lead_from_inputs(memory: "MemoryManager", inputs: "ProposalInputs", performed_by: str) -> dict:
    """Fill the lead's BLANK required fields from its site assessment — never
    overwriting a value already collected. Records which fields came from which
    proposal_inputs row (audit + timeline). Returns the fields filled ({} if none)."""
    lead = memory.get_lead(inputs.lead_id)
    if lead is None:
        return {}
    collected = dict(lead.collected_fields or {})
    filled = {k: v for k, v in required_fields_from_inputs(inputs).items() if not collected.get(k)}
    if not filled:
        return {}

    source = f"site assessment (proposal_inputs #{inputs.id})"
    apply_lead_fields(memory, lead, filled, performed_by=performed_by, via=source)
    memory.record_fields_filled_from_inputs(
        lead_id=lead.id, inputs_id=inputs.id, fields=filled, performed_by=performed_by,
    )
    return filled
