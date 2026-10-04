"""Nova must not name equipment makes, warranty terms, or a customer's subsidy outcome
on its own authority (app/agents/guardrails.py). Realistic allowed replies are tested
alongside the blocked ones so over-blocking shows up as a failure too."""
import pytest

from app.agents import guardrails as g
from app.llm.company_facts import COMPANY_FACTS
from app.memory.models import Equipment, EquipmentCategory

FALLBACK = "Let me get one of our team to pick this up with you properly."


def _enforce(reply, *, approved=(), customer=""):
    return g.enforce(reply, allowed_text=customer + "\n" + COMPANY_FACTS, user_message=customer,
                     fallback=FALLBACK, approved_equipment=approved, customer_text=customer)


def _codes(result):
    return {v["code"] for v in result.violations}


class TestEquipment:
    @pytest.mark.parametrize("reply", [
        "We use Longi panels on most residential rooftops.",
        "For a 5kW system we'd pair the panels with a Fronius inverter.",
        "Our installs typically use Jinko modules.",
        "The battery would be an Exide unit.",
        "We can fit LG panels for you.",
        "I'd suggest panels from Loom Solar for this roof.",
        "Most customers here go for a Growatt hybrid inverter and Luminous batteries.",
        "A good option is Novasun panels with a Fronius inverter.",
    ])
    def test_blocked_when_no_sku_record_approves_it(self, reply):
        result = _enforce(reply)
        assert result.blocked and g.UNAPPROVED_EQUIPMENT in _codes(result)
        assert result.reply == FALLBACK

    @pytest.mark.parametrize("reply", [
        "We'll recommend specific panel and inverter brands in your proposal after the site survey.",
        "Solar panels on a south-facing roof usually work well in Kolkata.",
        "Mono PERC panels and a string inverter are common for homes your size.",
        "DCR panels are required under PM Surya Ghar, and our team will confirm the exact models.",
        "Bifacial panels can help on a ground-mounted site.",
        "Could you tell me roughly how much roof space you have for the panels?",
        "Batteries are not something we offer as a current service yet.",
    ])
    def test_generic_equipment_talk_is_allowed(self, reply):
        assert not _enforce(reply).blocked

    def test_active_sku_record_approves_the_make_but_never_a_model_number(self, db_session, memory):
        db_session.add(Equipment(category=EquipmentCategory.module, brand="Rayzon", model_name="RZ-540",
                                 is_active=True))
        db_session.add(Equipment(category=EquipmentCategory.inverter, brand="Solis", model_name="S5K",
                                 is_active=False))
        db_session.commit()
        terms = memory.active_equipment_terms()
        assert not _enforce("We usually install Rayzon panels.", approved=terms).blocked
        result = _enforce("We usually install Rayzon RZ-540 panels.", approved=terms)
        assert result.blocked and g.MODEL_NUMBER in _codes(result)            # PM-A: never a model number
        assert _enforce("We'd pair them with a Solis inverter.", approved=terms).blocked  # inactive SKU

    def test_customer_named_make_may_be_named_back_but_not_endorsed(self):
        customer = "My neighbour got Loom Solar panels, are those any good?"
        allowed = ("I can't comment on Loom Solar panels specifically — our team will recommend equipment in "
                   "your proposal.")
        assert not _enforce(allowed, customer=customer).blocked
        for endorsed in ("Yes, Loom Solar panels are what we install too.",
                         "We use Loom Solar panels for most homes.",
                         "Our Loom Solar panels come with great output."):
            assert _enforce(endorsed, customer=customer).blocked, endorsed


class TestPackages:
    """PM-A — Grid Nova's master data: the package makes, the standard warranty, O&M and
    the panel life span may be stated; models, DCR claims, other makes, other warranty
    terms and package prices may not."""

    @pytest.mark.parametrize("reply", [
        "Nova Signature uses TATA, Waaree, Adani or Vikram panels, or equivalent.",
        "We use Waaree panels on most Nova Signature rooftops.",
        "Nova Assured pairs Sova Solar panels with a Deye inverter, or equivalent.",
        "For a 5kW system we'd pair the panels with a Sungrow inverter.",
        "I'd suggest panels from Vikram Solar for this roof.",
        "The Nova Signature package uses Mono PERC or TOPCon panels.",
        "Every system carries a standard warranty of 5 years from handover.",
        "You get a 5-year standard warranty, starting at handover.",
        "There is a five year warranty from handover, and the first year of O&M is free.",
        "Solar panels typically have a life span of 20–25 years.",
        "For prices, please use the Solar Calculator on our website or ask your associate.",
        "PM Surya Ghar requires DCR modules, and our team will confirm which apply to you.",
    ])
    def test_allowed(self, reply):
        result = _enforce(reply)
        assert not result.blocked, result.violations

    @pytest.mark.parametrize("reply,code", [
        ("We install Waaree WS-540 panels.", "equipment_model_number"),
        ("The inverter is a Sungrow SG5.0RS.", "equipment_model_number"),
        ("Our Nova Signature panels are DCR compliant.", "unverified_dcr_claim"),
        ("Adani modules are DCR certified, so you get the subsidy.", "unverified_dcr_claim"),
        ("All our panels are DCR.", "unverified_dcr_claim"),
        ("We use Longi panels in Nova Signature.", "unapproved_equipment_claim"),
        ("The panels come with a 25-year warranty.", "unverified_warranty_term"),
        ("Panels have a 20–25 year warranty.", "unverified_warranty_term"),
        ("You get a 5-year warranty on the inverter.", "unverified_warranty_term"),
        ("There's a 10 year warranty from handover.", "unverified_warranty_term"),
        ("Nova Assured for 3 kW costs ₹2,01,465.", "new_money_figure"),
    ])
    def test_blocked(self, reply, code):
        result = _enforce(reply)
        assert result.blocked and code in _codes(result), result.violations

    def test_facts_carry_the_packages_without_prices_or_models(self):
        assert "Nova Assured" in COMPANY_FACTS and "Nova Signature" in COMPANY_FACTS
        assert "Standard warranty: 5 years from handover." in COMPANY_FACTS
        assert "201465" not in COMPANY_FACTS and "2,01,465" not in COMPANY_FACTS
        life = next(line for line in COMPANY_FACTS.splitlines() if "life span" in line.lower())
        assert "warrant" not in life.lower() and "guarantee" not in life.lower()


class TestWarranty:
    @pytest.mark.parametrize("reply", [
        "The panels come with a 25-year warranty.",
        "You get 10 years on the inverter.",
        "There's a 5 year workmanship warranty on the installation.",
        "Panels are guaranteed for twenty five years of output.",
        "The system carries a warranty of 12 years.",
        "Expect 25 years performance guarantee on modules.",
    ])
    def test_specific_terms_are_blocked(self, reply):
        result = _enforce(reply)
        assert result.blocked and g.WARRANTY_TERM in _codes(result)

    @pytest.mark.parametrize("reply", [
        "Warranty terms are shared in your proposal.",
        "Our team will walk you through the warranty on each component.",
        "Most homeowners see payback within a few years of installation.",
        "We've installed over 1,200 PM Surya Ghar systems.",
        "A site visit usually takes about an hour.",
    ])
    def test_generic_warranty_talk_is_allowed(self, reply):
        assert not _enforce(reply).blocked

    def test_even_a_customer_quoted_term_is_not_confirmed(self):
        # Warranty terms come only from company facts, not from the conversation.
        customer = "Another installer promised me a 25-year warranty."
        assert _enforce("Yes, you'll get a 25-year warranty with us as well.", customer=customer).blocked


class TestSubsidy:
    @pytest.mark.parametrize("reply", [
        "You are eligible for the PM Surya Ghar subsidy.",
        "You're definitely eligible for ₹78,000.",
        "You will get ₹78,000 back as subsidy.",
        "You'll receive the full subsidy once installed.",
        "Good news — you qualify for the residential CFA.",
        "Your subsidy has been approved.",
        "This comes with a guaranteed subsidy.",
        "The subsidy is assured for homes like yours.",
        "You will be eligible for the maximum amount.",
    ])
    def test_definite_claims_are_blocked(self, reply):
        result = _enforce(reply)
        assert result.blocked and (g.SUBSIDY_CLAIM in _codes(result) or g.FALSE_CLAIM in _codes(result))

    @pytest.mark.parametrize("reply", [
        "Under PM Surya Ghar, residential rooftop systems get ₹30,000/kW for the first 2kW and ₹18,000 "
        "for the 3rd kW, capped at ₹78,000.",
        "Our team will confirm your eligibility for the subsidy.",
        "You may be eligible for the PM Surya Ghar subsidy — our team will check.",
        "If you qualify, the subsidy is paid by the government after commissioning.",
        "Whether you're eligible depends on your DISCOM and the roof being residential.",
        "The residential subsidy doesn't cover commercial rooftops.",
        "You will get a call back from our team within 48 business hours.",
        "You'll receive the proposal after the site survey.",
    ])
    def test_general_scheme_information_is_allowed(self, reply):
        result = _enforce(reply)
        assert not result.blocked, result.violations


class TestNegativeEligibility:
    @pytest.mark.parametrize("reply", [
        "You are not eligible for the PM Surya Ghar subsidy.",
        "You're not eligible, unfortunately.",
        "You aren't eligible for the residential CFA.",
        "You are ineligible for the scheme.",
        "You don't qualify for the subsidy.",
        "You won't qualify because the roof is shared.",
        "You wouldn't be eligible with a commercial connection.",
        "You can't get the subsidy for this roof.",
        "You cannot receive any subsidy here.",
        "Your home isn't eligible for the subsidy.",
        "Your connection doesn't qualify.",
    ])
    def test_negative_determinations_are_blocked(self, reply):
        result = _enforce(reply)
        assert result.blocked and g.SUBSIDY_CLAIM in _codes(result)
        assert result.reply == FALLBACK

    @pytest.mark.parametrize("reply", [
        "If you don't qualify, the system still pays for itself through lower bills.",
        "You may not be eligible if the connection is commercial — our team will check.",
        "You might not qualify for the residential slab; we'll confirm after the survey.",
        "Housing societies are not covered by the residential subsidy.",
        "Commercial rooftops don't qualify for the residential CFA.",
        "Whether you qualify depends on your DISCOM connection type.",
        "Our team will confirm whether you're eligible.",
        "Unless you're eligible under PM Surya Ghar, pricing is the full system cost.",
        "You can't get a site visit booked through this chat, but our team can arrange one.",
        "You don't need to arrange anything for the survey.",
    ])
    def test_conditional_hedged_and_general_statements_are_allowed(self, reply):
        result = _enforce(reply)
        assert not result.blocked, result.violations


class TestPipeline:
    def test_block_uses_the_fallback_and_is_logged(self, caplog):
        with caplog.at_level("WARNING", logger="app.agents.guardrails"):
            result = _enforce("We use Longi panels and you are eligible for the subsidy.")
        assert result.reply == FALLBACK and result.blocked
        assert {g.UNAPPROVED_EQUIPMENT, g.SUBSIDY_CLAIM} <= _codes(result)
        assert any("Guardrail blocked" in r.getMessage() for r in caplog.records)

    def test_sales_agent_blocks_and_records_it(self, client, memory, db_session, monkeypatch):
        from app.llm.client import llm_client
        monkeypatch.setattr(llm_client, "generate_reply", lambda **kwargs: "You are eligible for ₹78,000 subsidy.")
        r = client.post("/api/v1/chat/message", json={"message": "Hi, can I get a subsidy for home solar?"})
        assert r.status_code == 200
        assert "eligible" not in r.json()["reply"]
        logs = [l for l in memory.list_audit_logs() if l.action == "reply_blocked_by_guardrail"]
        assert any(g.SUBSIDY_CLAIM in str(l.after_value) for l in logs)
