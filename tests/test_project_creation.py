"""Every Lead now gets a real Project — MemoryManager.create_lead() creates one
automatically when the caller doesn't supply project_id (see migration
0015_project_required, which backfills this for pre-existing rows)."""
from app.memory.models import Project


class TestAutomaticProjectCreation:
    def test_create_lead_without_project_id_creates_one(self, memory, db_session):
        customer = memory.get_or_create_customer(email="auto-project@example.com", name="C")
        db_session.commit()

        lead = memory.create_lead(customer_id=customer.id)
        db_session.commit()

        assert lead.project_id is not None
        project = db_session.get(Project, lead.project_id)
        assert project is not None
        assert project.customer_id == customer.id

    def test_two_leads_for_the_same_customer_get_distinct_projects(self, memory, db_session):
        customer = memory.get_or_create_customer(email="two-leads@example.com", name="C")
        db_session.commit()

        lead1 = memory.create_lead(customer_id=customer.id)
        lead2 = memory.create_lead(customer_id=customer.id)
        db_session.commit()

        assert lead1.project_id != lead2.project_id

    def test_explicit_project_id_is_respected_not_overridden(self, memory, db_session):
        customer = memory.get_or_create_customer(email="explicit-project@example.com", name="C")
        db_session.commit()
        project = memory.create_project(customer_id=customer.id, roof_type="RCC")
        db_session.commit()

        lead = memory.create_lead(customer_id=customer.id, project_id=project.id)
        db_session.commit()

        assert lead.project_id == project.id

    def test_project_creation_is_audited(self, memory, db_session):
        customer = memory.get_or_create_customer(email="audited-project@example.com", name="C")
        db_session.commit()
        lead = memory.create_lead(customer_id=customer.id)
        db_session.commit()

        logs = memory.list_audit_logs(entity_type="project", entity_id=str(lead.project_id))
        assert any(l.action == "created" for l in logs)

    def test_chat_created_leads_still_get_a_project(self, client, memory, db_session):
        """End-to-end through the real public chat endpoint, not just the
        memory-manager unit — this is the path that was silently skipping
        Project creation before this change."""
        r = client.post("/api/v1/chat/message", json={"message": "hi, thinking about solar"})
        assert r.status_code == 200
        lead = memory.get_lead(r.json()["lead_id"])
        assert lead.project_id is not None
