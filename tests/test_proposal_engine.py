"""
Parameterized proposal composition engine — rule matching, composition, pricing,
fallback flagging, and startup validation.
"""
import copy

import pytest

from app.proposals import content_engine as ce
from app.proposals.content_engine import ProposalConfigError
from app.memory.models import PipelineStage

# These tests write employee ids directly (no request provisions them first).
pytestmark = pytest.mark.usefixtures("known_employees")


@pytest.fixture(scope="module")
def cfg():
    # The real, shipped config — validated on load.
    return ce.load_content_config(force_reload=True)


# ── Rule matching (one per rule) ─────────────────────────────────────────────────

class TestRuleMatching:
    def test_residential_rooftop(self, cfg):
        ids, fallback = ce.select_sections(
            {"project_type": "rooftop", "segment": "residential", "capacity_kw": 8}, cfg
        )
        assert fallback is False
        assert ids == ["residential_rooftop_scope", "pm_surya_ghar_subsidy_note"]

    def test_commercial_rooftop(self, cfg):
        ids, _ = ce.select_sections(
            {"project_type": "rooftop", "segment": "commercial", "capacity_kw": 40}, cfg
        )
        assert ids == ["commercial_rooftop_scope"]

    def test_industrial_rooftop(self, cfg):
        ids, _ = ce.select_sections(
            {"project_type": "rooftop", "segment": "industrial", "capacity_kw": 300}, cfg
        )
        assert ids == ["industrial_rooftop_scope"]

    def test_utility_scale_requires_min_capacity(self, cfg):
        # Below 500 kW → utility_scale rule does NOT fire → no match → fallback.
        ids_small, fb_small = ce.select_sections(
            {"project_type": "ground_mounted", "capacity_kw": 200}, cfg
        )
        assert fb_small is True
        # At/above 500 kW → utility_scale fires.
        ids_big, fb_big = ce.select_sections(
            {"project_type": "ground_mounted", "capacity_kw": 500}, cfg
        )
        assert fb_big is False
        assert ids_big == ["utility_scale_scope"]

    def test_agricultural_pump(self, cfg):
        ids, _ = ce.select_sections({"project_type": "solar_pump", "capacity_kw": 5}, cfg)
        assert ids == ["pm_kusum_note"]

    def test_government_pulls_compliance(self, cfg):
        ids, _ = ce.select_sections(
            {"project_type": "rooftop", "segment": "government", "capacity_kw": 60}, cfg
        )
        # segment=government matches only the government rule (rooftop rules need a
        # residential/commercial/industrial segment).
        assert ids == ["government_scope", "government_compliance_note"]


class TestMultiRuleComposition:
    def test_two_rules_compose_deduped_ordered(self, cfg):
        # solar_pump + government segment matches BOTH agricultural_pump and
        # government_institutional; contributions compose in rule-definition order.
        ids, fallback = ce.select_sections(
            {"project_type": "solar_pump", "segment": "government", "capacity_kw": 10}, cfg
        )
        assert fallback is False
        assert ids == ["pm_kusum_note", "government_scope", "government_compliance_note"]
        # No duplicates.
        assert len(ids) == len(set(ids))


# ── Pricing math ─────────────────────────────────────────────────────────────────

class TestPricing:
    def test_shipped_tiers_have_no_automatic_price(self, cfg):
        # BE-22: the shipped tiers are rate_per_kw: null — priced by staff, never ₹0.
        assert [t["rate_per_kw"] for t in cfg["pricing"]["tiers"]] == [None, None, None]
        assert ce.select_pricing("rooftop", 8, "residential", cfg) is None
        assert ce.select_pricing("ground_mounted", 800, "government", cfg) is None

    def test_tier_and_modifier_math(self):
        # Build a config with real rates + a modifier to verify the arithmetic.
        cfg = {
            "sections_library": {"fallback_generic_scope": "x"},
            "parameter_rules": [],
            "pricing": {
                "tiers": [
                    {"project_type": "rooftop", "capacity_min_kw": 0, "capacity_max_kw": 10, "rate_per_kw": 60000},
                    {"project_type": "rooftop", "capacity_min_kw": 10, "capacity_max_kw": 100, "rate_per_kw": 50000},
                    {"project_type": "ground_mounted", "capacity_min_kw": 0, "capacity_max_kw": None, "rate_per_kw": 40000},
                ],
                "segment_modifiers": {"government": 0.9},
            },
        }
        ce.validate_content_config(cfg)
        # 1) rooftop 5 kW, no segment → first tier, no modifier.
        assert ce.select_pricing("rooftop", 5, None, cfg) == 60000.0
        # 2) rooftop 50 kW, government segment → second tier * 0.9.
        assert ce.select_pricing("rooftop", 50, "government", cfg) == pytest.approx(45000.0)
        # 3) ground_mounted 2000 kW → unbounded tier.
        assert ce.select_pricing("ground_mounted", 2000, None, cfg) == 40000.0

    def test_no_matching_tier_returns_none(self, cfg):
        assert ce.select_pricing("carport", 5, None, cfg) is None
        assert ce.select_pricing(None, 5, None, cfg) is None
        assert ce.select_pricing("rooftop", None, None, cfg) is None


# ── Validation ───────────────────────────────────────────────────────────────────

class TestValidation:
    def test_overlapping_tiers_rejected(self):
        cfg = {
            "sections_library": {"fallback_generic_scope": "x"},
            "parameter_rules": [],
            "pricing": {"tiers": [
                {"project_type": "rooftop", "capacity_min_kw": 0, "capacity_max_kw": 50, "rate_per_kw": 1},
                {"project_type": "rooftop", "capacity_min_kw": 40, "capacity_max_kw": 100, "rate_per_kw": 1},
            ]},
        }
        with pytest.raises(ProposalConfigError, match="Overlapping pricing tiers"):
            ce.validate_content_config(cfg)

    def test_missing_section_id_rejected(self):
        cfg = {
            "sections_library": {"fallback_generic_scope": "x", "real_scope": "y"},
            "parameter_rules": [
                {"id": "r1", "match": {"project_type": "rooftop"}, "contributes": ["does_not_exist"]},
            ],
            "pricing": {"tiers": []},
        }
        with pytest.raises(ProposalConfigError, match="unknown section id"):
            ce.validate_content_config(cfg)

    def test_missing_fallback_rejected(self):
        cfg = {"sections_library": {"real_scope": "y"}, "parameter_rules": [], "pricing": {"tiers": []}}
        with pytest.raises(ProposalConfigError, match="fallback_generic_scope"):
            ce.validate_content_config(cfg)


# ── Composition through the full draft path (fallback flag persistence) ──────────

def _seed_lead(memory, db_session, collected):
    customer = memory.get_or_create_customer(email=collected["email"], name=collected["name"])
    db_session.commit()
    lead = memory.create_lead(customer_id=customer.id)
    memory.update_lead_fields(
        lead_id=lead.id, new_fields=collected, performed_by="test",
        stage=PipelineStage.qualified,
    )
    db_session.commit()
    return customer.id, lead.id


class TestDraftComposition:
    def test_matched_combination_composes_real_sections(self, memory, db_session):
        from app.agents.proposal_agent import proposal_agent
        _, lid = _seed_lead(memory, db_session, {
            "name": "Res User", "email": "res@example.com",
            "project_type": "rooftop", "segment": "residential",
            "location": "Kolkata", "capacity_kw": 8,
        })
        result = proposal_agent.draft_proposal(memory, lid, performed_by="emp-1")
        db_session.commit()
        assert result["used_fallback_template"] is False
        proposal = memory.get_proposal(result["proposal_id"])
        # The rule matched; since BE-13 the customer document's own text (config/
        # proposal_document.yaml) replaces the residential scope and subsidy note sections.
        assert proposal.content["matched_rule_ids"] == ["residential_rooftop"]
        assert proposal.content["document"]["segment"] == "residential"
        assert proposal.content["document"]["coverage"] is None
        section_ids = [s["id"] for s in proposal.content["sections"]]
        assert "residential_rooftop_scope" not in section_ids and "pm_surya_ghar_subsidy_note" not in section_ids
        assert proposal.used_fallback_template is False

    def test_unmatched_combination_flags_fallback(self, memory, db_session):
        from app.agents.proposal_agent import proposal_agent
        _, lid = _seed_lead(memory, db_session, {
            "name": "Odd User", "email": "odd@example.com",
            "project_type": "carport", "segment": "unknown",
            "location": "Nowhere", "capacity_kw": 12,
        })
        result = proposal_agent.draft_proposal(memory, lid, performed_by="emp-1")
        db_session.commit()
        assert result["used_fallback_template"] is True
        proposal = memory.get_proposal(result["proposal_id"])
        assert proposal.used_fallback_template is True
        section_ids = [s["id"] for s in proposal.content["sections"]]
        assert "fallback_generic_scope" in section_ids

    def test_two_matched_combinations_differ(self, memory, db_session):
        from app.agents.proposal_agent import proposal_agent
        _, lid_a = _seed_lead(memory, db_session, {
            "name": "A", "email": "a2@example.com", "project_type": "rooftop",
            "segment": "residential", "location": "Kolkata", "capacity_kw": 8,
        })
        _, lid_b = _seed_lead(memory, db_session, {
            "name": "B", "email": "b2@example.com", "project_type": "ground_mounted",
            "segment": "commercial", "location": "Durgapur", "capacity_kw": 800,
        })
        ra = proposal_agent.draft_proposal(memory, lid_a, performed_by="emp-1")
        rb = proposal_agent.draft_proposal(memory, lid_b, performed_by="emp-1")
        db_session.commit()
        pa = memory.get_proposal(ra["proposal_id"])
        pb = memory.get_proposal(rb["proposal_id"])
        ids_a = [s["id"] for s in pa.content["sections"]]
        ids_b = [s["id"] for s in pb.content["sections"]]
        assert ids_a != ids_b
        assert pa.content["matched_rule_ids"] == ["residential_rooftop"]
        # Utility-scale is a segment the templates do not cover: its scope section stays
        # (still Grid Nova's to supply) and blocks approval through the document.
        assert "utility_scale_scope" in ids_b
        assert pb.content["document"]["coverage"] == "utility"
