"""POST /api/v1/pricing/instant-estimate — the /solar-calculator flow, priced from Grid
Nova's price list (PM-A). Public, unauthenticated."""
from decimal import Decimal

import pytest

from app.pricing import price_list
from app.pricing.subsidy import MAX_SUBSIDY_INR

BASE_BODY = {
    "segment": "residential",
    "state": "West Bengal",
    "sizing_method": "by_units",
    "monthly_units_kwh": 600,        # 600 / (30 × 4.0) × 0.9 = 4.5 kW exact → priced 5 kW
    "connection_phase": "single_phase",
}


def _post(client, **overrides):
    body = {**BASE_BODY, **overrides}
    body = {k: v for k, v in body.items() if v is not None}
    return client.post("/api/v1/pricing/instant-estimate", json=body)


class TestListPrice:
    def test_both_packages_from_the_price_list(self, client):
        body = _post(client).json()
        assert body["price_status"] == "list_price" and body["custom_quote_reasons"] == []
        assert (body["recommended_kw_exact"], body["priced_kw"], body["priced_kw_differs"]) == (4.5, 5, True)
        assert body["phase"] == "SINGLE" and body["price_list_version"] == 1
        assert body["destination"] == "West Bengal"
        assured, signature = body["packages"]["NOVA_ASSURED"], body["packages"]["NOVA_SIGNATURE"]
        assert (assured["basic"], assured["gst"], assured["final"]) == ("287000.00", "25543.00", "312543.00")
        assert (signature["basic"], signature["gst"], signature["final"]) == ("321440.00", "28608.16", "350048.16")
        assert assured["display_name"] == "Nova Assured"
        assert body["gst_rule"]["effective_rate"] == "0.089"
        assert "70%" in body["gst_rule"]["description"]

    def test_top_level_fields_come_from_nova_assured(self, client):
        body = _post(client).json()
        assert body["base_cost"] == 287000.0 and body["subtotal_before_tax"] == 287000.0
        assert body["gst_amount"] == 25543.0
        assert body["net_cost"] == 312543.0 - MAX_SUBSIDY_INR
        assert body["adders"] == {} and body["component_breakdown"] == {}
        assert body["is_indicative_range"] is False and body["net_cost_range_min"] is None

    def test_list_prices_are_fixed_whatever_the_site(self, client):
        plain = _post(client).json()["packages"]
        tough = _post(client, roof_type="asbestos", floors=6, shading="heavy", access_difficulty="difficult",
                      is_remote=True).json()["packages"]
        assert plain == tough

    def test_significant_shading_keeps_the_list_price_with_a_note(self, client):
        body = _post(client, shading="significant").json()
        assert body["price_status"] == "list_price" and body["is_indicative_range"] is False
        assert body["net_cost_range_min"] is None and "site visit" in body["note"]

    def test_exact_whole_kw_is_not_flagged(self, client):
        body = _post(client, monthly_units_kwh=400).json()   # 400/120*0.9 = 3.0
        assert (body["recommended_kw_exact"], body["priced_kw"], body["priced_kw_differs"]) == (3.0, 3, False)

    def test_three_phase_table(self, client):
        body = _post(client, connection_phase="three_phase", monthly_units_kwh=900).json()   # 6.75 → 7
        assert body["priced_kw"] == 7 and body["packages"]["NOVA_ASSURED"]["final"] == "490050.00"

    def test_old_single_phase_5kw_cap_no_longer_limits_pricing(self, client):
        body = _post(client, monthly_units_kwh=1000).json()   # 7.5 → 8 kW single phase
        assert body["priced_kw"] == 8 and body["sizing_detail"]["phase_capped"] is False

    def test_housing_society_is_list_priced_without_subsidy(self, client):
        body = _post(client, is_housing_society=True).json()
        assert body["price_status"] == "list_price" and body["subsidy_inr"] == 0
        assert body["subsidy_conditions"] == []
        assert body["packages"]["NOVA_ASSURED"]["net_after_subsidy"] == "312543.00"


class TestSubsidy:
    def test_subsidy_shown_separately_with_conditions(self, client):
        body = _post(client).json()
        assert body["subsidy_inr"] == MAX_SUBSIDY_INR
        assert body["subsidy_conditions"] == ["dcr_modules_required", "scheme_eligibility"]
        assert body["packages"]["NOVA_ASSURED"]["net_after_subsidy"] == str(Decimal("312543.00") - Decimal("78000"))
        assert body["packages"]["NOVA_SIGNATURE"]["net_after_subsidy"] == str(Decimal("350048.16") - Decimal("78000"))

    def test_subsidy_on_the_priced_size(self, client):
        body = _post(client, monthly_units_kwh=160).json()   # 1.2 → 2 kW
        assert body["priced_kw"] == 2 and body["subsidy_inr"] == 60000.0


class TestCustomQuote:
    @pytest.mark.parametrize("overrides,reason", [
        ({"state": "Odisha"}, "outside_west_bengal"),
        ({"segment": "commercial"}, "segment_not_in_price_list"),
        ({"connection_type": "hybrid"}, "battery_or_off_grid"),
        ({"connection_type": "off_grid"}, "battery_or_off_grid"),
        ({"battery_storage": True}, "battery_or_off_grid"),
        ({"connection_phase": None}, "phase_unknown"),
        ({"monthly_units_kwh": 1500}, "size_above_max"),                                   # 11.25 kW
        ({"connection_phase": "three_phase", "monthly_units_kwh": 200}, "three_phase_below_min"),   # 1.5 → 2
    ])
    def test_reasons(self, client, overrides, reason):
        body = _post(client, **overrides).json()
        assert body["price_status"] == "custom_quote" and reason in body["custom_quote_reasons"]
        assert body["packages"] == {} and body["priced_kw"] is None
        assert body["base_cost"] is None and body["net_cost"] is None and body["gst_amount"] is None
        # Sizing and subsidy information still return.
        assert body["recommended_size_kw"] and body["recommended_kw_exact"]
        assert "subsidy_inr" in body

    def test_never_prices_a_one_or_two_kw_three_phase_system(self, client):
        for units in (40, 100, 200, 260):    # 0.3 / 0.75 / 1.5 / 1.95 kW
            body = _post(client, connection_phase="three_phase", monthly_units_kwh=units).json()
            assert body["price_status"] == "custom_quote" and body["priced_kw"] is None

    def test_three_phase_just_over_two_kw_is_priced_at_three(self, client):
        body = _post(client, connection_phase="three_phase", monthly_units_kwh=280).json()   # 2.1 → 3
        assert body["priced_kw"] == 3


class TestShape:
    def test_malformed_input_no_sizing_value_rejected(self, client):
        r = client.post("/api/v1/pricing/instant-estimate",
                        json={"segment": "residential", "state": "West Bengal", "sizing_method": "by_units"})
        assert r.status_code == 422

    def test_unknown_segment_rejected(self, client):
        assert _post(client, segment="not-a-real-segment").status_code == 422

    def test_a_tier_field_is_ignored(self, client):
        assert _post(client, tier="platinum").json() == _post(client).json()

    def test_no_engine_figure_in_the_response(self, client):
        from app.pricing import engine
        body = _post(client).json()
        # The old best-tier engine price for 4.5 kW would have been well above the list price.
        assert "tier" not in body and "tier_cost" not in body
        assert all(v not in (engine.PLACEHOLDER_GST_RATE_PERCENT,) for v in body.values() if not isinstance(v, dict))


class TestRateLimit:
    def test_rate_limited(self, client):
        from app.api.routes import pricing as pricing_routes
        pricing_routes._instant_estimate_request_log.clear()
        for _ in range(pricing_routes._INSTANT_ESTIMATE_RATE_LIMIT):
            assert _post(client).status_code == 200
        assert _post(client).status_code == 429
        pricing_routes._instant_estimate_request_log.clear()
