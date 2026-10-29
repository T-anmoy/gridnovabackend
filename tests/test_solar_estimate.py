"""BE-14 — the solar estimate engine (app/estimate/): one calculation of size, generation,
subsidy, savings and payback for every caller. config/solar_estimate.yaml and
config/subsidies.yaml; contract docs/api/solar_estimate.md.

Golden cases are the brief's, with the hand arithmetic in the comments. Tariff slabs in
these tests are SYNTHETIC (defined here only, never in config): no real tariff order or
bill was supplied, so config holds none."""
from __future__ import annotations

import ast
import json
import re
from datetime import date
from decimal import Decimal, FloatOperation, localcontext
from pathlib import Path

import pytest
import yaml

from app.estimate import config, engine, subsidy, tariff
from app.placeholders import registry

D = Decimal
ON = date(2026, 10, 25)
ROOT = Path(__file__).resolve().parents[1]
INSTANT = "/api/v1/pricing/instant-estimate"


def _subsidy(kw, state="West Bengal", segment="residential", on=ON, **kw_args):
    return subsidy.calculate(kw=D(kw), state=state, segment=segment, on=on, **kw_args)


def _inputs(**kw):
    kw.setdefault("state", "West Bengal")
    for key in ("monthly_units_kwh", "monthly_bill_inr", "usable_roof_area_sqft", "requested_kw",
                "sanctioned_load_kw"):
        if key in kw and kw[key] is not None:
            kw[key] = D(str(kw[key]))
    return engine.EstimateInputs(on=ON, **kw)


def _post(client, **body):
    base = {"segment": "residential", "state": "West Bengal", "sizing_method": "by_units",
            "connection_phase": "single_phase"}
    from app.api.routes import pricing
    pricing._instant_estimate_request_log.clear()
    return client.post(INSTANT, json={k: v for k, v in {**base, **body}.items() if v is not None})


# A synthetic DISCOM table (test only): 0–100 units ₹5, 101–300 ₹7, above ₹9; ₹50 a month
# fixed; 10 % duty on the energy charge.
SYNTHETIC = {"slabs": [{"up_to_units": 100, "rate": "5"}, {"up_to_units": 300, "rate": "7"},
                       {"up_to_units": None, "rate": "9"}],
             "fixed_charge": {"type": "flat_per_month", "value": "50"},
             "electricity_duty": {"type": "percent_of_energy", "value": "10"},
             "effective_from": date(2026, 4, 1), "status": "confirmed", "source": "synthetic test table",
             "as_of": date(2026, 10, 25)}


def _table(**overrides):
    return config.parse_tariff_table("TESTCO", "domestic", {**SYNTHETIC, **overrides})


@pytest.fixture
def synthetic_wbsedcl(monkeypatch):
    """Put the synthetic table in place of WBSEDCL's (null) one, for this test only."""
    cats = config.estimate_config()["tariffs"]["discoms"]["WBSEDCL"]["categories"]
    monkeypatch.setitem(cats, "domestic", config.parse_tariff_table("WBSEDCL", "domestic", SYNTHETIC))
    return cats["domestic"]


# ── Subsidy golden cases ─────────────────────────────────────────────────────────

class TestSubsidyGolden:
    def test_one_kw_west_bengal(self):
        # 1 kWp × 30,000 = 30,000 central (confirmed); WB announced 1 kW step = 5,000 (not deducted).
        r = _subsidy("1")
        assert r.central["category"] == "general" and r.central["total"] == D("30000.00")
        assert [(t["state"], t["amount"], t["status"]) for t in r.state] == [("West Bengal", D("5000.00"), "announced")]
        assert (r.confirmed_total, r.announced_total) == (D("30000.00"), D("5000.00"))

    @pytest.mark.parametrize("kw,total", [
        ("2.5", "69000.00"),   # 2 × 30,000 + 0.5 × 18,000
        ("1.5", "45000.00"),   # 1.5 × 30,000
        ("6", "78000.00"),     # 2 × 30,000 + 1 × 18,000; nothing above 3 kWp
        ("3", "78000.00"),
        ("0.5", "15000.00"),
    ])
    def test_general_pro_rata_and_cap(self, kw, total):
        assert _subsidy(kw, state="Odisha").central["total"] == D(total)

    def test_special_category(self):
        # Assam 3 kWp: 2 × 33,000 + 1 × 19,800 = 85,800 (the special maximum).
        assert _subsidy("3", state="Assam").central == {**_subsidy("3", state="Assam").central,
                                                        "category": "special", "total": D("85800.00")}
        # Uttarakhand 2.5 kWp: 2 × 33,000 + 0.5 × 19,800 = 66,000 + 9,900 = 75,900.
        assert _subsidy("2.5", state="Uttarakhand").central["total"] == D("75900.00")
        assert _subsidy("3", state="Jammu and Kashmir").central["category"] == "special"
        assert _subsidy("3", state="andaman & nicobar islands").central["category"] == "special"

    def test_bihar_central_plus_announced_state(self):
        # 3 kW: central 78,000; Bihar min(3 × 10,000, 20,000) = 20,000 announced → 98,000 if received.
        r = _subsidy("3", state="Bihar")
        assert (r.confirmed_total, r.announced_total) == (D("78000.00"), D("20000.00"))
        assert r.state[0]["valid_until"] == date(2027, 8, 13)
        assert _subsidy("1", state="Bihar").state[0]["amount"] == D("10000.00")   # 1 × 10,000

    def test_bihar_top_up_only_inside_its_dates(self):
        assert _subsidy("3", state="Bihar", on=date(2026, 8, 13)).state == []
        assert _subsidy("3", state="Bihar", on=date(2027, 8, 14)).state == []
        assert _subsidy("3", state="Bihar", on=date(2027, 8, 13)).state

    def test_society(self):
        # 100 kWp, 20 houses: min(100, 3 × 20 = 60, 500) = 60 × 18,000 = 10,80,000.
        r = _subsidy("100", is_housing_society=True, number_of_houses=20)
        assert r.society["eligible_kwp"] == D(60) and r.society["limited_by"] == "houses"
        assert r.confirmed_total == D("1080000.00") and r.central is None and r.state == []
        # 700 kWp, 200 houses: min(700, 600, 500) = 500 × 18,000 = 90,00,000.
        r = _subsidy("700", is_housing_society=True, number_of_houses=200)
        assert r.society["limited_by"] == "max_kwp" and r.confirmed_total == D("9000000.00")
        # Special-category society rate 19,800: 10 kWp, 5 houses → 10 × 19,800.
        assert _subsidy("10", state="Sikkim", is_housing_society=True, number_of_houses=5).confirmed_total \
            == D("198000.00")

    def test_society_without_houses_is_never_zero(self):
        r = _subsidy("10", is_housing_society=True)
        assert r.status == "needs_number_of_houses" and r.society is None and r.confirmed_total == 0

    @pytest.mark.parametrize("segment", ["commercial", "industrial", "government"])
    def test_non_residential_gets_nothing_with_a_reason(self, segment):
        r = _subsidy("10", segment=segment)
        assert (r.status, r.reason, r.confirmed_total) == ("not_eligible", "segment_not_eligible", 0)

    def test_off_grid_gets_nothing_hybrid_stays_eligible(self):
        assert _subsidy("3", connection_type="off_grid").reason == "off_grid_not_eligible"
        assert _subsidy("3", connection_type="hybrid").confirmed_total == D("78000.00")

    def test_module_dc_basis(self, monkeypatch):
        cen = config.subsidy_config()["central"]
        basis = cen["cfa_capacity_basis"]
        monkeypatch.setitem(cen, "cfa_capacity_basis", config.Value("module_dc", basis.unit, basis.status,
                                                                    basis.source, basis.as_of))
        # 1 kW of 550 Wp: ceil(1000 / 550) = 2 modules = 1.1 kWp → 1.1 × 30,000 = 33,000.
        r = _subsidy("1", module_wattage_wp=550)
        assert r.central["capacity_kwp"] == D("1.1") and r.central["basis"] == "module_dc"
        assert r.central["total"] == D("33000.00")
        # 3 kW: ceil(3000 / 550) = 6 → 3.3 kWp → 60,000 + 18,000 = 78,000 (the cap).
        assert _subsidy("3", module_wattage_wp=550).central["total"] == D("78000.00")
        # No wattage → nominal, said so.
        r = _subsidy("1")
        assert r.central["basis"] == "nominal" and r.notes == ["module_wattage_unknown_nominal_used"]

    def test_west_bengal_steps_and_never_asks_caste(self):
        assert [_subsidy(kw).announced_total for kw in ("0.5", "1", "1.5", "2", "2.5", "3", "5")] == [
            D("0.00"), D("5000.00"), D("5000.00"), D("10000.00"), D("10000.00"), D("15000.00"), D("15000.00")]
        assert "SC/ST" in _subsidy("3").state[0]["note"]
        assert "caste" not in json.dumps(engine.public(_subsidy("3").public())).lower()


# ── Sizing and generation golden cases ───────────────────────────────────────────

class TestSizingGolden:
    def test_three_sixty_units(self):
        # exact = 360 × 12 × 0.9 / 1,440 = 3,888 / 1,440 = 2.70 kW → ceil 3, floor 2.
        inp = _inputs(monthly_units_kwh=360)
        sized = engine.size(inp, engine.consumption(inp))
        assert sized.exact_kw == D("2.70") and engine.whole_kw(sized.exact_kw) == (3, 2)
        # Coverage: 3 kW → 360 / 360 = 100.0 %; 2 kW → 240 / 360 = 66.7 %.
        assert engine.coverage_percent(engine.monthly_generation(D(3), "West Bengal"), D(360)) == D("100.0")
        assert engine.coverage_percent(engine.monthly_generation(D(2), "West Bengal"), D(360)) == D("66.7")

    def test_three_kw_generation_matches_the_proposal_sample(self):
        assert engine.monthly_generation(D(3), "West Bengal") == D(360)       # 3 × 120
        assert engine.monthly_generation(D(3), "Bihar") * 12 == D(4320)

    def test_roof_area(self):
        inp = _inputs(usable_roof_area_sqft=300)                               # 300 / 100
        sized = engine.size(inp, engine.consumption(inp))
        assert (sized.exact_kw, sized.sizing_basis, sized.roof_limited) == (D("3.00"), "roof_area", False)
        # Consumption 5 kW (5 × 120 / 0.9 = 666.67 units) and a 300 sq ft roof → 3.0 kW, roof-limited.
        inp = _inputs(monthly_units_kwh="666.67", usable_roof_area_sqft=300)
        sized = engine.size(inp, engine.consumption(inp))
        assert sized.exact_kw == D("3.00") and sized.roof_limited is True and sized.sizing_basis == "roof_area"

    def test_api_size_options_and_coverage(self, client):
        body = _post(client, monthly_units_kwh=360).json()
        assert (body["recommended_kw_exact"], body["priced_kw"]) == (2.7, 3)
        assert [(o["kw"], o["role"], o["coverage_percent"]) for o in body["size_options"]] == [
            (3, "priced", "100.0"), (2, "floor", "66.7")]
        assert body["coverage_percent"] == "100.0" and body["generation"]["monthly_units"] == "360"

    def test_four_ten_units_shows_both_sizes(self, client):
        # 410 × 0.9 / 120 = 3.075 → 3.08 → 4 kW (480 / 410 = 117.1 %) and 3 kW (360 / 410 = 87.8 %).
        body = _post(client, monthly_units_kwh=410).json()
        assert [(o["kw"], o["coverage_percent"]) for o in body["size_options"]] == [(4, "117.1"), (3, "87.8")]
        floor = body["size_options"][1]
        assert floor["packages"]["NOVA_ASSURED"]["final"] == "201465.00"

    def test_no_floor_option_below_the_phase_minimum(self, client):
        body = _post(client, connection_phase="three_phase", monthly_units_kwh=360).json()   # 2.7 → 3, floor 2 < 3
        assert body["priced_kw"] == 3 and [o["kw"] for o in body["size_options"]] == [3]

    def test_above_sanctioned_load_warns_never_caps(self, client):
        body = _post(client, monthly_units_kwh=600, sanctioned_load_kw=3).json()   # 4.5 → 5 kW
        assert body["priced_kw"] == 5 and "above_sanctioned_load" in body["warnings"]
        assert body["warning_details"]["above_sanctioned_load"] == {"sanctioned_load_kw": "3", "exact_kw": "4.50",
                                                                     "priced_kw": 5}
        assert "above_sanctioned_load" not in _post(client, monthly_units_kwh=600, sanctioned_load_kw=5).json()["warnings"]

    def test_unconfirmed_generation_outside_wb_and_bihar(self, client):
        body = _post(client, state="Odisha", monthly_units_kwh=360).json()
        assert body["generation"]["status"] == "unconfirmed" and "generation_unconfirmed_for_state" in body["warnings"]
        assert body["generation"]["units_per_kwp_per_month"] == "120"


class TestPriceGolden:
    def test_three_kw_assured_less_central(self, client):
        # 3 kW single phase Nova Assured: 1,85,000 + 8.9 % GST 16,465 = 2,01,465; less 78,000 = 1,23,465.
        body = _post(client, monthly_units_kwh=400).json()   # 400 × 0.9 / 120 = 3.0
        assert body["packages"]["NOVA_ASSURED"]["final"] == "201465.00"
        assert body["packages"]["NOVA_ASSURED"]["net_after_subsidy"] == "123465.00"
        assert body["net_cost"] == 123465.0 and body["subsidy_inr"] == 78000.0

    def test_bihar_net_cost_if_announced_support_received(self, client):
        body = _post(client, state="Bihar", monthly_units_kwh=400).json()
        # 2,01,465 − 78,000 = 1,23,465 (deducted); − 20,000 announced = 1,03,465 (shown separately).
        assert body["net_cost"] == 123465.0 and body["net_cost_if_announced_support_received"] == "103465.00"
        assert body["packages"]["NOVA_ASSURED"]["net_if_announced_support_received"] == "103465.00"
        assert [(s["state"], s["amount"], s["status"]) for s in body["announced_state_support"]] == [
            ("Bihar", "20000.00", "announced")]
        assert body["subsidy"]["confirmed_total"] == "78000.00" and body["subsidy"]["announced_total"] == "20000.00"


# ── Tariffs and savings ──────────────────────────────────────────────────────────

class TestTariffModel:
    def test_slab_bill(self):
        model = tariff.BillModel("tariff_table", table=_table())
        # 250 units: 100 × 5 + 150 × 7 = 1,550 energy; duty 155; fixed 50 → 1,755.00.
        assert tariff.bill(model, D(250)) == D("1755.00")
        # 0 units: the fixed charge only.
        assert tariff.bill(model, D(0)) == D("50.00")
        # 400 units: 500 + 1,400 + 900 = 2,800; duty 280; + 50 = 3,130.00.
        assert tariff.bill(model, D(400)) == D("3130.00")

    @pytest.mark.parametrize("units", ["37", "100", "250", "300", "301", "999.5"])
    def test_inversion_returns_the_units(self, units):
        model = tariff.BillModel("tariff_table", table=_table())
        solved, why = tariff.units_from_bill(model, tariff.bill(model, D(units)))
        assert why is None and abs(solved - D(units)) < D("0.01")

    def test_inversion_reasons(self):
        model = tariff.BillModel("tariff_table", table=_table())
        assert tariff.units_from_bill(model, D("49")) == (None, "bill_below_fixed_charges")
        free = tariff.BillModel("tariff_table", free_units=D(125), table=_table())
        assert tariff.units_from_bill(free, D("50")) == (None, "bill_within_free_units")
        per_kw = tariff.BillModel("tariff_table", table=_table(fixed_charge={"type": "per_kw_per_month",
                                                                             "value": "20"}))
        assert tariff.units_from_bill(per_kw, D("500")) == (None, "fixed_charge_needs_sanctioned_load")

    def test_free_units_waive_the_first_units_charges(self):
        free = tariff.BillModel("tariff_table", free_units=D(125), table=_table())
        # 200 units, first 125 free: E(200) − E(125) = (500 + 700) − (500 + 175) = 525; duty 52.50; + 50.
        assert tariff.bill(free, D(200)) == D("627.50")
        assert tariff.bill(free, D(100)) == D("50.00")
        flat = tariff.BillModel("flat_rate", free_units=D(125), rate=D(9))
        assert tariff.bill(flat, D(200)) == D("675.00")                      # 75 × 9
        assert tariff.units_from_bill(flat, D("900")) == (D(225), None)      # 125 + 900 / 9

    def test_marginal_saving_keeps_the_fixed_charge(self):
        model = tariff.BillModel("tariff_table", table=_table())
        # 400 units, 240 solar: bill(400) 3,130.00 − bill(160) (500 + 420 = 920; 92; 50 → 1,062.00) = 2,068.00.
        assert tariff.marginal_saving(model, D(400), D(240)) == D("2068.00")
        # An unknown per-kW fixed charge cancels out.
        per_kw = tariff.BillModel("tariff_table", table=_table(fixed_charge={"type": "per_kw_per_month",
                                                                             "value": "20"}))
        assert tariff.marginal_saving(per_kw, D(400), D(240)) == D("2068.00")


class TestSavings:
    def test_units_only_flat_rate_is_low_confidence(self, client):
        # 360 units, 3 kW makes 360: saving 360 × ₹9 = 3,240 a month, 38,880 a year;
        # payback 1,23,465 / 38,880 = 3.175… → 3.2 years.
        body = _post(client, monthly_units_kwh=360).json()
        s = body["savings"]
        assert (s["monthly"], s["annual"], s["method"], s["tariff_source"]) == ("3240.00", "38880.00", "marginal",
                                                                               "flat_rate")
        assert (s["monthly_bill_before"], s["monthly_bill_after"]) == ("3240.00", "0.00")
        assert body["confidence"] == "low" and body["payback_years"] == "3.2"
        assert body["consumption_basis"] == "monthly_units"

    def test_units_and_bill_use_the_customers_rate(self, client):
        # 400 units, ₹3,200 → ₹8.00/unit; 3 kW (360): 3,200.00 − 40 × 8 = 2,880.00.
        body = _post(client, monthly_units_kwh=400, monthly_bill_inr=3200).json()
        assert body["consumption_basis"] == "monthly_units_and_bill" and body["confidence"] == "medium"
        assert body["savings"]["rate_inr_per_kwh"] == "8.00" and body["savings"]["monthly"] == "2880.00"

    def test_an_implausible_bill_rate_falls_back_with_a_warning(self, client):
        body = _post(client, monthly_units_kwh=400, monthly_bill_inr=40).json()   # ₹0.10 / unit
        assert "implied_tariff_outside_range" in body["warnings"]
        assert body["savings"]["tariff_source"] == "flat_rate" and body["consumption_basis"] == "monthly_units"

    def test_bill_only_flat_rate(self, client):
        # ₹5,000 / ₹9 = 555.56 units → × 0.9 / 120 = 4.17 kW → 5 kW.
        body = _post(client, sizing_method="by_bill", monthly_bill_inr=5000).json()
        assert body["consumption_basis"] == "monthly_bill_flat_rate" and body["monthly_units_used"] == "555.56"
        assert body["recommended_kw_exact"] == 4.17 and body["confidence"] == "low"

    def test_roof_area_only_has_generation_no_savings(self, client):
        body = _post(client, sizing_method="by_roof_area", usable_roof_area_sqft=300).json()
        assert body["savings"] is None and body["payback_years"] is None and body["confidence"] == "low"
        assert body["generation"]["monthly_units"] == "360" and body["long_term"]["total_savings"] is None

    def test_bihar_free_units(self, client):
        # 300 units, 125 free, flat ₹9: before 175 × 9 = 1,575; 2 kW makes 240 → 60 units left, all
        # free → after 0; saving 1,575.00 (not 240 × 9 = 2,160).
        inp = _inputs(state="Bihar", monthly_units_kwh=300)
        cons = engine.consumption(inp)
        assert cons.model.free_units == D(125)
        assert engine.savings_block(D(2), inp, cons)["monthly"] == D("1575.00")
        # Bill only, ₹900: 125 + 900 / 9 = 225 units.
        body = _post(client, state="Bihar", sizing_method="by_bill", monthly_bill_inr=900).json()
        assert body["monthly_units_used"] == "225.00"
        assert any(a["name"] == "free_units_per_month" and a["status"] == "announced" for a in body["assumptions"])

    def test_a_confirmed_tariff_table_is_used_and_high_confidence(self, client, synthetic_wbsedcl):
        # 400 units on the synthetic table, 3 kW = 360: bill(400) 3,130.00 − bill(40) (200; 20; 50 → 270.00).
        body = _post(client, monthly_units_kwh=400, discom="WBSEDCL").json()
        assert body["savings"]["tariff_source"] == "discom_tariff" and body["confidence"] == "high"
        assert body["savings"]["monthly"] == "2860.00" and body["savings"]["monthly_bill_before"] == "3130.00"
        # Bill only through the table: ₹3,130 → 400 units.
        body = _post(client, sizing_method="by_bill", monthly_bill_inr=3130, discom="WBSEDCL").json()
        assert body["consumption_basis"] == "monthly_bill_tariff_table" and body["monthly_units_used"] == "400.00"
        assert body["confidence"] == "medium"

    def test_an_unconfirmed_table_is_never_used(self, client):
        body = _post(client, monthly_units_kwh=400, discom="WBSEDCL").json()
        assert body["savings"]["tariff_source"] == "flat_rate" and "tariff_table_not_available" in body["warnings"]

    def test_no_export_credit_by_default(self):
        # 100 units, 3 kW makes 360: saving is the whole bill (900), never the 260 surplus.
        inp = _inputs(monthly_units_kwh=100)
        assert engine.savings_block(D(3), inp, engine.consumption(inp))["monthly"] == D("900.00")

    def test_long_term(self):
        # 3 kW, 360 units at ₹9: year 1 = 4,320 units; year n ≥ 2 = 4,320 × (0.98 − (n − 2) × 0.0055),
        # whole units each year. Generation never exceeds use, so each year saves 9 × its units.
        inp = _inputs(monthly_units_kwh=360)
        lt = engine.long_term_block(D(3), inp, engine.consumption(inp))
        rows = lt["by_year"]
        assert (rows[0]["generation_units"], rows[0]["savings"], rows[0]["output_percent"]) == (D(4320),
                                                                                                D("38880.00"),
                                                                                                D("100.00"))
        assert rows[1]["output_percent"] == D("98.00") and rows[1]["generation_units"] == D(4234)   # 4,233.6
        assert rows[24]["output_percent"] == D("85.35") and rows[24]["generation_units"] == D(3687)  # 3,687.12
        assert lt["total_generation"] == sum(r["generation_units"] for r in rows) == D(99369)
        assert lt["total_savings"] == D("894321.00") == D(99369) * 9
        assert lt["assumptions"]["tariff_escalation_percent_per_year"] == 0


# ── API contract ─────────────────────────────────────────────────────────────────

PRE_BE14_KEYS = {"recommended_size_kw", "sizing_detail", "base_cost", "adders", "component_breakdown",
                 "subtotal_before_tax", "gst_amount", "subsidy_inr", "net_cost", "is_indicative_range",
                 "net_cost_range_min", "net_cost_range_max", "note", "price_status", "custom_quote_reasons",
                 "recommended_kw_exact", "priced_kw", "priced_kw_differs", "phase", "packages", "gst_rule",
                 "price_list_version", "destination", "subsidy_conditions"}


class TestApi:
    def test_every_old_field_is_kept(self, client):
        body = _post(client, monthly_units_kwh=600).json()
        assert PRE_BE14_KEYS <= set(body)
        assert (body["recommended_size_kw"], body["recommended_kw_exact"], body["priced_kw"]) == (4.5, 4.5, 5)
        assert body["sizing_detail"] == {"consumption_based_kw": 4.5, "area_based_kw": None, "phase_capped": False}
        assert body["estimate_version"] == config.version()

    def test_assumptions_list_every_value_used(self, client):
        body = _post(client, monthly_units_kwh=600, usable_roof_area_sqft=1000).json()
        names = {a["name"] for a in body["assumptions"]}
        assert {"units_per_kwp_per_month", "target_offset_percent", "sqft_per_kwp", "flat_rate_inr_per_kwh",
                "degradation_first_year_percent", "degradation_annual_percent",
                "tariff_escalation_percent_per_year", "long_term_years", "export_credit", "central_cfa",
                "cfa_capacity_basis"} <= names
        assert all(a["status"] for a in body["assumptions"])

    @pytest.mark.parametrize("field,value", [
        ("monthly_units_kwh", 0.5), ("monthly_units_kwh", 100_001), ("monthly_bill_inr", 0.5),
        ("monthly_bill_inr", 5_000_001), ("number_of_houses", 0), ("number_of_houses", 5_001),
        ("module_wattage_wp", 50), ("sanctioned_load_kw", 0), ("discom", "x" * 61)])
    def test_input_bounds(self, client, field, value):
        assert _post(client, **{"monthly_units_kwh": 360, field: value}).status_code == 422

    def test_assumptions_endpoint(self, client):
        from app.api.routes import pricing
        pricing._assumptions_request_log.clear()
        r = client.get("/api/v1/pricing/estimate-assumptions")
        assert r.status_code == 200 and "max-age" in r.headers["cache-control"]
        body = r.json()
        assert body["estimate_version"] == config.version()
        assert body["subsidies"]["central"]["residential"]["special"]["max_inr"] == "85800"
        assert {t["state"]: t["status"] for t in body["subsidies"]["state_top_ups"]} == {
            "Bihar": "announced", "West Bengal": "announced"}
        assert all(t["available"] is False and t["slabs"] is None for t in body["tariffs"]["discoms"])
        assert "Only confirmed subsidies are deducted" in body["subsidies"]["deduction_rule"]
        codes = [client.get("/api/v1/pricing/estimate-assumptions").status_code for _ in range(20)]
        assert codes[-1] == 429
        pricing._assumptions_request_log.clear()

    def test_estimate_route_uses_the_engine(self, client):
        from app.api.routes import pricing
        pricing._estimate_request_log.clear()
        body = client.post("/api/v1/pricing/estimate", json={"segment": "residential", "state": "Bihar",
                                                             "monthly_bill_inr": 900,
                                                             "connection_phase": "single_phase"}).json()
        # 125 + 900 / 9 = 225 units → × 0.9 / 120 = 1.6875 → 1.69 kW → 2 kW.
        assert (body["recommended_kw_exact"], body["priced_kw"], body["sizing_basis"]) == (
            1.69, 2, "monthly_bill_flat_rate")


# ── Proposal ─────────────────────────────────────────────────────────────────────

class TestProposalFigures:
    def test_special_category_slabs_on_a_proposal(self):
        from app.proposals import document as d
        from tests.proposal_document_support import compact_document
        doc = compact_document()
        doc.site.state = "Assam"
        f = d.figures(doc)
        assert [(s["kw"], s["rate"], s["amount"]) for s in f["subsidy"]["slabs"]] == [
            ("2", "33000.00", "66000.00"), ("1", "19800.00", "19800.00")]
        assert f["subsidy"]["total"] == "85800.00" and f["subsidy"]["state_lines"] == []

    def test_announced_top_ups_are_never_printed_confirmed_only_with_the_flag(self, monkeypatch):
        from app.proposals import document as d
        from tests.proposal_document_support import compact_document
        doc = compact_document()
        prop = config.subsidy_config()["proposal"]
        monkeypatch.setitem(prop, "print_confirmed_state_top_up", True)
        assert d.figures(doc)["subsidy"]["state_lines"] == []          # West Bengal's is only announced
        tops = config.subsidy_config()["state_top_ups"]
        confirmed = ({**tops["west_bengal"][0], "status": "confirmed"},)
        monkeypatch.setitem(tops, "west_bengal", confirmed)
        f = d.figures(doc)
        assert f["subsidy"]["state_lines"] == [{"state": "West Bengal", "amount": "15000.00"}]
        assert (f["subsidy"]["total"], f["subsidy"]["effective_cost"]) == ("93000.00", "108465.00")
        monkeypatch.setitem(prop, "print_confirmed_state_top_up", False)
        assert d.figures(doc)["subsidy"]["total"] == "78000.00"


# ── Config, registry and code guards ─────────────────────────────────────────────

def _walk(node, path=""):
    if isinstance(node, dict):
        if "status" in node and "source" in node:
            yield path, node
        for key, value in node.items():
            yield from _walk(value, f"{path}.{key}" if path else str(key))
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from _walk(value, f"{path}[{i}]")


class TestConfig:
    def test_both_files_load(self):
        config.clear_caches()
        config.load()
        assert re.fullmatch(r"1\.1\+[0-9a-f]{12}", config.version())

    @pytest.mark.parametrize("path", ["config/solar_estimate.yaml", "config/subsidies.yaml"])
    def test_every_value_not_confirmed_has_a_marker_and_a_registry_entry(self, path):
        text = (ROOT / path).read_text(encoding="utf-8")
        data = yaml.safe_load(text)
        anchors = registry.yaml_line_anchors(text)
        lines = text.splitlines()
        entries = [e for e in registry.load().entries if e.file == path]
        nodes = [(re.sub(r"\[\d+\]", "", where), where, node) for where, node in _walk(data)]
        for dotted, where, node in nodes:
            assert node["status"] in config.STATUSES, where
            # This node's own lines: under its key, but not under a deeper node with its own status.
            deeper = [p for p, _, _ in nodes if p.startswith(dotted + ".")]
            marked = any(registry.MARKERS.search(lines[i]) for i, a in enumerate(anchors)
                         if (a == dotted or a.startswith(dotted + "."))
                         and not any(a == p or a.startswith(p + ".") for p in deeper))
            if node["status"] == "confirmed":
                assert not marked, f"{where} is confirmed but still carries a marker"
                continue
            assert marked, f"{where} is {node['status']} but carries no marker"
            assert any(registry.claims(e, registry.MarkerHit(path, 0, dotted, "")) and e.status == "open"
                       for e in entries), f"{where}: no open registry entry"

    def _load_with(self, monkeypatch, path, mutate):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        mutate(data)
        validate = config._validate_estimate if path == config.ESTIMATE_PATH else config._validate_subsidies
        return lambda: validate(data)

    @pytest.mark.parametrize("mutate,message", [
        (lambda d: d["generation"]["degradation"]["annual_percent"].update(value=0.55), "decimal written as text"),
        (lambda d: d["generation"]["units_per_kwp_per_month"]["states"]["bihar"].pop("status"), "status"),
        (lambda d: d["generation"]["units_per_kwp_per_month"]["states"]["bihar"].update(value="117.5"), "whole"),
        (lambda d: d["roof_area"]["sqft_per_kwp_low"].update(value=120), "sqft_per_kwp_low"),
        (lambda d: d["tariffs"]["discoms"]["WBSEDCL"]["categories"]["domestic"].update(status="confirmed"),
         "without slabs"),
        (lambda d: d["tariffs"]["discoms"]["CESC"]["categories"]["domestic"].update(
            slabs=[{"up_to_units": 300, "rate": "7"}, {"up_to_units": 100, "rate": "5"},
                   {"up_to_units": None, "rate": "9"}]), "ascending"),
        (lambda d: d["tariffs"]["discoms"]["CESC"]["categories"]["domestic"].update(
            slabs=[{"up_to_units": 100, "rate": "5"}]), "last slab must be open"),
        (lambda d: d["savings"]["export_credit"]["fallback"].update(rule="full_net_metering"), "rule"),
        (lambda d: d["tariffs"]["implied_rate_sanity_inr_per_kwh"].update(min=30), "min < max"),
    ])
    def test_estimate_config_is_validated(self, monkeypatch, mutate, message):
        with pytest.raises(config.EstimateConfigError, match=message):
            self._load_with(monkeypatch, config.ESTIMATE_PATH, mutate)()

    @pytest.mark.parametrize("mutate,message", [
        (lambda d: d["central"]["residential"]["general"].update(max_inr=80000), "must equal the slabs"),
        (lambda d: d["central"]["residential"]["special"]["slabs"].reverse(), "ascending"),
        (lambda d: d["central"]["cfa_capacity_basis"].update(value="inverter"), "cfa_capacity_basis"),
        (lambda d: d["state_top_ups"]["bihar"][0].update(valid_until=date(2026, 1, 1)), "valid_until"),
        (lambda d: d["state_top_ups"]["bihar"][0]["amount_rule"].update(type="lump_sum"), "amount_rule.type"),
        (lambda d: d["state_top_ups"]["bihar"][0].update(state="West Bengal"), "state must name"),
        (lambda d: d["central"].pop("source"), "central.source"),
    ])
    def test_subsidy_config_is_validated(self, monkeypatch, mutate, message):
        with pytest.raises(config.EstimateConfigError, match=message):
            self._load_with(monkeypatch, config.SUBSIDIES_PATH, mutate)()

    def test_nothing_unconfirmed_is_ever_deducted(self, monkeypatch):
        tops = config.subsidy_config()["state_top_ups"]
        monkeypatch.setitem(tops, "bihar", ({**tops["bihar"][0], "status": "unconfirmed"},))
        r = _subsidy("3", state="Bihar")
        assert (r.confirmed_total, r.announced_total) == (D("78000.00"), D("0.00"))
        monkeypatch.setitem(tops, "bihar", ({**tops["bihar"][0], "status": "confirmed"},))
        assert _subsidy("3", state="Bihar").confirmed_total == D("98000.00")


class TestCodeGuards:
    ENGINE_DIR = ROOT / "app" / "estimate"

    def test_no_float_in_the_calculation_path(self):
        """No float literal, float() call or math module in app/estimate/."""
        for path in sorted(self.ENGINE_DIR.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                assert not (isinstance(node, ast.Constant) and isinstance(node.value, float)), (path.name, node.lineno)
                assert not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                            and node.func.id == "float"), (path.name, node.lineno)
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    names = [a.name for a in node.names] + [getattr(node, "module", None) or ""]
                    assert "math" not in names, path.name

    def test_no_float_reaches_a_decimal_operation(self):
        """With FloatOperation trapped, mixing a float into any Decimal raises: run the
        engine end to end on every input path."""
        with localcontext() as ctx:
            ctx.traps[FloatOperation] = True
            for inp in (_inputs(monthly_units_kwh=360), _inputs(monthly_units_kwh=400, monthly_bill_inr=3200),
                        _inputs(state="Bihar", monthly_bill_inr=900), _inputs(usable_roof_area_sqft=300),
                        _inputs(state="Assam", monthly_units_kwh=500, sanctioned_load_kw=3),
                        _inputs(is_housing_society=True, number_of_houses=20, monthly_units_kwh=9000)):
                cons = engine.consumption(inp)
                sized = engine.size(inp, cons)
                fig = engine.financials(sized.exact_kw, inp, cons, D("201465.00"))
                engine.public(fig)
                engine.assumptions(inp, cons, sized)
                engine.size_warnings(inp, sized, 3)

    def test_the_old_constants_are_gone_everywhere(self):
        """Roof area, generation, tariff and subsidy have exactly one source now."""
        banned = re.compile(r"PLACEHOLDER_ASSUMED_TARIFF_INR_PER_UNIT|PLACEHOLDER_ASSUMED_ANNUAL_YIELD_PER_KW|"
                            r"ROOF_AREA_SQFT_PER_KW|PLACEHOLDER_REGIONAL_GENERATION_FACTOR|"
                            r"PLACEHOLDER_DEFAULT_GENERATION_FACTOR|DEFAULT_TARGET_OFFSET_PERCENT|"
                            r"PLACEHOLDER_PHASE_CAP_KW|RATE_PER_KW_FIRST_2KW|RATE_PER_KW_THIRD_KW|MAX_SUBSIDY_INR|"
                            r"calculate_pm_surya_ghar_subsidy|estimate_size_from_(bill|units)|"
                            r"app\.pricing\.(sizing|subsidy)|from app\.pricing import (sizing|subsidy)")
        for folder in ("app", "scripts"):
            for path in (ROOT / folder).rglob("*.py"):
                if "__pycache__" in path.parts:
                    continue
                hit = banned.search(path.read_text(encoding="utf-8"))
                assert hit is None, f"{path.relative_to(ROOT)}: {hit.group(0)}"
        for name in ("site_survey.yaml", "proposal_document.yaml"):
            data = yaml.safe_load((ROOT / "config" / name).read_text(encoding="utf-8"))
            assert not {"area_sqft_per_kw", "implied_tariff_inr_per_kwh", "generation", "space_required"} & set(data)
        assert not (ROOT / "app" / "pricing" / "sizing.py").exists()
        assert not (ROOT / "app" / "pricing" / "subsidy.py").exists()

    def test_every_caller_uses_the_engine(self):
        for rel in ("app/pricing/instant_estimate.py", "app/api/routes/pricing.py", "app/survey/results.py",
                    "app/proposals/document.py", "app/llm/company_facts.py"):
            assert "app.estimate" in (ROOT / rel).read_text(encoding="utf-8"), rel
        # The associate estimate goes through the instant estimate.
        assert "instant_estimate.calculate_instant_estimate" in (ROOT / "app/onboarding/estimate.py").read_text(encoding="utf-8")
