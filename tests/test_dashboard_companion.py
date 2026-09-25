"""
Dashboard companion endpoints: recent activity feed, CSV exports, overview stats.
"""
import csv
from datetime import datetime, timedelta, timezone

import pytest

from app.memory.models import LeadSource, PipelineStage, ProposalStatus
from tests.conftest import admin_headers, management_headers, sales_headers, engineer_headers

# These tests write employee ids directly (no request provisions them first).
pytestmark = pytest.mark.usefixtures("known_employees")


def _lead(memory, db_session, *, email, source=LeadSource.website_chat, created_at=None,
          follow_up_date=None, stage=PipelineStage.new):
    c = memory.get_or_create_customer(email=email, name=f"Cust {email}")
    db_session.commit()
    lead = memory.create_lead(customer_id=c.id, source=source)
    memory.update_lead_fields(lead_id=lead.id, new_fields={"name": "C"}, performed_by="t", stage=stage)
    if created_at is not None:
        lead.created_at = created_at
    if follow_up_date is not None:
        lead.follow_up_date = follow_up_date
    db_session.commit()
    return lead


def _proposal(memory, db_session, lead, *, value=100_000, signoff=False, status=ProposalStatus.draft,
              date_created=None):
    p = memory.create_proposal_draft(lead_id=lead.id, content={}, total_value=value,
                                     requires_manager_signoff=signoff, performed_by="emp-1")
    if status != ProposalStatus.draft:
        approver = "emp-admin-1" if status == ProposalStatus.approved else None
        memory.transition_proposal_status(proposal_id=p.id, new_status=status, performed_by="t",
                                          approved_by_employee_id=approver)
    if date_created is not None:
        p.date_created = date_created
    db_session.commit()
    return p


# ── 1. Recent activity feed ──────────────────────────────────────────────────

class TestRecentActivity:
    def test_requires_auth(self, client):
        assert client.get("/api/v1/dashboard/activity/recent").status_code == 401

    def test_all_roles_allowed(self, client):
        for hdr in (admin_headers(), sales_headers(), management_headers(), engineer_headers()):
            assert client.get("/api/v1/dashboard/activity/recent", headers=hdr).status_code == 200

    def test_limit_15_newest_first(self, client, memory, db_session):
        for i in range(20):  # each creates several audit rows
            _lead(memory, db_session, email=f"a{i}@x.com")
        r = client.get("/api/v1/dashboard/activity/recent", headers=admin_headers())
        items = r.json()["items"]
        assert len(items) == 15
        stamps = [i["created_at"] for i in items]
        assert stamps == sorted(stamps, reverse=True)  # newest first

    def test_human_readable_description(self, client, memory, db_session):
        # Pre-provision the admin identity so the auth layer's own get-or-create
        # (see app/auth/dependencies.py) doesn't add an "Employee created" audit
        # entry ahead of the proposal event we're asserting on below.
        memory.get_or_create_employee("emp-admin-1", "Admin", "emp-admin-1@gridnova.in", "admin")
        db_session.commit()
        lead = _lead(memory, db_session, email="desc@x.com")
        p = _proposal(memory, db_session, lead)
        memory.transition_proposal_status(proposal_id=p.id, new_status=ProposalStatus.approved,
                                          performed_by="emp-1", approved_by_employee_id="emp-admin-1")
        db_session.commit()
        items = client.get("/api/v1/dashboard/activity/recent", headers=admin_headers()).json()["items"]
        top = items[0]
        assert top["description"].startswith("Proposal #")
        assert "approved" in top["description"]

    def test_engineer_excludes_pricing_entries(self, client, memory, db_session):
        lead = _lead(memory, db_session, email="price@x.com")
        p = _proposal(memory, db_session, lead)
        memory.adjust_proposal_pricing(proposal_id=p.id, new_value=640_000,
                                       requires_manager_signoff=True, reason="scope grew",
                                       performed_by="emp-sales-1")
        db_session.commit()
        eng = client.get("/api/v1/dashboard/activity/recent", headers=engineer_headers()).json()["items"]
        adm = client.get("/api/v1/dashboard/activity/recent", headers=admin_headers()).json()["items"]
        assert not any(i["action"] == "pricing_adjusted" for i in eng)
        assert any(i["action"] == "pricing_adjusted" for i in adm)


# ── 2. CSV exports ───────────────────────────────────────────────────────────

def _parse_csv(text: str):
    return list(csv.reader(text.splitlines()))


class TestLeadsExport:
    def test_role_gating(self, client):
        assert client.get("/api/v1/dashboard/leads/export", headers=sales_headers()).status_code == 403
        assert client.get("/api/v1/dashboard/leads/export", headers=engineer_headers()).status_code == 403
        assert client.get("/api/v1/dashboard/leads/export", headers=admin_headers()).status_code == 200
        assert client.get("/api/v1/dashboard/leads/export", headers=management_headers()).status_code == 200

    def test_csv_structure_and_headers(self, client, memory, db_session):
        _lead(memory, db_session, email="csv1@x.com", source=LeadSource.whatsapp)
        r = client.get("/api/v1/dashboard/leads/export", headers=admin_headers())
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/csv")
        assert "attachment; filename=" in r.headers["content-disposition"]
        assert "leads-export-" in r.headers["content-disposition"]
        rows = _parse_csv(r.text)
        assert rows[0] == ["lead_id", "customer_name", "customer_email", "source",
                           "pipeline_stage", "lead_score", "assigned_employee_id", "created_at"]
        assert len(rows) >= 2  # header + at least one lead

    def test_source_filter_respected(self, client, memory, db_session):
        _lead(memory, db_session, email="wa@x.com", source=LeadSource.whatsapp)
        _lead(memory, db_session, email="ig@x.com", source=LeadSource.instagram)
        r = client.get("/api/v1/dashboard/leads/export", params={"source": "whatsapp"},
                       headers=admin_headers())
        rows = _parse_csv(r.text)[1:]  # drop header
        assert len(rows) == 1
        assert rows[0][3] == "whatsapp"


class TestProposalsExport:
    def test_role_gating(self, client):
        assert client.get("/api/v1/dashboard/proposals/export", headers=sales_headers()).status_code == 403
        assert client.get("/api/v1/dashboard/proposals/export", headers=admin_headers()).status_code == 200

    def test_status_filter_respected(self, client, memory, db_session):
        l1 = _lead(memory, db_session, email="p1@x.com")
        l2 = _lead(memory, db_session, email="p2@x.com")
        _proposal(memory, db_session, l1, status=ProposalStatus.draft)
        _proposal(memory, db_session, l2, status=ProposalStatus.approved)
        r = client.get("/api/v1/dashboard/proposals/export", params={"status": "approved"},
                       headers=admin_headers())
        rows = _parse_csv(r.text)
        assert rows[0][0] == "proposal_id"
        data = rows[1:]
        assert len(data) == 1
        assert data[0][4] == "approved"


# ── 3. Overview stats + trends ───────────────────────────────────────────────

class TestOverviewStats:
    def test_requires_auth(self, client):
        assert client.get("/api/v1/dashboard/overview-stats").status_code == 401

    def test_trend_computed_across_two_days(self, client, memory, db_session):
        now = datetime.now(timezone.utc)
        today = datetime(now.year, now.month, now.day, 12, 0, tzinfo=timezone.utc)
        yesterday = today - timedelta(days=1)

        # 2 leads today, 1 lead yesterday.
        _lead(memory, db_session, email="t1@x.com", created_at=today)
        _lead(memory, db_session, email="t2@x.com", created_at=today)
        _lead(memory, db_session, email="y1@x.com", created_at=yesterday)

        body = client.get("/api/v1/dashboard/overview-stats", headers=admin_headers()).json()
        assert body["new_leads_today"] == 2
        assert body["trends"]["new_leads_today"] == 1
        # additive shape: the four headline fields + a trends object
        assert set(body) >= {"new_leads_today", "follow_ups_due",
                             "pending_approval", "pending_manager_signoff", "trends"}

    def test_pending_and_followups(self, client, memory, db_session):
        now = datetime.now(timezone.utc)
        lead = _lead(memory, db_session, email="ov@x.com",
                     follow_up_date=now - timedelta(hours=1))  # due
        _proposal(memory, db_session, lead, signoff=False, status=ProposalStatus.draft)
        _proposal(memory, db_session, lead, signoff=True, status=ProposalStatus.draft)
        body = client.get("/api/v1/dashboard/overview-stats", headers=admin_headers()).json()
        assert body["follow_ups_due"] == 1
        assert body["pending_approval"] == 1
        assert body["pending_manager_signoff"] == 1
