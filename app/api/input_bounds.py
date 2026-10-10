"""Sanity ceilings for numbers the API accepts from the outside.

The values are the ones the associate onboarding schema already uses
(app/api/routes/associate_onboarding.py OnboardingFields) — technical limits far above
any real customer, there only so Infinity, NaN or an absurd figure is a 422 instead of
an overflow or a 500 (`size_kw=Infinity` used to 500 the public estimate). They are not
business rules.
"""
from __future__ import annotations

from typing import Optional

from pydantic import Field

MAX_KW = 1_000_000
MAX_MONTHLY_INR = 100_000_000
MAX_MONTHLY_KWH = 100_000_000
MAX_AREA_SQFT = 10_000_000
MAX_YEARS = 200
MAX_FLOORS = 200
# A whole project's price (indicative totals sent along with leads).
MAX_PROJECT_INR = 10_000_000_000


def bounded(*, le: float, gt: Optional[float] = None, ge: Optional[float] = None, default=None):
    """An Optional finite float field with a ceiling."""
    return Field(default=default, gt=gt, ge=ge, le=le, allow_inf_nan=False)

