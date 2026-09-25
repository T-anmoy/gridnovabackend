"""POST /api/v1/public/contact — the website contact form into the CRM, guarded the
same way as /public/estimate-request."""
import pytest

import app.notifications.notifier as notifier_module
from app.config import settings
from app.memory.models import LeadSource
from app.notifications.notifier import EmailNotifier, LogNotifier

CONTACT = "/api/v1/public/contact"
KEY = "contact-test-key"


def _body(**overrides):
    body = {"name": "Arjun Roy", "email": "arjun@example.com", "phone": "9831000000",
            "message": "Can you visit our factory roof next week?"}
    body.update(overrides)
    return body


class TestSubmit:
    def test_creates_a_contact_lead_with_the_message_as_notes(self, client, memory, db_session):
        r = client.post(CONTACT, json=_body(interest="Commercial rooftop"))
        assert r.status_code == 200
        assert set(r.json()) == {"lead_id", "success"} and r.json()["success"] is True
        db_session.expire_all()
        lead = memory.get_lead(r.json()["lead_id"])
        assert lead.source == LeadSource.website_contact
        assert lead.collected_fields["notes"] == "Can you visit our factory roof next week?"
        assert lead.collected_fields["interest"] == "Commercial rooftop"
        assert lead.collected_fields["phone"] == "9831000000"
        assert lead.project is not None and lead.project.reference_id

    def test_matches_an_existing_customer(self, client, memory, db_session):
        existing = memory.get_or_create_customer(email="arjun@example.com", name="Arjun")
        db_session.commit()
        lead_id = client.post(CONTACT, json=_body(email="ARJUN@example.com")).json()["lead_id"]
        db_session.expire_all()
        assert memory.get_lead(lead_id).customer_id == existing.id

    @pytest.mark.parametrize("override", [
        {"message": "x" * 2001}, {"message": "   "}, {"message": ""}, {"name": ""},
        {"email": "not-an-email"}, {"phone": "9" * 31},
    ])
    def test_validation(self, client, override):
        assert client.post(CONTACT, json=_body(**override)).status_code == 422

    def test_message_of_exactly_2000_chars_is_accepted(self, client):
        assert client.post(CONTACT, json=_body(message="y" * 2000)).status_code == 200


class TestGuards:
    def test_per_ip_rate_limit(self, client):
        for i in range(10):
            assert client.post(CONTACT, json=_body(email=f"c{i}@example.com")).status_code == 200
        assert client.post(CONTACT, json=_body(email="c-over@example.com")).status_code == 429

    def test_per_email_cooldown(self, client):
        assert client.post(CONTACT, json=_body()).status_code == 200
        r = client.post(CONTACT, json=_body())
        assert r.status_code == 429 and "A message for this email" in r.json()["detail"]

    def test_buckets_are_separate_from_the_estimate_form(self, client):
        assert client.post("/api/v1/public/estimate-request",
                           json={"name": "A", "email": "arjun@example.com"}).status_code == 200
        assert client.post(CONTACT, json=_body()).status_code == 200

    def test_internal_key_skips_ip_limit_and_honours_customer_ip(self, client, monkeypatch):
        monkeypatch.setattr(settings, "internal_api_key", KEY)
        for i in range(12):
            r = client.post(CONTACT, json=_body(email=f"k{i}@example.com"), headers={"X-Internal-Key": KEY})
            assert r.status_code == 200
        visitor = {"X-Internal-Key": KEY, "X-Customer-IP": "203.0.113.5"}
        for i in range(10):
            assert client.post(CONTACT, json=_body(email=f"v{i}@example.com"), headers=visitor).status_code == 200
        assert client.post(CONTACT, json=_body(email="v-over@example.com"), headers=visitor).status_code == 429

    def test_wrong_key_is_ignored(self, client, monkeypatch):
        monkeypatch.setattr(settings, "internal_api_key", KEY)
        wrong = {"X-Internal-Key": "nope", "X-Customer-IP": "203.0.113.5"}
        for i in range(10):
            assert client.post(CONTACT, json=_body(email=f"w{i}@example.com"), headers=wrong).status_code == 200
        assert client.post(CONTACT, json=_body(email="w-over@example.com"), headers=wrong).status_code == 429


class TestNoStaffEmail:
    def test_no_email_is_sent(self, client, monkeypatch):
        sent = []
        monkeypatch.setattr(notifier_module.httpx, "post",
                            lambda *a, **k: sent.append(k.get("json")) or type("R", (), {"status_code": 200})())
        monkeypatch.setattr(notifier_module, "notifier", EmailNotifier(
            api_key="re_x", from_email="n@gridnova.in", staff_emails=["ops@gridnova.in"],
            frontend_base_url="https://www.gridnova.in", inner=LogNotifier()))
        assert client.post(CONTACT, json=_body()).status_code == 200
        assert sent == []

    def test_contact_source_is_filtered_even_if_notified(self, monkeypatch):
        sent = []
        monkeypatch.setattr(notifier_module.httpx, "post", lambda *a, **k: sent.append(1))
        EmailNotifier(api_key="re_x", from_email="n@gridnova.in", staff_emails=["ops@gridnova.in"],
                      frontend_base_url="https://www.gridnova.in", inner=LogNotifier()).new_lead(
            lead_id=1, source="website_contact", customer_name="A", created_by="website")
        assert sent == []
