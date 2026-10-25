"""
Instant estimate — the solar estimate engine (app/estimate/: size, generation, subsidy,
savings, payback) and Grid Nova's price list (app/pricing/price_list.py, applied by
app/pricing/list_pricing.py), in one result for the public /solar-calculator flow and the
associate's indicative estimate.

PRICES come only from the price list: both packages (Nova Assured, Nova Signature) for
the priced size. List prices are fixed — no shading, roof, floor, access or state
modifiers, and no price range. When the configuration is outside the price list
(another state, a non-residential segment, battery/off-grid, an unknown phase, a size
outside the table) there is NO price: `price_status` is "custom_quote" with reason
codes, while sizing, generation, subsidy and savings information still return.

SIZE: the calculated size (consumption- and/or roof-area-based, the smaller wins) is
exact (`recommended_kw_exact`); the priced size is the next whole kW within the phase's
range (`priced_kw`, policy `size_rounding: ceil`). When the whole kW below is also in the
range, it is returned too (`size_options`), each with its coverage, so the customer sees
both. A requested size (the associate's override) is never snapped: a size the price list
does not have raises UnsupportedSize when the configuration is otherwise list-priceable.
Nothing caps the size silently: above the customer's sanctioned load is a warning.

SUBSIDY: `net_cost` and every package's `net_after_subsidy` deduct CONFIRMED subsidies
only (the central CFA, residential or housing society). Announced state support is
returned separately (`announced_state_support`, `net_cost_if_announced_support_received`).

BACKWARD COMPATIBILITY: every pre-BE-14 field keeps its name and meaning. The top-level
cost fields (base_cost, subtotal_before_tax, gst_amount, net_cost) come from Nova Assured
when list-priced and are null for a custom quote. `recommended_size_kw` is the raw size
to the nearest 0.5 kW. `subsidy_inr` is the confirmed subsidy — null (never 0) for a
housing society whose number of houses is not known. `adders` and `component_breakdown`
are empty and `is_indicative_range` is always false.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING, Optional

from app.estimate import engine
from app.pricing import list_pricing, price_list
from app.pricing.list_pricing import UnsupportedSize  # noqa: F401 — re-exported for callers

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

KNOWN_SIZING_METHODS = ("by_units", "by_bill", "by_roof_area")


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
    # BE-14 (all optional)
    discom: Optional[str] = None
    tariff_category: Optional[str] = None
    sanctioned_load_kw: Optional[float] = None
    number_of_houses: Optional[int] = None
    module_wattage_wp: Optional[int] = None


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
    subsidy_inr: Optional[float]
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
    packages: dict = field(default_factory=dict)   # {NOVA_ASSURED: {basic, gst, final, net_after_subsidy, …}, …}
    gst_rule: dict = field(default_factory=dict)
    price_list_version: int = 0
    destination: str = ""
    subsidy_conditions: list = field(default_factory=list)
    # Solar estimate engine (BE-14) — JSON-ready (decimal strings)
    estimate_version: str = ""
    sizing_basis: Optional[str] = None
    consumption_basis: Optional[str] = None
    monthly_units_used: Optional[str] = None
    roof_limited: bool = False
    figures_for_kw: Optional[str] = None
    size_options: list = field(default_factory=list)
    generation: dict = field(default_factory=dict)
    coverage_percent: Optional[str] = None
    subsidy: dict = field(default_factory=dict)
    subsidy_status: Optional[str] = None
    announced_state_support: list = field(default_factory=list)
    net_cost_if_announced_support_received: Optional[str] = None
    savings: Optional[dict] = None
    payback_years: Optional[str] = None
    long_term: dict = field(default_factory=dict)
    assumptions: list = field(default_factory=list)
    confidence: str = "low"
    warnings: list = field(default_factory=list)
    warning_details: dict = field(default_factory=dict)


def _check_method(inputs: InstantEstimateInputs) -> None:
    method = (inputs.sizing_method or "").strip().lower()
    if method not in KNOWN_SIZING_METHODS:
        raise ValueError(f"Unknown sizing_method: {inputs.sizing_method!r}. Expected one of {KNOWN_SIZING_METHODS}.")
    if method == "by_units" and inputs.monthly_units_kwh is None:
        raise ValueError("monthly_units_kwh is required for sizing_method='by_units'.")
    if method == "by_bill" and inputs.monthly_bill_inr is None:
        raise ValueError("monthly_bill_inr is required for sizing_method='by_bill'.")
    if method == "by_roof_area" and inputs.usable_roof_area_sqft is None:
        raise ValueError("usable_roof_area_sqft is required for sizing_method='by_roof_area'.")


def engine_inputs(inputs: InstantEstimateInputs) -> engine.EstimateInputs:
    d = engine.to_decimal
    return engine.EstimateInputs(
        state=inputs.state, segment=inputs.segment, is_housing_society=inputs.is_housing_society,
        monthly_units_kwh=d(inputs.monthly_units_kwh), monthly_bill_inr=d(inputs.monthly_bill_inr),
        usable_roof_area_sqft=d(inputs.usable_roof_area_sqft), requested_kw=d(inputs.requested_size_kw),
        discom=inputs.discom, tariff_category=inputs.tariff_category,
        sanctioned_load_kw=d(inputs.sanctioned_load_kw), number_of_houses=inputs.number_of_houses,
        module_wattage_wp=inputs.module_wattage_wp,
        connection_type=(inputs.connection_type or "on_grid").strip().lower())


def package_amounts(lp: list_pricing.ListPricing, confirmed_subsidy: Decimal,
                    announced: Decimal = Decimal(0)) -> dict:
    """Both packages' list amounts (decimal strings) and the net after the CONFIRMED
    subsidy (and, separately, after announced state support too) — shared with the survey
    results (app/survey/results.py)."""
    return {package: {**q.amounts(), "display_name": price_list.display_name(package),
                      "net_after_subsidy": str(q.final - confirmed_subsidy),
                      "net_if_announced_support_received": str(q.final - confirmed_subsidy - announced)}
            for package, q in lp.packages.items()}


def _option(kw: int, role: str, phase: str, est: engine.EstimateInputs, cons: engine.Consumption) -> dict:
    """A whole-kW size the customer can compare: coverage, generation, subsidy, savings
    and payback, with both packages' prices when the price list has that size."""
    quotes = {p: price_list.quote(p, phase, kw) for p in price_list.PACKAGES} if phase else {}
    quotes = {p: q for p, q in quotes.items() if not isinstance(q, price_list.Unsupported)}
    assured = quotes.get("NOVA_ASSURED")
    fig = engine.financials(Decimal(kw), est, cons, assured.final if assured else None)
    sub = fig["subsidy"]
    return engine.public({
        "kw": kw, "role": role, "coverage_percent": fig["coverage_percent"],
        "generation": {"monthly_units": fig["generation"]["monthly_units"],
                       "annual_units": fig["generation"]["annual_units"]},
        "subsidy_confirmed_total": sub.confirmed_total, "announced_state_support_total": sub.announced_total,
        "packages": {p: {**q.amounts(), "net_after_subsidy": str(q.final - sub.confirmed_total)}
                     for p, q in quotes.items()},
        "net_cost": fig["net_cost"],
        "monthly_saving": fig["savings"]["monthly"] if fig["savings"] else None,
        "annual_saving": fig["savings"]["annual"] if fig["savings"] else None,
        "payback_years": fig["payback_years"],
    })


def _kw2(value: Optional[Decimal]) -> Optional[float]:
    return float(value.quantize(engine.KW_STEP, rounding=ROUND_HALF_UP)) if value is not None else None


def calculate_instant_estimate(memory: "MemoryManager", inputs: InstantEstimateInputs) -> InstantEstimateResult:
    from app.pricing.engine import KNOWN_SEGMENTS
    if (inputs.segment or "").strip().lower() not in KNOWN_SEGMENTS:
        raise ValueError(f"Unknown segment: {inputs.segment!r}. Expected one of {KNOWN_SEGMENTS}.")
    if inputs.requested_size_kw is not None:
        if inputs.requested_size_kw <= 0:
            raise ValueError("requested_size_kw must be greater than zero.")
    else:
        _check_method(inputs)
    est = engine_inputs(inputs)
    cons = engine.consumption(est)
    sizing = engine.size(est, cons)

    lp = list_pricing.evaluate(
        state=inputs.state, segment=inputs.segment, is_housing_society=inputs.is_housing_society,
        connection_type=inputs.connection_type, battery_storage=inputs.battery_storage,
        connection_phase=inputs.connection_phase,
        exact_kw=None if inputs.requested_size_kw is not None else float(sizing.exact_kw),
        requested_kw=inputs.requested_size_kw)

    assured = lp.packages.get("NOVA_ASSURED")
    system_kw = Decimal(lp.priced_kw) if lp.priced_kw is not None else sizing.exact_kw
    fig = engine.financials(system_kw, est, cons, assured.final if assured else None)
    sub = fig["subsidy"]
    known = sub.status != "needs_number_of_houses"
    confirmed = sub.confirmed_total

    options = []
    if lp.priced_kw is not None and inputs.requested_size_kw is None:
        options.append(_option(lp.priced_kw, "priced", lp.phase, est, cons))
        _ceil_kw, floor_kw = engine.whole_kw(sizing.exact_kw)
        min_kw = price_list.phase_range(lp.phase)["min_kw"]
        if floor_kw != lp.priced_kw and floor_kw >= max(min_kw, 1):
            options.append(_option(floor_kw, "floor", lp.phase, est, cons))

    codes, details = engine.warnings_public(cons.warnings + engine.size_warnings(est, sizing, lp.priced_kw))
    significant = (inputs.shading or "").strip().lower() == SIGNIFICANT_SHADING_VALUE
    savings = fig["savings"]
    return InstantEstimateResult(
        recommended_size_kw=float(sizing.legacy_half_kw),
        consumption_based_kw=_kw2(sizing.consumption_kw),
        area_based_kw=_kw2(sizing.area_kw),
        phase_capped=False,
        base_cost=float(assured.basic) if assured else None,
        adders={},
        component_breakdown={},
        subtotal_before_tax=float(assured.basic) if assured else None,
        gst_amount=float(assured.gst) if assured else None,
        subsidy_inr=float(confirmed) if known else None,
        net_cost=float(assured.final - confirmed) if assured else None,
        is_indicative_range=False,
        net_cost_range_min=None,
        net_cost_range_max=None,
        note=SIGNIFICANT_SHADING_NOTE if significant else None,
        price_status=lp.price_status,
        custom_quote_reasons=list(lp.custom_quote_reasons),
        recommended_kw_exact=float(sizing.exact_kw),
        priced_kw=lp.priced_kw,
        priced_kw_differs=lp.priced_kw_differs,
        phase=lp.phase,
        packages=package_amounts(lp, confirmed, sub.announced_total),
        gst_rule=price_list.gst_rule(),
        price_list_version=price_list.version(),
        destination=price_list.destination(),
        subsidy_conditions=list(sub.conditions) if confirmed > 0 else [],
        estimate_version=engine.estimate_version(),
        sizing_basis=sizing.sizing_basis,
        consumption_basis=cons.basis,
        monthly_units_used=str(cons.monthly_units) if cons.monthly_units is not None else None,
        roof_limited=sizing.roof_limited,
        figures_for_kw=str(system_kw),
        size_options=options,
        generation=engine.public(fig["generation"]),
        coverage_percent=engine.public(fig["coverage_percent"]),
        subsidy=sub.public(),
        subsidy_status=sub.status,
        announced_state_support=engine.public(fig["announced_state_support"]),
        net_cost_if_announced_support_received=engine.public(fig["net_cost_if_announced_support_received"]),
        savings=engine.public(savings),
        payback_years=engine.public(fig["payback_years"]),
        long_term=engine.public(fig["long_term"]),
        assumptions=engine.assumptions(est, cons, sizing),
        confidence=engine.confidence(cons, inputs.state),
        warnings=codes,
        warning_details=details,
    )


# The pre-BE-14 response keys, in order; then the engine's.
_KEYS = ("base_cost", "adders", "component_breakdown", "subtotal_before_tax", "gst_amount", "subsidy_inr",
         "net_cost", "is_indicative_range", "net_cost_range_min", "net_cost_range_max", "note", "price_status",
         "custom_quote_reasons", "recommended_kw_exact", "priced_kw", "priced_kw_differs", "phase", "packages",
         "gst_rule", "price_list_version", "destination", "subsidy_conditions",
         "estimate_version", "sizing_basis", "consumption_basis", "monthly_units_used", "roof_limited",
         "figures_for_kw", "size_options", "generation", "coverage_percent", "subsidy", "subsidy_status",
         "announced_state_support", "net_cost_if_announced_support_received", "savings", "payback_years",
         "long_term", "assumptions", "confidence", "warnings", "warning_details")


def response(result: InstantEstimateResult) -> dict:
    """The API shape (public /instant-estimate and associate estimates)."""
    return {
        "recommended_size_kw": result.recommended_size_kw,
        "sizing_detail": {"consumption_based_kw": result.consumption_based_kw,
                          "area_based_kw": result.area_based_kw, "phase_capped": result.phase_capped},
        **{key: getattr(result, key) for key in _KEYS},
    }
