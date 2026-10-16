"""Tests for the Pricing API (/api/v1/pricing) — the public /estimate endpoint (priced
from Grid Nova's price list since PM-A) and the Admin/Sales-only /breakdown endpoint
(the layered placeholder engine, internal only).
"""
import pytest

from tests.conftest import admin_headers, engineer_headers, management_headers, sales_headers


def _payload(**overrides) -> dict:
    payload = {"segment": "residential", "size_kw": 5, "state": "West Bengal", "roof_type": "RCC",
               "connection_phase": "single_phase"}
    payload.update(overrides)
    return payload


class TestPublicEstimate:
    def test_no_authentication_required(self, client):
        r = client.post("/api/v1/pricing/estimate", json=_payload())
        assert r.status_code == 200

    def test_response_shape(self, client):
        r = client.post("/api/v1/pricing/estimate", json=_payload())
        assert r.status_code == 200
        body = r.json()
        assert body["indicative_estimate"] is True
        # Nova Assured's final list price for 5 kW single phase.
        assert body["total_inr"] == 312543.0 and body["categories"] is None
        assert body["price_status"] == "list_price" and body["priced_kw"] == 5
        assert body["packages"]["NOVA_SIGNATURE"]["final"] == "350048.16"

    def test_size_is_rounded_up_to_a_whole_kw(self, client):
        body = client.post("/api/v1/pricing/estimate", json=_payload(size_kw=4.2)).json()
        assert (body["priced_kw"], body["priced_kw_differs"]) == (5, True)

    def test_outside_the_price_list_is_a_custom_quote(self, client):
        for overrides in ({"state": "Odisha"}, {"segment": "commercial"}, {"connection_phase": None},
                          {"size_kw": 12}):
            body = client.post("/api/v1/pricing/estimate",
                               json={k: v for k, v in _payload(**overrides).items() if v is not None}).json()
            assert body["total_inr"] is None and body["price_status"] == "custom_quote", overrides
            assert body["custom_quote_reasons"]

    def test_never_returns_the_itemized_breakdown(self, client):
        r = client.post("/api/v1/pricing/estimate", json=_payload())
        body = r.json()
        for key in (
            "base_rate_per_kw", "equipment_cost", "complexity_modifier",
            "gst_amount", "bos_addition", "civil_addition", "regulatory_addition",
        ):
            assert key not in body

    def test_sizing_by_monthly_bill_instead_of_size_kw(self, client):
        r = client.post(
            "/api/v1/pricing/estimate",
            json={"segment": "residential", "monthly_bill_inr": 4000, "state": "West Bengal",
                  "connection_phase": "single_phase"},
        )
        assert r.status_code == 200
        assert r.json()["total_inr"] > 0 and r.json()["price_status"] == "list_price"

    def test_missing_both_sizing_inputs_is_422(self, client):
        r = client.post(
            "/api/v1/pricing/estimate", json={"segment": "residential", "state": "West Bengal"},
        )
        assert r.status_code == 422

    def test_invalid_segment_is_422(self, client):
        r = client.post("/api/v1/pricing/estimate", json=_payload(segment="not_a_real_segment"))
        assert r.status_code == 422

    def test_negative_size_is_422(self, client):
        r = client.post("/api/v1/pricing/estimate", json=_payload(size_kw=-5))
        assert r.status_code == 422

    def test_missing_state_is_422(self, client):
        r = client.post(
            "/api/v1/pricing/estimate", json={"segment": "residential", "size_kw": 5},
        )
        assert r.status_code == 422

    def test_equipment_selection_is_not_an_accepted_public_field(self, client):
        """The public request model has no equipment_selection field at all —
        even if a client sends one, it's silently ignored by pydantic, not
        wired to anything that could leak procurement cost."""
        r = client.post(
            "/api/v1/pricing/estimate",
            json=_payload(equipment_selection=[1, 2, 3]),
        )
        assert r.status_code == 200
        assert "equipment_selection" not in r.json()

    def test_public_estimate_never_leaks_procurement_cost_even_with_equipment_selected(
        self, client, memory, db_session
    ):
        """The strongest version of the security check: create equipment with a
        real, distinctive procurement price, select it (via the fields the
        request model actually accepts — none exist on the public model, so
        this also proves the public endpoint has no way to be pointed at
        specific SKUs at all), and confirm the distinctive value never
        appears anywhere in the response, at any level."""
        from app.memory.models import EquipmentCategory

        distinctive_cost = 246_813.0
        item = memory.create_equipment(
            category=EquipmentCategory.module, brand="A", model_name="1",
            performed_by="test", procurement_price_inr=distinctive_cost,
        )
        db_session.commit()

        r = client.post(
            "/api/v1/pricing/estimate",
            json=_payload(equipment_selection=[item.id]),  # ignored by the public model
        )
        assert r.status_code == 200
        as_text = str(r.json())
        assert str(distinctive_cost) not in as_text
        assert "246813" not in as_text


class TestFullBreakdown:
    def test_requires_auth(self, client):
        r = client.post("/api/v1/pricing/breakdown", json=_payload())
        assert r.status_code == 401

    def test_admin_and_sales_allowed(self, client):
        for headers in (admin_headers(), sales_headers()):
            r = client.post("/api/v1/pricing/breakdown", json=_payload(), headers=headers)
            assert r.status_code == 200

    def test_engineer_and_management_forbidden(self, client):
        for headers in (engineer_headers(), management_headers()):
            r = client.post("/api/v1/pricing/breakdown", json=_payload(), headers=headers)
            assert r.status_code == 403

    def test_returns_the_full_itemized_breakdown(self, client):
        r = client.post("/api/v1/pricing/breakdown", json=_payload(), headers=admin_headers())
        assert r.status_code == 200
        body = r.json()
        for key in (
            "base_rate_per_kw", "size_band_modifier", "equipment_cost",
            "complexity_modifier", "location_modifier", "bos_addition",
            "civil_addition", "regulatory_addition", "optional_services_total",
            "gst_rate_percent", "gst_amount", "total_inr", "category_totals",
        ):
            assert key in body

    def test_equipment_selection_affects_the_breakdown(self, client, memory, db_session):
        from app.memory.models import EquipmentCategory

        item = memory.create_equipment(
            category=EquipmentCategory.module, brand="A", model_name="1",
            performed_by="test", procurement_price_inr=50_000.0,
        )
        db_session.commit()

        without = client.post("/api/v1/pricing/breakdown", json=_payload(), headers=admin_headers()).json()
        with_eq = client.post(
            "/api/v1/pricing/breakdown", json=_payload(equipment_selection=[item.id]), headers=admin_headers(),
        ).json()
        assert with_eq["equipment_cost"] > without["equipment_cost"]

    def test_breakdown_never_returns_procurement_price_directly(self, client, memory, db_session):
        """Even in the internal breakdown, only the derived selling-price
        figure appears — the raw procurement_price_inr value is never
        surfaced, matching calculate_equipment_cost()'s own guarantee."""
        from app.memory.models import EquipmentCategory

        distinctive_cost = 951_753.0
        item = memory.create_equipment(
            category=EquipmentCategory.module, brand="A", model_name="1",
            performed_by="test", procurement_price_inr=distinctive_cost,
        )
        db_session.commit()

        r = client.post(
            "/api/v1/pricing/breakdown", json=_payload(equipment_selection=[item.id]), headers=admin_headers(),
        )
        as_text = str(r.json())
        assert str(distinctive_cost) not in as_text
        assert "951753" not in as_text

    def test_invalid_segment_is_422(self, client):
        r = client.post(
            "/api/v1/pricing/breakdown", json=_payload(segment="nope"), headers=admin_headers(),
        )
        assert r.status_code == 422


class TestOneBillSizingRule:
    """BE-7 N6: the same bill and state give the same size through /estimate,
    /breakdown and /instant-estimate (app/pricing/sizing.py)."""

    @pytest.mark.parametrize("bill, state", [(5000, "West Bengal"), (4000, "Bihar"), (12000, "Odisha")])
    def test_estimate_and_instant_estimate_agree(self, client, bill, state):
        from app.pricing import sizing

        est = client.post("/api/v1/pricing/estimate",
                          json={"segment": "residential", "monthly_bill_inr": bill, "state": state,
                                "connection_phase": "single_phase"})
        inst = client.post("/api/v1/pricing/instant-estimate",
                           json={"segment": "residential", "state": state, "sizing_method": "by_bill",
                                 "monthly_bill_inr": bill, "connection_phase": "single_phase"})
        assert est.status_code == 200 and inst.status_code == 200, (est.text, inst.text)
        assert est.json()["recommended_kw_exact"] == inst.json()["recommended_kw_exact"]
        assert est.json()["priced_kw"] == inst.json()["priced_kw"]
        assert inst.json()["sizing_detail"]["consumption_based_kw"] == round(sizing.estimate_size_from_bill(bill, state), 2)
        assert est.json()["recommended_kw_exact"] == round(sizing.estimate_size_from_bill(bill, state), 2)

    def test_five_thousand_a_month_is_four_point_one_seven_kw(self, client):
        """₹5,000 ÷ ₹9/unit ÷ (30 × 4 kWh/kW/day) × 90% offset = 4.17 kW (was 4.57 kW
        through the removed annual-yield helper)."""
        body = client.post("/api/v1/pricing/estimate",
                           json={"segment": "residential", "monthly_bill_inr": 5000, "state": "West Bengal"}).json()
        assert body["recommended_kw_exact"] == 4.17

    def test_breakdown_sizes_the_bill_the_same_way(self, client, monkeypatch):
        from app.api.routes import pricing as pricing_route
        from app.pricing import engine

        seen = []
        real = engine.calculate_full_estimate
        monkeypatch.setattr(engine, "calculate_full_estimate",
                            lambda memory, inputs: seen.append(inputs.size_kw) or real(memory, inputs))
        assert pricing_route.BreakdownRequest(segment="residential", monthly_bill_inr=5000,
                                              state="West Bengal").resolved_size_kw() == 4.17
        r = client.post("/api/v1/pricing/breakdown", headers=admin_headers(),
                        json={"segment": "residential", "monthly_bill_inr": 5000, "state": "West Bengal"})
        assert r.status_code == 200, r.text
        assert seen == [4.17]

    def test_a_given_size_is_kept(self, client):
        body = client.post("/api/v1/pricing/estimate",
                           json={"segment": "residential", "size_kw": 3, "monthly_bill_inr": 5000,
                                 "state": "West Bengal", "connection_phase": "single_phase"}).json()
        assert body["priced_kw"] == 3
