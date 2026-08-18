"""
Orchestrator — LangGraph StateGraph coordinating the Sales and Proposal agents.

ARCHITECTURAL INVARIANTS:
- Never generates a proposal or qualifies a lead itself — only routes.
- Agents communicate exclusively through this orchestrator and shared memory.
- Every future agent is an additive graph node, not a rewrite.
"""
from __future__ import annotations

import logging
from typing import Optional

from langgraph.graph import END, StateGraph
from typing_extensions import TypedDict

from app.rules.business_rules import evaluate_lead

logger = logging.getLogger(__name__)


# ── Shared state schema ────────────────────────────────────────────────────────

class OrchestratorState(TypedDict, total=False):
    lead_id: Optional[int]
    customer_id: Optional[int]
    channel: str
    message: str
    collected_fields: dict
    reply_text: str
    missing_fields: list
    is_qualified: bool
    proposal_id: Optional[int]
    proposal_version: Optional[int]
    proposal_status: Optional[str]
    requires_manager_signoff: Optional[bool]
    total_value: Optional[float]
    can_draft: bool
    performed_by: str
    error: Optional[str]


# ── Node functions ─────────────────────────────────────────────────────────────

def _sales_node(state: OrchestratorState) -> OrchestratorState:
    """Route to Sales Agent."""
    from app.agents.sales_agent import sales_agent
    from app.memory.db import SessionLocal
    from app.memory.memory_manager import MemoryManager
    from app.memory.models import Channel

    db = SessionLocal()
    try:
        memory = MemoryManager(db)
        channel_str = state.get("channel", "website")
        try:
            channel = Channel(channel_str)
        except ValueError:
            channel = Channel.website

        result = sales_agent.handle_message(
            memory=memory,
            customer_id=state["customer_id"],
            lead_id=state["lead_id"],
            message=state.get("message", ""),
            channel=channel,
        )
        db.commit()
        return {**state, **result}
    except Exception as exc:
        db.rollback()
        logger.error("Sales node failed: %s", exc, exc_info=True)
        return {**state, "error": str(exc), "reply_text": "I'm sorry, something went wrong. Please try again shortly."}
    finally:
        db.close()


def _proposal_gate_node(state: OrchestratorState) -> OrchestratorState:
    """
    Check whether the lead is ready for a proposal draft.
    If not, return the missing fields — do NOT invoke Proposal Agent.
    """
    from app.memory.db import SessionLocal
    from app.memory.memory_manager import MemoryManager

    db = SessionLocal()
    try:
        memory = MemoryManager(db)
        lead = memory.get_lead(state["lead_id"])
        if not lead:
            return {**state, "can_draft": False, "error": "Lead not found"}

        collected = dict(lead.collected_fields or {})
        eval_result = evaluate_lead(collected)

        error = None
        if not eval_result.is_ready_for_proposal:
            error = f"Lead is not yet fully qualified. Missing fields: {', '.join(eval_result.missing_fields)}"

        return {
            **state,
            "can_draft": eval_result.is_ready_for_proposal,
            "missing_fields": eval_result.missing_fields,
            "is_qualified": eval_result.is_ready_for_proposal,
            "collected_fields": collected,
            "error": error,
        }
    finally:
        db.close()


def _proposal_node(state: OrchestratorState) -> OrchestratorState:
    """Route to Proposal Agent — only reached when can_draft is True."""
    from app.agents.proposal_agent import proposal_agent
    from app.memory.db import SessionLocal
    from app.memory.memory_manager import MemoryManager

    db = SessionLocal()
    try:
        memory = MemoryManager(db)
        result = proposal_agent.draft_proposal(
            memory=memory,
            lead_id=state["lead_id"],
            performed_by=state.get("performed_by", "system"),
        )
        db.commit()
        return {
            **state,
            "proposal_id": result.get("proposal_id"),
            "proposal_version": result.get("version"),
            "proposal_status": result.get("status"),
            "requires_manager_signoff": result.get("requires_manager_signoff"),
            "total_value": result.get("total_value"),
            "missing_fields": result.get("missing_fields", []),
            "error": result.get("error"),
        }
    except Exception as exc:
        db.rollback()
        logger.error("Proposal node failed: %s", exc, exc_info=True)
        return {**state, "error": str(exc)}
    finally:
        db.close()


def _gate_router(state: OrchestratorState) -> str:
    return "proposal" if state.get("can_draft") else END


# ── Graph construction ─────────────────────────────────────────────────────────

def _build_customer_graph() -> object:
    g = StateGraph(OrchestratorState)
    g.add_node("sales", _sales_node)
    g.set_entry_point("sales")
    g.add_edge("sales", END)
    return g.compile()


def _build_proposal_graph() -> object:
    g = StateGraph(OrchestratorState)
    g.add_node("gate", _proposal_gate_node)
    g.add_node("proposal", _proposal_node)
    g.set_entry_point("gate")
    g.add_conditional_edges("gate", _gate_router, {"proposal": "proposal", END: END})
    g.add_edge("proposal", END)
    return g.compile()


_customer_graph = _build_customer_graph()
_proposal_graph = _build_proposal_graph()


# ── Facade ─────────────────────────────────────────────────────────────────────

class Orchestrator:
    def handle_customer_message(
        self,
        customer_id: int,
        lead_id: int,
        message: str,
        channel: str = "website",
    ) -> dict:
        state: OrchestratorState = {
            "customer_id": customer_id,
            "lead_id": lead_id,
            "message": message,
            "channel": channel,
            "collected_fields": {},
            "missing_fields": [],
            "is_qualified": False,
            "can_draft": False,
            "performed_by": "customer",
        }
        result = _customer_graph.invoke(state)
        return dict(result)

    def request_proposal_draft(
        self,
        lead_id: int,
        customer_id: int,
        performed_by: str,
    ) -> dict:
        state: OrchestratorState = {
            "lead_id": lead_id,
            "customer_id": customer_id,
            "performed_by": performed_by,
            "channel": "dashboard",
            "message": "",
            "collected_fields": {},
            "missing_fields": [],
            "is_qualified": False,
            "can_draft": False,
        }
        result = _proposal_graph.invoke(state)
        return dict(result)


orchestrator = Orchestrator()
