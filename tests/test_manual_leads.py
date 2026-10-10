"""Staff-entered leads (POST /leads), reassignment (PATCH /leads/{id}/assign), and the
customer/project record on lead detail and list."""
import pytest

from app.memory.memory_manager import is_placeholder_email
from app.memory.models import (
    Customer, Employee, EmployeeRole, LeadSource, PipelineStage, Project, TimelineEventType,
)
from tests.conftest import (
    admin_headers, engineer_headers, management_headers, sales_headers, seed_employees,
)

LEADS = "/api/v1/dashboard/leads"


def _body(**overrides) -> dict:
    body = {"name": "Ravi Kumar", "phone": "+91 98765 43210", "source": "cold_call"}
    body.update(overrides)
    return body


QUALIFYING = dict(email="ravi@example.com", location="Howrah, West Bengal",
                  project_type="rooftop", capacity_kw=6)


class TestCreateManualLead:
    def test_sales_creates_and_owns_the_lead(self, client, memory, db_session):
        r = client.post(LEADS, json=_body(**QUALIFYING, monthly_electricity_bill=4200,
                                          notes="Called after seeing the site van"),
                        headers=sales_headers())
        assert r.status_code == 201, r.text
        data = r.json()
        assert data["assigned_employee_id"] == "emp-sales-1"
        assert data["source"] == "cold_call"
        assert data["is_qualified"] is True and data["missing_fields"] == []
        assert data["pipeline_stage"] == "qualified"
        assert data["project_reference_id"].startswith("GNE-XX-ROF-")
        assert data["customer_matched_existing"] is False

        db_session.expire_all()
        lead = memory.get_lead(data["lead_id"])
        assert lead.collected_fields["notes"] == "Called after seeing the site van"
        assert lead.collected_fields["monthly_electricity_bill"] == 4200
        project = db_session.get(Project, data["project_id"])
        assert project.project_type == "rooftop" and project.capacity_kw == 6
        assert project.location == "Howrah, West Bengal"
        events = {e.event_type for e in memory.get_lead_context(lead.id)["timeline"]}
        assert {TimelineEventType.lead_created, TimelineEventType.lead_assigned,
                TimelineEventType.qualification_completed} <= events

    @pytest.mark.parametrize("headers", [admin_headers(), management_headers()])
    def test_admin_and_management_leave_it_unassigned(self, client, headers):
        r = client.post(LEADS, json=_body(), headers=headers)
        assert r.status_code == 201
        assert r.json()["assigned_employee_id"] is None

    def test_engineer_forbidden(self, client):
        assert client.post(LEADS, json=_body(), headers=engineer_headers()).status_code == 403

    def test_partial_details_leave_the_lead_qualifying(self, client):
        data = client.post(LEADS, json=_body(), headers=admin_headers()).json()
        assert data["pipeline_stage"] == "qualifying"
        assert set(data["missing_fields"]) == {"email", "project_type", "location", "capacity_kw"}

    @pytest.mark.parametrize("source", ["website_chat", "website_estimate_form", "website_booking",
                                        "instagram", "nope"])
    def test_only_manual_sources_are_accepted(self, client, source):
        assert client.post(LEADS, json=_body(source=source), headers=admin_headers()).status_code == 422

    @pytest.mark.parametrize("source", ["whatsapp", "cold_call", "referral", "other"])
    def test_each_manual_source(self, client, memory, db_session, source):
        data = client.post(LEADS, json=_body(source=source), headers=admin_headers()).json()
        db_session.expire_all()
        assert memory.get_lead(data["lead_id"]).source == LeadSource(source)

    def test_needs_a_phone_or_an_email(self, client):
        r = client.post(LEADS, json={"name": "No Contact", "source": "other"}, headers=admin_headers())
        assert r.status_code == 422

    def test_rejects_invalid_email_and_unknown_project_type(self, client):
        assert client.post(LEADS, json=_body(email="not-an-email"), headers=admin_headers()).status_code == 422
        assert client.post(LEADS, json=_body(project_type="nuclear"), headers=admin_headers()).status_code == 422

    def test_phone_only_uses_a_placeholder_customer(self, client, db_session):
        data = client.post(LEADS, json=_body(), headers=admin_headers()).json()
        db_session.expire_all()
        customer = db_session.get(Customer, data["customer_id"])
        assert is_placeholder_email(customer.email)
        assert customer.name == "Ravi Kumar" and customer.phone == "+91 98765 43210"

    def test_dedup_by_email(self, client, memory, db_session):
        existing = memory.get_or_create_customer(email="ravi@example.com", name="Ravi")
        db_session.commit()
        data = client.post(LEADS, json=_body(email="RAVI@example.com "), headers=admin_headers()).json()
        assert data["customer_id"] == existing.id and data["customer_matched_existing"] is True

    def test_dedup_by_phone_when_no_email(self, client, memory, db_session):
        first = client.post(LEADS, json=_body(), headers=admin_headers()).json()
        second = client.post(LEADS, json=_body(phone="9876543210"), headers=admin_headers()).json()
        assert second["customer_id"] == first["customer_id"]
        assert second["customer_matched_existing"] is True
        assert second["lead_id"] != first["lead_id"]

    def test_everything_is_audited(self, client, memory, db_session):
        data = client.post(LEADS, json=_body(**QUALIFYING), headers=sales_headers()).json()
        lead_actions = {l.action for l in memory.list_audit_logs(entity_type="lead", entity_id=str(data["lead_id"]))}
        assert {"created", "reassigned", "fields_updated"} <= lead_actions
        assert memory.list_audit_logs(entity_type="project", entity_id=str(data["project_id"]))


class TestAssign:
    def _lead(self, client):
        return client.post(LEADS, json=_body(), headers=admin_headers()).json()["lead_id"]

    @pytest.mark.parametrize("headers", [admin_headers(), management_headers()])
    def test_admin_and_management_assign(self, client, memory, db_session, headers):
        lid = self._lead(client)
        seed_employees(db_session, "emp-sales-2")
        r = client.patch(f"{LEADS}/{lid}/assign", json={"employee_id": "emp-sales-2"}, headers=headers)
        assert r.status_code == 200, r.text
        assert r.json() == {"lead_id": lid, "previous_assigned_employee_id": None,
                            "assigned_employee_id": "emp-sales-2"}

    def test_reassign_is_audited_with_a_timeline_event(self, client, memory, db_session):
        lid = client.post(LEADS, json=_body(), headers=sales_headers()).json()["lead_id"]
        seed_employees(db_session, "emp-sales-2")
        r = client.patch(f"{LEADS}/{lid}/assign", json={"employee_id": "emp-sales-2"}, headers=admin_headers())
        assert r.json()["previous_assigned_employee_id"] == "emp-sales-1"
        logs = [l for l in memory.list_audit_logs(entity_type="lead", entity_id=str(lid))
                if l.action == "reassigned" and l.after_value == {"assigned_employee_id": "emp-sales-2"}]
        assert len(logs) == 1
        assert logs[0].before_value == {"assigned_employee_id": "emp-sales-1"}
        assert logs[0].performed_by == "emp-admin-1"
        db_session.expire_all()
        events = [e for e in memory.get_lead_context(lid)["timeline"] if e.event_type == TimelineEventType.lead_assigned]
        assert any("emp-sales-1" in e.description and "emp-sales-2" in e.description for e in events)

    @pytest.mark.parametrize("headers", [sales_headers(), engineer_headers()])
    def test_sales_and_engineer_forbidden(self, client, db_session, headers):
        lid = self._lead(client)
        seed_employees(db_session, "emp-sales-2")
        assert client.patch(f"{LEADS}/{lid}/assign", json={"employee_id": "emp-sales-2"},
                            headers=headers).status_code == 403

    def test_target_must_be_active_admin_or_sales(self, client, db_session):
        lid = self._lead(client)
        db_session.add_all([
            Employee(id="emp-eng-x", name="E", email="e@x.in", role=EmployeeRole.engineer),
            Employee(id="emp-mgmt-x", name="M", email="m@x.in", role=EmployeeRole.management),
            Employee(id="emp-gone", name="G", email="g@x.in", role=EmployeeRole.sales, is_active=False),
        ])
        db_session.commit()
        for target in ("emp-eng-x", "emp-mgmt-x", "emp-gone", "emp-nobody"):
            r = client.patch(f"{LEADS}/{lid}/assign", json={"employee_id": target}, headers=admin_headers())
            assert r.status_code == 422, target

    def test_same_assignee_is_409_and_unknown_lead_404(self, client, db_session):
        lid = client.post(LEADS, json=_body(), headers=sales_headers()).json()["lead_id"]
        assert client.patch(f"{LEADS}/{lid}/assign", json={"employee_id": "emp-sales-1"},
                            headers=admin_headers()).status_code == 409
        assert client.patch(f"{LEADS}/99999/assign", json={"employee_id": "emp-sales-1"},
                            headers=admin_headers()).status_code == 404


class TestProjectVisibility:
    def test_detail_carries_the_project_record(self, client):
        data = client.post(LEADS, json=_body(**QUALIFYING), headers=admin_headers()).json()
        r = client.get(f"{LEADS}/{data['lead_id']}", headers=admin_headers())
        project = r.json()["project"]
        assert project["reference_id"] == data["project_reference_id"]
        assert project["project_type"] == "rooftop" and project["project_family"] == "rooftop_solar"
        assert project["location"] == "Howrah, West Bengal" and project["capacity_kw"] == 6
        assert set(project) == {"id", "reference_id", "project_type", "project_family", "state", "location",
                                "capacity_kw", "roof_type", "monthly_electricity_bill", "budget",
                                "timeline", "created_at"}
        assert r.json()["assigned_employee_id"] is None

    def test_list_rows_carry_the_reference_id(self, client):
        data = client.post(LEADS, json=_body(), headers=admin_headers()).json()
        rows = {row["id"]: row for row in client.get(LEADS, headers=admin_headers()).json()["items"]}
        assert rows[data["lead_id"]]["project_reference_id"] == data["project_reference_id"]
        assert "assigned_employee_id" in rows[data["lead_id"]]

    def test_engineer_view_is_unchanged(self, client):
        data = client.post(LEADS, json=_body(**QUALIFYING), headers=admin_headers()).json()
        body = client.get(f"{LEADS}/{data['lead_id']}", headers=engineer_headers()).json()
        assert set(body) == {"lead_id", "pipeline_stage", "collected_fields"}
        assert set(body["collected_fields"]) <= {"project_type", "capacity_kw", "location", "roof_type"}
