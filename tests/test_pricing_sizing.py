"""Tests for the sizing module (app/pricing/sizing.py) — both sizing methods
independently, the take-the-smaller rule, phase capping, and rounding."""
import pytest

from app.pricing import sizing


class TestConsumptionSizing:
    def test_by_units(self):
        result = sizing.calculate_recommended_size(
            sizing.SizingInputs(
                sizing_method="by_units", state="West Bengal", monthly_units_kwh=600,
            )
        )
        # 600 / (30 * 4.0) * 0.9 = 4.5
        assert result.recommended_size_kw == 4.5
        assert result.consumption_based_kw == 4.5
        assert result.area_based_kw is None

    def test_by_bill_converts_via_engine_tariff(self):
        from app.pricing.engine import PLACEHOLDER_ASSUMED_TARIFF_INR_PER_UNIT

        bill = 600 * PLACEHOLDER_ASSUMED_TARIFF_INR_PER_UNIT
        result = sizing.calculate_recommended_size(
            sizing.SizingInputs(sizing_method="by_bill", state="West Bengal", monthly_bill_inr=bill)
        )
        assert result.recommended_size_kw == 4.5

    def test_unknown_state_uses_default_generation_factor(self):
        result = sizing.calculate_recommended_size(
            sizing.SizingInputs(sizing_method="by_units", state="Rajasthan", monthly_units_kwh=600)
        )
        assert result.recommended_size_kw > 0

    def test_by_units_requires_the_matching_field(self):
        with pytest.raises(ValueError):
            sizing.calculate_recommended_size(
                sizing.SizingInputs(sizing_method="by_units", state="West Bengal")
            )

    def test_unknown_sizing_method_raises(self):
        with pytest.raises(ValueError):
            sizing.calculate_recommended_size(
                sizing.SizingInputs(sizing_method="by_magic", state="West Bengal", monthly_units_kwh=100)
            )


class TestAreaSizing:
    def test_by_roof_area(self):
        result = sizing.calculate_recommended_size(
            sizing.SizingInputs(
                sizing_method="by_roof_area", state="West Bengal", usable_roof_area_sqft=1000,
            )
        )
        assert result.recommended_size_kw == 10.0  # 1000 / 100
        assert result.area_based_kw == 10.0
        assert result.consumption_based_kw is None


class TestTakeTheSmaller:
    def test_area_constrains_a_larger_consumption_estimate(self):
        # Consumption alone would want ~15kW; roof only fits 3kW.
        result = sizing.calculate_recommended_size(
            sizing.SizingInputs(
                sizing_method="by_units",
                state="West Bengal",
                monthly_units_kwh=2000,
                usable_roof_area_sqft=300,
            )
        )
        assert result.consumption_based_kw > result.area_based_kw
        assert result.recommended_size_kw == result.area_based_kw

    def test_consumption_constrains_a_larger_area_estimate(self):
        # Roof could fit 20kW; consumption (100 units/(30*4.0)*0.9 = 0.75kW)
        # only justifies a much smaller system — consumption wins.
        result = sizing.calculate_recommended_size(
            sizing.SizingInputs(
                sizing_method="by_roof_area",
                state="West Bengal",
                usable_roof_area_sqft=2000,
                monthly_units_kwh=100,
            )
        )
        assert result.area_based_kw == 20.0
        assert result.consumption_based_kw == 0.75
        assert result.recommended_size_kw == 1.0  # 0.75 rounds to nearest 0.5


class TestPhaseCapping:
    def test_single_phase_caps_a_large_system(self):
        result = sizing.calculate_recommended_size(
            sizing.SizingInputs(
                sizing_method="by_roof_area",
                state="West Bengal",
                usable_roof_area_sqft=2000,  # would be 20kW uncapped
                connection_phase="single_phase",
            )
        )
        assert result.recommended_size_kw == sizing.PLACEHOLDER_PHASE_CAP_KW["single_phase"]
        assert result.phase_capped is True

    def test_three_phase_caps_at_its_own_higher_limit(self):
        result = sizing.calculate_recommended_size(
            sizing.SizingInputs(
                sizing_method="by_roof_area",
                state="West Bengal",
                usable_roof_area_sqft=5000,  # 50kW uncapped
                connection_phase="three_phase",
            )
        )
        assert result.recommended_size_kw == sizing.PLACEHOLDER_PHASE_CAP_KW["three_phase"]

    def test_unstated_phase_is_not_capped(self):
        result = sizing.calculate_recommended_size(
            sizing.SizingInputs(
                sizing_method="by_roof_area", state="West Bengal", usable_roof_area_sqft=2000,
            )
        )
        assert result.recommended_size_kw == 20.0
        assert result.phase_capped is False

    def test_a_system_under_the_cap_is_not_marked_capped(self):
        result = sizing.calculate_recommended_size(
            sizing.SizingInputs(
                sizing_method="by_roof_area", state="West Bengal",
                usable_roof_area_sqft=200, connection_phase="single_phase",
            )
        )
        assert result.recommended_size_kw == 2.0
        assert result.phase_capped is False


class TestRounding:
    def test_rounds_to_nearest_half_kw(self):
        result = sizing.calculate_recommended_size(
            sizing.SizingInputs(
                sizing_method="by_roof_area", state="West Bengal", usable_roof_area_sqft=170,
            )
        )
        assert result.recommended_size_kw in (1.5, 2.0)  # 1.7 rounds to nearest 0.5
        assert (result.recommended_size_kw * 2) == round(result.recommended_size_kw * 2)

    def test_never_rounds_down_to_zero(self):
        result = sizing.calculate_recommended_size(
            sizing.SizingInputs(
                sizing_method="by_roof_area", state="West Bengal", usable_roof_area_sqft=10,
            )
        )
        assert result.recommended_size_kw >= 0.5
