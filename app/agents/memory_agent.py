"""
Memory Agent — answers an employee's questions about a customer from stored memory.

ARCHITECTURAL INVARIANT: like every other agent here, this module must never import
from agents.sales_agent or agents.proposal_agent. It reads what those agents left in
shared memory; it never calls them.

WHAT MAKES THIS AN AGENT AND NOT A SEARCH BOX
It resolves who is being asked about, decides what to retrieve, and refuses to
answer when the question is ambiguous — answering confidently about the wrong
customer is a worse failure than asking which one was meant.

WHO IT TALKS TO
Employees only, through the authenticated dashboard. It is never wired into the
public chat endpoint: this agent can read every customer's history, so exposing it
to a customer would leak other customers' data. That restriction lives in the
route's auth dependency, and this docstring exists so nobody removes it by accident.

GROUNDING
The answer is generated from retrieved records alone. Anything not on record must
come back as not on record, never as a plausible guess — this is a CRM lookup.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

from app.llm.client import llm_client
from app.memory import recall

if TYPE_CHECKING:
    from app.memory.memory_manager import MemoryManager

logger = logging.getLogger(__name__)

MAX_QUESTION_CHARS = 500


class MemoryAgent:
    def answer(
        self,
        memory: "MemoryManager",
        question: str,
        customer_id: Optional[int] = None,
        performed_by: str = "employee",
    ) -> dict:
        # Authorisation happens in the harness before this method is entered.
        question = (question or "").strip()
        if not question:
            return {"answer": "Ask me something about a customer and I'll check what we have on file.",
                    "resolved": False, "candidates": [], "citations": [], "mode": "none"}
        question = question[:MAX_QUESTION_CHARS]

        customer, candidates = recall.resolve_customer(memory, question, customer_id)

        if customer is None and len(candidates) > 1:
            names = ", ".join(self._label(c) for c in candidates)
            return {
                "answer": f"More than one customer matches that. Which did you mean: {names}?",
                "resolved": False,
                "candidates": [self._summarise(c) for c in candidates],
                "citations": [],
                "mode": "none",
            }

        if customer is None:
            return {
                "answer": "I couldn't tell which customer you mean. Give me a name, an "
                          "email or a company — or open the customer and ask from there.",
                "resolved": False,
                "candidates": [],
                "citations": [],
                "mode": "none",
            }

        retrieved = recall.retrieve(memory, customer.id, question)
        context_block = retrieved["context_block"]
        memories = retrieved["memories"]

        answer = llm_client.answer_from_memory(
            question=question,
            context_block=context_block,
            customer_label=self._label(customer),
        )

        logger.info(
            "Memory recall answered",
            extra={"customer_id": customer.id, "performed_by": performed_by,
                   "memories_used": len(memories), "mode": retrieved["mode"]},
        )

        return {
            "answer": answer,
            "resolved": True,
            "customer": self._summarise(customer),
            "candidates": [],
            "citations": [
                {"id": m.id, "kind": m.kind.value, "summary": m.summary,
                 "occurred_at": m.occurred_at.isoformat() if m.occurred_at else None}
                for m in memories
            ],
            "mode": retrieved["mode"],
        }

    @staticmethod
    def _label(customer) -> str:
        parts = [customer.name or "unnamed"]
        if customer.company:
            parts.append(f"({customer.company})")
        parts.append(f"<{customer.email}>")
        return " ".join(parts)

    @staticmethod
    def _summarise(customer) -> dict:
        return {
            "customer_id": customer.id,
            "name": customer.name,
            "email": customer.email,
            "company": customer.company,
        }


memory_agent = MemoryAgent()
