"""
Proposal Agent — drafts commercial proposals for qualified leads.

ARCHITECTURAL INVARIANT: This module must never import from agents.sales_agent.
All cross-agent data flows through the Orchestrator and MemoryManager only.

This agent DRAFTS only. It never sends anything. Every send action is an
explicit, authenticated, human-triggered operation via the dashboard API.

Proposal content is composed deterministically by app.proposals.content_engine
from config/proposal_content.yaml — no LLM is involved in selecting sections or
pricing.

MEMORY
This agent remembers what humans did with its drafts (approved, sent back with a
reason, repriced, accepted) and reads that back on the next draft. It stays
deterministic throughout: memory is attached as advisory `drafting_notes` for the
reviewing employee AFTER pricing is settled, and can never move a price or change
which sections are selected.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.memory.models import PipelineStage
from app.proposals import content_engine, drafting_notes
from app.rules.business_rules import evaluate_lead

if TYPE_CHECKING:
    from app.memory.memory_manager import MemoryManager

logger = logging.getLogger(__name__)


class ProposalAgent:
    """
    Creates proposal drafts. Never sends, never calls SalesAgent.
    Always creates a new proposal row (new version) — never edits existing.
    Re-validates lead readiness even though the Orchestrator already checked it.
    """

    def draft_proposal(
        self,
        memory: "MemoryManager",
        lead_id: int,
        performed_by: str,
    ) -> dict:
        # Authorisation happens in the harness before this method is entered.
        cfg = content_engine.load_content_config()
        ctx = memory.get_lead_context(lead_id)
        lead = ctx.get("lead")

        if not lead:
            return {"error": "Lead not found", "lead_id": lead_id}

        collected = dict(lead.collected_fields or {})

        # Compose content first — this also resolves the deterministic pricing that
        # feeds the manager-signoff rule.
        content = content_engine.compose_content(ctx, cfg)
        total_value = content["pricing"]["total_value"]
        used_fallback = content["used_fallback_template"]

        # Re-validate even though Orchestrator checked — defence in depth.
        eval_result = evaluate_lead(collected, total_value=total_value)
        if not eval_result.is_ready_for_proposal:
            return {
                "error": "Lead is not yet fully qualified",
                "missing_fields": eval_result.missing_fields,
                "lead_id": lead_id,
            }

        content["requires_manager_signoff"] = eval_result.requires_manager_signoff
        content["total_value"] = total_value
        content["lead_id"] = lead_id

        # What happened to this customer's earlier proposals, and what the Sales
        # Agent learned about them in conversation. Advisory only: it is attached
        # AFTER composition and pricing are already settled, so it cannot reach back
        # and change either — see app/proposals/drafting_notes.py for why that
        # boundary is absolute. Internal too: the PDF renders named keys and the
        # sections list, never this.
        notes = drafting_notes.build_drafting_notes(
            memory=memory,
            customer_id=lead.customer_id,
            lead_id=lead_id,
            total_value=total_value,
            used_fallback=used_fallback,
        )
        if notes:
            content["drafting_notes"] = notes

        proposal = memory.create_proposal_draft(
            lead_id=lead_id,
            content=content,
            total_value=total_value,
            requires_manager_signoff=eval_result.requires_manager_signoff,
            performed_by=performed_by,
            used_fallback_template=used_fallback,
        )

        # The employee who picks up a lead to draft its first proposal becomes its
        # owner — never reassigns a lead that already has one. This is what makes
        # Sales-role scoping (own leads, own proposals) actually resolve to real
        # rows instead of everything staying unassigned forever.
        memory.assign_lead_if_unassigned(lead_id, employee_id=performed_by, performed_by=performed_by)

        # The draft is itself an episode worth remembering — without it, a later
        # "what have we sent this customer" has outcomes with no starting point.
        from app.memory import reflection

        reflection.record_proposal_outcome(
            memory, proposal.id, reflection.OUTCOME_DRAFTED, performed_by
        )

        # Advance pipeline stage
        memory.update_lead_fields(
            lead_id=lead_id,
            new_fields={},
            performed_by=performed_by,
            stage=PipelineStage.proposal_draft,
        )

        if eval_result.requires_director_notify:
            from app.notifications.notifier import notifier
            capacity_kw = float(collected.get("capacity_kw", 0) or 0)
            notifier.director_capacity_alert(lead_id=lead_id, capacity_kw=capacity_kw)

        return {
            "proposal_id": proposal.id,
            "lead_id": lead_id,
            "version": proposal.version,
            "status": proposal.status.value,
            "requires_manager_signoff": proposal.requires_manager_signoff,
            "total_value": proposal.total_value,
            "used_fallback_template": proposal.used_fallback_template,
        }


proposal_agent = ProposalAgent()
