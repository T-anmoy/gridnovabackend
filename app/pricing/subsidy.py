"""
PM Surya Ghar Muft Bijli Yojana — Central Financial Assistance (CFA) subsidy.

Given its own module rather than folded into engine.py's placeholder constants
for the same reason margin.py got its own file: this is a real, sourced,
government-published figure, not a Grid-Nova-commercial placeholder pending
Raju Paul — mixing the two under one PLACEHOLDER_-prefixed namespace would
misrepresent which numbers here are actually confirmed.

Source: PM Surya Ghar Muft Bijli Yojana (MNRE) residential rooftop solar CFA
slabs — ₹30,000/kW for the first 2kW of capacity, ₹18,000 for the 3rd kW, with
a hard cap of ₹78,000 for any system of 3kW or more (2×30,000 + 1×18,000).
Applies to genuine individual residential rooftop only — explicitly NOT to
Housing Society/RWA or any commercial/industrial/government segment, which
this scheme's residential CFA slab does not cover.
"""
from __future__ import annotations

RATE_PER_KW_FIRST_2KW = 30_000.0
RATE_PER_KW_THIRD_KW = 18_000.0
MAX_SUBSIDY_INR = 78_000.0  # cap reached at 3kW and stays flat above it
SUBSIDY_ELIGIBLE_UP_TO_KW = 3.0


def subsidy_slabs(
    size_kw: float,
    segment: str,
    is_housing_society: bool = False,
) -> list[tuple[float, float, float]]:
    """The CFA by slab as (kW in the slab, rate per kW, amount): the first 2 kW at
    30,000 and the 3rd kW at 18,000. Empty where the scheme's residential slab does not
    apply. The proposal document prints these lines; the total is
    calculate_pm_surya_ghar_subsidy."""
    if is_housing_society or (segment or "").strip().lower() != "residential":
        return []
    if size_kw <= 0:
        return []
    slabs = [(min(size_kw, 2.0), RATE_PER_KW_FIRST_2KW)]
    if size_kw > 2.0:
        slabs.append((min(size_kw - 2.0, 1.0), RATE_PER_KW_THIRD_KW))
    return [(kw, rate, round(kw * rate, 2)) for kw, rate in slabs]


def calculate_pm_surya_ghar_subsidy(
    size_kw: float,
    segment: str,
    is_housing_society: bool = False,
) -> float:
    """Residential-only. Returns 0 for Housing Society, commercial, industrial,
    or government — this scheme's residential CFA slab does not apply there."""
    subsidy = sum(amount for _kw, _rate, amount in subsidy_slabs(size_kw, segment, is_housing_society))
    return round(min(subsidy, MAX_SUBSIDY_INR), 2)
