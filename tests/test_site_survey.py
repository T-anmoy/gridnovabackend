"""S1 — the on-site site survey, Steps 1–4: survey fields on the onboarding write path
(validation, derived values), survey status and live results (associate and staff),
on-screen signatures and the submit gate. (The migration: tests/test_associate_migrations.py.)"""
import json

import pytest
from sqlalchemy import select

from app.memory.models import AuditLog, CustomerOnboarding, OnboardingStatus, ProposalInputs, TimelineEvent
from tests.onboarding_support import (  # noqa: F401 — fixtures
    ADMIN, ALIPURDUAR, BASE, DASH, ENG, KOLKATA, MGMT, PNG, SALES, SURVEY, complete_survey, confirm_site_location,
    headers, kyc_ready, make_associate, onboarding_body, onboarding_ready, sign_survey, territories,
    upload_required_documents,
)

OPS = {"X-Employee-Id": "emp-ops-1", "X-Employee-Role": "operations"}
ACCOUNTS = {"X-Employee-Id": "emp-acc-1", "X-Employee-Role": "accounts"}
MONTHS = [{"month": "2026-03", "units_kwh": 300, "bill_inr": 2400}, {"month": "2026-04", "units_kwh": 400,
                                                                       "bill_inr": 3300},
          {"month": "2026-05", "units_kwh": 500, "bill_inr": 4100.5}]


def _create(client, gna, **kw) -> int:
    r = client.post(BASE, json=onboarding_body(**kw), headers=headers(gna))
    assert r.status_code == 201, r.text
    return r.json()["onboarding_id"]


def _patch(client, gna, oid, body):
    return client.patch(f"{BASE}/{oid}", json=body, headers=headers(gna))


def _survey(client, gna, oid):
    return client.get(f"{BASE}/{oid}/survey", headers=headers(gna)).json()


def _results(client, gna, oid):
    r = client.get(f"{BASE}/{oid}/survey/results", headers=headers(gna))
    assert r.status_code == 200, r.text
    return r.json()


def _codes(results):
    return {w["code"]: w["params"] for w in results["warnings"]}


def _error(r):
    assert r.status_code == 422, r.text
    return r.json()["detail"]


# ── Step 2: write path ──────────────────────────────────────────────────────────

class TestWritePath:
    def test_create_with_survey_fields_and_derived_values(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        r = client.post(BASE, json=onboarding_body(
            survey_variant="housing_society", roof_material="concrete_flat", roof_obstacles=["trees", "water_tank"],
            roof_sections=[{"label": "A", "length_m": 10, "width_m": 6, "obstacle_area_sqm": 4},
                           {"label": "B", "length_m": "5.5", "width_m": 4}],
            distance_panel_to_inverter_m=12.345, consumption_history=MONTHS, avg_monthly_units_kwh=None,
            roofs_available_count=2, roofs_considered_count=1, society_flat_count=40,
            society_noc_status="in_progress"), headers=headers(gna))
        assert r.status_code == 201, r.text
        sa = r.json()["site_assessment"]
        assert sa["survey_variant"] == "housing_society"
        # (10×6 − 4) + 5.5×4 = 78 m² → 839.6 sq ft, derived by the server.
        assert sa["shadow_free_area_sqft"] == 839.6
        assert sa["roof_sections"][1] == {"label": "B", "length_m": "5.50", "width_m": "4.00",
                                          "obstacle_area_sqm": None}
        assert sa["roof_obstacles"] == ["water_tank", "trees"]          # canonical order, no duplicates
        assert sa["distance_panel_to_inverter_m"] == "12.35"            # metres as decimal strings
        assert sa["avg_monthly_units_kwh"] == 400.0 and sa["avg_monthly_bill_inr"] == 3266.83
        assert (sa["roofs_available_count"], sa["roofs_considered_count"]) == (2, 1)

    def test_an_explicit_average_is_kept(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        oid = _create(client, gna)
        sa = _patch(client, gna, oid, {"consumption_history": MONTHS, "avg_monthly_units_kwh": 420}).json()[
            "site_assessment"]
        assert sa["avg_monthly_units_kwh"] == 420 and sa["avg_monthly_bill_inr"] == 3266.83

    @pytest.mark.parametrize("months,code", [
        ([{"month": "2026-03", "units_kwh": 1}, {"month": "2026-03", "units_kwh": 2}], "duplicate_month"),
        ([{"month": "2026-3", "units_kwh": 1}], "invalid_month"),
        ([{"month": "2026-13", "units_kwh": 1}], "invalid_month"),
        ([{"month": "2099-01", "units_kwh": 1}], "future_month"),
        ([{"month": "2026-03", "bill_inr": 100}], "units_required"),
        ([{"month": f"2025-{m:02d}", "units_kwh": 1} for m in range(1, 13)] + [{"month": "2026-01", "units_kwh": 1}],
         "too_many_months"),
    ])
    def test_bad_bills_are_refused(self, client, db_session, onboarding_ready, months, code):
        gna = make_associate(db_session)
        oid = _create(client, gna)
        detail = _error(_patch(client, gna, oid, {"consumption_history": months}))
        assert (detail["code"], detail["field"]) == (code, "consumption_history")

    def test_negative_values_are_refused(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        oid = _create(client, gna)
        assert _patch(client, gna, oid, {"consumption_history": [{"month": "2026-03", "units_kwh": -1}]}
                      ).status_code == 422
        assert _patch(client, gna, oid, {"distance_method": "guess"}).status_code == 422
        assert _patch(client, gna, oid, {"distance_panel_to_inverter_m": -1}).status_code == 422
        assert _patch(client, gna, oid, {"roof_obstacles": ["dragon"]}).status_code == 422
        assert _patch(client, gna, oid, {"society_flat_count": 0}).status_code == 422
        assert _patch(client, gna, oid, {"roof_sections": [{"label": "x", "length_m": 0, "width_m": 1}]}
                      ).status_code == 422
        seven = [{"label": str(i), "length_m": 1, "width_m": 1} for i in range(7)]
        assert _patch(client, gna, oid, {"roof_sections": seven}).status_code == 422

    def test_shadow_free_area_is_derived_when_sections_exist(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        oid = _create(client, gna)
        detail = _error(_patch(client, gna, oid, {"roof_sections": [{"label": "A", "length_m": 2, "width_m": 2}],
                                                  "shadow_free_area_sqft": 10}))
        assert (detail["code"], detail["field"]) == ("derived_field", "shadow_free_area_sqft")
        assert _patch(client, gna, oid, {"shadow_free_area_sqft": 300}).json()["site_assessment"][
            "shadow_free_area_sqft"] == 300                       # no sections: entered directly
        _patch(client, gna, oid, {"roof_sections": [{"label": "A", "length_m": 2, "width_m": 2}]})
        assert _error(_patch(client, gna, oid, {"shadow_free_area_sqft": 10}))["code"] == "derived_field"
        sa = _patch(client, gna, oid, {"roof_sections": []}).json()["site_assessment"]
        assert sa["roof_sections"] is None and sa["shadow_free_area_sqft"] is None

    def test_cross_field_limits(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        oid = _create(client, gna)
        detail = _error(_patch(client, gna, oid, {"roof_sections": [
            {"label": "A", "length_m": 2, "width_m": 2, "obstacle_area_sqm": 5}]}))
        assert (detail["code"], detail["index"]) == ("obstacle_exceeds_section", 0)
        detail = _error(_patch(client, gna, oid, {"distance_inverter_to_lt_panel_m": 301}))
        assert (detail["code"], detail["max_m"]) == ("out_of_range", 300)
        _patch(client, gna, oid, {"roofs_available_count": 1})
        assert _error(_patch(client, gna, oid, {"roofs_considered_count": 2}))["code"] == \
            "roofs_considered_exceeds_available"

    def test_blank_text_becomes_null_and_changes_are_audited(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        oid = _create(client, gna)
        sa = _patch(client, gna, oid, {"roof_access_note": "  ", "distance_panel_to_inverter_m": 7}).json()[
            "site_assessment"]
        assert sa["roof_access_note"] is None
        audit = db_session.execute(select(AuditLog).where(AuditLog.entity_type == "customer_onboarding",
                                                          AuditLog.action == "updated")).scalars().all()[-1]
        assert audit.after_value["distance_panel_to_inverter_m"] == "7.00"
        assert audit.before_value["distance_panel_to_inverter_m"] is None

    def test_verified_customer_cannot_be_edited(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        oid = _create(client, gna)
        o = db_session.get(CustomerOnboarding, oid)
        o.status = OnboardingStatus.closed
        db_session.commit()
        assert _patch(client, gna, oid, {"roof_material": "tin_shed"}).status_code == 409


# ── Step 3: status and results ──────────────────────────────────────────────────

class TestStatus:
    def test_a_new_customer_lists_what_is_missing(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        oid = _create(client, gna)
        s = _survey(client, gna, oid)
        assert s["variant"] == "individual_house" and s["complete"] is False and s["required_to_submit"] is True
        sections = {x["key"]: x for x in s["sections"]}
        assert list(sections) == ["connection", "bills", "roof", "roof_area", "layout"]
        assert sections["connection"]["missing"] == ["discom", "connection_phase"]
        assert sections["bills"] == {"key": "bills", "complete": False, "missing": ["consumption_history"],
                                     "months_recorded": 0, "months_required": 3, "months_requested": 6}
        assert s["signatures"] == {"customer": {"signed": False, "signed_at": None},
                                   "associate": {"signed": False, "signed_at": None}}
        assert s["declaration"] == {"name": None, "declared_at": None}

    def test_complete_and_signed(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        oid = _create(client, gna)
        s = complete_survey(client, gna, oid)
        assert s["complete"] is True and all(x["complete"] for x in s["sections"])
        assert s["signatures"]["customer"]["signed"] and s["signatures"]["associate"]["signed"]
        assert s["declaration"]["name"] == "Customer" and s["declaration"]["declared_at"]

    def test_variant_and_conditional_fields_come_from_config(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        oid = _create(client, gna)
        complete_survey(client, gna, oid, sign=False)
        _patch(client, gna, oid, {"survey_variant": "housing_society", "roof_material": "other",
                                  "roof_ownership_confirmed": False})
        sections = {x["key"]: x for x in _survey(client, gna, oid)["sections"]}
        assert sections["connection"]["missing"] == ["society_flat_count", "society_noc_status"]
        assert sections["roof"]["missing"] == ["roof_material_other", "tenant_noc_available"]
        _patch(client, gna, oid, {"survey_variant": "commercial_roof", "roof_material_other": "RCC slab",
                                  "tenant_noc_available": True})
        sections = {x["key"]: x for x in _survey(client, gna, oid)["sections"]}
        assert sections["connection"]["missing"] == ["contract_demand_kva"] and sections["roof"]["complete"]

    def test_an_empty_obstacle_list_means_none(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        oid = _create(client, gna)
        complete_survey(client, gna, oid, sign=False)
        roof = next(x for x in _survey(client, gna, oid)["sections"] if x["key"] == "roof")
        assert roof["complete"] is True


class TestResults:
    def test_sizes_suggestion_and_estimate(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        oid = _create(client, gna)
        complete_survey(client, gna, oid, sign=False)
        r = _results(client, gna, oid)
        # 60 m² = 645.8 sq ft / 90 → 7.18 kW; load 5 × 1.0; usage 400 kWh → 3.0 kW (90 % offset).
        assert (r["max_by_area_kw"], r["max_by_load_kw"], r["recommended_by_usage_kw"]) == (7.18, 5.0, 3.0)
        assert (r["suggested_kw"], r["limiting_factor"], r["requested_kw"]) == (3, "usage", 3)
        assert r["bills"] == {"months_recorded": 3, "average_monthly_units_kwh": 400.0,
                              "average_monthly_bill_inr": "3200.00", "implied_tariff_inr_per_kwh": "8.00"}
        est = r["estimate"]
        assert (est["price_status"], est["priced_kw"], est["phase"]) == ("list_price", 3, "SINGLE")
        assert set(est["packages"]) == {"NOVA_ASSURED", "NOVA_SIGNATURE"}
        assert isinstance(est["packages"]["NOVA_ASSURED"]["final"], str) and est["subsidy_inr"] == "78000.00"
        assert r["warnings"] == []

    def test_custom_quote_outside_the_list(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        oid = _create(client, gna)
        complete_survey(client, gna, oid, sign=False, survey_variant="commercial_roof", contract_demand_kva=10)
        est = _results(client, gna, oid)["estimate"]
        assert est["price_status"] == "custom_quote" and "segment_not_in_price_list" in est["custom_quote_reasons"]
        assert est["subsidy_inr"] == "0.00" and est["packages"] == {}

    def test_not_enough_to_size(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        oid = _create(client, gna, sanctioned_load_kw=None, avg_monthly_units_kwh=None)
        r = _results(client, gna, oid)
        assert (r["suggested_kw"], r["limiting_factor"]) == (None, None)
        assert r["estimate"]["price_status"] == "not_enough_to_size"

    def test_warnings(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        oid = _create(client, gna, desired_output_kw=2, sanctioned_load_kw=1.5)
        complete_survey(client, gna, oid, sign=False, survey_variant="housing_society", roof_material="tin_shed",
                        roof_obstacles=["overhead_line"], connection_phase="three_phase", roof_area_sqft=100,
                        roof_ownership_confirmed=False, society_flat_count=20, society_noc_status="not_available",
                        consumption_history=[{"month": "2026-0%d" % m, "units_kwh": 400, "bill_inr": 10000}
                                             for m in (3, 4, 5)],
                        avg_monthly_units_kwh=1000)
        w = _codes(_results(client, gna, oid))
        assert w["requested_above_suggested"] == {"requested_kw": 2.0, "suggested_kw": 1, "limiting_factor": "load"}
        assert w["load_enhancement_needed"] == {"required_kw": 2.0, "sanctioned_load_kw": 1.5, "max_by_load_kw": 1.5}
        assert w["implied_tariff_outside_range"] == {"implied_tariff_inr_per_kwh": "25.00", "min": 3, "max": 20}
        assert w["units_bill_mismatch"] == {"field": "avg_monthly_units_kwh", "entered": "1000.0",
                                            "from_bills": "400.0"}
        assert w["three_phase_below_min"] == {"kw": 2.0, "min_kw": 3}
        assert w["fragile_roof"] == {"roof_material": "tin_shed"}
        assert w["overhead_line"] == {} and w["roof_not_owned_no_noc"] == {}
        assert w["society_noc_missing"] == {"society_noc_status": "not_available"}
        assert w["area_implausible"]["reason"] == "shadow_free_exceeds_total"
        assert "duplicate_consumer_number" not in w
        # Codes and params only — no English sentences.
        assert all(set(x) == {"code", "params"} for x in _results(client, gna, oid)["warnings"])

    def test_duplicate_consumer_number_never_names_the_other_associate(self, client, db_session,
                                                                       onboarding_ready):
        first, second = make_associate(db_session), make_associate(db_session)
        _create(client, first, n=1)
        oid = _create(client, second, n=2, consumer_number="CN-000001")
        r = _results(client, second, oid)
        assert _codes(r)["duplicate_consumer_number"] == {}
        assert first not in json.dumps(r)

    def test_territory_preferred_reads_strangers_do_not(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session, home_district=ALIPURDUAR)
        head = make_associate(db_session, level="district_head", territory=ALIPURDUAR)
        sibling = make_associate(db_session, level="district_head", territory=KOLKATA)
        oid = _create(client, gna)
        for path in ("survey", "survey/results"):
            assert client.get(f"{BASE}/{oid}/{path}", headers=headers(head)).status_code == 200
            assert client.get(f"{BASE}/{oid}/{path}", headers=headers(sibling)).status_code == 403
        assert sign_survey(client, head, oid, "associate").status_code == 403


class TestStaffReads:
    def test_roles(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        oid = _create(client, gna)
        complete_survey(client, gna, oid, sign=False)
        for who in (ADMIN, MGMT, SALES, ENG):
            assert client.get(f"{DASH}/onboardings/{oid}/survey", headers=who).status_code == 200
        for who in (OPS, ACCOUNTS):
            assert client.get(f"{DASH}/onboardings/{oid}/survey", headers=who).status_code == 403
            assert client.get(f"{DASH}/onboardings/{oid}/survey/results", headers=who).status_code == 403
        assert "estimate" in client.get(f"{DASH}/onboardings/{oid}/survey/results", headers=ADMIN).json()
        # Engineers review the survey but never see Grid Nova's prices.
        eng = client.get(f"{DASH}/onboardings/{oid}/survey/results", headers=ENG).json()
        assert "estimate" not in eng and eng["suggested_kw"] == 3

    def test_sales_only_own_or_unassigned(self, client, db_session, onboarding_ready):
        from app.memory.models import Lead
        from tests.conftest import seed_employees
        seed_employees(db_session, "emp-sales-2")
        gna = make_associate(db_session)
        oid = _create(client, gna)
        lead = db_session.get(Lead, db_session.get(CustomerOnboarding, oid).lead_id)
        lead.assigned_employee_id = "emp-sales-2"
        db_session.commit()
        assert client.get(f"{DASH}/onboardings/{oid}/survey", headers=SALES).status_code == 403


# ── Step 4: signatures and the submit gate ──────────────────────────────────────

class TestSignatures:
    def test_customer_signature_needs_name_and_confirmation(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        oid = _create(client, gna)
        r = sign_survey(client, gna, oid, declaration_name=" ")
        assert _error(r)["code"] == "declaration_name_required"
        r = sign_survey(client, gna, oid, customer_confirms_data="false")
        assert _error(r)["code"] == "confirmation_required"
        r = sign_survey(client, gna, oid, declaration_name="Rina Das")
        assert r.status_code == 201, r.text
        assert r.json()["declaration"]["name"] == "Rina Das" and r.json()["signatures"]["customer"]["signed"]
        inputs = db_session.get(ProposalInputs, db_session.get(CustomerOnboarding, oid).proposal_inputs_id)
        db_session.refresh(inputs)
        ref = inputs.customer_signature_ref
        assert ref["storage_key"].startswith(f"proposal-inputs/{inputs.id}/signature/customer_")
        assert ref["content_type"] == "image/png" and ref["signed_by"] == f"associate:{gna}"
        [audit] = db_session.execute(select(AuditLog).where(AuditLog.action == "survey_signed")).scalars().all()
        assert audit.after_value["declaration_name"] == "Rina Das" and audit.after_value["customer_confirms_data"]
        assert db_session.execute(select(TimelineEvent).where(
            TimelineEvent.event_type == "survey_signed")).scalar_one()

    def test_only_png_within_the_limit(self, client, db_session, onboarding_ready, monkeypatch):
        gna = make_associate(db_session)
        oid = _create(client, gna)
        r = sign_survey(client, gna, oid, "associate", content=b"\xff\xd8\xff\xe0" + b"\x00" * 64)
        assert _error(r)["code"] == "signature_not_png"
        r = sign_survey(client, gna, oid, "associate", content=PNG + b"\x00" * (500 * 1024))
        assert r.status_code == 413
        from app.survey import config
        monkeypatch.setitem(config.load(), "signature_max_kb", 1)
        r = sign_survey(client, gna, oid, "associate", content=PNG + b"\x00" * 1100)
        assert r.status_code == 413

    def test_upload_route_is_bounded(self):
        from app.security.upload_limit import limit_for
        bound, detail = limit_for("/api/v1/associate/onboardings/7/survey/signatures/customer")
        assert bound == 500 * 1024 + 64 * 1024 and "500 KB" in detail

    def test_editing_a_signed_survey_clears_the_signatures(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        oid = _create(client, gna)
        complete_survey(client, gna, oid)
        _patch(client, gna, oid, {"customer_timeline": "later"})     # not a survey field
        assert _survey(client, gna, oid)["signatures"]["customer"]["signed"] is True
        _patch(client, gna, oid, {"distance_method": "laser"})
        s = _survey(client, gna, oid)
        assert s["signatures"]["customer"]["signed"] is False and s["declaration"]["name"] is None

    def test_signing_a_submitted_customer_returns_it_to_draft(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        oid = _create(client, gna)
        upload_required_documents(client, gna, oid)
        confirm_site_location(client, gna, oid)
        complete_survey(client, gna, oid)
        assert client.post(f"{BASE}/{oid}/submit", headers=headers(gna)).status_code == 200
        assert sign_survey(client, gna, oid, "associate").status_code == 201
        assert db_session.get(CustomerOnboarding, oid).status == OnboardingStatus.draft


class TestSubmitGate:
    def _ready_but_survey(self, client, db_session):
        gna = make_associate(db_session)
        oid = _create(client, gna)
        upload_required_documents(client, gna, oid)
        confirm_site_location(client, gna, oid)
        return gna, oid

    def test_incomplete_survey_and_missing_signatures_are_refused(self, client, db_session, onboarding_ready):
        gna, oid = self._ready_but_survey(client, db_session)
        detail = _error(client.post(f"{BASE}/{oid}/submit", headers=headers(gna)))
        assert detail["code"] == "incomplete"
        assert detail["missing_survey_sections"] == ["connection", "bills", "roof", "roof_area", "layout"]
        assert detail["survey_unsigned"] == ["customer", "associate"]
        assert detail["missing_documents"] == [] and detail["site_location_unconfirmed"] is False
        complete_survey(client, gna, oid, sign=False)
        sign_survey(client, gna, oid, "customer")
        detail = _error(client.post(f"{BASE}/{oid}/submit", headers=headers(gna)))
        assert (detail["missing_survey_sections"], detail["survey_unsigned"]) == ([], ["associate"])
        sign_survey(client, gna, oid, "associate")
        assert client.post(f"{BASE}/{oid}/submit", headers=headers(gna)).status_code == 200

    def test_grandfathered_customers_are_not_gated(self, client, db_session, onboarding_ready):
        gna, oid = self._ready_but_survey(client, db_session)
        db_session.get(CustomerOnboarding, oid).survey_grandfathered = True
        db_session.commit()
        assert client.post(f"{BASE}/{oid}/submit", headers=headers(gna)).status_code == 200
        assert _survey(client, gna, oid)["required_to_submit"] is False

    def test_gate_can_be_switched_off(self, client, db_session, onboarding_ready, monkeypatch):
        from app.survey import config
        monkeypatch.setitem(config.load(), "survey_required_to_submit", False)
        gna, oid = self._ready_but_survey(client, db_session)
        assert client.post(f"{BASE}/{oid}/submit", headers=headers(gna)).status_code == 200

    def test_ready_to_submit_waits_for_the_survey(self, client, db_session, onboarding_ready):
        gna, oid = self._ready_but_survey(client, db_session)

        def kinds():
            items = client.get("/api/v1/associate/home", headers=headers(gna)).json()["items"]
            return {i["kind"] for i in items if i["onboarding_id"] == oid}
        assert "ready_to_submit" not in kinds()
        complete_survey(client, gna, oid)
        assert "ready_to_submit" in kinds()


class TestConfig:
    def test_options_carry_the_survey(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        survey = client.get(f"{BASE}/options", headers=headers(gna)).json()["survey"]
        assert [s["key"] for s in survey["variants"]["individual_house"]] == [
            "connection", "bills", "roof", "roof_area", "layout"]
        assert survey["discom_options"] == ["WBSEDCL", "CESC", "NBPDCL", "SBPDCL", "Other"]
        assert (survey["requested_consumption_months"], survey["min_consumption_months"]) == (6, 3)
        assert survey["max_roof_sections"] == 6 and survey["signature_max_kb"] == 500

    def test_shipped_config_is_valid(self):
        from app.survey import config
        config.load.cache_clear()
        cfg = config.load()
        assert cfg["min_consumption_months"] == 3 and cfg["area_sqft_per_kw"] == 90

    def test_unknown_field_refused(self, monkeypatch):
        from app.survey import config
        data = json.loads(json.dumps(config.load()))
        data["sections"][0]["fields"].append("not_a_field")
        with pytest.raises(config.SurveyConfigError):
            config._validate(data)

    def test_survey_keys_are_scrubbed_from_sentry(self):
        from app.security.sentry_scrub import _NO_LOCALS_MODULES, SENSITIVE_KEYS, scrub_event
        assert {"consumer_number", "customer_signature_ref", "declaration_name"} <= SENSITIVE_KEYS
        assert "app.survey" in _NO_LOCALS_MODULES and "app.api.routes.associate_survey" in _NO_LOCALS_MODULES
        event = scrub_event({"extra": {"consumer_number": "CN-1", "declaration_name": "Rina"}}, None)
        assert event["extra"] == {"consumer_number": "[Filtered]", "declaration_name": "[Filtered]"}
