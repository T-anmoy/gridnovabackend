"""
Solar system sizing — independent of app/pricing/engine.py's cost calculation,
but feeds it a size_kw. The ONE bill-to-size rule: the Solar Calculator, onboarding
estimate, survey results and (BE-7 N6) /api/v1/pricing/estimate and /breakdown all
size a monthly bill through estimate_size_from_bill() here.

Three sizing methods, matching the frontend's expanded /solar-calculator flow:
  - by_units:      customer knows their monthly consumption directly (kWh).
  - by_bill:       customer only knows their monthly bill (INR) — converted to
                    units via engine.py's own tariff assumption (imported, not
                    duplicated, so the two can never quietly drift apart).
  - by_roof_area:  customer only knows usable roof area (sq ft).

When both a consumption estimate and a roof-area estimate are available, the
smaller wins — a roof physically cannot host more capacity than it has room
for, regardless of what consumption alone would suggest.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.pricing.engine import PLACEHOLDER_ASSUMED_TARIFF_INR_PER_UNIT

# ── Placeholder constants — every one pending real business/DISCOM input ────────

# kWh generated per kW of installed capacity, per day. Varies by region (solar
# irradiance differs across states) — this is a DIFFERENT, more granular figure
# than engine.py's PLACEHOLDER_ASSUMED_ANNUAL_YIELD_PER_KW (a single flat annual
# national assumption that fed the older bill->kW helper, removed in BE-7). Kept as
# its own constant rather than silently repurposing that one for a subtly
# different (daily, state-varying) meaning. Grid Nova (Raju Paul, 15 Oct 2026):
# 4 units per kW per day on average, for West Bengal, Bihar and the default.
PLACEHOLDER_REGIONAL_GENERATION_FACTOR_KWH_PER_KW_PER_DAY: dict[str, float] = {
    "west bengal": 4.0,
    "bihar": 4.0,
}
PLACEHOLDER_DEFAULT_GENERATION_FACTOR = 4.0  # any state outside the primary two

# Fraction of monthly consumption the recommended system targets covering — a
# system sized for less than 100% offset avoids over-generation/net-metering
# complications from a small oversizing error. [PLACEHOLDER — pending real
# Grid Nova sizing-policy confirmation]
DEFAULT_TARGET_OFFSET_PERCENT = 90.0

# PM Surya Ghar Yojana (MNRE) published eligibility guidance: approx. 10 sq. m
# (~100 sq ft) of usable, shadow-free roof area per kW of installed capacity.
# Matches the frontend's own PLACEHOLDER_ROOF_AREA_SQFT_PER_KW (app/solar-
# calculator/lib.ts) so both sides of one calculation agree on this figure.
ROOF_AREA_SQFT_PER_KW = 100.0

# Per-DISCOM sanctioned-load caps are genuinely unconfirmed — this is a
# conservative, commonly-cited default, not a real per-DISCOM table.
# [PLACEHOLDER — real per-DISCOM caps pending Raju Paul]
PLACEHOLDER_PHASE_CAP_KW: dict[str, float] = {
    "single_phase": 5.0,
    "three_phase": 10.0,
}
# Unknown/unstated phase: left uncapped rather than defensively applying the
# single-phase cap — we would otherwise silently under-recommend for a
# three-phase customer who simply didn't know or state their phase.

RESIDENTIAL_ROUNDING_KW = 0.5

DAYS_PER_MONTH = 30.0


def _normalize_key(value: Optional[str]) -> str:
    return (value or "").strip().lower()


def estimate_size_from_units(
    monthly_units_kwh: float,
    state: str,
    target_offset_percent: float = DEFAULT_TARGET_OFFSET_PERCENT,
) -> float:
    """monthly units / (30 * regional generation factor) * target offset."""
    factor = PLACEHOLDER_REGIONAL_GENERATION_FACTOR_KWH_PER_KW_PER_DAY.get(
        _normalize_key(state), PLACEHOLDER_DEFAULT_GENERATION_FACTOR
    )
    return (monthly_units_kwh / (DAYS_PER_MONTH * factor)) * (target_offset_percent / 100.0)


def estimate_size_from_bill(
    monthly_bill_inr: float,
    state: str,
    target_offset_percent: float = DEFAULT_TARGET_OFFSET_PERCENT,
) -> float:
    """Same consumption-based formula, with the bill converted to units first
    via engine.py's own tariff assumption (imported, single source of truth)."""
    monthly_units = monthly_bill_inr / PLACEHOLDER_ASSUMED_TARIFF_INR_PER_UNIT
    return estimate_size_from_units(monthly_units, state, target_offset_percent)


def estimate_size_from_roof_area(usable_roof_area_sqft: float) -> float:
    return usable_roof_area_sqft / ROOF_AREA_SQFT_PER_KW


def apply_phase_cap(size_kw: float, connection_phase: Optional[str]) -> float:
    cap = PLACEHOLDER_PHASE_CAP_KW.get(_normalize_key(connection_phase))
    if cap is None:
        return size_kw
    return min(size_kw, cap)


def round_for_residential(size_kw: float) -> float:
    return round(size_kw / RESIDENTIAL_ROUNDING_KW) * RESIDENTIAL_ROUNDING_KW


@dataclass
class SizingInputs:
    sizing_method: str  # "by_units" | "by_bill" | "by_roof_area"
    state: str
    monthly_units_kwh: Optional[float] = None
    monthly_bill_inr: Optional[float] = None
    usable_roof_area_sqft: Optional[float] = None
    connection_phase: Optional[str] = None
    target_offset_percent: float = DEFAULT_TARGET_OFFSET_PERCENT


@dataclass
class SizingResult:
    recommended_size_kw: float
    consumption_based_kw: Optional[float]
    area_based_kw: Optional[float]
    phase_capped: bool


_KNOWN_SIZING_METHODS = ("by_units", "by_bill", "by_roof_area")


def calculate_recommended_size(inputs: SizingInputs) -> SizingResult:
    """Resolve a recommended, phase-capped, residential-rounded system size.

    sizing_method declares which primary input the caller collected (used to
    validate that the matching field was actually supplied), but the
    calculation itself is driven by whichever of the underlying fields are
    present — NOT an exclusive switch on the method. A caller who supplies
    both a consumption figure (units or bill) AND a roof area gets both
    computed, and the smaller wins: a roof cannot physically host more
    capacity than it has room for, regardless of what consumption alone
    would suggest.
    """
    method = _normalize_key(inputs.sizing_method)
    if method not in _KNOWN_SIZING_METHODS:
        raise ValueError(
            f"Unknown sizing_method: {inputs.sizing_method!r}. "
            f"Expected one of {_KNOWN_SIZING_METHODS}."
        )
    if method == "by_units" and inputs.monthly_units_kwh is None:
        raise ValueError("monthly_units_kwh is required for sizing_method='by_units'.")
    if method == "by_bill" and inputs.monthly_bill_inr is None:
        raise ValueError("monthly_bill_inr is required for sizing_method='by_bill'.")
    if method == "by_roof_area" and inputs.usable_roof_area_sqft is None:
        raise ValueError("usable_roof_area_sqft is required for sizing_method='by_roof_area'.")

    consumption_kw: Optional[float] = None
    if inputs.monthly_units_kwh is not None:
        consumption_kw = estimate_size_from_units(
            inputs.monthly_units_kwh, inputs.state, inputs.target_offset_percent
        )
    elif inputs.monthly_bill_inr is not None:
        consumption_kw = estimate_size_from_bill(
            inputs.monthly_bill_inr, inputs.state, inputs.target_offset_percent
        )

    area_kw: Optional[float] = None
    if inputs.usable_roof_area_sqft is not None:
        area_kw = estimate_size_from_roof_area(inputs.usable_roof_area_sqft)

    candidates = [v for v in (consumption_kw, area_kw) if v is not None]
    raw_size = min(candidates)

    capped = apply_phase_cap(raw_size, inputs.connection_phase)
    recommended = round_for_residential(capped)
    # A cap or rounding that would otherwise floor out at 0 makes no sense for
    # a real recommendation — 0.5kW is the smallest real system this rounds to.
    recommended = max(recommended, RESIDENTIAL_ROUNDING_KW)

    return SizingResult(
        recommended_size_kw=recommended,
        consumption_based_kw=round(consumption_kw, 2) if consumption_kw is not None else None,
        area_based_kw=round(area_kw, 2) if area_kw is not None else None,
        phase_capped=capped < raw_size,
    )
