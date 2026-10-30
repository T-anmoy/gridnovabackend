"""What stops a proposal reaching a customer before it is real.

Approval is internal and may happen on draft copy; what a customer receives may not.
A proposal with a BE-13 document cannot even be approved while required data is missing
(document_service.problem — the approve and manager-sign-off routes check it too).
Used by every path that puts a proposal in a customer's hands — the emailed send
(dashboard /proposals/{id}/send) and in-person delivery by an associate or staff
(app/onboarding/formal.py) — so neither can bypass the other.

Grid Nova's own wording in a section (terms, scope and notes registered as approved_text
in config/placeholder_registry.yaml) must be approved by an Admin exactly as the section
stores it (BE-22): otherwise the section is unapproved_sections and the proposal is held
the same way. A proposal that fell back to the generic scope (used_fallback_template) is
held for staff to classify it (manual_review_required), even once that scope is approved.

reverification_problem() is the second rule: a proposal flagged after a survey change
(app/survey/changes.py) waits for staff to re-verify it. It applies to the staff send,
in-person acceptance and the online accept link alike.
"""
from __future__ import annotations

from typing import Optional

from app.proposals import content_engine


def placeholder_problem(proposal, db=None) -> Optional[dict]:
    """None when the proposal may go to a customer; otherwise the 409 error body. A
    BE-13 document missing required data answers missing_required with the list."""
    from app.proposals import document_service
    missing = document_service.problem(proposal)
    if missing is not None:
        return missing
    unapproved = content_engine.unapproved_sections(proposal.content, db)
    fallback = bool(getattr(proposal, "used_fallback_template", False))
    if not content_engine.has_placeholder_content(proposal.content, proposal.total_value)             and not unapproved and not fallback:
        return None
    sections = content_engine.placeholder_sections(proposal.content)
    problems = []
    if sections:
        problems.append(f"placeholder text in section(s): {', '.join(sections)}")
    if unapproved:
        problems.append(f"wording not yet approved by an Admin in section(s): {', '.join(unapproved)}")
    if not proposal.total_value:
        problems.append("no price set — Price to be set by Grid Nova (use adjust-pricing)")
    if fallback:
        problems.append("it used the generic fallback scope and needs manual review (set the segment / "
                        "consumer category so the right scope applies)")
    if sections or not proposal.total_value:
        code = "placeholder_content"
    elif unapproved:
        code = "unapproved_sections"
    else:
        code = "manual_review_required"
    return {
        "code": code,
        "message": "This proposal cannot be sent to a customer yet: " + "; ".join(problems) + ".",
        "placeholder_sections": sections,
        "unapproved_sections": unapproved,
        "manual_review_required": fallback,
        "missing_total_value": not proposal.total_value,
    }


def reverification_problem(proposal) -> Optional[dict]:
    """None unless the proposal is flagged for re-verification; otherwise the 409 body
    {code: needs_reverification, message, fields}."""
    from app.survey import changes
    return changes.problem(proposal)
