"""Tests for the PM Surya Ghar subsidy calculation (app/pricing/subsidy.py) —
real, sourced slabs, at 1kW/2kW/3kW/5kW, and the residential-only restriction
(never Housing Society, never commercial/industrial/government)."""
from app.pricing.subsidy import (
    MAX_SUBSIDY_INR,
    calculate_pm_surya_ghar_subsidy,
)


class TestRealSlabsByKw:
    def test_1kw(self):
        assert calculate_pm_surya_ghar_subsidy(1.0, "residential") == 30_000.0

    def test_2kw(self):
        assert calculate_pm_surya_ghar_subsidy(2.0, "residential") == 60_000.0

    def test_3kw_reaches_the_cap(self):
        assert calculate_pm_surya_ghar_subsidy(3.0, "residential") == 78_000.0
        assert calculate_pm_surya_ghar_subsidy(3.0, "residential") == MAX_SUBSIDY_INR

    def test_5kw_stays_capped_not_higher(self):
        """Above 3kW the subsidy does not keep growing — it's a hard cap."""
        assert calculate_pm_surya_ghar_subsidy(5.0, "residential") == MAX_SUBSIDY_INR

    def test_0_5kw_partial_first_slab(self):
        assert calculate_pm_surya_ghar_subsidy(0.5, "residential") == 15_000.0

    def test_partial_third_kw(self):
        # 2kW fully at 30k/kW = 60,000, plus 0.5kW of the third kW at 18k/kW.
        assert calculate_pm_surya_ghar_subsidy(2.5, "residential") == 69_000.0


class TestResidentialOnlyRestriction:
    def test_housing_society_gets_zero_even_though_segment_is_residential(self):
        assert calculate_pm_surya_ghar_subsidy(3.0, "residential", is_housing_society=True) == 0.0

    def test_commercial_gets_zero(self):
        assert calculate_pm_surya_ghar_subsidy(3.0, "commercial") == 0.0

    def test_industrial_gets_zero(self):
        assert calculate_pm_surya_ghar_subsidy(3.0, "industrial") == 0.0

    def test_government_gets_zero(self):
        assert calculate_pm_surya_ghar_subsidy(3.0, "government") == 0.0

    def test_zero_or_negative_size_gets_zero(self):
        assert calculate_pm_surya_ghar_subsidy(0.0, "residential") == 0.0
        assert calculate_pm_surya_ghar_subsidy(-1.0, "residential") == 0.0
