"""Memory Manager: idempotent creation, audit trail, proposal versioning."""
import pytest
from app.memory.memory_manager import MemoryManager
from app.memory.models import ProposalStatus, PipelineStage


def test_get_or_create_customer_idempotent(memory, db_session):
    c1 = memory.get_or_create_customer(email="test@example.com", name="Test User")
    db_session.commit()
    c2 = memory.get_or_create_customer(email="test@example.com", name="Different Name")
    db_session.commit()
    assert c1.id == c2.id
    # Name is NOT overwritten on second call (idempotent by email)
    assert c2.name == "Test User"


def test_audit_log_written_on_customer_creation(memory, db_session):
    memory.get_or_create_customer(email="audit@example.com", name="Audit User")
    db_session.commit()
    logs = memory.list_audit_logs(entity_type="customer")
    assert len(logs) >= 1
    assert logs[0].action == "created"


def test_audit_log_written_on_lead_update(memory, db_session):
    customer = memory.get_or_create_customer(email="lead@example.com", name="Lead User")
    db_session.commit()
    lead = memory.create_lead(customer_id=customer.id)
    db_session.commit()
    memory.update_lead_fields(lead_id=lead.id, new_fields={"name": "Lead User"}, performed_by="test")
    db_session.commit()
    logs = memory.list_audit_logs(entity_type="lead")
    actions = [l.action for l in logs]
    assert "created" in actions
    assert "fields_updated" in actions


def test_proposal_versioning_never_overwrites(memory, db_session):
    customer = memory.get_or_create_customer(email="prop@example.com", name="Prop User")
    db_session.commit()
    lead = memory.create_lead(customer_id=customer.id)
    db_session.commit()

    p1 = memory.create_proposal_draft(
        lead_id=lead.id, content={"note": "v1"}, total_value=100_000,
        requires_manager_signoff=False, performed_by="emp-1",
    )
    db_session.commit()
    assert p1.version == 1

    p2 = memory.create_proposal_draft(
        lead_id=lead.id, content={"note": "v2"}, total_value=110_000,
        requires_manager_signoff=False, performed_by="emp-1",
    )
    db_session.commit()
    assert p2.version == 2
    assert p2.id != p1.id

    # v1 content is untouched
    from sqlalchemy import select
    from app.memory.models import Proposal
    original = db_session.get(Proposal, p1.id)
    assert original.content["note"] == "v1"


def test_get_lead_context_assembles_all_data(memory, db_session):
    customer = memory.get_or_create_customer(email="ctx@example.com", name="Context User")
    db_session.commit()
    lead = memory.create_lead(customer_id=customer.id)
    db_session.commit()
    memory.log_message(channel="website", role="customer", content="Hello", lead_id=lead.id, customer_id=customer.id)
    db_session.commit()

    ctx = memory.get_lead_context(lead.id)
    assert ctx["lead"].id == lead.id
    assert ctx["customer"].id == customer.id
    assert len(ctx["messages"]) >= 1
    assert len(ctx["timeline"]) >= 1  # lead_created event


def test_proposal_status_transition_audit(memory, db_session):
    customer = memory.get_or_create_customer(email="trans@example.com", name="Trans User")
    db_session.commit()
    lead = memory.create_lead(customer_id=customer.id)
    db_session.commit()
    proposal = memory.create_proposal_draft(
        lead_id=lead.id, content={}, total_value=50_000,
        requires_manager_signoff=False, performed_by="emp-1",
    )
    db_session.commit()

    memory.transition_proposal_status(
        proposal_id=proposal.id,
        new_status=ProposalStatus.approved,
        performed_by="emp-1",
        approved_by_employee_id="emp-1",
    )
    db_session.commit()

    logs = memory.list_audit_logs(entity_type="proposal", entity_id=str(proposal.id))
    actions = [l.action for l in logs]
    assert any("approved" in a for a in actions)
