"""Tests for Margin Control (app/pricing/margin.py) — margin calculation,
health-status thresholds, and its connections to the Pricing Engine
(margin_from_breakdown) and the Approval Engine (shared MARGIN_FLOOR_PERCENT).
"""
import pytest

from app.pricing import engine as pricing_engine
from app.pricing.margin import (
    MARGIN_FLOOR_PERCENT,
    MARGIN_WARNING_PERCENT,
    MarginStatus,
    calculate_margin,
    check_margin_health,
    margin_from_breakdown,
)


class TestCalculateMargin:
    def test_basic_margin(self):
        result = calculate_margin(selling_price=1_000.0, estimated_cost=800.0)
        assert result.margin_inr == 200.0
        assert result.margin_percent == 20.0

    def test_zero_margin(self):
        result = calculate_margin(selling_price=1_000.0, estimated_cost=1_000.0)
        assert result.margin_inr == 0.0
        assert result.margin_percent == 0.0

    def test_negative_margin_selling_below_cost(self):
        result = calculate_margin(selling_price=800.0, estimated_cost=1_000.0)
        assert result.margin_inr == -200.0
        assert result.margin_percent == -25.0
        assert result.status == MarginStatus.below_floor

    def test_zero_selling_price_does_not_raise(self):
        result = calculate_margin(selling_price=0.0, estimated_cost=100.0)
        assert result.margin_percent == 0.0

    def test_status_is_attached_and_consistent_with_check_margin_health(self):
        result = calculate_margin(selling_price=1_000.0, estimated_cost=750.0)
        assert result.status == check_margin_health(result.margin_percent)


class TestCheckMarginHealth:
    def test_at_or_above_warning_threshold_is_healthy(self):
        assert check_margin_health(MARGIN_WARNING_PERCENT) == MarginStatus.healthy
        assert check_margin_health(MARGIN_WARNING_PERCENT + 10) == MarginStatus.healthy

    def test_between_floor_and_warning_is_warning(self):
        midpoint = (MARGIN_FLOOR_PERCENT + MARGIN_WARNING_PERCENT) / 2
        assert check_margin_health(midpoint) == MarginStatus.warning

    def test_below_floor_is_below_floor(self):
        assert check_margin_health(MARGIN_FLOOR_PERCENT - 0.01) == MarginStatus.below_floor

    def test_at_the_floor_exactly_is_not_yet_below_floor(self):
        assert check_margin_health(MARGIN_FLOOR_PERCENT) == MarginStatus.warning

    def test_negative_margin_is_below_floor(self):
        assert check_margin_health(-10.0) == MarginStatus.below_floor


class TestMarginFromBreakdown:
    def test_derives_margin_from_a_real_pricing_breakdown(self, memory):
        inputs = pricing_engine.PricingInputs(segment="residential", size_kw=5, state="West Bengal")
        breakdown = pricing_engine.calculate_full_estimate(memory, inputs)

        # A plausible placeholder cost well under the breakdown's total.
        estimated_cost = breakdown.total * 0.7
        result = margin_from_breakdown(breakdown, estimated_cost)

        assert result.margin_inr == pytest.approx(breakdown.total - estimated_cost, rel=1e-6)
        assert result.status in (MarginStatus.healthy, MarginStatus.warning, MarginStatus.below_floor)

    def test_a_cost_close_to_the_total_produces_a_thin_or_below_floor_margin(self, memory):
        inputs = pricing_engine.PricingInputs(segment="residential", size_kw=5, state="West Bengal")
        breakdown = pricing_engine.calculate_full_estimate(memory, inputs)

        result = margin_from_breakdown(breakdown, estimated_cost=breakdown.total * 0.95)
        assert result.status in (MarginStatus.warning, MarginStatus.below_floor)
