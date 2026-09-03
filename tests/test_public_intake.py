"""POST /api/v1/public/estimate-request — the public, unauthenticated lead-intake
endpoint behind the frontend's /estimate form. Deliberately public (anonymous
website visitors), guarded the same way app/api/routes/chat.py and
public_proposals.py already are: no auth, hand-rolled per-IP rate limit.
"""
import app.api.routes.public_intake as public_intake
from app.memory.models import LeadSource, Project


def _payload(**overrides) -> dict:
    payload = {
        "name": "Test User",
        "email": "estimate-test@example.com",
        "phone": "9876543210",
        "customer_type": "residential",
        "state": "West Bengal",
        "city": "Kolkata",
        "average_bill": 4500,
        "roof_type": "RCC",
        "roof_area": 800,
        "floors": 2,
        "connection_phase": "single_phase",
    }
    payload.update(overrides)
    return payload


class TestSubmitEstimateRequest:
    def test_success_returns_only_lead_id_and_success(self, client):
        r = client.post("/api/v1/public/estimate-request", json=_payload())
        assert r.status_code == 200
        body = r.json()
        assert set(body.keys()) == {"lead_id", "success"}
        assert body["success"] is True
        assert isinstance(body["lead_id"], int)

    def test_no_authentication_required(self, client):
        # No X-Employee-Id / X-Employee-Role headers at all.
        r = client.post("/api/v1/public/estimate-request", json=_payload(email="no-auth@example.com"))
        assert r.status_code == 200

    def test_creates_a_lead_tagged_with_the_estimate_form_source(self, client, memory):
        r = client.post("/api/v1/public/estimate-request", json=_payload(email="source-tag@example.com"))
        lead = memory.get_lead(r.json()["lead_id"])
        assert lead.source == LeadSource.website_estimate_form

    def test_creates_a_real_project_with_the_submitted_details(self, client, memory, db_session):
        r = client.post("/api/v1/public/estimate-request", json=_payload(email="project-details@example.com"))
        lead = memory.get_lead(r.json()["lead_id"])
        project = db_session.get(Project, lead.project_id)
        assert project is not None
        assert project.roof_type == "RCC"
        assert project.monthly_electricity_bill == 4500
        assert "Kolkata" in project.location and "West Bengal" in project.location

    def test_response_carries_no_pricing_or_sensitive_data(self, client):
        r = client.post("/api/v1/public/estimate-request", json=_payload(email="no-leak@example.com"))
        body = r.json()
        assert "price" not in body and "total_value" not in body
        assert "email" not in body and "phone" not in body

    def test_only_name_and_email_are_required(self, client):
        r = client.post("/api/v1/public/estimate-request", json={
            "name": "Minimal User", "email": "minimal@example.com",
        })
        assert r.status_code == 200

    def test_missing_name_is_rejected(self, client):
        r = client.post("/api/v1/public/estimate-request", json={"email": "no-name@example.com"})
        assert r.status_code == 422

    def test_missing_email_is_rejected(self, client):
        r = client.post("/api/v1/public/estimate-request", json={"name": "No Email"})
        assert r.status_code == 422

    def test_malformed_email_is_rejected(self, client):
        r = client.post("/api/v1/public/estimate-request", json=_payload(email="not-an-email"))
        assert r.status_code == 422

    def test_negative_average_bill_is_rejected(self, client):
        r = client.post(
            "/api/v1/public/estimate-request",
            json=_payload(email="negative-bill@example.com", average_bill=-100),
        )
        assert r.status_code == 422

    def test_zero_floors_is_rejected(self, client):
        r = client.post(
            "/api/v1/public/estimate-request", json=_payload(email="zero-floors@example.com", floors=0)
        )
        assert r.status_code == 422


class TestAbuseGuards:
    def setup_method(self):
        public_intake._request_log.clear()
        public_intake._recent_emails.clear()

    def teardown_method(self):
        public_intake._request_log.clear()
        public_intake._recent_emails.clear()

    def test_per_ip_rate_limit_kicks_in(self, client):
        for i in range(public_intake._RATE_LIMIT):
            r = client.post(
                "/api/v1/public/estimate-request",
                json=_payload(email=f"rate-{i}@example.com"),
            )
            assert r.status_code == 200
        r = client.post(
            "/api/v1/public/estimate-request", json=_payload(email="rate-overflow@example.com")
        )
        assert r.status_code == 429

    def test_resubmitting_the_same_email_immediately_is_rejected(self, client):
        client.post("/api/v1/public/estimate-request", json=_payload(email="cooldown@example.com"))
        r = client.post("/api/v1/public/estimate-request", json=_payload(email="cooldown@example.com"))
        assert r.status_code == 429

    def test_a_different_email_is_not_blocked_by_anothers_cooldown(self, client):
        client.post("/api/v1/public/estimate-request", json=_payload(email="first@example.com"))
        r = client.post("/api/v1/public/estimate-request", json=_payload(email="second@example.com"))
        assert r.status_code == 200
