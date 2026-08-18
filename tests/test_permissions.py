"""
Permissions matrix enforcement — the most critical correctness requirement.
Every test directly confirms a blocked or allowed action.
"""
import pytest
from app.memory.models import PipelineStage, ProposalStatus
from tests.conftest import admin_headers, sales_headers, management_headers, engineer_headers


def _make_over_threshold_proposal(memory, db_session):
    """Seed a qualified lead with a proposal that requires manager sign-off."""
    customer = memory.get_or_create_customer(email="hv@example.com", name="High Value")
    db_session.commit()
    lead = memory.create_lead(customer_id=customer.id)
    memory.update_lead_fields(
        lead_id=lead.id,
        new_fields={
            "name": "High Value",
            "email": "hv@example.com",
            "project_type": "industrial",
            "location": "Faridabad",
            "capacity_kw": 1500,  # > 1 MW → requires signoff
            "budget": 70_000_000,
            "timeline": "12 months",
            "monthly_electricity_bill": 500000,
        },
        performed_by="test",
        stage=PipelineStage.qualified,
    )
    db_session.commit()
    proposal = memory.create_proposal_draft(
        lead_id=lead.id,
        content={"customer_name": "High Value"},
        total_value=70_000_000,
        requires_manager_signoff=True,
        performed_by="emp-1",
    )
    db_session.commit()
    return lead.id, customer.id, proposal.id


def _make_standard_proposal(memory, db_session):
    """Seed a qualified lead with a proposal below the threshold."""
    customer = memory.get_or_create_customer(email="std@example.com", name="Standard")
    db_session.commit()
    lead = memory.create_lead(customer_id=customer.id)
    memory.update_lead_fields(
        lead_id=lead.id,
        new_fields={
            "name": "Standard",
            "email": "std@example.com",
            "project_type": "residential",
            "location": "Kolkata",
            "capacity_kw": 5,
        },
        performed_by="test",
        stage=PipelineStage.qualified,
    )
    db_session.commit()
    proposal = memory.create_proposal_draft(
        lead_id=lead.id,
        content={"customer_name": "Standard"},
        total_value=200_000,
        requires_manager_signoff=False,
        performed_by="emp-1",
    )
    db_session.commit()
    return lead.id, customer.id, proposal.id


class TestApprovalGate:
    def test_sales_cannot_approve_over_threshold_proposal(self, client, memory, db_session):
        """CRITICAL: Sales role is rejected (403) on a manager-sign-off proposal."""
        _, _, pid = _make_over_threshold_proposal(memory, db_session)
        r = client.post(f"/api/v1/dashboard/proposals/{pid}/approve", headers=sales_headers())
        assert r.status_code == 403

    def test_management_cannot_standard_approve(self, client, memory, db_session):
        """Management role cannot use the standard approval endpoint on any proposal."""
        _, _, pid = _make_standard_proposal(memory, db_session)
        r = client.post(f"/api/v1/dashboard/proposals/{pid}/approve", headers=management_headers())
        assert r.status_code == 403

    def test_management_can_sign_off_over_threshold(self, client, memory, db_session):
        _, _, pid = _make_over_threshold_proposal(memory, db_session)
        r = client.post(f"/api/v1/dashboard/proposals/{pid}/manager-sign-off", headers=management_headers())
        assert r.status_code == 200

    def test_sales_cannot_manager_sign_off(self, client, memory, db_session):
        _, _, pid = _make_over_threshold_proposal(memory, db_session)
        r = client.post(f"/api/v1/dashboard/proposals/{pid}/manager-sign-off", headers=sales_headers())
        assert r.status_code == 403

    def test_admin_can_standard_approve(self, client, memory, db_session):
        _, _, pid = _make_standard_proposal(memory, db_session)
        r = client.post(f"/api/v1/dashboard/proposals/{pid}/approve", headers=admin_headers())
        assert r.status_code == 200

    def test_admin_can_manager_sign_off(self, client, memory, db_session):
        _, _, pid = _make_over_threshold_proposal(memory, db_session)
        r = client.post(f"/api/v1/dashboard/proposals/{pid}/manager-sign-off", headers=admin_headers())
        assert r.status_code == 200

    def test_sales_cannot_use_manager_signoff_route_on_standard_proposal(self, client, memory, db_session):
        """Using the manager-sign-off route on a standard proposal returns 400 (wrong endpoint)."""
        _, _, pid = _make_standard_proposal(memory, db_session)
        r = client.post(f"/api/v1/dashboard/proposals/{pid}/manager-sign-off", headers=management_headers())
        assert r.status_code == 400  # wrong route for this proposal type

    def test_engineer_cannot_draft_proposal(self, client, memory, db_session):
        customer = memory.get_or_create_customer(email="eng@example.com", name="Eng")
        db_session.commit()
        lead = memory.create_lead(customer_id=customer.id)
        db_session.commit()
        r = client.post("/api/v1/dashboard/proposals/draft", json={
            "lead_id": lead.id, "customer_id": customer.id,
        }, headers=engineer_headers())
        assert r.status_code == 403

    def test_revision_blocked_for_management(self, client, memory, db_session):
        _, _, pid = _make_standard_proposal(memory, db_session)
        r = client.post(f"/api/v1/dashboard/proposals/{pid}/request-revision",
                        json={"reason": "Change needed"},
                        headers=management_headers())
        assert r.status_code == 403

    def test_send_blocked_for_management(self, client, memory, db_session):
        """Management cannot send a proposal even if it's approved."""
        memory2 = memory
        _, _, pid = _make_standard_proposal(memory2, db_session)
        # Approve it first as admin
        client.post(f"/api/v1/dashboard/proposals/{pid}/approve", headers=admin_headers())
        r = client.post(f"/api/v1/dashboard/proposals/{pid}/send", headers=management_headers())
        assert r.status_code == 403
