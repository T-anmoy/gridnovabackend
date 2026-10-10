"""
Phase 1 — lead source attribution: persistence, default, and dashboard filtering.
"""
import pytest

from app.memory.models import LeadSource
from tests.conftest import admin_headers


ALL_SOURCES = ["website_chat", "whatsapp", "instagram", "cold_call", "referral", "other"]


class TestSourcePersistence:
    @pytest.mark.parametrize("source", ALL_SOURCES)
    def test_each_source_persists(self, memory, db_session, source):
        customer = memory.get_or_create_customer(email=f"{source}@example.com", name="C")
        db_session.commit()
        lead = memory.create_lead(customer_id=customer.id, source=LeadSource(source))
        db_session.commit()
        assert memory.get_lead(lead.id).source == LeadSource(source)

    def test_default_is_website_chat(self, memory, db_session):
        customer = memory.get_or_create_customer(email="def@example.com", name="C")
        db_session.commit()
        lead = memory.create_lead(customer_id=customer.id)  # source omitted
        db_session.commit()
        assert memory.get_lead(lead.id).source == LeadSource.website_chat

    def test_chat_widget_defaults_to_website_chat(self, client, memory, db_session):
        from tests.chat_support import lead_of
        r = client.post("/api/v1/chat/message", json={"message": "hi"})
        assert r.status_code == 200
        assert lead_of(db_session, r.json()["session_token"]).source == LeadSource.website_chat

    @pytest.mark.parametrize("forced", ["instagram", "associate", "preferred_associate", "carrier_pigeon"])
    def test_chat_source_cannot_be_forced(self, client, db_session, forced):
        """BE-1 S2: the deprecated `source` field is ignored — a public chat lead is
        always website_chat (it used to accept any LeadSource, including associate)."""
        from tests.chat_support import lead_of
        r = client.post("/api/v1/chat/message", json={"message": "hi", "source": forced})
        assert r.status_code == 200
        assert lead_of(db_session, r.json()["session_token"]).source == LeadSource.website_chat


class TestSourceFiltering:
    def _seed(self, memory, db_session, source, n=1):
        for i in range(n):
            c = memory.get_or_create_customer(email=f"{source}-{i}@example.com", name="C")
            db_session.commit()
            memory.create_lead(customer_id=c.id, source=LeadSource(source))
        db_session.commit()

    def test_filter_returns_only_matching(self, client, memory, db_session):
        self._seed(memory, db_session, "whatsapp", n=2)
        self._seed(memory, db_session, "referral", n=3)
        r = client.get("/api/v1/dashboard/leads", params={"source": "whatsapp"}, headers=admin_headers())
        assert r.status_code == 200
        items = r.json()["items"]
        assert len(items) == 2
        assert all(i["source"] == "whatsapp" for i in items)

    def test_list_includes_source_field(self, client, memory, db_session):
        self._seed(memory, db_session, "cold_call")
        r = client.get("/api/v1/dashboard/leads", headers=admin_headers())
        assert r.status_code == 200
        assert all("source" in i for i in r.json()["items"])

    def test_invalid_filter_422(self, client):
        r = client.get("/api/v1/dashboard/leads", params={"source": "nope"}, headers=admin_headers())
        assert r.status_code == 422

    def test_detail_includes_source(self, client, memory, db_session):
        c = memory.get_or_create_customer(email="detail@example.com", name="C")
        db_session.commit()
        lead = memory.create_lead(customer_id=c.id, source=LeadSource.referral)
        db_session.commit()
        r = client.get(f"/api/v1/dashboard/leads/{lead.id}", headers=admin_headers())
        assert r.status_code == 200
        assert r.json()["source"] == "referral"


class TestLeadOrdering:
    """list_leads() previously had no order_by at all, so the dashboard's Leads
    view could render in arbitrary (effectively insertion/oldest-first) order.
    Newest-first is the expected order everywhere leads are listed."""

    def test_leads_are_returned_newest_first(self, client, memory, db_session):
        ids = []
        for i in range(4):
            c = memory.get_or_create_customer(email=f"order-{i}@example.com", name="C")
            db_session.commit()
            lead = memory.create_lead(customer_id=c.id, source=LeadSource.referral)
            db_session.commit()
            ids.append(lead.id)

        r = client.get("/api/v1/dashboard/leads", headers=admin_headers())
        assert r.status_code == 200
        returned_ids = [i["id"] for i in r.json()["items"] if i["id"] in ids]
        # Same insertion order as `ids` (oldest -> newest); the endpoint must
        # reverse that, newest first.
        assert returned_ids == list(reversed(ids))
