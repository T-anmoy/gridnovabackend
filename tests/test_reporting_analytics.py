"""
Phase 3 — reporting analytics: aggregation math, empty-data edges, role-gating.
"""
from datetime import datetime, timezone

import pytest

from app.memory.models import LeadSource, PipelineStage, ProposalStatus
from tests.conftest import admin_headers, management_headers, sales_headers, engineer_headers

ENDPOINTS = [
    "/api/v1/dashboard/analytics/conversion-by-source",
    "/api/v1/dashboard/analytics/deal-size-by-tier",
    "/api/v1/dashboard/analytics/rep-performance",
    "/api/v1/dashboard/analytics/pipeline-trend",
]


def _lead(memory, db_session, *, email, source=LeadSource.website_chat, stage=PipelineStage.new,
          assigned_to=None, project_type="rooftop", capacity_kw=8):
    c = memory.get_or_create_customer(email=email, name="C")
    db_session.commit()
    lead = memory.create_lead(customer_id=c.id, source=source)
    memory.update_lead_fields(
        lead_id=lead.id,
        new_fields={"name": "C", "project_type": project_type, "capacity_kw": capacity_kw},
        performed_by="t", stage=stage,
    )
    if assigned_to is not None:
        lead.assigned_employee_id = assigned_to
    db_session.commit()
    return lead


class TestRoleGating:
    @pytest.mark.parametrize("path", ENDPOINTS)
    def test_requires_auth(self, client, path):
        assert client.get(path).status_code == 401

    @pytest.mark.parametrize("path", ENDPOINTS)
    def test_sales_and_engineer_forbidden(self, client, path):
        assert client.get(path, headers=sales_headers()).status_code == 403
        assert client.get(path, headers=engineer_headers()).status_code == 403

    @pytest.mark.parametrize("path", ENDPOINTS)
    def test_admin_and_management_allowed(self, client, path):
        assert client.get(path, headers=admin_headers()).status_code == 200
        assert client.get(path, headers=management_headers()).status_code == 200


class TestEmptyData:
    @pytest.mark.parametrize("path", ENDPOINTS)
    def test_empty_returns_200(self, client, path):
        r = client.get(path, headers=admin_headers())
        assert r.status_code == 200


class TestConversionBySource:
    def test_math(self, client, memory, db_session):
        # whatsapp: 2 leads, 1 won → 0.5 ; referral: 1 lead, 0 won → 0.0
        _lead(memory, db_session, email="w1@x.com", source=LeadSource.whatsapp, stage=PipelineStage.won)
        _lead(memory, db_session, email="w2@x.com", source=LeadSource.whatsapp, stage=PipelineStage.new)
        _lead(memory, db_session, email="r1@x.com", source=LeadSource.referral, stage=PipelineStage.new)
        r = client.get("/api/v1/dashboard/analytics/conversion-by-source", headers=admin_headers())
        by = {row["source"]: row for row in r.json()["by_source"]}
        assert by["whatsapp"]["total_leads"] == 2 and by["whatsapp"]["won_leads"] == 1
        assert by["whatsapp"]["conversion_rate"] == 0.5
        assert by["referral"]["conversion_rate"] == 0.0


class TestDealSizeByTier:
    def test_groups_and_averages(self, client, memory, db_session):
        # Two rooftop 0-10kW proposals (8kW) with values 100k and 200k → avg 150k.
        for email, val in [("a@x.com", 100_000), ("b@x.com", 200_000)]:
            lead = _lead(memory, db_session, email=email, project_type="rooftop", capacity_kw=8)
            memory.create_proposal_draft(lead_id=lead.id, content={}, total_value=val,
                                         requires_manager_signoff=False, performed_by="t")
        db_session.commit()
        r = client.get("/api/v1/dashboard/analytics/deal-size-by-tier", headers=admin_headers())
        tiers = {row["tier"]: row for row in r.json()["by_tier"]}
        rooftop_small = tiers["rooftop 0-10kW"]
        assert rooftop_small["proposal_count"] == 2
        assert rooftop_small["total_value"] == 300_000
        assert rooftop_small["average_value"] == 150_000


class TestRepPerformance:
    def test_counts_and_time_to_close(self, client, memory, db_session):
        memory.get_or_create_employee("emp-sales-1", "Rep One", "rep1@gridnova.in", "sales")
        db_session.commit()
        lead = _lead(memory, db_session, email="rep@x.com", assigned_to="emp-sales-1",
                     stage=PipelineStage.won)
        p = memory.create_proposal_draft(lead_id=lead.id, content={}, total_value=100_000,
                                         requires_manager_signoff=False, performed_by="t")
        memory.transition_proposal_status(proposal_id=p.id, new_status=ProposalStatus.approved,
                                          performed_by="t", approved_by_employee_id="emp-sales-1")
        db_session.commit()
        r = client.get("/api/v1/dashboard/analytics/rep-performance", headers=admin_headers())
        reps = {row["employee_id"]: row for row in r.json()["reps"]}
        assert "emp-sales-1" in reps
        rep = reps["emp-sales-1"]
        assert rep["leads_handled"] == 1
        assert rep["proposals_drafted"] == 1
        assert rep["proposals_won"] == 1
        assert rep["avg_time_to_close_hours"] is not None


class TestPipelineTrend:
    def test_returns_12_weeks(self, client, memory, db_session):
        _lead(memory, db_session, email="pt@x.com")
        r = client.get("/api/v1/dashboard/analytics/pipeline-trend", headers=admin_headers())
        weeks = r.json()["weeks"]
        assert len(weeks) == 12
        assert all({"week_start", "leads_created", "proposals_won"} <= set(w) for w in weeks)
        # The lead created now lands in the most recent week bucket.
        assert weeks[-1]["leads_created"] >= 1
