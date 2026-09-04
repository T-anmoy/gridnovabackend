"""Tests for the 3-tier Approval Engine (app/rules/approval_engine.py) and its
additive wiring into dashboard.py — the existing 2-tier approve/manager-sign-off
flow is untouched; this only adds a new, consultable determination.
"""
from app.auth.dependencies import AuthenticatedEmployee
from app.config import settings
from app.memory.models import EmployeeRole
from app.rules import approval_engine
from app.rules.approval_engine import ApprovalTier, determine_required_approval_tier
from app.pricing.margin import MARGIN_FLOOR_PERCENT

from tests.conftest import admin_headers, engineer_headers, management_headers, sales_headers


class TestCanApprovePartnerSignoff:
    def test_admin_only(self):
        for role in EmployeeRole:
            employee = AuthenticatedEmployee(employee_id="x", role=role)
            expected = role == EmployeeRole.admin
            assert employee.can_approve_partner_signoff() is expected


class TestDetermineRequiredApprovalTierByValueAlone:
    def test_small_value_is_standard(self):
        assert determine_required_approval_tier(1_000.0) == ApprovalTier.standard

    def test_above_the_manager_threshold_escalates(self):
        above = settings.proposal_value_manager_approval_threshold + 1
        assert determine_required_approval_tier(above) == ApprovalTier.manager_signoff

    def test_at_the_manager_threshold_does_not_escalate(self):
        at = settings.proposal_value_manager_approval_threshold
        assert determine_required_approval_tier(at) == ApprovalTier.standard

    def test_above_the_partner_threshold_escalates_to_partner(self):
        above = approval_engine.PLACEHOLDER_PARTNER_SIGNOFF_VALUE_THRESHOLD_INR + 1
        assert determine_required_approval_tier(above) == ApprovalTier.partner_signoff

    def test_none_value_alone_is_standard(self):
        assert determine_required_approval_tier(None) == ApprovalTier.standard


class TestDiscountCanEscalateIndependentlyOfValue:
    def test_a_small_deal_with_a_high_discount_still_escalates(self):
        """The core requirement: value alone says standard, but the discount
        pushes it higher regardless."""
        tier = determine_required_approval_tier(
            proposal_value=1_000.0,
            discount_percent=approval_engine.PLACEHOLDER_HIGH_DISCOUNT_PERCENT_THRESHOLD,
        )
        assert tier == ApprovalTier.manager_signoff

    def test_a_small_deal_with_a_very_high_discount_reaches_partner_tier(self):
        tier = determine_required_approval_tier(
            proposal_value=1_000.0,
            discount_percent=approval_engine.PLACEHOLDER_VERY_HIGH_DISCOUNT_PERCENT_THRESHOLD,
        )
        assert tier == ApprovalTier.partner_signoff

    def test_a_low_discount_does_not_escalate(self):
        tier = determine_required_approval_tier(proposal_value=1_000.0, discount_percent=2.0)
        assert tier == ApprovalTier.standard


class TestMarginCanEscalateIndependentlyOfValue:
    def test_a_small_deal_with_a_below_floor_margin_still_escalates(self):
        tier = determine_required_approval_tier(
            proposal_value=1_000.0, margin_percent=MARGIN_FLOOR_PERCENT - 1,
        )
        assert tier == ApprovalTier.manager_signoff

    def test_a_healthy_margin_does_not_escalate(self):
        tier = determine_required_approval_tier(proposal_value=1_000.0, margin_percent=50.0)
        assert tier == ApprovalTier.standard

    def test_margin_and_approval_engine_share_the_same_floor_constant(self):
        """The connection Phase 4 asked for — one number, not two independently
        tuned copies."""
        import app.pricing.margin as margin_module
        assert approval_engine.MARGIN_FLOOR_PERCENT is margin_module.MARGIN_FLOOR_PERCENT


class TestFlaggedRiskEscalatesByOneTier:
    def test_escalates_standard_to_manager(self):
        tier = determine_required_approval_tier(proposal_value=1_000.0, is_flagged_risk=True)
        assert tier == ApprovalTier.manager_signoff

    def test_escalates_manager_to_partner(self):
        above = settings.proposal_value_manager_approval_threshold + 1
        tier = determine_required_approval_tier(proposal_value=above, is_flagged_risk=True)
        assert tier == ApprovalTier.partner_signoff

    def test_never_escalates_past_partner(self):
        above = approval_engine.PLACEHOLDER_PARTNER_SIGNOFF_VALUE_THRESHOLD_INR + 1
        tier = determine_required_approval_tier(proposal_value=above, is_flagged_risk=True)
        assert tier == ApprovalTier.partner_signoff


class TestAllFourFactorsCombineByTakingTheHighest:
    def test_the_highest_of_all_signals_wins_not_the_last_one_checked(self):
        # Value alone -> standard. Discount alone -> manager. Margin alone ->
        # manager. Risk flag alone would escalate standard->manager. All
        # combined must still land on manager (the max), not stack further
        # just because multiple signals separately said "manager".
        tier = determine_required_approval_tier(
            proposal_value=1_000.0,
            discount_percent=approval_engine.PLACEHOLDER_HIGH_DISCOUNT_PERCENT_THRESHOLD,
            margin_percent=MARGIN_FLOOR_PERCENT - 1,
            is_flagged_risk=False,
        )
        assert tier == ApprovalTier.manager_signoff


class TestRequiredApprovalTierEndpoint:
    """Additive — the actual /approve and /manager-sign-off endpoints and
    Proposal.requires_manager_signoff are untouched; this is a new, separate,
    read-only consult."""

    def _make_proposal(self, client, memory, db_session, total_value=None):
        customer = memory.get_or_create_customer(email="tier-endpoint@example.com", name="C")
        db_session.commit()
        lead = memory.create_lead(customer_id=customer.id)
        db_session.commit()
        proposal = memory.create_proposal_draft(
            lead_id=lead.id, content={}, total_value=total_value,
            requires_manager_signoff=False, performed_by="test",
        )
        db_session.commit()
        return proposal.id

    def test_requires_auth(self, client, memory, db_session):
        pid = self._make_proposal(client, memory, db_session)
        r = client.get(f"/api/v1/dashboard/proposals/{pid}/required-approval-tier")
        assert r.status_code == 401

    def test_engineer_forbidden(self, client, memory, db_session):
        pid = self._make_proposal(client, memory, db_session)
        r = client.get(
            f"/api/v1/dashboard/proposals/{pid}/required-approval-tier", headers=engineer_headers(),
        )
        assert r.status_code == 403

    def test_admin_sales_management_allowed(self, client, memory, db_session):
        pid = self._make_proposal(client, memory, db_session)
        for headers in (admin_headers(), sales_headers(), management_headers()):
            r = client.get(
                f"/api/v1/dashboard/proposals/{pid}/required-approval-tier", headers=headers,
            )
            assert r.status_code == 200

    def test_returns_the_tier_based_on_the_real_proposal_value(self, client, memory, db_session):
        above = settings.proposal_value_manager_approval_threshold + 1
        pid = self._make_proposal(client, memory, db_session, total_value=above)
        r = client.get(
            f"/api/v1/dashboard/proposals/{pid}/required-approval-tier", headers=admin_headers(),
        )
        assert r.status_code == 200
        body = r.json()
        assert body["required_tier"] == "manager_signoff"
        assert body["based_on"]["proposal_value"] == above

    def test_query_params_feed_the_richer_factors(self, client, memory, db_session):
        pid = self._make_proposal(client, memory, db_session, total_value=1_000.0)
        r = client.get(
            f"/api/v1/dashboard/proposals/{pid}/required-approval-tier",
            params={"discount_percent": approval_engine.PLACEHOLDER_VERY_HIGH_DISCOUNT_PERCENT_THRESHOLD},
            headers=admin_headers(),
        )
        assert r.status_code == 200
        assert r.json()["required_tier"] == "partner_signoff"

    def test_the_existing_2tier_flag_is_shown_unchanged_alongside_it(self, client, memory, db_session):
        pid = self._make_proposal(client, memory, db_session, total_value=1_000.0)
        r = client.get(
            f"/api/v1/dashboard/proposals/{pid}/required-approval-tier", headers=admin_headers(),
        )
        assert r.json()["current_2tier_requires_manager_signoff"] is False

    def test_nonexistent_proposal_404(self, client):
        r = client.get(
            "/api/v1/dashboard/proposals/999999/required-approval-tier", headers=admin_headers(),
        )
        assert r.status_code == 404

    def test_existing_approve_endpoint_is_completely_unaffected(self, client, memory, db_session):
        """Proof this session's work didn't touch the existing 2-tier flow:
        the same approve endpoint, same gating, same behaviour as before."""
        pid = self._make_proposal(client, memory, db_session, total_value=1_000.0)
        r = client.post(f"/api/v1/dashboard/proposals/{pid}/approve", headers=sales_headers())
        assert r.status_code == 200
        assert r.json()["status"] == "approved"
