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


def calculate_pm_surya_ghar_subsidy(
    size_kw: float,
    segment: str,
    is_housing_society: bool = False,
) -> float:
    """Residential-only. Returns 0 for Housing Society, commercial, industrial,
    or government — this scheme's residential CFA slab does not apply there."""
    if is_housing_society or (segment or "").strip().lower() != "residential":
        return 0.0
    if size_kw <= 0:
        return 0.0

    first_slab_kw = min(size_kw, 2.0)
    subsidy = first_slab_kw * RATE_PER_KW_FIRST_2KW

    if size_kw > 2.0:
        third_slab_kw = min(size_kw - 2.0, 1.0)
        subsidy += third_slab_kw * RATE_PER_KW_THIRD_KW

    return round(min(subsidy, MAX_SUBSIDY_INR), 2)
