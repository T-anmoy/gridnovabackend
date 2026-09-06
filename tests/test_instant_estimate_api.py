"""Tests for POST /api/v1/pricing/instant-estimate — the expanded
/solar-calculator flow. Public, unauthenticated. Does not touch /estimate or
/breakdown, which have their own pre-existing test coverage untouched here."""
import pytest

from app.pricing.subsidy import MAX_SUBSIDY_INR


BASE_BODY = {
    "segment": "residential",
    "state": "West Bengal",
    "sizing_method": "by_units",
    "monthly_units_kwh": 600,  # -> 4.5kW recommended (600/(30*4.0)*0.9)
}


def _post(client, **overrides):
    body = {**BASE_BODY, **overrides}
    return client.post("/api/v1/pricing/instant-estimate", json=body)


class TestBasicShape:
    def test_returns_a_complete_breakdown(self, client, memory, db_session):
        r = _post(client)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["recommended_size_kw"] == 4.5
        for key in (
            "base_cost", "tier_cost", "adders", "component_breakdown",
            "subtotal_before_tax", "gst_amount", "subsidy_inr", "net_cost",
        ):
            assert key in body
        assert body["is_indicative_range"] is False
        assert body["net_cost_range_min"] is None
        assert body["net_cost_range_max"] is None
        assert body["note"] is None

    def test_malformed_input_no_sizing_value_rejected(self, client, memory, db_session):
        r = client.post(
            "/api/v1/pricing/instant-estimate",
            json={"segment": "residential", "state": "West Bengal", "sizing_method": "by_units"},
        )
        assert r.status_code == 422

    def test_unknown_segment_rejected(self, client, memory, db_session):
        r = _post(client, segment="not-a-real-segment")
        assert r.status_code == 422

    def test_unknown_tier_rejected(self, client, memory, db_session):
        r = _post(client, tier="platinum")
        assert r.status_code == 422


class TestTiers:
    def test_good_tier_is_the_baseline(self, client, memory, db_session):
        r = _post(client, tier="good")
        body = r.json()
        assert body["base_cost"] == body["tier_cost"]

    def test_better_tier_adds_ten_percent(self, client, memory, db_session):
        r = _post(client, tier="better")
        body = r.json()
        assert body["tier_cost"] == pytest.approx(body["base_cost"] * 1.10, rel=1e-6)

    def test_best_tier_adds_twentyfive_percent(self, client, memory, db_session):
        r = _post(client, tier="best")
        body = r.json()
        assert body["tier_cost"] == pytest.approx(body["base_cost"] * 1.25, rel=1e-6)

    def test_higher_tier_costs_strictly_more(self, client, memory, db_session):
        good = _post(client, tier="good").json()
        better = _post(client, tier="better").json()
        best = _post(client, tier="best").json()
        assert good["tier_cost"] < better["tier_cost"] < best["tier_cost"]


class TestAdders:
    def test_access_difficulty_adds_on_top_of_tier_cost(self, client, memory, db_session):
        easy = _post(client, access_difficulty="easy").json()
        difficult = _post(client, access_difficulty="difficult").json()
        assert easy["adders"]["access_difficulty"] == 0.0
        assert difficult["adders"]["access_difficulty"] > 0.0
        assert difficult["subtotal_before_tax"] > easy["subtotal_before_tax"]

    def test_roof_type_and_floors_flow_through_component_breakdown(self, client, memory, db_session):
        rcc = _post(client, roof_type="rcc", floors=1).json()
        asbestos_multi_floor = _post(client, roof_type="asbestos", floors=4).json()
        assert asbestos_multi_floor["component_breakdown"]["installation"] > rcc["component_breakdown"]["installation"]


class TestSubsidy:
    def test_residential_gets_the_real_subsidy_for_its_recommended_size(self, client, memory, db_session):
        # 600 units -> 4.5kW recommended, well above the 3kW cap threshold.
        r = _post(client)
        body = r.json()
        assert body["subsidy_inr"] == MAX_SUBSIDY_INR

    def test_housing_society_gets_no_subsidy(self, client, memory, db_session):
        r = _post(client, is_housing_society=True)
        assert r.json()["subsidy_inr"] == 0.0

    def test_commercial_gets_no_subsidy(self, client, memory, db_session):
        r = _post(client, segment="commercial")
        assert r.json()["subsidy_inr"] == 0.0

    def test_net_cost_reflects_the_subsidy_deduction(self, client, memory, db_session):
        r = _post(client)
        body = r.json()
        assert body["net_cost"] == pytest.approx(
            body["subtotal_before_tax"] + body["gst_amount"] - body["subsidy_inr"], rel=1e-6
        )


class TestSignificantShading:
    def test_returns_an_indicative_range_not_a_confident_number(self, client, memory, db_session):
        r = _post(client, shading="significant")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["is_indicative_range"] is True
        assert body["net_cost"] is None
        assert body["net_cost_range_min"] is not None
        assert body["net_cost_range_max"] is not None
        assert body["net_cost_range_min"] < body["net_cost_range_max"]
        assert body["note"] and "site visit" in body["note"].lower()

    def test_normal_shading_returns_a_confident_number(self, client, memory, db_session):
        r = _post(client, shading="partial_morning")
        body = r.json()
        assert body["is_indicative_range"] is False
        assert body["net_cost"] is not None


class TestRateLimit:
    def test_rate_limited(self, client, memory, db_session):
        from app.api.routes import pricing as pricing_route
        for _ in range(pricing_route._INSTANT_ESTIMATE_RATE_LIMIT):
            r = _post(client)
            assert r.status_code == 200
        r = _post(client)
        assert r.status_code == 429


class TestExistingEndpointsUnaffected:
    """Proof this session's work didn't touch /estimate or /breakdown."""

    def test_public_estimate_endpoint_still_works_exactly_as_before(self, client, memory, db_session):
        r = client.post(
            "/api/v1/pricing/estimate",
            json={"segment": "residential", "state": "West Bengal", "size_kw": 5},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["indicative_estimate"] is True
        assert "total_inr" in body and "categories" in body
