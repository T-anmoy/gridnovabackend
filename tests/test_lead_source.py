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
        r = client.post("/api/v1/chat/message", json={"message": "hi"})
        assert r.status_code == 200
        lead = memory.get_lead(r.json()["lead_id"])
        assert lead.source == LeadSource.website_chat

    def test_chat_accepts_explicit_source(self, client, memory, db_session):
        r = client.post("/api/v1/chat/message", json={"message": "hi", "source": "instagram"})
        assert r.status_code == 200
        assert memory.get_lead(r.json()["lead_id"]).source == LeadSource.instagram

    def test_chat_rejects_unknown_source(self, client):
        r = client.post("/api/v1/chat/message", json={"message": "hi", "source": "carrier_pigeon"})
        assert r.status_code == 422


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
        items = r.json()
        assert len(items) == 2
        assert all(i["source"] == "whatsapp" for i in items)

    def test_list_includes_source_field(self, client, memory, db_session):
        self._seed(memory, db_session, "cold_call")
        r = client.get("/api/v1/dashboard/leads", headers=admin_headers())
        assert r.status_code == 200
        assert all("source" in i for i in r.json())

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
