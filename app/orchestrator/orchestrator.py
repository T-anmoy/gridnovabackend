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
    # Memory recall (employee asking about a customer)
    question: Optional[str]
    answer: Optional[str]
    resolved: Optional[bool]
    candidates: Optional[list]
    citations: Optional[list]
    recall_mode: Optional[str]
    # A Sales employee's id when recall must stay inside their lead scope.
    scope_employee_id: Optional[str]
    # Reply language for the website chat (en | hi | bn); wording only.
    locale: Optional[str]
    # Correlates one turn across the agent, the harness and the audit trail.
    agent_run_id: Optional[str]


# ── Node functions ─────────────────────────────────────────────────────────────

def _sales_node(state: OrchestratorState) -> OrchestratorState:
    """Route to the Sales Agent, under the harness."""
    from app.agents import harness, sales_agent as sales_module
    from app.governance import registry
    from app.memory.db import SessionLocal
    from app.memory.memory_manager import MemoryManager
    from app.memory.models import Channel

    agent = sales_module.sales_agent
    db = SessionLocal()
    try:
        memory = MemoryManager(db)
        channel_str = state.get("channel", "website")
        try:
            channel = Channel(channel_str)
        except ValueError:
            channel = Channel.website

        run = harness.run(
            registry.SALES_AGENT.id,
            registry.REPLY_TO_CUSTOMER,
            lambda: agent.handle_message(
                memory=memory,
                customer_id=state["customer_id"],
                lead_id=state["lead_id"],
                message=state.get("message", ""),
                channel=channel,
                locale=state.get("locale", "en"),
            ),
            memory=memory,
            lead_id=state["lead_id"],
            customer_id=state["customer_id"],
            # A refused agent still answers the customer — the harness cannot know
            # what a safe answer sounds like here, so the agent supplies it.
            on_denied=lambda decision: agent.stand_down(
                memory=memory, decision=decision, customer_id=state["customer_id"],
                lead_id=state["lead_id"], message=state.get("message", ""),
                channel=channel,
            ),
            on_error=lambda exc: {
                "reply_text": "I'm sorry, something went wrong. Please try again shortly.",
            },
        )

        if run.code == harness.FAILED:
            # The agent's own writes are discarded; the harness has already recorded
            # the failed run on a separate session, so the record survives this.
            db.rollback()
            return {**state, **run.result, "error": run.error, "agent_run_id": run.run_id}

        db.commit()

        # Reflection runs AFTER the commit, on its own session and off the reply
        # path: the customer's answer never waits on the agent writing down what it
        # learned, and the reflection sees a conversation that includes this turn.
        trigger = run.result.pop("reflection_trigger", None)
        if trigger:
            from app.memory import reflection

            reflection.start_reflection(
                lead_id=state["lead_id"], trigger=trigger, performed_by="sales_agent"
            )

        return {**state, **run.result, "agent_run_id": run.run_id}
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
    from app.agents import harness
    from app.agents.proposal_agent import proposal_agent
    from app.governance import registry
    from app.memory.db import SessionLocal
    from app.memory.memory_manager import MemoryManager

    db = SessionLocal()
    try:
        memory = MemoryManager(db)
        run = harness.run(
            registry.PROPOSAL_AGENT.id,
            registry.DRAFT_PROPOSAL,
            lambda: proposal_agent.draft_proposal(
                memory=memory,
                lead_id=state["lead_id"],
                performed_by=state.get("performed_by", "system"),
            ),
            memory=memory,
            lead_id=state["lead_id"],
            customer_id=state.get("customer_id"),
        )
        if run.code == harness.FAILED:
            db.rollback()
            return {**state, "error": run.error, "agent_run_id": run.run_id}

        result = run.result
        if result.get("proposal_id") and result.get("requires_manager_signoff"):
            from app.notifications.notifier import notifier
            proposal_id, lead_id, value = result["proposal_id"], state["lead_id"], result.get("total_value")
            memory.notify_after_commit(
                lambda: notifier.manager_signoff_required(proposal_id=proposal_id, lead_id=lead_id, total_value=value),
                "manager_signoff_required",
            )
        db.commit()
        return {
            **state,
            "agent_run_id": run.run_id,
            "proposal_id": result.get("proposal_id"),
            "proposal_version": result.get("version"),
            "proposal_status": result.get("status"),
            "requires_manager_signoff": result.get("requires_manager_signoff"),
            "total_value": result.get("total_value"),
            "missing_fields": result.get("missing_fields", []),
            "error": result.get("error"),
        }
    finally:
        db.close()


def _recall_node(state: OrchestratorState) -> OrchestratorState:
    """Route to the Memory Agent — answers an employee's question about a customer.

    Read-only: this node never mutates a lead, a customer or a proposal, so unlike
    the other nodes it has nothing to commit and nothing to roll back.
    """
    from app.agents import harness
    from app.agents.memory_agent import memory_agent
    from app.governance import registry
    from app.memory.db import SessionLocal
    from app.memory.memory_manager import MemoryManager

    db = SessionLocal()
    try:
        memory = MemoryManager(db)
        run = harness.run(
            registry.MEMORY_AGENT.id,
            registry.ANSWER_EMPLOYEE_QUESTION,
            lambda: memory_agent.answer(
                memory=memory,
                question=state.get("question", ""),
                customer_id=state.get("customer_id"),
                performed_by=state.get("performed_by", "employee"),
                scope_employee_id=state.get("scope_employee_id"),
            ),
            memory=memory,
            customer_id=state.get("customer_id"),
            on_denied=lambda decision: {"answer": decision.reason, "resolved": False},
            on_error=lambda exc: {
                "answer": "I couldn't reach the customer memory just now. Try again shortly.",
            },
        )
        # Read-only as far as the customer record goes — but the harness's own run
        # record is a write, and without this commit a recall would leave no trace
        # of ever having happened.
        db.commit()
        result = run.result
        return {
            "agent_run_id": run.run_id,
            **state,
            "answer": result.get("answer"),
            "resolved": result.get("resolved", False),
            "candidates": result.get("candidates", []),
            "citations": result.get("citations", []),
            "recall_mode": result.get("mode"),
            "customer_id": (result.get("customer") or {}).get("customer_id") or state.get("customer_id"),
            "error": run.error,
        }
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


def _build_recall_graph() -> object:
    g = StateGraph(OrchestratorState)
    g.add_node("recall", _recall_node)
    g.set_entry_point("recall")
    g.add_edge("recall", END)
    return g.compile()


_customer_graph = _build_customer_graph()
_proposal_graph = _build_proposal_graph()
_recall_graph = _build_recall_graph()


# ── Facade ─────────────────────────────────────────────────────────────────────

class Orchestrator:
    def handle_customer_message(
        self,
        customer_id: int,
        lead_id: int,
        message: str,
        channel: str = "website",
        locale: str = "en",
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
            "locale": locale,
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

    def answer_customer_question(
        self,
        question: str,
        performed_by: str,
        customer_id: Optional[int] = None,
        scope_employee_id: Optional[str] = None,
    ) -> dict:
        """Answer an employee's question about a customer from stored memory.

        Employee-facing only — see app/agents/memory_agent.py for why this must
        never be wired into the public chat endpoint.
        """
        state: OrchestratorState = {
            "question": question,
            "customer_id": customer_id,
            "performed_by": performed_by,
            "scope_employee_id": scope_employee_id,
            "channel": "dashboard",
            "message": "",
            "resolved": False,
            "candidates": [],
            "citations": [],
        }
        result = _recall_graph.invoke(state)
        return dict(result)


orchestrator = Orchestrator()
