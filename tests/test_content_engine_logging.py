"""
Phase 7 content-engine visibility fixes:
- rooftop leads that fall back to the generic template specifically because
  `segment` was never collected now log a clearly diagnosable reason
- placeholder pricing tiers (rate_per_kw == 0) log a prominent startup warning
  instead of passing validation silently
"""
import logging

import pytest

from app.proposals.content_engine import ProposalConfigError, compose_content, validate_content_config

_BASE_CFG = {
    "company": {"name": "Grid Nova Energy"},
    "letterhead": {"logo_text": "Grid Nova"},
    "base_sections": {"cover_letter_intro": "x", "standard_terms": "x", "closing": "x"},
    "sections_library": {
        "fallback_generic_scope": "generic",
        "residential_rooftop_scope": "residential scope",
    },
    "parameter_rules": [
        {"id": "residential_rooftop", "match": {"project_type": "rooftop", "segment": "residential"},
         "contributes": ["residential_rooftop_scope"]},
    ],
    "pricing": {
        "tiers": [{"project_type": "rooftop", "capacity_min_kw": 0, "capacity_max_kw": None, "rate_per_kw": None}],
        "segment_modifiers": {},
    },
}


class _FakeLead:
    def __init__(self, id_, collected_fields):
        self.id = id_
        self.collected_fields = collected_fields


class TestFallbackReasonLogging:
    def test_rooftop_missing_segment_logs_specific_reason(self, caplog):
        lead = _FakeLead(42, {"project_type": "rooftop", "capacity_kw": 5, "location": "Pune"})
        ctx = {"lead": lead, "customer": None}
        with caplog.at_level(logging.WARNING, logger="app.proposals.content_engine"):
            content = compose_content(ctx, _BASE_CFG)
        assert content["used_fallback_template"] is True
        assert any("never collected for a rooftop lead" in r.message for r in caplog.records)

    def test_matched_rule_does_not_log_fallback_warning(self, caplog):
        lead = _FakeLead(43, {"project_type": "rooftop", "segment": "residential",
                              "capacity_kw": 5, "location": "Pune"})
        ctx = {"lead": lead, "customer": None}
        with caplog.at_level(logging.WARNING, logger="app.proposals.content_engine"):
            content = compose_content(ctx, _BASE_CFG)
        assert content["used_fallback_template"] is False
        assert not any("fell back" in r.message for r in caplog.records)

    def test_other_unmatched_profile_logs_generic_reason(self, caplog):
        lead = _FakeLead(44, {"project_type": "solar_pump", "capacity_kw": 5, "location": "Pune"})
        ctx = {"lead": lead, "customer": None}
        with caplog.at_level(logging.WARNING, logger="app.proposals.content_engine"):
            content = compose_content(ctx, _BASE_CFG)
        assert content["used_fallback_template"] is True
        assert any("no parameter rule matched" in r.message for r in caplog.records)
        assert not any("never collected for a rooftop lead" in r.message for r in caplog.records)


class TestPlaceholderPricingWarning:
    """BE-22: a tier's rate is positive or null (no automatic price) — never ₹0."""

    @pytest.mark.parametrize("rate", [0, 0.0, -5, "45000", True])
    def test_zero_or_invalid_rate_is_refused(self, rate):
        cfg = {**_BASE_CFG, "pricing": {
            "tiers": [{"project_type": "rooftop", "capacity_min_kw": 0, "capacity_max_kw": None, "rate_per_kw": rate}],
            "segment_modifiers": {},
        }}
        with pytest.raises(ProposalConfigError, match="positive INR/kW or null"):
            validate_content_config(cfg)

    def test_null_rate_is_valid_and_silent(self, caplog):
        with caplog.at_level(logging.WARNING, logger="app.proposals.content_engine"):
            validate_content_config(_BASE_CFG)
        assert not caplog.records

    def test_real_rate_tier_does_not_warn(self, caplog):
        cfg = {**_BASE_CFG, "pricing": {
            "tiers": [{"project_type": "rooftop", "capacity_min_kw": 0, "capacity_max_kw": None, "rate_per_kw": 45000}],
            "segment_modifiers": {},
        }}
        with caplog.at_level(logging.WARNING, logger="app.proposals.content_engine"):
            validate_content_config(cfg)
        assert not any("PLACEHOLDER" in r.message for r in caplog.records)
