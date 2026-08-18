"""
Dashboard read/list endpoints — verifies query-level role scoping, projections,
pagination, and the Part 9 permissions matrix on the new read surface.
"""
import pytest

from app.memory.models import PipelineStage, ProposalStatus, TimelineEventType
from tests.conftest import (
    admin_headers,
    sales_headers,
    management_headers,
    engineer_headers,
)


def _seed_lead_with_proposal(
    memory,
    db_session,
    *,
    email,
    assigned_to=None,
    total_value=200_000,
    requires_signoff=False,
    stage=PipelineStage.qualified,
    capacity_kw=50,
):
    customer = memory.get_or_create_customer(email=email, name=f"Cust {email}")
    db_session.commit()
    lead = memory.create_lead(customer_id=customer.id)
    memory.update_lead_fields(
        lead_id=lead.id,
        new_fields={
            "name": f"Cust {email}",
            "email": email,
            "project_type": "commercial",
            "location": "Kolkata",
            "capacity_kw": capacity_kw,
            "roof_type": "flat",
            "budget": total_value,
        },
        performed_by="test",
        stage=stage,
    )
    if assigned_to is not None:
        lead.assigned_employee_id = assigned_to
    db_session.commit()
    proposal = memory.create_proposal_draft(
        lead_id=lead.id,
        content={"customer_name": f"Cust {email}", "line_items": ["panel"]},
        total_value=total_value,
        requires_manager_signoff=requires_signoff,
        performed_by="emp-1",
    )
    db_session.commit()
    return customer.id, lead.id, proposal.id


class TestProposalsList:
    def test_requires_auth(self, client):
        assert client.get("/api/v1/dashboard/proposals").status_code == 401

    def test_admin_sees_all_with_envelope(self, client, memory, db_session):
        _seed_lead_with_proposal(memory, db_session, email="a@example.com")
        _seed_lead_with_proposal(memory, db_session, email="b@example.com")
        r = client.get("/api/v1/dashboard/proposals", headers=admin_headers())
        assert r.status_code == 200
        body = r.json()
        assert set(body) == {"items", "page", "page_size", "total"}
        assert body["total"] >= 2
        assert len(body["items"]) >= 2

    def test_sales_scoped_to_own_leads_at_query_level(self, client, memory, db_session):
        _seed_lead_with_proposal(memory, db_session, email="own@example.com", assigned_to="emp-sales-1")
        _seed_lead_with_proposal(memory, db_session, email="other@example.com", assigned_to="emp-sales-2")
        r = client.get("/api/v1/dashboard/proposals", headers=sales_headers("emp-sales-1"))
        assert r.status_code == 200
        items = r.json()["items"]
        assert len(items) == 1
        assert items[0]["lead"]["id"] is not None
        assert items[0]["customer"]["email"] == "own@example.com"

    def test_status_filter(self, client, memory, db_session):
        _seed_lead_with_proposal(memory, db_session, email="draft@example.com")
        r = client.get(
            "/api/v1/dashboard/proposals",
            params={"status": "draft"},
            headers=admin_headers(),
        )
        assert r.status_code == 200
        assert all(i["status"] == ProposalStatus.draft.value for i in r.json()["items"])

    def test_invalid_status_422(self, client):
        r = client.get(
            "/api/v1/dashboard/proposals",
            params={"status": "nonsense"},
            headers=admin_headers(),
        )
        assert r.status_code == 422

    def test_pagination(self, client, memory, db_session):
        for i in range(3):
            _seed_lead_with_proposal(memory, db_session, email=f"p{i}@example.com")
        r = client.get(
            "/api/v1/dashboard/proposals",
            params={"page": 1, "page_size": 2},
            headers=admin_headers(),
        )
        assert r.status_code == 200
        body = r.json()
        assert len(body["items"]) == 2
        assert body["total"] >= 3

    def test_engineer_projection_hides_pricing(self, client, memory, db_session):
        _seed_lead_with_proposal(memory, db_session, email="eng@example.com")
        r = client.get("/api/v1/dashboard/proposals", headers=engineer_headers())
        assert r.status_code == 200
        item = r.json()["items"][0]
        assert "total_value" not in item
        assert "customer" not in item
        assert "technical_fields" in item
        assert item["technical_fields"]["capacity_kw"] == 50


class TestProposalDetail:
    def test_not_found(self, client):
        assert client.get("/api/v1/dashboard/proposals/99999", headers=admin_headers()).status_code == 404

    def test_admin_full_detail_with_versions(self, client, memory, db_session):
        _, lead_id, pid = _seed_lead_with_proposal(memory, db_session, email="d@example.com")
        # second version for the same lead
        memory.create_proposal_draft(
            lead_id=lead_id, content={"v": 2}, total_value=250_000,
            requires_manager_signoff=False, performed_by="emp-1",
        )
        db_session.commit()
        r = client.get(f"/api/v1/dashboard/proposals/{pid}", headers=admin_headers())
        assert r.status_code == 200
        body = r.json()
        assert body["content"] is not None
        assert len(body["versions"]) == 2

    def test_engineer_detail_projection(self, client, memory, db_session):
        _, _, pid = _seed_lead_with_proposal(memory, db_session, email="engd@example.com")
        r = client.get(f"/api/v1/dashboard/proposals/{pid}", headers=engineer_headers())
        assert r.status_code == 200
        body = r.json()
        assert "content" not in body
        assert "total_value" not in body
        assert "technical_fields" in body

    def test_sales_blocked_on_other_leads_proposal(self, client, memory, db_session):
        _, _, pid = _seed_lead_with_proposal(
            memory, db_session, email="mine.not@example.com", assigned_to="emp-sales-2"
        )
        r = client.get(f"/api/v1/dashboard/proposals/{pid}", headers=sales_headers("emp-sales-1"))
        assert r.status_code == 403


class TestProposalPdfAccess:
    """The PDF carries pricing + contact — Engineers blocked, Sales scoped to own."""

    def _seed_with_pdf(self, memory, db_session, assigned_to=None):
        _, lead_id, pid = _seed_lead_with_proposal(
            memory, db_session, email=f"pdf{id(self)}@example.com", assigned_to=assigned_to
        )
        from app.memory.models import ProposalStatus
        memory.transition_proposal_status(
            proposal_id=pid, new_status=ProposalStatus.approved,
            performed_by="test", approved_by_employee_id="emp-admin-1",
            pdf_url="https://r2/proposals/x.pdf",
        )
        db_session.commit()
        return pid

    def test_engineer_blocked_from_pdf(self, client, memory, db_session):
        pid = self._seed_with_pdf(memory, db_session)
        r = client.get(f"/api/v1/dashboard/proposals/{pid}/pdf", headers=engineer_headers())
        assert r.status_code == 403

    def test_sales_blocked_on_other_leads_pdf(self, client, memory, db_session):
        pid = self._seed_with_pdf(memory, db_session, assigned_to="emp-sales-2")
        r = client.get(f"/api/v1/dashboard/proposals/{pid}/pdf", headers=sales_headers("emp-sales-1"))
        assert r.status_code == 403

    def test_admin_and_management_can_access_pdf(self, client, memory, db_session):
        pid = self._seed_with_pdf(memory, db_session)
        assert client.get(f"/api/v1/dashboard/proposals/{pid}/pdf", headers=admin_headers()).status_code == 200
        assert client.get(f"/api/v1/dashboard/proposals/{pid}/pdf", headers=management_headers()).status_code == 200


class TestAnalytics:
    def test_engineer_blocked(self, client):
        assert client.get("/api/v1/dashboard/analytics", headers=engineer_headers()).status_code == 403

    def test_admin_full_shape(self, client, memory, db_session):
        _seed_lead_with_proposal(memory, db_session, email="an@example.com", stage=PipelineStage.won)
        r = client.get("/api/v1/dashboard/analytics", headers=admin_headers())
        assert r.status_code == 200
        body = r.json()
        assert set(body["funnel"]) == {"new", "qualified", "proposal_sent", "won"}
        for key in (
            "new_leads_count", "qualification_completion_rate",
            "lead_to_proposal_conversion_rate", "avg_proposal_turnaround_hours",
            "ai_call_count", "ai_estimated_cost", "escalation_rate",
        ):
            assert key in body
        assert body["funnel"]["won"] >= 1

    def test_sales_cannot_query_other_employee(self, client):
        r = client.get(
            "/api/v1/dashboard/analytics",
            params={"employee_id": "emp-sales-2"},
            headers=sales_headers("emp-sales-1"),
        )
        assert r.status_code == 403

    def test_sales_own_metrics_scoped(self, client, memory, db_session):
        _seed_lead_with_proposal(memory, db_session, email="mine@example.com", assigned_to="emp-sales-1")
        _seed_lead_with_proposal(memory, db_session, email="theirs@example.com", assigned_to="emp-sales-2")
        r = client.get("/api/v1/dashboard/analytics", headers=sales_headers("emp-sales-1"))
        assert r.status_code == 200
        assert r.json()["new_leads_count"] == 1


class TestEmployees:
    def test_list_admin_only(self, client, memory, db_session):
        memory.get_or_create_employee("emp-x", "X", "x@example.com", "sales")
        db_session.commit()
        assert client.get("/api/v1/dashboard/employees", headers=sales_headers()).status_code == 403
        r = client.get("/api/v1/dashboard/employees", headers=admin_headers())
        assert r.status_code == 200
        assert any(e["id"] == "emp-x" for e in r.json())

    def test_role_update_admin_only(self, client, memory, db_session):
        memory.get_or_create_employee("emp-role", "R", "r@example.com", "sales")
        db_session.commit()
        # Non-admin blocked
        r = client.post(
            "/api/v1/dashboard/employees/emp-role/role",
            json={"role": "management"},
            headers=management_headers(),
        )
        assert r.status_code == 403
        # Admin allowed; Clerk sync skipped in dev (no secret key)
        r = client.post(
            "/api/v1/dashboard/employees/emp-role/role",
            json={"role": "management"},
            headers=admin_headers(),
        )
        assert r.status_code == 200
        body = r.json()
        assert body["role"] == "management"
        assert body["clerk_synced"] is False
        assert body["local_mirror"] is True
        # Local mirror actually changed
        assert memory.get_employee("emp-role").role.value == "management"

    def test_role_update_invalid_role_422(self, client):
        r = client.post(
            "/api/v1/dashboard/employees/whoever/role",
            json={"role": "wizard"},
            headers=admin_headers(),
        )
        assert r.status_code == 422


class TestAuditLog:
    def test_admin_and_management_allowed_others_blocked(self, client, memory, db_session):
        _seed_lead_with_proposal(memory, db_session, email="aud@example.com")
        assert client.get("/api/v1/dashboard/audit-log", headers=admin_headers()).status_code == 200
        assert client.get("/api/v1/dashboard/audit-log", headers=management_headers()).status_code == 200
        assert client.get("/api/v1/dashboard/audit-log", headers=sales_headers()).status_code == 403
        assert client.get("/api/v1/dashboard/audit-log", headers=engineer_headers()).status_code == 403

    def test_envelope_and_entity_filter(self, client, memory, db_session):
        _seed_lead_with_proposal(memory, db_session, email="audf@example.com")
        r = client.get(
            "/api/v1/dashboard/audit-log",
            params={"entity_type": "proposal", "page_size": 5},
            headers=admin_headers(),
        )
        assert r.status_code == 200
        body = r.json()
        assert set(body) == {"items", "page", "page_size", "total"}
        assert all(i["entity_type"] == "proposal" for i in body["items"])
