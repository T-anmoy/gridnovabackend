"""
PATCH /proposals/{id}/adjust-pricing — the manager-signoff flag must be recomputed
from the Business Rules Engine on every price change, with no way to bypass it.
"""
import pytest

from app.memory.models import PipelineStage, ProposalStatus, TimelineEventType
from tests.conftest import admin_headers, sales_headers, management_headers, engineer_headers

# These tests write employee ids directly (no request provisions them first).
pytestmark = pytest.mark.usefixtures("known_employees")


def _seed_proposal(memory, db_session, *, email, capacity_kw=20, total_value=100_000,
                   requires_signoff=False, assigned_to="emp-sales-1", status=ProposalStatus.draft):
    customer = memory.get_or_create_customer(email=email, name="Cust")
    db_session.commit()
    lead = memory.create_lead(customer_id=customer.id)
    memory.update_lead_fields(
        lead_id=lead.id,
        new_fields={
            "name": "Cust", "email": email, "project_type": "rooftop",
            "segment": "commercial", "location": "Kolkata", "capacity_kw": capacity_kw,
        },
        performed_by="test", stage=PipelineStage.qualified,
    )
    if assigned_to is not None:
        lead.assigned_employee_id = assigned_to
    db_session.commit()
    proposal = memory.create_proposal_draft(
        lead_id=lead.id, content={"x": 1}, total_value=total_value,
        requires_manager_signoff=requires_signoff, performed_by="emp-1",
    )
    if status != ProposalStatus.draft:
        # An approved proposal must carry an approver (DB CheckConstraint).
        approver = "emp-admin-1" if status == ProposalStatus.approved else None
        memory.transition_proposal_status(
            proposal_id=proposal.id, new_status=status, performed_by="test",
            approved_by_employee_id=approver,
        )
    db_session.commit()
    return customer.id, lead.id, proposal.id


class TestFlagRecomputation:
    def test_under_to_over_threshold_flips_signoff_true(self, client, memory, db_session):
        # Start under 500k (standard), adjust to over → must require manager sign-off.
        _, _, pid = _seed_proposal(memory, db_session, email="u2o@example.com",
                                   total_value=100_000, requires_signoff=False)
        r = client.patch(f"/api/v1/dashboard/proposals/{pid}/adjust-pricing",
                         json={"new_value": 750_000, "reason": "added storage + carport"},
                         headers=sales_headers())
        assert r.status_code == 200, r.text
        assert r.json()["requires_manager_signoff"] is True
        assert memory.get_proposal(pid).requires_manager_signoff is True

    def test_over_to_under_threshold_flips_signoff_false(self, client, memory, db_session):
        # Start over 500k (signoff required), adjust down → reverts to standard approval.
        _, _, pid = _seed_proposal(memory, db_session, email="o2u@example.com",
                                   total_value=750_000, requires_signoff=True)
        r = client.patch(f"/api/v1/dashboard/proposals/{pid}/adjust-pricing",
                         json={"new_value": 200_000, "reason": "customer dropped storage"},
                         headers=sales_headers())
        assert r.status_code == 200
        assert r.json()["requires_manager_signoff"] is False
        assert memory.get_proposal(pid).requires_manager_signoff is False

    def test_capacity_driven_signoff_survives_price_drop(self, client, memory, db_session):
        # >1MW capacity requires signoff regardless of value — dropping the price
        # must NOT clear it (recompute uses the full rule, not just value).
        _, _, pid = _seed_proposal(memory, db_session, email="cap@example.com",
                                   capacity_kw=1500, total_value=800_000, requires_signoff=True)
        r = client.patch(f"/api/v1/dashboard/proposals/{pid}/adjust-pricing",
                         json={"new_value": 50_000, "reason": "nudge under to dodge signoff"},
                         headers=sales_headers())
        assert r.status_code == 200
        assert r.json()["requires_manager_signoff"] is True  # capacity keeps it required


class TestGuards:
    def test_requires_auth(self, client, memory, db_session):
        _, _, pid = _seed_proposal(memory, db_session, email="auth@example.com")
        r = client.patch(f"/api/v1/dashboard/proposals/{pid}/adjust-pricing",
                         json={"new_value": 1, "reason": "x"})
        assert r.status_code == 401

    def test_management_and_engineer_forbidden(self, client, memory, db_session):
        _, _, pid = _seed_proposal(memory, db_session, email="perm@example.com")
        for hdr in (management_headers(), engineer_headers()):
            r = client.patch(f"/api/v1/dashboard/proposals/{pid}/adjust-pricing",
                             json={"new_value": 200_000, "reason": "x"}, headers=hdr)
            assert r.status_code == 403

    def test_admin_allowed(self, client, memory, db_session):
        _, _, pid = _seed_proposal(memory, db_session, email="adm@example.com")
        r = client.patch(f"/api/v1/dashboard/proposals/{pid}/adjust-pricing",
                         json={"new_value": 300_000, "reason": "revised BOM"}, headers=admin_headers())
        assert r.status_code == 200

    def test_reason_required(self, client, memory, db_session):
        _, _, pid = _seed_proposal(memory, db_session, email="reason@example.com")
        r = client.patch(f"/api/v1/dashboard/proposals/{pid}/adjust-pricing",
                         json={"new_value": 300_000}, headers=sales_headers())
        assert r.status_code == 422  # reason is mandatory — no silent overrides

    def test_blocked_after_approved(self, client, memory, db_session):
        _, _, pid = _seed_proposal(memory, db_session, email="apprv@example.com",
                                   status=ProposalStatus.approved)
        r = client.patch(f"/api/v1/dashboard/proposals/{pid}/adjust-pricing",
                         json={"new_value": 300_000, "reason": "too late"}, headers=sales_headers())
        assert r.status_code == 409

    def test_allowed_in_pending_pricing_validation(self, client, memory, db_session):
        _, _, pid = _seed_proposal(memory, db_session, email="ppv@example.com",
                                   status=ProposalStatus.pending_pricing_validation)
        r = client.patch(f"/api/v1/dashboard/proposals/{pid}/adjust-pricing",
                         json={"new_value": 300_000, "reason": "validated pricing"}, headers=sales_headers())
        assert r.status_code == 200

    def test_sales_blocked_on_other_leads_proposal(self, client, memory, db_session):
        _, _, pid = _seed_proposal(memory, db_session, email="other@example.com",
                                   assigned_to="emp-sales-2")
        r = client.patch(f"/api/v1/dashboard/proposals/{pid}/adjust-pricing",
                         json={"new_value": 300_000, "reason": "x"}, headers=sales_headers("emp-sales-1"))
        assert r.status_code == 403


class TestAuditAndTimeline:
    def test_audit_and_timeline_recorded(self, client, memory, db_session):
        _, lead_id, pid = _seed_proposal(memory, db_session, email="audit@example.com",
                                         total_value=100_000)
        client.patch(f"/api/v1/dashboard/proposals/{pid}/adjust-pricing",
                     json={"new_value": 640_000, "reason": "scope grew"}, headers=admin_headers())
        # Audit entry with before/after + reason.
        logs = memory.list_audit_logs(entity_type="proposal", entity_id=str(pid))
        adj = [l for l in logs if l.action == "pricing_adjusted"]
        assert adj, "expected a pricing_adjusted audit entry"
        assert adj[0].after_value["reason"] == "scope grew"
        assert adj[0].before_value["total_value"] == 100_000
        assert adj[0].after_value["total_value"] == 640_000
        # Timeline event.
        ctx = memory.get_lead_context(lead_id)
        assert any(e.event_type == TimelineEventType.pricing_adjusted for e in ctx["timeline"])
