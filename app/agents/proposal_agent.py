"""
Proposal Agent — drafts commercial proposals for qualified leads.

ARCHITECTURAL INVARIANT: This module must never import from agents.sales_agent.
All cross-agent data flows through the Orchestrator and MemoryManager only.

This agent DRAFTS only. It never sends anything. Every send action is an
explicit, authenticated, human-triggered operation via the dashboard API.

Proposal content is composed deterministically by app.proposals.content_engine
from config/proposal_content.yaml — no LLM is involved in selecting sections or
pricing.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.memory.models import PipelineStage
from app.proposals import content_engine
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

        proposal = memory.create_proposal_draft(
            lead_id=lead_id,
            content=content,
            total_value=total_value,
            requires_manager_signoff=eval_result.requires_manager_signoff,
            performed_by=performed_by,
            used_fallback_template=used_fallback,
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
