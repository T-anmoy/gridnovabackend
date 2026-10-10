"""
Instant estimate — sizing (app/pricing/sizing.py), Grid Nova's price list
(app/pricing/price_list.py, applied by app/pricing/list_pricing.py) and the PM Surya
Ghar subsidy (app/pricing/subsidy.py), in one result for the public /solar-calculator
flow and the associate's indicative estimate.

PRICES come only from the price list: both packages (Nova Assured, Nova Signature) for
the priced size. List prices are fixed — no shading, roof, floor, access or state
modifiers, and no price range. When the configuration is outside the price list
(another state, a non-residential segment, battery/off-grid, an unknown phase, a size
outside the table) there is NO price: `price_status` is "custom_quote" with reason
codes, while sizing, generation and subsidy information still return.

The layered placeholder engine (app/pricing/engine.py, PRC-1…7) is no longer used for
any customer-facing price; it is kept for the staff-only /pricing/breakdown.

SIZE: the calculated size (consumption- and/or roof-area-based, the smaller wins) is
exact (`recommended_kw_exact`); the priced size is the next whole kW within the phase's
range (`priced_kw`, policy `size_rounding: ceil`). The old 5 kW single-phase cap
(PRC-12) is replaced, for pricing, by the price list's ranges. A requested size (the
associate's override) is never snapped: a size the price list does not have raises
UnsupportedSize when the configuration is otherwise list-priceable.

BACKWARD COMPATIBILITY: the earlier top-level cost fields (base_cost,
subtotal_before_tax, gst_amount, net_cost) are populated from Nova Assured when
list-priced, and are null for a custom quote. `adders` and `component_breakdown` are
empty (a list price has no adders or components), `is_indicative_range` is always false.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Optional

from app.pricing import list_pricing, price_list
from app.pricing import sizing as sizing_module
from app.pricing.list_pricing import UnsupportedSize  # noqa: F401 — re-exported for callers
from app.pricing.subsidy import calculate_pm_surya_ghar_subsidy

if TYPE_CHECKING:
    from app.memory.memory_manager import MemoryManager

# Superseded for list-priced paths (registry PRC-13): list prices are fixed, so a
# significantly shaded site no longer gets a price range — it gets the list price and
# the note below. Kept so the registry entry can still point at it.
SIGNIFICANT_SHADING_RANGE_PERCENT = 15.0

SIGNIFICANT_SHADING_VALUE = "significant"

SIGNIFICANT_SHADING_NOTE = (
    "Shading at this site was reported as significant — a site visit is needed to "
    "confirm the system size before Grid Nova prepares a formal proposal."
)

# PM Surya Ghar needs DCR modules and the customer's own scheme eligibility; DCR status
# per make is not in Grid Nova's master data, so the subsidy is shown with conditions.
SUBSIDY_CONDITIONS = ["dcr_modules_required", "scheme_eligibility"]


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
    # associate onboarding when the customer has asked for a particular kW.
    requested_size_kw: Optional[float] = None
    # Grid-connected without battery unless said otherwise.
    connection_type: Optional[str] = None
    battery_storage: bool = False


@dataclass
class InstantEstimateResult:
    recommended_size_kw: float
    consumption_based_kw: Optional[float]
    area_based_kw: Optional[float]
    phase_capped: bool
    base_cost: Optional[float]
    adders: dict
    component_breakdown: dict
    subtotal_before_tax: Optional[float]
    gst_amount: Optional[float]
    subsidy_inr: float
    net_cost: Optional[float]
    is_indicative_range: bool
    net_cost_range_min: Optional[float]
    net_cost_range_max: Optional[float]
    note: Optional[str]
    # Price list (PM-A)
    price_status: str = "custom_quote"
    custom_quote_reasons: list = field(default_factory=list)
    recommended_kw_exact: Optional[float] = None
    priced_kw: Optional[int] = None
    priced_kw_differs: bool = False
    phase: Optional[str] = None
    packages: dict = field(default_factory=dict)   # {NOVA_ASSURED: {basic, gst, final, net_after_subsidy}, …}
    gst_rule: dict = field(default_factory=dict)
    price_list_version: int = 0
    destination: str = ""
    subsidy_conditions: list = field(default_factory=list)


def _size(inputs: InstantEstimateInputs) -> sizing_module.SizingResult:
    if inputs.requested_size_kw is not None:
        if inputs.requested_size_kw <= 0:
            raise ValueError("requested_size_kw must be greater than zero.")
        return sizing_module.SizingResult(recommended_size_kw=inputs.requested_size_kw, consumption_based_kw=None,
                                          area_based_kw=None, phase_capped=False)
    # No phase cap: the price list's phase ranges decide what can be priced.
    return sizing_module.calculate_recommended_size(sizing_module.SizingInputs(
        sizing_method=inputs.sizing_method, state=inputs.state, monthly_units_kwh=inputs.monthly_units_kwh,
        monthly_bill_inr=inputs.monthly_bill_inr, usable_roof_area_sqft=inputs.usable_roof_area_sqft,
        connection_phase=None))


def package_amounts(lp: list_pricing.ListPricing, subsidy: float) -> dict:
    """Both packages' list amounts (decimal strings) and the net after the subsidy —
    shared with the survey results (app/survey/results.py)."""
    return {package: {**q.amounts(), "display_name": price_list.display_name(package),
                      "net_after_subsidy": str(q.final - Decimal(str(subsidy)))}
            for package, q in lp.packages.items()}


def calculate_instant_estimate(memory: "MemoryManager", inputs: InstantEstimateInputs) -> InstantEstimateResult:
    from app.pricing.engine import KNOWN_SEGMENTS
    if (inputs.segment or "").strip().lower() not in KNOWN_SEGMENTS:
        raise ValueError(f"Unknown segment: {inputs.segment!r}. Expected one of {KNOWN_SEGMENTS}.")
    sizing_result = _size(inputs)
    if inputs.requested_size_kw is not None:
        exact = float(inputs.requested_size_kw)
    else:
        candidates = [v for v in (sizing_result.consumption_based_kw, sizing_result.area_based_kw) if v is not None]
        exact = min(candidates)

    lp = list_pricing.evaluate(
        state=inputs.state, segment=inputs.segment, is_housing_society=inputs.is_housing_society,
        connection_type=inputs.connection_type, battery_storage=inputs.battery_storage,
        connection_phase=inputs.connection_phase,
        exact_kw=None if inputs.requested_size_kw is not None else exact,
        requested_kw=inputs.requested_size_kw)

    subsidy_kw = lp.priced_kw if lp.priced_kw is not None else sizing_result.recommended_size_kw
    subsidy = calculate_pm_surya_ghar_subsidy(size_kw=subsidy_kw, segment=inputs.segment,
                                              is_housing_society=inputs.is_housing_society)
    packages = package_amounts(lp, subsidy)
    assured = lp.packages.get("NOVA_ASSURED")
    significant = (inputs.shading or "").strip().lower() == SIGNIFICANT_SHADING_VALUE

    return InstantEstimateResult(
        recommended_size_kw=sizing_result.recommended_size_kw,
        consumption_based_kw=sizing_result.consumption_based_kw,
        area_based_kw=sizing_result.area_based_kw,
        phase_capped=False,
        base_cost=float(assured.basic) if assured else None,
        adders={},
        component_breakdown={},
        subtotal_before_tax=float(assured.basic) if assured else None,
        gst_amount=float(assured.gst) if assured else None,
        subsidy_inr=subsidy,
        net_cost=float(assured.final - Decimal(str(subsidy))) if assured else None,
        is_indicative_range=False,
        net_cost_range_min=None,
        net_cost_range_max=None,
        note=SIGNIFICANT_SHADING_NOTE if significant else None,
        price_status=lp.price_status,
        custom_quote_reasons=list(lp.custom_quote_reasons),
        recommended_kw_exact=round(exact, 2),
        priced_kw=lp.priced_kw,
        priced_kw_differs=lp.priced_kw_differs,
        phase=lp.phase,
        packages=packages,
        gst_rule=price_list.gst_rule(),
        price_list_version=price_list.version(),
        destination=price_list.destination(),
        subsidy_conditions=list(SUBSIDY_CONDITIONS) if subsidy else [],
    )


def response(result: InstantEstimateResult) -> dict:
    """The API shape (public /instant-estimate and associate estimates)."""
    return {
        "recommended_size_kw": result.recommended_size_kw,
        "sizing_detail": {"consumption_based_kw": result.consumption_based_kw,
                          "area_based_kw": result.area_based_kw, "phase_capped": result.phase_capped},
        "base_cost": result.base_cost,
        "adders": result.adders,
        "component_breakdown": result.component_breakdown,
        "subtotal_before_tax": result.subtotal_before_tax,
        "gst_amount": result.gst_amount,
        "subsidy_inr": result.subsidy_inr,
        "net_cost": result.net_cost,
        "is_indicative_range": result.is_indicative_range,
        "net_cost_range_min": result.net_cost_range_min,
        "net_cost_range_max": result.net_cost_range_max,
        "note": result.note,
        "price_status": result.price_status,
        "custom_quote_reasons": result.custom_quote_reasons,
        "recommended_kw_exact": result.recommended_kw_exact,
        "priced_kw": result.priced_kw,
        "priced_kw_differs": result.priced_kw_differs,
        "phase": result.phase,
        "packages": result.packages,
        "gst_rule": result.gst_rule,
        "price_list_version": result.price_list_version,
        "destination": result.destination,
        "subsidy_conditions": result.subsidy_conditions,
    }
