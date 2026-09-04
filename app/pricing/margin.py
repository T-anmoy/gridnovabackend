"""
Margin Control.

Connected to two other pieces of this session's work, not left isolated:
- app.pricing.engine.PricingBreakdown supplies the (placeholder-based) real
  cost figure calculate_margin() needs — see margin_from_breakdown() below,
  which derives a margin from an actual cost breakdown rather than inventing
  one separately.
- app.rules.approval_engine imports MARGIN_FLOOR_PERCENT from this module
  (not a second, independently-tuned copy) so a below-floor margin can
  escalate the required approval tier — see determine_required_approval_tier().

SECURITY: estimated_cost here is a legitimate INTERNAL cost figure (Admin/
Sales only, same audience as the /pricing/breakdown endpoint) — never
returned by the public /pricing/estimate endpoint or anything it calls.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass

from app.pricing.engine import PricingBreakdown

# Below this margin %, a proposal is losing money relative to what Grid Nova
# considers acceptable — before any real number exists from Raju Paul, these
# are the placeholder boundaries. [PLACEHOLDER — pending real business
# confirmation]
MARGIN_WARNING_PERCENT = 20.0   # below this: warn, but not yet a hard floor
MARGIN_FLOOR_PERCENT = 10.0     # below this: below_floor — see approval_engine.py


class MarginStatus(str, enum.Enum):
    healthy = "healthy"
    warning = "warning"
    below_floor = "below_floor"


@dataclass
class MarginResult:
    margin_inr: float
    margin_percent: float
    status: MarginStatus


def calculate_margin(selling_price: float, estimated_cost: float) -> MarginResult:
    """Margin in ₹ and as a percentage of selling price. A zero or negative
    selling price has no meaningful percentage — reported as 0.0% rather than
    raising or dividing by zero, since a malformed estimate shouldn't crash
    whatever is asking about its margin."""
    margin_inr = selling_price - estimated_cost
    margin_percent = (margin_inr / selling_price * 100.0) if selling_price > 0 else 0.0
    return MarginResult(
        margin_inr=round(margin_inr, 2),
        margin_percent=round(margin_percent, 2),
        status=check_margin_health(margin_percent),
    )


def check_margin_health(margin_percent: float) -> MarginStatus:
    if margin_percent < MARGIN_FLOOR_PERCENT:
        return MarginStatus.below_floor
    if margin_percent < MARGIN_WARNING_PERCENT:
        return MarginStatus.warning
    return MarginStatus.healthy


def margin_from_breakdown(breakdown: PricingBreakdown, estimated_cost: float) -> MarginResult:
    """Derive a margin from a real (placeholder-based) PricingBreakdown's
    total, rather than inventing a selling price separately — the connection
    Phase 4 of this session asked for."""
    return calculate_margin(selling_price=breakdown.total, estimated_cost=estimated_cost)
