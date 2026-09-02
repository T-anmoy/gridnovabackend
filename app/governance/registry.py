"""
Agent registry — the declared source of truth for what each agent is allowed to do.

WHY A REGISTRY AND NOT JUST DOCSTRINGS
Until now the rules that matter most about these agents — the Proposal Agent never
sends, the Sales Agent never prices, agents never call each other — lived in prose
at the top of each module and in the README. Prose cannot be checked, cannot be
enforced at the point of action, and cannot be shown to anyone asking what the AI in
this system is permitted to do. This module turns those rules into data.

HOW IT IS USED
- app/governance/policy.py enforces it: every governed action is authorised against
  the spec below before it happens, so a prohibition is a runtime denial rather than
  a comment someone might not read.
- tests/test_governance.py enforces the structural half: that the modules really do
  not import each other, and that no agent has a capability it also declares as a
  prohibition.
- The dashboard serves it, so Grid Nova can answer "what is the AI allowed to do
  here" without reading source.

CHANGING A SPEC IS A GOVERNANCE DECISION
Widening `capabilities` or dropping a `prohibition` changes what the system may do
autonomously. Those edits deserve the same scrutiny as a change to the approval
thresholds, which is exactly why they are in one reviewable file rather than spread
across the agents that happen to implement them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# ── Governed actions ───────────────────────────────────────────────────────────
# Every action an agent can take that a human would want a say in. Reading data is
# not listed: the interesting boundary is what an agent can DO.

REPLY_TO_CUSTOMER = "reply_to_customer"
EXTRACT_FIELDS = "extract_fields"
OFFER_MEETING_SLOTS = "offer_meeting_slots"
BOOK_MEETING = "book_meeting"
WRITE_CUSTOMER_MEMORY = "write_customer_memory"
DRAFT_PROPOSAL = "draft_proposal"
SET_PROPOSAL_PRICE = "set_proposal_price"
APPROVE_PROPOSAL = "approve_proposal"
SEND_PROPOSAL = "send_proposal"
ANSWER_EMPLOYEE_QUESTION = "answer_employee_question"
READ_CUSTOMER_MEMORY = "read_customer_memory"


class Autonomy:
    """How much rope an agent has, in the terms a manager would use."""
    AUTONOMOUS = "autonomous"                    # acts without a human in the loop
    HUMAN_TRIGGERED = "human_triggered"          # a person starts it, it then runs alone
    READ_ONLY = "read_only"                      # cannot change anything


@dataclass(frozen=True)
class AgentSpec:
    id: str
    name: str
    purpose: str
    owner: str                      # the team accountable for this agent's behaviour
    autonomy: str
    audience: str                   # who it is allowed to speak to
    capabilities: tuple             # actions it MAY take
    prohibitions: tuple             # actions it must NEVER take, enforced at runtime
    human_gates: tuple              # what a person must do before its work has effect
    uses_llm: bool
    guardrails: tuple = field(default_factory=tuple)
    module: str = ""

    def may(self, action: str) -> bool:
        return action in self.capabilities and action not in self.prohibitions


SALES_AGENT = AgentSpec(
    id="sales_agent",
    name="Nova (Sales Agent)",
    purpose="Qualify inbound leads by conversation, and book a meeting once qualified.",
    owner="Sales",
    autonomy=Autonomy.AUTONOMOUS,
    audience="customers",
    capabilities=(
        REPLY_TO_CUSTOMER, EXTRACT_FIELDS, OFFER_MEETING_SLOTS, BOOK_MEETING,
        WRITE_CUSTOMER_MEMORY, READ_CUSTOMER_MEMORY,
    ),
    prohibitions=(
        # Pricing is deterministic and belongs to the Proposal Agent and the YAML.
        SET_PROPOSAL_PRICE,
        DRAFT_PROPOSAL,
        APPROVE_PROPOSAL,
        SEND_PROPOSAL,
    ),
    human_gates=(),  # its replies and bookings take effect immediately
    uses_llm=True,
    guardrails=("deterministic output checks", "NeMo disclosure rail (optional)"),
    module="app.agents.sales_agent",
)

PROPOSAL_AGENT = AgentSpec(
    id="proposal_agent",
    name="Proposal Agent",
    purpose="Draft a commercial proposal for a qualified lead, from configured rules.",
    owner="Sales operations",
    autonomy=Autonomy.HUMAN_TRIGGERED,
    audience="internal only",
    capabilities=(DRAFT_PROPOSAL, READ_CUSTOMER_MEMORY, WRITE_CUSTOMER_MEMORY),
    prohibitions=(
        # The whole design rests on these three.
        SEND_PROPOSAL,
        APPROVE_PROPOSAL,
        REPLY_TO_CUSTOMER,
        # It composes a price from the tier table; it may not choose one.
        SET_PROPOSAL_PRICE,
    ),
    human_gates=(
        "an employee triggers every draft",
        "approval or manager sign-off before it can be sent",
        "an employee sends it",
    ),
    uses_llm=False,  # composition and pricing are deterministic, by design
    guardrails=("readiness gate", "re-validation after pricing", "advisory notes only"),
    module="app.agents.proposal_agent",
)

MEMORY_AGENT = AgentSpec(
    id="memory_agent",
    name="Memory Agent",
    purpose="Answer an employee's question about a customer from stored records.",
    owner="Sales",
    autonomy=Autonomy.READ_ONLY,
    audience="employees",
    capabilities=(ANSWER_EMPLOYEE_QUESTION, READ_CUSTOMER_MEMORY),
    prohibitions=(
        # It can read every customer's history, so it must never face a customer.
        REPLY_TO_CUSTOMER,
        WRITE_CUSTOMER_MEMORY,
        DRAFT_PROPOSAL,
        SEND_PROPOSAL,
        APPROVE_PROPOSAL,
        BOOK_MEETING,
    ),
    human_gates=("an authenticated employee asks the question",),
    uses_llm=True,
    guardrails=("answers only from retrieved records", "role and ownership scoping"),
    module="app.agents.memory_agent",
)

AGENTS = {spec.id: spec for spec in (SALES_AGENT, PROPOSAL_AGENT, MEMORY_AGENT)}


def get(agent_id: str) -> Optional[AgentSpec]:
    return AGENTS.get(agent_id)


def all_specs() -> list:
    return list(AGENTS.values())
