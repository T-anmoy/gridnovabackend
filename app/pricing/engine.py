"""
Pricing Engine — the real multi-layer calculation framework.

Separate, additive, and independent from app/proposals/content_engine.py's
flat rate_per_kw pricing (config/proposal_content.yaml), which the Proposal
Agent's document-drafting logic still uses unchanged. This module is not
wired into that path in this session — it exists behind its own API
(app/api/routes/pricing.py) for the frontend configurator and, eventually,
internal use.

EVERY NUMBER IN THIS FILE IS A PLACEHOLDER, pending real business input from
Raju Paul / Grid Nova ops. Every one is a named constant, defined here, with a
comment saying so — never a magic number inside a function body. See the
final session report for the master list.

SECURITY: Equipment.procurement_price_inr (Admin-only, see app/api/routes/
equipment.py) must never appear in this module's output, directly or by a
calculation an attacker could invert. calculate_equipment_cost() is the ONLY
function that ever reads procurement_price_inr, and it returns a SELLING
price (procurement × a markup) — the cost figure itself never leaves that
one function. See PLACEHOLDER_EQUIPMENT_MARKUP_PERCENT below.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from app.memory.memory_manager import MemoryManager


# ── Placeholder constants — every one pending real business input ──────────────

# Base ₹/kW by segment and size band. [PLACEHOLDER — real rates pending Raju Paul]
# Size bands match content_engine.py's existing rooftop tier boundaries (0-10,
# 10-100, 100+) for at least superficial consistency, even though this is a
# separate system.
PLACEHOLDER_BASE_RATE_PER_KW: dict[str, dict[str, float]] = {
    "residential": {"small": 55_000.0, "medium": 50_000.0, "large": 46_000.0},
    "commercial": {"small": 50_000.0, "medium": 46_000.0, "large": 42_000.0},
    "industrial": {"small": 48_000.0, "medium": 44_000.0, "large": 40_000.0},
    "government": {"small": 47_000.0, "medium": 43_000.0, "large": 39_000.0},
}
# Segments known to this module — anything else is a caller error (422 at the
# route layer), not silently defaulted, unlike the free-text fields below.
KNOWN_SEGMENTS = tuple(PLACEHOLDER_BASE_RATE_PER_KW.keys())

# Economies-of-scale multiplier applied on TOP of the base rate, by size band.
# [PLACEHOLDER — pending real cost-engineering input]
PLACEHOLDER_SIZE_BAND_MODIFIER: dict[str, float] = {
    "small": 1.00,
    "medium": 0.97,
    "large": 0.94,
}

# Selling price = procurement cost × (1 + this / 100). No margin figure exists
# from Raju Paul yet, so this is the placeholder markup used to derive a safe,
# public-facing selling price from Equipment.procurement_price_inr — the ONLY
# place that field is read. [PLACEHOLDER — real markup pending Raju Paul]
PLACEHOLDER_EQUIPMENT_MARKUP_PERCENT = 20.0

# Roof-type installation-complexity multiplier. [PLACEHOLDER]
PLACEHOLDER_ROOF_TYPE_MODIFIER: dict[str, float] = {
    "rcc": 1.00,
    "tin": 1.05,
    "metal": 1.05,
    "asbestos": 1.08,
}
PLACEHOLDER_DEFAULT_ROOF_TYPE_MODIFIER = 1.03  # unknown/unlisted roof type

# Added multiplier per floor beyond the first (more floors = harder cable
# run / staging), capped so a data-entry typo can't blow up the estimate.
# [PLACEHOLDER]
PLACEHOLDER_PER_EXTRA_FLOOR_MODIFIER = 0.02
PLACEHOLDER_MAX_EXTRA_FLOORS_COUNTED = 5

# Shading-condition multiplier — more shading mitigation work. [PLACEHOLDER]
PLACEHOLDER_SHADING_MODIFIER: dict[str, float] = {
    "none": 1.00,
    "partial_morning": 1.03,
    "partial_evening": 1.03,
    "heavy": 1.07,
}
PLACEHOLDER_DEFAULT_SHADING_MODIFIER = 1.00  # unspecified

# Location multiplier by state — primary service states get no premium; a
# state outside West Bengal/Bihar carries a logistics premium. See
# app/memory/reference_id.py for the same WB/BR-primary convention.
# [PLACEHOLDER — pending real logistics-cost input]
PLACEHOLDER_STATE_MODIFIER: dict[str, float] = {
    "west bengal": 1.00,
    "bihar": 1.00,
}
PLACEHOLDER_DEFAULT_STATE_MODIFIER = 1.05  # any state outside the primary two
PLACEHOLDER_REMOTE_SITE_MODIFIER = 1.03  # additional multiplier when is_remote

# Balance-of-system (cables, connectors, earthing, etc.) — flat ₹/kW addition.
# [PLACEHOLDER]
PLACEHOLDER_BOS_RATE_PER_KW = 3_000.0

# Civil work addition by site type (site prep, foundations). [PLACEHOLDER]
PLACEHOLDER_CIVIL_ADDITION_BY_SITE_TYPE: dict[str, float] = {
    "rooftop_residential": 8_000.0,
    "rooftop_commercial": 15_000.0,
    "rooftop_institutional": 20_000.0,
    "ground_mounted": 60_000.0,
    "carport": 35_000.0,
}
PLACEHOLDER_DEFAULT_CIVIL_ADDITION = 10_000.0  # unknown/unlisted site type

# Regulatory/liaison flat addition — statutory filings, DISCOM liaison.
# [PLACEHOLDER]
PLACEHOLDER_REGULATORY_BASE_BY_STATE: dict[str, float] = {
    "west bengal": 5_000.0,
    "bihar": 6_000.0,
}
PLACEHOLDER_DEFAULT_REGULATORY_BASE = 8_000.0  # any state outside the primary two
PLACEHOLDER_NET_METERING_ADDITION = 4_000.0  # additional filing when required

# Optional service flat prices. [PLACEHOLDER — real service catalog pending]
PLACEHOLDER_OPTIONAL_SERVICE_PRICES: dict[str, float] = {
    "extended_warranty": 15_000.0,
    "annual_maintenance_contract": 8_000.0,
    "monitoring_app_premium": 5_000.0,
}

# Equipment-quality tier multiplier for the public instant-estimate flow (Good/
# Better/Best) — applied on top of the fully-computed subtotal, same pattern as
# every other modifier in this module. [PLACEHOLDER — real tier economics
# pending Raju Paul]
PLACEHOLDER_TIER_MULTIPLIER: dict[str, float] = {
    "good": 1.00,
    "better": 1.10,
    "best": 1.25,
}
KNOWN_TIERS = tuple(PLACEHOLDER_TIER_MULTIPLIER.keys())

# Site-access-difficulty multiplier — separate from roof TYPE (already covered
# by PLACEHOLDER_ROOF_TYPE_MODIFIER above): this is about reaching the site at
# all (stairs-only access, tight lane, no crane access), not what the roof is
# made of. [PLACEHOLDER]
PLACEHOLDER_ACCESS_DIFFICULTY_MODIFIER: dict[str, float] = {
    "easy": 1.00,
    "moderate": 1.05,
    "difficult": 1.12,
}
PLACEHOLDER_DEFAULT_ACCESS_DIFFICULTY_MODIFIER = 1.00  # unspecified

# Blended, single GST rate applied to the whole subtotal. Real GST treatment
# for a solar EPC contract is genuinely nuanced (modules vs. balance-of-system
# vs. installation service can attract different rates, and intra-state vs.
# inter-state changes the CGST/SGST/IGST split, not the total). `state` is
# accepted so a future real implementation can vary by it — the placeholder
# does not yet. [PLACEHOLDER — real GST treatment pending Raju Paul / counsel]
PLACEHOLDER_GST_RATE_PERCENT = 13.8


# BE-14: sizing, generation, the bill-to-units tariff and the subsidy live in the solar
# estimate engine (app/estimate/, config/solar_estimate.yaml + config/subsidies.yaml).
# The ₹9/unit and 4 units/kWp/day (1,440 a year, not 4 × 365) are read from there; this
# layered placeholder engine only prices the staff-only /pricing/breakdown.


def _size_band(size_kw: float) -> str:
    if size_kw < 10:
        return "small"
    if size_kw < 100:
        return "medium"
    return "large"


def _normalize_key(value: Optional[str]) -> str:
    return (value or "").strip().lower()


# ── Layer 1: base rate ──────────────────────────────────────────────────────────

def calculate_base_rate(segment: str, size_kw: float) -> float:
    """₹/kW base rate for this segment and size band. Raises ValueError for an
    unrecognized segment — segment is a closed, validated vocabulary (see
    app.memory.models.CustomerType / the `segment` field elsewhere in this
    codebase), unlike the free-text fields below, so a bad value here is a
    caller bug, not something to silently default."""
    segment_key = _normalize_key(segment)
    if segment_key not in PLACEHOLDER_BASE_RATE_PER_KW:
        raise ValueError(f"Unknown segment: {segment!r}. Expected one of {KNOWN_SEGMENTS}.")
    return PLACEHOLDER_BASE_RATE_PER_KW[segment_key][_size_band(size_kw)]


# ── Layer 2: size-band modifier ──────────────────────────────────────────────────

def calculate_size_band_modifier(size_kw: float) -> float:
    return PLACEHOLDER_SIZE_BAND_MODIFIER[_size_band(size_kw)]


# ── Layer 3: equipment cost (selling price only — see module docstring) ────────

def calculate_equipment_cost(memory: "MemoryManager", equipment_selection: list[int]) -> float:
    """Sum the SELLING price of every selected, active Equipment item.

    procurement_price_inr is read here and ONLY here in this whole module —
    it is converted to a selling price via PLACEHOLDER_EQUIPMENT_MARKUP_PERCENT
    before it is added to the total. The raw procurement figure is never
    returned, logged, or included in anything this function hands back.
    Unknown or deactivated equipment ids are skipped, not an error — a stale
    id in a client-held selection shouldn't break an estimate.
    """
    total = 0.0
    for equipment_id in equipment_selection:
        item = memory.get_equipment(equipment_id)
        if item is None or not item.is_active:
            continue
        if item.procurement_price_inr is None:
            continue
        selling_price = item.procurement_price_inr * (1 + PLACEHOLDER_EQUIPMENT_MARKUP_PERCENT / 100.0)
        total += selling_price
    return total


# ── Layer 4: complexity modifier ────────────────────────────────────────────────

def calculate_complexity_modifier(
    roof_type: Optional[str], floors: Optional[int], shading: Optional[str],
) -> float:
    roof_factor = PLACEHOLDER_ROOF_TYPE_MODIFIER.get(
        _normalize_key(roof_type), PLACEHOLDER_DEFAULT_ROOF_TYPE_MODIFIER
    )
    extra_floors = max(0, (floors or 1) - 1)
    extra_floors = min(extra_floors, PLACEHOLDER_MAX_EXTRA_FLOORS_COUNTED)
    floor_factor = 1.0 + extra_floors * PLACEHOLDER_PER_EXTRA_FLOOR_MODIFIER
    shading_factor = PLACEHOLDER_SHADING_MODIFIER.get(
        _normalize_key(shading), PLACEHOLDER_DEFAULT_SHADING_MODIFIER
    )
    return roof_factor * floor_factor * shading_factor


# ── Layer 5: location modifier ──────────────────────────────────────────────────

def calculate_location_modifier(state: str, is_remote: bool = False) -> float:
    state_factor = PLACEHOLDER_STATE_MODIFIER.get(
        _normalize_key(state), PLACEHOLDER_DEFAULT_STATE_MODIFIER
    )
    remote_factor = PLACEHOLDER_REMOTE_SITE_MODIFIER if is_remote else 1.0
    return state_factor * remote_factor


# ── Layer 6: balance-of-system ──────────────────────────────────────────────────

def calculate_bos_addition(size_kw: float) -> float:
    return PLACEHOLDER_BOS_RATE_PER_KW * size_kw


# ── Layer 7: civil work ──────────────────────────────────────────────────────────

def calculate_civil_addition(site_type: Optional[str]) -> float:
    return PLACEHOLDER_CIVIL_ADDITION_BY_SITE_TYPE.get(
        _normalize_key(site_type), PLACEHOLDER_DEFAULT_CIVIL_ADDITION
    )


# ── Layer 8: regulatory / liaison ────────────────────────────────────────────────

def calculate_regulatory_addition(state: str, requires_net_metering: bool = False) -> float:
    base = PLACEHOLDER_REGULATORY_BASE_BY_STATE.get(
        _normalize_key(state), PLACEHOLDER_DEFAULT_REGULATORY_BASE
    )
    return base + (PLACEHOLDER_NET_METERING_ADDITION if requires_net_metering else 0.0)


# ── Layer 9: optional services ──────────────────────────────────────────────────

def calculate_optional_services(selected_services: list[str]) -> float:
    return sum(
        PLACEHOLDER_OPTIONAL_SERVICE_PRICES.get(_normalize_key(s), 0.0)
        for s in selected_services
    )


# ── Layer 10: GST ────────────────────────────────────────────────────────────────

def calculate_gst(subtotal: float, state: str) -> float:
    # `state` accepted for interface stability — the placeholder rate does not
    # yet vary by it. See PLACEHOLDER_GST_RATE_PERCENT above.
    return subtotal * (PLACEHOLDER_GST_RATE_PERCENT / 100.0)


# ── Layer 11: tier multiplier (public instant-estimate flow only) ──────────────

def calculate_tier_multiplier(tier: str) -> float:
    """Raises ValueError for an unrecognized tier — like segment, this is a
    closed, validated vocabulary a caller controls (a UI toggle), not free text."""
    tier_key = _normalize_key(tier)
    if tier_key not in PLACEHOLDER_TIER_MULTIPLIER:
        raise ValueError(f"Unknown tier: {tier!r}. Expected one of {KNOWN_TIERS}.")
    return PLACEHOLDER_TIER_MULTIPLIER[tier_key]


# ── Layer 12: access difficulty (public instant-estimate flow only) ────────────

def calculate_access_difficulty_modifier(access_difficulty: Optional[str]) -> float:
    return PLACEHOLDER_ACCESS_DIFFICULTY_MODIFIER.get(
        _normalize_key(access_difficulty), PLACEHOLDER_DEFAULT_ACCESS_DIFFICULTY_MODIFIER
    )


# ── Combined estimate ────────────────────────────────────────────────────────────

@dataclass
class PricingInputs:
    segment: str
    size_kw: float
    state: str
    roof_type: Optional[str] = None
    floors: Optional[int] = None
    shading: Optional[str] = None
    site_type: Optional[str] = None
    is_remote: bool = False
    requires_net_metering: bool = False
    equipment_selection: list[int] = field(default_factory=list)
    selected_services: list[str] = field(default_factory=list)


@dataclass
class PricingBreakdown:
    """Every layer, for internal/staff use. See category_totals for the
    reduced, public-safe view app/api/routes/pricing.py's public endpoint
    actually returns — never this dataclass directly."""
    base_rate_per_kw: float
    size_band_modifier: float
    base_installation_subtotal: float  # base_rate * size_kw * size_band_modifier
    equipment_cost: float              # SELLING price only — see module docstring
    complexity_modifier: float
    location_modifier: float
    installation_subtotal: float       # base_installation_subtotal * complexity * location
    bos_addition: float
    civil_addition: float
    regulatory_addition: float
    optional_services_total: float
    subtotal_before_tax: float
    gst_rate_percent: float
    gst_amount: float
    total: float
    category_totals: dict[str, float]


def calculate_full_estimate(memory: "MemoryManager", inputs: PricingInputs) -> PricingBreakdown:
    """Combine every layer into one breakdown. Labor/design cost (the base
    rate) scales with installation complexity and location; equipment,
    balance-of-system, civil, regulatory and optional-service costs are
    additive line items that don't scale with those modifiers — a panel
    doesn't cost more because the roof is hard to access, the labor to
    install it does.
    """
    base_rate = calculate_base_rate(inputs.segment, inputs.size_kw)
    size_band_modifier = calculate_size_band_modifier(inputs.size_kw)
    base_installation_subtotal = base_rate * inputs.size_kw * size_band_modifier

    complexity_modifier = calculate_complexity_modifier(inputs.roof_type, inputs.floors, inputs.shading)
    location_modifier = calculate_location_modifier(inputs.state, inputs.is_remote)
    installation_subtotal = base_installation_subtotal * complexity_modifier * location_modifier

    equipment_cost = calculate_equipment_cost(memory, inputs.equipment_selection)
    bos_addition = calculate_bos_addition(inputs.size_kw)
    civil_addition = calculate_civil_addition(inputs.site_type)
    regulatory_addition = calculate_regulatory_addition(inputs.state, inputs.requires_net_metering)
    optional_services_total = calculate_optional_services(inputs.selected_services)

    subtotal_before_tax = (
        installation_subtotal + equipment_cost + bos_addition
        + civil_addition + regulatory_addition + optional_services_total
    )
    gst_amount = calculate_gst(subtotal_before_tax, inputs.state)
    total = subtotal_before_tax + gst_amount

    category_totals = {
        "equipment": round(equipment_cost + bos_addition, 2),
        "installation": round(installation_subtotal + civil_addition, 2),
        "other": round(regulatory_addition + optional_services_total + gst_amount, 2),
    }

    return PricingBreakdown(
        base_rate_per_kw=base_rate,
        size_band_modifier=size_band_modifier,
        base_installation_subtotal=round(base_installation_subtotal, 2),
        equipment_cost=round(equipment_cost, 2),
        complexity_modifier=complexity_modifier,
        location_modifier=location_modifier,
        installation_subtotal=round(installation_subtotal, 2),
        bos_addition=round(bos_addition, 2),
        civil_addition=round(civil_addition, 2),
        regulatory_addition=round(regulatory_addition, 2),
        optional_services_total=round(optional_services_total, 2),
        subtotal_before_tax=round(subtotal_before_tax, 2),
        gst_rate_percent=PLACEHOLDER_GST_RATE_PERCENT,
        gst_amount=round(gst_amount, 2),
        total=round(total, 2),
        category_totals=category_totals,
    )
