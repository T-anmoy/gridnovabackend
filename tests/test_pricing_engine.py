"""Tests for app/pricing/engine.py — the multi-layer pricing calculation
framework. Every value in the engine is a placeholder pending real business
input; these tests check the CALCULATION LOGIC is correct given those
placeholders, not that the placeholder numbers themselves are final.
"""
import pytest

from app.pricing import engine as pricing_engine
from app.pricing.engine import PricingInputs


class TestBaseRate:
    def test_known_segment_and_band(self):
        assert pricing_engine.calculate_base_rate("residential", 5) == \
            pricing_engine.PLACEHOLDER_BASE_RATE_PER_KW["residential"]["small"]

    def test_case_insensitive_segment(self):
        assert pricing_engine.calculate_base_rate("Residential", 5) == \
            pricing_engine.calculate_base_rate("residential", 5)

    def test_unknown_segment_raises(self):
        with pytest.raises(ValueError):
            pricing_engine.calculate_base_rate("not_a_segment", 5)

    def test_size_bands_select_different_rates(self):
        small = pricing_engine.calculate_base_rate("commercial", 5)
        medium = pricing_engine.calculate_base_rate("commercial", 50)
        large = pricing_engine.calculate_base_rate("commercial", 500)
        assert small != medium != large


class TestSizeBandModifier:
    def test_band_boundaries(self):
        assert pricing_engine.calculate_size_band_modifier(9.99) == pricing_engine.PLACEHOLDER_SIZE_BAND_MODIFIER["small"]
        assert pricing_engine.calculate_size_band_modifier(10) == pricing_engine.PLACEHOLDER_SIZE_BAND_MODIFIER["medium"]
        assert pricing_engine.calculate_size_band_modifier(99.99) == pricing_engine.PLACEHOLDER_SIZE_BAND_MODIFIER["medium"]
        assert pricing_engine.calculate_size_band_modifier(100) == pricing_engine.PLACEHOLDER_SIZE_BAND_MODIFIER["large"]

    def test_larger_systems_are_cheaper_per_kw(self):
        assert pricing_engine.PLACEHOLDER_SIZE_BAND_MODIFIER["large"] < pricing_engine.PLACEHOLDER_SIZE_BAND_MODIFIER["small"]


class TestEquipmentCost:
    def test_empty_selection_is_zero(self, memory):
        assert pricing_engine.calculate_equipment_cost(memory, []) == 0.0

    def test_sums_selling_price_not_procurement_price(self, memory, db_session):
        item = memory.create_equipment(
            category=__import__("app.memory.models", fromlist=["EquipmentCategory"]).EquipmentCategory.module,
            brand="Waaree", model_name="Test-540", performed_by="test",
            procurement_price_inr=10_000.0,
        )
        db_session.commit()

        cost = pricing_engine.calculate_equipment_cost(memory, [item.id])
        expected = 10_000.0 * (1 + pricing_engine.PLACEHOLDER_EQUIPMENT_MARKUP_PERCENT / 100.0)
        assert cost == pytest.approx(expected)
        assert cost != 10_000.0  # never the raw procurement figure

    def test_inactive_equipment_is_skipped(self, memory, db_session):
        from app.memory.models import EquipmentCategory

        item = memory.create_equipment(
            category=EquipmentCategory.inverter, brand="Growatt", model_name="X",
            performed_by="test", procurement_price_inr=5_000.0,
        )
        db_session.commit()
        memory.update_equipment(item.id, performed_by="test", is_active=False)
        db_session.commit()

        assert pricing_engine.calculate_equipment_cost(memory, [item.id]) == 0.0

    def test_unknown_equipment_id_is_skipped_not_an_error(self, memory):
        assert pricing_engine.calculate_equipment_cost(memory, [999999]) == 0.0

    def test_equipment_with_no_procurement_price_contributes_nothing(self, memory, db_session):
        from app.memory.models import EquipmentCategory

        item = memory.create_equipment(
            category=EquipmentCategory.structure, brand="Generic", model_name="Rail",
            performed_by="test",
        )
        db_session.commit()
        assert pricing_engine.calculate_equipment_cost(memory, [item.id]) == 0.0

    def test_sums_multiple_items(self, memory, db_session):
        from app.memory.models import EquipmentCategory

        i1 = memory.create_equipment(
            category=EquipmentCategory.module, brand="A", model_name="1",
            performed_by="test", procurement_price_inr=1_000.0,
        )
        i2 = memory.create_equipment(
            category=EquipmentCategory.inverter, brand="B", model_name="2",
            performed_by="test", procurement_price_inr=2_000.0,
        )
        db_session.commit()
        total = pricing_engine.calculate_equipment_cost(memory, [i1.id, i2.id])
        markup = 1 + pricing_engine.PLACEHOLDER_EQUIPMENT_MARKUP_PERCENT / 100.0
        assert total == pytest.approx((1_000.0 + 2_000.0) * markup)


class TestComplexityModifier:
    def test_rcc_single_floor_no_shading_is_baseline(self):
        assert pricing_engine.calculate_complexity_modifier("RCC", 1, "none") == pytest.approx(1.0)

    def test_tin_roof_is_more_expensive_than_rcc(self):
        tin = pricing_engine.calculate_complexity_modifier("tin", 1, "none")
        rcc = pricing_engine.calculate_complexity_modifier("rcc", 1, "none")
        assert tin > rcc

    def test_more_floors_increases_modifier(self):
        one = pricing_engine.calculate_complexity_modifier("rcc", 1, "none")
        three = pricing_engine.calculate_complexity_modifier("rcc", 3, "none")
        assert three > one

    def test_extra_floors_are_capped(self):
        capped = pricing_engine.calculate_complexity_modifier("rcc", 100, "none")
        at_cap = pricing_engine.calculate_complexity_modifier(
            "rcc", pricing_engine.PLACEHOLDER_MAX_EXTRA_FLOORS_COUNTED + 1, "none",
        )
        assert capped == at_cap

    def test_heavy_shading_costs_more_than_none(self):
        heavy = pricing_engine.calculate_complexity_modifier("rcc", 1, "heavy")
        none = pricing_engine.calculate_complexity_modifier("rcc", 1, "none")
        assert heavy > none

    def test_unknown_or_missing_values_use_sensible_defaults(self):
        # Doesn't raise — roof_type/shading are free text, not a closed enum.
        result = pricing_engine.calculate_complexity_modifier(None, None, None)
        assert result > 0


class TestLocationModifier:
    def test_primary_states_have_no_premium(self):
        assert pricing_engine.calculate_location_modifier("West Bengal") == 1.0
        assert pricing_engine.calculate_location_modifier("Bihar") == 1.0

    def test_case_insensitive(self):
        assert pricing_engine.calculate_location_modifier("WEST BENGAL") == 1.0

    def test_other_states_carry_a_premium(self):
        assert pricing_engine.calculate_location_modifier("Odisha") > 1.0

    def test_remote_adds_an_additional_premium(self):
        normal = pricing_engine.calculate_location_modifier("West Bengal", is_remote=False)
        remote = pricing_engine.calculate_location_modifier("West Bengal", is_remote=True)
        assert remote > normal


class TestBosAddition:
    def test_scales_with_size(self):
        small = pricing_engine.calculate_bos_addition(5)
        large = pricing_engine.calculate_bos_addition(50)
        assert large == pytest.approx(small * 10)


class TestCivilAddition:
    def test_ground_mounted_costs_more_than_residential_rooftop(self):
        gm = pricing_engine.calculate_civil_addition("ground_mounted")
        res = pricing_engine.calculate_civil_addition("rooftop_residential")
        assert gm > res

    def test_unknown_site_type_uses_default(self):
        assert pricing_engine.calculate_civil_addition("something_unrecognized") == \
            pricing_engine.PLACEHOLDER_DEFAULT_CIVIL_ADDITION
        assert pricing_engine.calculate_civil_addition(None) == \
            pricing_engine.PLACEHOLDER_DEFAULT_CIVIL_ADDITION


class TestRegulatoryAddition:
    def test_net_metering_adds_a_fee(self):
        without = pricing_engine.calculate_regulatory_addition("West Bengal", False)
        with_nm = pricing_engine.calculate_regulatory_addition("West Bengal", True)
        assert with_nm == pytest.approx(without + pricing_engine.PLACEHOLDER_NET_METERING_ADDITION)

    def test_unknown_state_uses_default_base(self):
        assert pricing_engine.calculate_regulatory_addition("Odisha", False) == \
            pricing_engine.PLACEHOLDER_DEFAULT_REGULATORY_BASE


class TestOptionalServices:
    def test_empty_list_is_zero(self):
        assert pricing_engine.calculate_optional_services([]) == 0.0

    def test_sums_known_services(self):
        total = pricing_engine.calculate_optional_services(
            ["extended_warranty", "annual_maintenance_contract"]
        )
        expected = (
            pricing_engine.PLACEHOLDER_OPTIONAL_SERVICE_PRICES["extended_warranty"]
            + pricing_engine.PLACEHOLDER_OPTIONAL_SERVICE_PRICES["annual_maintenance_contract"]
        )
        assert total == pytest.approx(expected)

    def test_unrecognized_service_contributes_nothing(self):
        assert pricing_engine.calculate_optional_services(["not_a_real_service"]) == 0.0


class TestGst:
    def test_applies_the_placeholder_rate(self):
        assert pricing_engine.calculate_gst(100_000, "West Bengal") == pytest.approx(
            100_000 * pricing_engine.PLACEHOLDER_GST_RATE_PERCENT / 100.0
        )


class TestCalculateFullEstimate:
    def _inputs(self, **overrides) -> PricingInputs:
        defaults = dict(segment="residential", size_kw=5, state="West Bengal")
        defaults.update(overrides)
        return PricingInputs(**defaults)

    def test_returns_a_complete_breakdown(self, memory):
        result = pricing_engine.calculate_full_estimate(memory, self._inputs())
        assert result.total > 0
        assert result.subtotal_before_tax > 0
        assert result.gst_amount == pytest.approx(result.subtotal_before_tax * pricing_engine.PLACEHOLDER_GST_RATE_PERCENT / 100.0)
        assert result.total == pytest.approx(result.subtotal_before_tax + result.gst_amount)

    def test_category_totals_sum_to_the_total(self, memory):
        result = pricing_engine.calculate_full_estimate(memory, self._inputs())
        categories_sum = sum(result.category_totals.values())
        assert categories_sum == pytest.approx(result.total, rel=1e-6)

    def test_category_totals_have_exactly_the_three_expected_buckets(self, memory):
        result = pricing_engine.calculate_full_estimate(memory, self._inputs())
        assert set(result.category_totals.keys()) == {"equipment", "installation", "other"}

    def test_larger_size_produces_a_larger_total(self, memory):
        small = pricing_engine.calculate_full_estimate(memory, self._inputs(size_kw=3))
        large = pricing_engine.calculate_full_estimate(memory, self._inputs(size_kw=300))
        assert large.total > small.total

    def test_equipment_selection_increases_the_equipment_category(self, memory, db_session):
        from app.memory.models import EquipmentCategory

        item = memory.create_equipment(
            category=EquipmentCategory.module, brand="A", model_name="1",
            performed_by="test", procurement_price_inr=50_000.0,
        )
        db_session.commit()

        without = pricing_engine.calculate_full_estimate(memory, self._inputs())
        with_equipment = pricing_engine.calculate_full_estimate(
            memory, self._inputs(equipment_selection=[item.id])
        )
        assert with_equipment.category_totals["equipment"] > without.category_totals["equipment"]
        assert with_equipment.total > without.total

    def test_invalid_segment_raises(self, memory):
        with pytest.raises(ValueError):
            pricing_engine.calculate_full_estimate(memory, self._inputs(segment="not_real"))

    def test_procurement_cost_never_appears_in_the_breakdown(self, memory, db_session):
        """The security constraint, checked directly against a real, distinctive
        procurement price value that must never surface anywhere in the output."""
        from app.memory.models import EquipmentCategory
        import dataclasses

        distinctive_cost = 137_777.0
        item = memory.create_equipment(
            category=EquipmentCategory.module, brand="A", model_name="1",
            performed_by="test", procurement_price_inr=distinctive_cost,
        )
        db_session.commit()

        result = pricing_engine.calculate_full_estimate(
            memory, self._inputs(equipment_selection=[item.id])
        )
        as_text = str(dataclasses.asdict(result))
        assert str(distinctive_cost) not in as_text
        assert "137777" not in as_text.replace(".0", "")
