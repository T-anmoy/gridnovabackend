"""PM-A Step 4 — formal proposals priced from Grid Nova's price list, with the
deterministic sections from its master data."""
import pytest
from sqlalchemy import select

from app.memory.models import AuditLog, Proposal
from app.pdf.generator import _build_html
from app.proposals.content_engine import load_content_config
from app.proposals.send_guard import placeholder_problem
from tests.test_proposal_request_agreement import _prepared  # noqa: F401
from tests.onboarding_support import (  # noqa: F401 — fixtures
    ADMIN, DASH, SALES, kyc_ready, onboarding_ready, territories,
)

LISTED = {"connection_phase": "single_phase", "equipment_package": "nova_assured"}


def _proposal(client, db_session, **kw):
    gna, o, pid = _prepared(client, db_session, **kw)
    return db_session.get(Proposal, pid)


def _section(content, sid):
    return next((s for s in content["sections"] if s["id"] == sid), None)


class TestListPriced:
    def test_pricing_section_total_value_and_provenance(self, client, db_session, onboarding_ready):
        p = _proposal(client, db_session, **LISTED)    # 3 kW single phase, West Bengal
        assert p.total_value == 201465.0
        pricing = p.content["pricing"]
        assert (pricing["price_status"], pricing["package"], pricing["phase"], pricing["kw"]) == (
            "list_price", "NOVA_ASSURED", "SINGLE", 3)
        assert (pricing["basic"], pricing["gst"], pricing["final"]) == ("185000.00", "16465.00", "201465.00")
        assert pricing["price_list_version"] == 1 and len(pricing["source_sha256"]) == 64
        assert p.content["price_list"] == {"version": 1, "source_sha256": pricing["source_sha256"]}
        body = _section(p.content, "pricing")["body"]
        assert "Nova Assured — 3 kW single-phase" in body
        assert "Basic price: Rs 1,85,000.00" in body and "Final price (including GST): Rs 2,01,465.00" in body
        assert "5% on 70%" in body and "18%" in body and "8.9%" in body

    def test_signature_package(self, client, db_session, onboarding_ready):
        p = _proposal(client, db_session, connection_phase="single_phase", equipment_package="nova_signature")
        assert p.total_value == 225640.8 and p.content["pricing"]["package"] == "NOVA_SIGNATURE"
        eq = _section(p.content, "equipment_makes")["body"]
        assert "TATA / Waaree / Adani / Vikram or equivalent (Mono PERC, TOPCon), 550 / 560 / 580 / 600 Wp" in eq
        assert "Cables: Polycab / Mescab / Finolex / KEI / RR Kabel / Havells or equivalent" in eq

    def test_deterministic_sections(self, client, db_session, onboarding_ready):
        p = _proposal(client, db_session, **LISTED)
        eq = _section(p.content, "equipment_makes")["body"]
        assert "Solar panels: Sova Solar / HR Solar / V-Sole or equivalent (Mono PERC, TOPCon), 550 / 560 / 580 / 600 Wp" in eq
        assert "Inverter: Deye / V-Sole / Micro Tech / Power One or equivalent" in eq
        spec = _section(p.content, "electrical_specification")["body"]
        assert "AC cable: 4 sqmm Copper — Polycab / Mescab / Finolex / KEI / RR Kabel / Havells" in spec
        assert "Conduit pipe: Water SDP pipe — Supreme" in spec and "use only inner side" in spec
        assert _section(p.content, "payment_terms")["body"] == \
            "20% On Order\n60% Against Material Bill\n20% Before Commissioning"
        warranty = _section(p.content, "warranty_and_service")["body"]
        assert "Standard warranty: 5 years from handover." in warranty and "First year free" in warranty
        assert "Solar panel life span: 25 years (a life span, not a warranty)." in warranty
        terms = _section(p.content, "price_terms")["body"]
        assert "within West Bengal" in terms and "Company reserves right to change price" in terms
        for sid in ("equipment_makes", "electrical_specification", "payment_terms", "warranty_and_service",
                    "price_terms"):
            assert "[PLACEHOLDER" not in _section(p.content, sid)["body"]

    def test_electrical_spec_is_omitted_above_5_kw(self, client, db_session, onboarding_ready):
        p = _proposal(client, db_session, **LISTED, desired_output_kw=7, sanctioned_load_kw=10)
        assert p.total_value == 453024.0
        assert _section(p.content, "electrical_specification") is None

    def test_still_blocked_by_the_unsupplied_proposal_wording(self, client, db_session, onboarding_ready):
        p = _proposal(client, db_session, **LISTED)
        problem = placeholder_problem(p)
        assert problem and problem["missing_total_value"] is False
        assert "pricing" not in problem["placeholder_sections"] and "standard_terms" in problem["placeholder_sections"]

    def test_pdf_prints_rs(self, client, db_session, onboarding_ready):
        p = _proposal(client, db_session, **LISTED)
        html = _build_html({**p.content, "total_value": p.total_value}, load_content_config())
        assert "Rs 2,01,465.00" in html and "INR" not in html and "Payment Terms" in html

    def test_adjust_pricing_records_the_variance(self, client, db_session, onboarding_ready):
        p = _proposal(client, db_session, **LISTED)
        r = client.patch(f"{DASH}/proposals/{p.id}/adjust-pricing", headers=ADMIN,
                         json={"new_value": 200000, "reason": "Rounded down for the customer."})
        assert r.status_code == 200
        assert (r.json()["list_price_final"], r.json()["variance"]) == ("201465.00", "-1465.00")
        audit = db_session.execute(select(AuditLog).where(AuditLog.action == "pricing_adjusted")).scalar_one()
        assert audit.after_value["variance"] == "-1465.00"
        db_session.refresh(p)
        assert "Rs 2,00,000.00" in _section(p.content, "pricing")["body"]


class TestNotListPriced:
    @pytest.mark.parametrize("kw,reason", [
        ({"connection_phase": "single_phase"}, "package_not_chosen"),                      # as recommended / none
        ({"equipment_package": "nova_assured"}, "phase_unknown"),
        ({**LISTED, "desired_output_kw": 3.5}, "non_integer_size"),
        ({"connection_phase": "three_phase", "equipment_package": "nova_assured", "desired_output_kw": 2},
         "three_phase_below_min"),
        ({**LISTED, "connection_type": "hybrid", "battery_storage": True}, "battery_or_off_grid"),
    ])
    def test_price_to_be_confirmed(self, client, db_session, onboarding_ready, kw, reason):
        p = _proposal(client, db_session, **kw)
        assert p.total_value is None
        assert p.content["pricing"]["price_status"] == "custom_quote"
        assert reason in p.content["pricing"]["custom_quote_reasons"]
        assert _section(p.content, "pricing")["body"] == "[PLACEHOLDER: Price to be confirmed by Grid Nova.]"
        assert placeholder_problem(p)["missing_total_value"] is True
        assert _section(p.content, "price_terms") is None

    def test_adjust_pricing_on_an_unlisted_proposal_has_no_variance(self, client, db_session, onboarding_ready):
        p = _proposal(client, db_session, connection_phase="single_phase")
        r = client.patch(f"{DASH}/proposals/{p.id}/adjust-pricing", headers=SALES,
                         json={"new_value": 210000, "reason": "Quoted after the site visit."})
        assert r.status_code == 200 and r.json()["list_price_final"] is None and r.json()["variance"] is None
