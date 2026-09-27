"""What stops a proposal reaching a customer before it is real.

Approval is internal and may happen on draft copy; what a customer receives may not.
Used by every path that puts a proposal in a customer's hands — the emailed send
(dashboard /proposals/{id}/send) and in-person delivery by an associate or staff
(app/onboarding/formal.py) — so neither can bypass the other.
"""
from __future__ import annotations

from typing import Optional

from app.proposals import content_engine


def placeholder_problem(proposal) -> Optional[dict]:
    """None when the proposal may go to a customer; otherwise the 409 error body."""
    if not content_engine.has_placeholder_content(proposal.content, proposal.total_value):
        return None
    sections = content_engine.placeholder_sections(proposal.content)
    problems = []
    if sections:
        problems.append(f"placeholder text in section(s): {', '.join(sections)}")
    if not proposal.total_value:
        problems.append("no price set (use adjust-pricing)")
    return {
        "code": "placeholder_content",
        "message": "This proposal cannot be sent to a customer yet: " + "; ".join(problems) + ".",
        "placeholder_sections": sections,
        "missing_total_value": not proposal.total_value,
    }
