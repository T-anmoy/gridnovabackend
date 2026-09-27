"""
Instant estimate — orchestrates sizing (app/pricing/sizing.py), the layered
cost engine (app/pricing/engine.py), the access-difficulty modifier added to
that engine, and the PM Surya Ghar subsidy (app/pricing/subsidy.py) into one
breakdown for the public /solar-calculator flow.

Kept separate from engine.py itself: engine.py is the general-purpose layered
cost model shared by the public /estimate and staff /breakdown endpoints
(untouched by this module). This orchestration — sizing first, tier and
subsidy applied after, the shading-uncertainty branch — is specific to the
instant-estimate use case and has no business inside the general engine.

TIER: this public path always prices at the fixed best tier — see
FIXED_PUBLIC_TIER below. Tier is no longer a customer-supplied input here;
engine.py's tier concept itself (calculate_tier_multiplier, KNOWN_TIERS) is
untouched, since a staff-facing path may legitimately need real tier
flexibility later. This module just no longer exposes that choice publicly,
and no longer reports which tier the price reflects — a customer gets one
real number, not "the price at tier X."

SHADING: "significant" shading is not part of engine.py's own shading
vocabulary (none/partial_morning/partial_evening/heavy) — it is a distinct,
stronger signal that the input itself is too unreliable for a confident single
number, the same "never invent a confident number from an unreliable input"
principle already used elsewhere in this codebase (guardrails.py refusing to
invent a price the conversation doesn't justify; margin.py flagging
below-floor rather than silently proceeding). When reported, this module still
computes a real underlying estimate (using "heavy" as the closest known
modifier) but returns it as an indicative range with a recommendation for a
real site visit, never as a single confident figure.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from app.pricing import engine as pricing_engine
from app.pricing import sizing as sizing_module
from app.pricing.subsidy import calculate_pm_surya_ghar_subsidy

if TYPE_CHECKING:
    from app.memory.memory_manager import MemoryManager

# How wide the indicative range is around the computed point estimate when
# shading is reported as significant — a real site visit is what actually
# resolves this, not a wider guess, so this is deliberately a rough band, not
# a precision claim. [PLACEHOLDER — pending real Grid Nova site-survey data]
SIGNIFICANT_SHADING_RANGE_PERCENT = 15.0

SIGNIFICANT_SHADING_VALUE = "significant"

SIGNIFICANT_SHADING_NOTE = (
    "Shading at this site was reported as significant, so this is an "
    "indicative range rather than a firm number — a real site visit is "
    "needed to size and price this accurately."
)

# The public instant-estimate path always prices at the top tier — no longer a
# customer-supplied choice. engine.py's KNOWN_TIERS still has "good"/"better"
# too, for any staff-facing path that legitimately needs that flexibility.
FIXED_PUBLIC_TIER = "best"


@dataclass
class InstantEstimateInputs:
    segment: str
    state: str
    sizing_method: str
    monthly_units_kwh: Optional[float] = None
    monthly_bill_inr: Optional[float] = None
    usable_roof_area_sqft: Optional[float] = None
    connection_phase: Optional[str] = None
    roof_type: Optional[str] = None
    floors: Optional[int] = None
    shading: Optional[str] = None
    access_difficulty: Optional[str] = None
    is_remote: bool = False
    is_housing_society: bool = False
    # A specific size to price instead of sizing from consumption/roof area — used by
    # associate onboarding when the customer has asked for a particular kW. The public
    # /instant-estimate route never sets it, so its behaviour is unchanged.
    requested_size_kw: Optional[float] = None


@dataclass
class InstantEstimateResult:
    recommended_size_kw: float
    consumption_based_kw: Optional[float]
    area_based_kw: Optional[float]
    phase_capped: bool
    base_cost: float
    adders: dict[str, float]
    component_breakdown: dict[str, float]
    subtotal_before_tax: float
    gst_amount: float
    subsidy_inr: float
    net_cost: Optional[float]
    is_indicative_range: bool
    net_cost_range_min: Optional[float]
    net_cost_range_max: Optional[float]
    note: Optional[str]


def calculate_instant_estimate(
    memory: "MemoryManager", inputs: InstantEstimateInputs
) -> InstantEstimateResult:
    significant_shading = (
        (inputs.shading or "").strip().lower() == SIGNIFICANT_SHADING_VALUE
    )
    # engine.py doesn't recognize "significant" — "heavy" is the closest real
    # modifier it knows, used here only to produce the point estimate this
    # module then widens into a range. Never passed back out as-is.
    engine_shading = "heavy" if significant_shading else inputs.shading

    if inputs.requested_size_kw is not None:
        if inputs.requested_size_kw <= 0:
            raise ValueError("requested_size_kw must be greater than zero.")
        sizing_result = sizing_module.SizingResult(
            recommended_size_kw=inputs.requested_size_kw, consumption_based_kw=None,
            area_based_kw=None, phase_capped=False,
        )
    else:
        sizing_result = sizing_module.calculate_recommended_size(
            sizing_module.SizingInputs(
                sizing_method=inputs.sizing_method,
                state=inputs.state,
                monthly_units_kwh=inputs.monthly_units_kwh,
                monthly_bill_inr=inputs.monthly_bill_inr,
                usable_roof_area_sqft=inputs.usable_roof_area_sqft,
                connection_phase=inputs.connection_phase,
            )
        )
    size_kw = sizing_result.recommended_size_kw

    breakdown = pricing_engine.calculate_full_estimate(
        memory,
        pricing_engine.PricingInputs(
            segment=inputs.segment,
            size_kw=size_kw,
            state=inputs.state,
            roof_type=inputs.roof_type,
            floors=inputs.floors,
            shading=engine_shading,
            is_remote=inputs.is_remote,
        ),
    )

    tier_multiplier = pricing_engine.calculate_tier_multiplier(FIXED_PUBLIC_TIER)
    access_modifier = pricing_engine.calculate_access_difficulty_modifier(inputs.access_difficulty)

    # engine_subtotal: the engine's own full multi-layer subtotal (equipment,
    # installation incl. roof/floor/shading/location, balance-of-system,
    # civil, regulatory, optional services — everything engine.py already
    # computes), BEFORE the fixed best-tier multiplier. Never returned as its
    # own field — this path reports one real number, not "the price before
    # tier" alongside "the price at tier X". component_breakdown below shows
    # what already makes up base_cost — informational only, NOT to be added
    # again on top of it.
    engine_subtotal = breakdown.subtotal_before_tax
    base_cost = engine_subtotal * tier_multiplier
    access_difficulty_adder = round(base_cost * (access_modifier - 1.0), 2)
    subtotal_before_tax = base_cost + access_difficulty_adder

    gst_amount = pricing_engine.calculate_gst(subtotal_before_tax, inputs.state)
    subtotal_with_gst = subtotal_before_tax + gst_amount

    subsidy = calculate_pm_surya_ghar_subsidy(
        size_kw=size_kw, segment=inputs.segment, is_housing_society=inputs.is_housing_society,
    )
    point_net_cost = round(subtotal_with_gst - subsidy, 2)

    # The ONLY genuinely additional line item beyond base_cost/tier_cost — see
    # the note above about component_breakdown not being additive.
    adders = {"access_difficulty": access_difficulty_adder}
    component_breakdown = {
        "equipment": breakdown.equipment_cost,
        "installation": breakdown.installation_subtotal,
        "balance_of_system": breakdown.bos_addition,
        "civil": breakdown.civil_addition,
        "regulatory": breakdown.regulatory_addition,
    }

    if significant_shading:
        band = SIGNIFICANT_SHADING_RANGE_PERCENT / 100.0
        return InstantEstimateResult(
            recommended_size_kw=size_kw,
            consumption_based_kw=sizing_result.consumption_based_kw,
            area_based_kw=sizing_result.area_based_kw,
            phase_capped=sizing_result.phase_capped,
            base_cost=round(base_cost, 2),
            adders=adders,
            component_breakdown=component_breakdown,
            subtotal_before_tax=round(subtotal_before_tax, 2),
            gst_amount=round(gst_amount, 2),
            subsidy_inr=subsidy,
            net_cost=None,
            is_indicative_range=True,
            net_cost_range_min=round(point_net_cost * (1 - band), 2),
            net_cost_range_max=round(point_net_cost * (1 + band), 2),
            note=SIGNIFICANT_SHADING_NOTE,
        )

    return InstantEstimateResult(
        recommended_size_kw=size_kw,
        consumption_based_kw=sizing_result.consumption_based_kw,
        area_based_kw=sizing_result.area_based_kw,
        phase_capped=sizing_result.phase_capped,
        base_cost=round(base_cost, 2),
        adders=adders,
        component_breakdown=component_breakdown,
        subtotal_before_tax=round(subtotal_before_tax, 2),
        gst_amount=round(gst_amount, 2),
        subsidy_inr=subsidy,
        net_cost=point_net_cost,
        is_indicative_range=False,
        net_cost_range_min=None,
        net_cost_range_max=None,
        note=None,
    )
