"""
Approval Engine — 3-tier approval-tier determination.

Extends the existing 2-tier system (business_rules.evaluate_lead's
requires_manager_signoff, gated by AuthenticatedEmployee.can_approve_standard/
can_approve_manager_signoff in app/auth/dependencies.py) with a 3rd tier,
partner_signoff, and a genuinely multi-factor decision — value, discount,
margin, and an explicit risk flag all considered together, not value alone.

Kept in its own module rather than folded into business_rules.py: that
module's existing thresholds are settings-driven, already-confirmed real
values; this module's thresholds are, by the letter of this session's brief,
still-open placeholders tied to two brand-new subsystems (the Pricing Engine
and Margin Control) that don't exist in business_rules.py's world at all.

ADDITIVE, NOT A REPLACEMENT: this module does not change
Proposal.requires_manager_signoff, does not change can_approve_standard() or
can_approve_manager_signoff(), and does not alter the existing
/proposals/{id}/approve or /manager-sign-off endpoints in dashboard.py. It is
a new, separate determination the existing approval flow can consult (see
GET /api/v1/dashboard/proposals/{id}/required-approval-tier) — the 2-tier
gate that actually controls approval today is untouched.

STILL AN OPEN QUESTION: this session builds the ROUTING LOGIC ITSELF. It does
not resolve "Approval Engine vs. a plain ₹ ladder" — that's a real, still-open
business question. The thresholds below are placeholders precisely because
that question hasn't been answered yet.
"""
from __future__ import annotations

import enum
from typing import Optional

from app.config import settings
from app.pricing.margin import MARGIN_FLOOR_PERCENT


class ApprovalTier(str, enum.Enum):
    standard = "standard"
    manager_signoff = "manager_signoff"
    partner_signoff = "partner_signoff"


_TIER_ORDER = (ApprovalTier.standard, ApprovalTier.manager_signoff, ApprovalTier.partner_signoff)


def _rank(tier: ApprovalTier) -> int:
    return _TIER_ORDER.index(tier)


def _escalate_to(current: ApprovalTier, candidate: ApprovalTier) -> ApprovalTier:
    """Never downgrades — the highest tier any single factor calls for wins."""
    return candidate if _rank(candidate) > _rank(current) else current


def _escalate_by_one(current: ApprovalTier) -> ApprovalTier:
    idx = min(_rank(current) + 1, len(_TIER_ORDER) - 1)
    return _TIER_ORDER[idx]


# ── Placeholder thresholds — pending real business confirmation ────────────────
# (the "Approval Engine vs. ₹ ladder" open question)

# The tier-2 (manager) value boundary REUSES the existing, already-confirmed
# real setting (app.config.settings.proposal_value_manager_approval_threshold)
# rather than inventing a parallel placeholder for the same concept the
# 2-tier system already answers.
#
# The tier-3 (partner) value boundary is genuinely new and IS a placeholder.
# [PLACEHOLDER — pending real business confirmation]
PLACEHOLDER_PARTNER_SIGNOFF_VALUE_THRESHOLD_INR = 2_000_000.0

# A discount at or above this escalates to at least manager tier, regardless
# of value. [PLACEHOLDER]
PLACEHOLDER_HIGH_DISCOUNT_PERCENT_THRESHOLD = 15.0
# A discount at or above this escalates all the way to partner tier.
# [PLACEHOLDER]
PLACEHOLDER_VERY_HIGH_DISCOUNT_PERCENT_THRESHOLD = 25.0

# Margin uses MARGIN_FLOOR_PERCENT from app.pricing.margin — the SAME number
# Margin Control's own check_margin_health() calls "below_floor", imported
# rather than redefined here so the two pieces of logic can never silently
# drift apart (see Phase 4 of this session's report).


def determine_required_approval_tier(
    proposal_value: Optional[float],
    discount_percent: float = 0.0,
    margin_percent: Optional[float] = None,
    is_flagged_risk: bool = False,
) -> ApprovalTier:
    """The highest tier called for by ANY of the four factors — value,
    discount, margin, or an explicit risk flag — considered independently and
    combined by taking the max, not by value alone. A small deal with an
    unusually large discount, or a thin margin, can reach a higher tier than
    its value alone would suggest.
    """
    tier = ApprovalTier.standard

    if proposal_value is not None:
        if proposal_value > PLACEHOLDER_PARTNER_SIGNOFF_VALUE_THRESHOLD_INR:
            tier = _escalate_to(tier, ApprovalTier.partner_signoff)
        elif proposal_value > settings.proposal_value_manager_approval_threshold:
            tier = _escalate_to(tier, ApprovalTier.manager_signoff)

    if discount_percent >= PLACEHOLDER_VERY_HIGH_DISCOUNT_PERCENT_THRESHOLD:
        tier = _escalate_to(tier, ApprovalTier.partner_signoff)
    elif discount_percent >= PLACEHOLDER_HIGH_DISCOUNT_PERCENT_THRESHOLD:
        tier = _escalate_to(tier, ApprovalTier.manager_signoff)

    if margin_percent is not None and margin_percent < MARGIN_FLOOR_PERCENT:
        tier = _escalate_to(tier, ApprovalTier.manager_signoff)

    if is_flagged_risk:
        tier = _escalate_by_one(tier)

    return tier
