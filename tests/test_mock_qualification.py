"""
Mock-mode chat qualification fix:
- heuristic field extraction actually populates fields from free text when no
  real LLM_API_KEY is configured (previously always returned {})
- PATCH /api/v1/dashboard/leads/{lead_id}/fields — the manual employee-side
  escape hatch, gated by can_edit_lead()
"""
from app.llm.client import _mock_extract_fields
from app.memory.models import LeadSource, PipelineStage
from tests.conftest import admin_headers, engineer_headers, management_headers, sales_headers


# ── Heuristic extraction (unit-level, no HTTP) ──────────────────────────────────

class TestMockFieldExtraction:
    def test_extracts_all_required_fields_from_one_message(self):
        text = (
            "My name is Rohan Mehta, email rohan.mehta@example.com. I need a "
            "rooftop solar system, about 8kW capacity, located in Pune, Maharashtra."
        )
        fields = _mock_extract_fields(text)
        assert fields["name"] == "Rohan Mehta"
        assert fields["email"] == "rohan.mehta@example.com"
        assert fields["project_type"] == "rooftop"
        assert fields["capacity_kw"] == 8.0
        assert fields["location"] == "Pune, Maharashtra"

    def test_extracts_phone_and_segment_and_ground_mounted(self):
        text = "We're a factory looking at a 250kW ground mounted system, based in Nashik. Call me at 9876543210."
        fields = _mock_extract_fields(text)
        assert fields["phone"] == "9876543210"
        assert fields["capacity_kw"] == 250.0
        assert fields["project_type"] == "ground_mounted"
        assert fields["segment"] == "industrial"
        assert fields["location"] == "Nashik"

    def test_no_false_positive_on_generic_message(self):
        assert _mock_extract_fields("Tell me about solar panels") == {}

    def test_does_not_extract_name_from_lowercase_continuation(self):
        # "I'm interested" must not be mistaken for "I'm <Name>"
        fields = _mock_extract_fields("I'm interested in a 50kW commercial solar system in Kolkata.")
        assert "name" not in fields
        assert fields["location"] == "Kolkata"
        assert fields["segment"] == "commercial"


# ── Live: full chat-only qualification flow, no manual DB seeding ──────────────

class TestChatOnlyQualification:
    def test_lead_becomes_qualified_through_chat_alone(self, client):
        r1 = client.post("/api/v1/chat/message", json={
            "message": "Hi, I am interested in getting solar panels installed for my home."
        })
        assert r1.status_code == 200
        lead_id = r1.json()["lead_id"]
        customer_id = r1.json()["customer_id"]
        assert r1.json()["is_qualified"] is False

        r2 = client.post("/api/v1/chat/message", json={
            "message": (
                "My name is Rohan Mehta, email rohan.mehta@example.com. I need a "
                "rooftop solar system, about 8kW capacity, located in Pune, Maharashtra."
            ),
            "lead_id": lead_id,
            "customer_id": customer_id,
        })
        assert r2.status_code == 200
        assert r2.json()["is_qualified"] is True
        assert r2.json()["missing_fields"] == []


# ── PATCH /leads/{lead_id}/fields ───────────────────────────────────────────────

def _new_lead(memory, db_session, email="edit-test@example.com"):
    customer = memory.get_or_create_customer(email=email, name="Edit Test")
    db_session.commit()
    lead = memory.create_lead(customer_id=customer.id, source=LeadSource.website_chat)
    db_session.commit()
    return lead.id


class TestManualLeadFieldsEdit:
    def test_requires_auth(self, client):
        r = client.patch("/api/v1/dashboard/leads/1/fields", json={"fields": {"name": "X"}})
        assert r.status_code == 401

    def test_engineer_cannot_edit(self, client, memory, db_session):
        lead_id = _new_lead(memory, db_session, "eng-block@example.com")
        r = client.patch(f"/api/v1/dashboard/leads/{lead_id}/fields",
                          json={"fields": {"name": "X"}}, headers=engineer_headers())
        assert r.status_code == 403

    def test_management_cannot_edit(self, client, memory, db_session):
        lead_id = _new_lead(memory, db_session, "mgmt-block@example.com")
        r = client.patch(f"/api/v1/dashboard/leads/{lead_id}/fields",
                          json={"fields": {"name": "X"}}, headers=management_headers())
        assert r.status_code == 403

    def test_empty_fields_rejected(self, client, memory, db_session):
        lead_id = _new_lead(memory, db_session, "empty-fields@example.com")
        r = client.patch(f"/api/v1/dashboard/leads/{lead_id}/fields",
                          json={"fields": {}}, headers=admin_headers())
        assert r.status_code == 422

    def test_unknown_field_rejected(self, client, memory, db_session):
        lead_id = _new_lead(memory, db_session, "unknown-field@example.com")
        r = client.patch(f"/api/v1/dashboard/leads/{lead_id}/fields",
                          json={"fields": {"not_a_real_field": "x"}}, headers=admin_headers())
        assert r.status_code == 422

    def test_nonexistent_lead(self, client):
        r = client.patch("/api/v1/dashboard/leads/999999/fields",
                          json={"fields": {"name": "X"}}, headers=admin_headers())
        assert r.status_code == 404

    def test_sales_blocked_from_lead_assigned_to_someone_else(self, client, memory, db_session):
        lead_id = _new_lead(memory, db_session, "owned-by-other@example.com")
        memory.assign_lead_if_unassigned(lead_id, employee_id="emp-sales-2", performed_by="test")
        db_session.commit()
        r = client.patch(f"/api/v1/dashboard/leads/{lead_id}/fields",
                          json={"fields": {"name": "X"}}, headers=sales_headers("emp-sales-1"))
        assert r.status_code == 403

    def test_sales_can_edit_unassigned_lead(self, client, memory, db_session):
        lead_id = _new_lead(memory, db_session, "unassigned@example.com")
        r = client.patch(f"/api/v1/dashboard/leads/{lead_id}/fields",
                          json={"fields": {"name": "Sales Edited"}}, headers=sales_headers())
        assert r.status_code == 200
        assert r.json()["collected_fields"]["name"] == "Sales Edited"

    def test_manual_edit_completes_qualification(self, client, memory, db_session):
        lead_id = _new_lead(memory, db_session, "complete-me@example.com")
        r = client.patch(f"/api/v1/dashboard/leads/{lead_id}/fields", headers=admin_headers(), json={
            "fields": {
                "name": "Complete Me",
                "email": "complete-me@example.com",
                "project_type": "rooftop",
                "location": "Chennai",
                "capacity_kw": 5,
            }
        })
        assert r.status_code == 200
        body = r.json()
        assert body["is_qualified"] is True
        assert body["missing_fields"] == []
        assert body["pipeline_stage"] == PipelineStage.qualified

    def test_partial_edit_merges_not_overwrites(self, client, memory, db_session):
        lead_id = _new_lead(memory, db_session, "merge-me@example.com")
        memory.update_lead_fields(lead_id=lead_id, new_fields={"name": "Original Name"}, performed_by="test")
        db_session.commit()
        r = client.patch(f"/api/v1/dashboard/leads/{lead_id}/fields",
                          json={"fields": {"location": "Delhi"}}, headers=admin_headers())
        assert r.status_code == 200
        assert r.json()["collected_fields"]["name"] == "Original Name"
        assert r.json()["collected_fields"]["location"] == "Delhi"
