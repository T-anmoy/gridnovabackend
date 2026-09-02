"""
Drafting notes — what the Proposal Agent remembers, turned into advice for a human.

THE CONSTRAINT THAT SHAPES THIS WHOLE MODULE
The Proposal Agent is deterministic on purpose: sections come from parameter rules,
pricing comes from a capacity tier times a segment modifier, and the manager-signoff
flag comes from a threshold. Memory is NOT allowed to move any of that. If a past
rejection could quietly bend a price, the pricing would stop being reproducible and
"the AI never invents a number" would no longer be true.

So memory does the one thing it can do safely: it tells the human what happened last
time, on the draft they are about to review. Every warning below is a comparison
between two recorded numbers — not a judgement, not a suggestion of what to charge.
The employee decides; adjust-pricing already exists for that and is audited.

Notes ride along on proposal.content under `drafting_notes`. The PDF renders only
the named keys and the `sections` list, so none of this reaches the customer.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

from app.memory.models import MemoryKind

if TYPE_CHECKING:
    from app.memory.memory_manager import MemoryManager

logger = logging.getLogger(__name__)

# Kinds worth showing whoever reviews the draft. Preferences and objections come
# from the SALES agent's reflection — this is where the two agents' memory finally
# meets, without either one calling the other.
_CUSTOMER_CONTEXT_KINDS = (MemoryKind.preference, MemoryKind.objection, MemoryKind.commitment)

_MAX_PER_SECTION = 5


def build_drafting_notes(
    memory: "MemoryManager",
    customer_id: int,
    lead_id: int,
    total_value: Optional[float],
    used_fallback: bool,
) -> dict:
    """Assemble the advisory block for a draft about to be written.

    Returns {} when there is nothing to say, which keeps a first-ever proposal's
    content identical to what it was before this feature existed.
    """
    from app.memory.reflection import PROPOSAL_TAG

    try:
        memories = memory.list_customer_memories(customer_id)
        if not memories:
            return {}

        def _tagged(m) -> bool:
            return PROPOSAL_TAG in [str(t).lower() for t in (m.tags or [])]

        outcomes = [m for m in memories if m.kind == MemoryKind.outcome]
        lessons = [m for m in memories if m.kind == MemoryKind.self_critique and _tagged(m)]
        context = [m for m in memories if m.kind in _CUSTOMER_CONTEXT_KINDS]

        notes: dict = {}
        if outcomes:
            notes["prior_outcomes"] = [m.summary for m in outcomes[:_MAX_PER_SECTION]]
        if lessons:
            notes["lessons_from_past_drafts"] = [m.summary for m in lessons[:_MAX_PER_SECTION]]
        if context:
            notes["customer_context"] = [
                f"({m.kind.value}) {m.summary}" for m in context[:_MAX_PER_SECTION]
            ]

        warnings = _price_warnings(memory, customer_id, total_value)
        if used_fallback and any("fallback" in (m.summary or "").lower() for m in lessons):
            warnings.append(
                "This is not the first draft for this customer to fall back to the "
                "generic scope — the parameter rules still do not match their fields."
            )
        if warnings:
            notes["warnings"] = warnings

        if notes:
            notes["basis"] = (
                "Advisory only. Sections and pricing were composed deterministically "
                "from config/proposal_content.yaml and are not affected by anything here."
            )
        return notes
    except Exception:
        logger.exception("Could not build drafting notes for customer %s", customer_id)
        return {}


def _price_warnings(
    memory: "MemoryManager", customer_id: int, total_value: Optional[float]
) -> list:
    """Flag a draft that repeats a price this customer has already pushed back on.

    Both comparisons are between two numbers we actually recorded. Nothing here
    infers what the price *should* be.
    """
    if not total_value:
        return []

    warnings = []
    accepted = memory.last_accepted_proposal_value(customer_id)
    if accepted and total_value > accepted:
        pct = ((total_value - accepted) / accepted) * 100
        warnings.append(
            f"This drafts at INR {total_value:,.0f}, {pct:.0f}% above the "
            f"INR {accepted:,.0f} this customer previously accepted."
        )

    adjusted = memory.last_adjusted_down_value(customer_id)
    if adjusted and total_value > adjusted:
        warnings.append(
            f"A previous proposal for this customer was repriced down to "
            f"INR {adjusted:,.0f}; the tiered rate has produced "
            f"INR {total_value:,.0f} again."
        )
    return warnings
