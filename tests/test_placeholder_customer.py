"""
Placeholder customers — a chat can start with no name/email at all, and the
placeholder record is filled in place once identity is extracted. Merging into an
existing customer is a staff-path option only (allow_merge=True); the anonymous chat
never merges (BE-1 S2, tests/test_chat_sessions.py).
"""
import pytest
from sqlalchemy import func, select

from app.llm.client import llm_client
from app.memory.memory_manager import MemoryManager, is_placeholder_email
from app.memory.models import Channel, Customer, MessageRole
from tests.chat_support import chat, customer_of, lead_of


def _customer_count(db_session) -> int:
    return int(db_session.execute(select(func.count(Customer.id))).scalar_one())


class TestPlaceholderCreation:
    def test_zero_identity_creates_placeholder_and_proceeds(self, client, db_session):
        r = chat(client, "hi, curious about solar")
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["session_token"]
        assert "lead_id" not in data and "customer_id" not in data
        assert len(data["reply"]) > 0

        lead = lead_of(db_session, data["session_token"])
        customer = customer_of(db_session, data["session_token"])
        assert lead.customer_id == customer.id
        assert is_placeholder_email(customer.email)
        assert customer.name is None

    def test_real_email_is_used_directly(self, client, db_session):
        r = chat(client, "hello", email="real@example.com", name="Real Person")
        assert r.status_code == 200
        customer = customer_of(db_session, r.json()["session_token"])
        assert customer.email == "real@example.com"
        assert not is_placeholder_email(customer.email)


class TestInPlaceUpdate:
    def test_extracted_details_update_same_customer(self, memory, db_session, monkeypatch):
        from app.agents.sales_agent import sales_agent

        placeholder = memory.get_or_create_customer(
            email="pending-abc@pending.gridnova.internal", name=None,
        )
        db_session.commit()
        lead = memory.create_lead(customer_id=placeholder.id)
        db_session.commit()
        before_count = _customer_count(db_session)

        # Extraction now pulls a real name + email out of the conversation.
        monkeypatch.setattr(
            llm_client, "extract_fields",
            lambda **k: {"name": "Ravi", "email": "ravi@example.com", "project_type": "rooftop"},
        )
        result = sales_agent.handle_message(
            memory, placeholder.id, lead.id, "Hi, I'm Ravi — ravi@example.com, rooftop please",
        )
        db_session.commit()

        # Same customer id, updated in place — no second customer/lead created.
        assert result["customer_id"] == placeholder.id
        refreshed = db_session.get(Customer, placeholder.id)
        assert refreshed.name == "Ravi"
        assert refreshed.email == "ravi@example.com"
        assert _customer_count(db_session) == before_count
        assert memory.get_lead(lead.id).customer_id == placeholder.id


class TestEmailCollisionMerge:
    def test_collision_merges_into_existing_customer_when_allowed(self, memory, db_session):
        """allow_merge=True — the staff-initiated paths."""
        # An existing, real customer messaged before.
        existing = memory.get_or_create_customer(email="ravi@example.com", name="Ravi")
        db_session.commit()

        # A new session started anonymously → placeholder customer + lead + a message.
        placeholder = memory.get_or_create_customer(
            email="pending-xyz@pending.gridnova.internal", name=None,
        )
        db_session.commit()
        lead = memory.create_lead(customer_id=placeholder.id)
        memory.log_message(
            channel=Channel.website, role=MessageRole.customer,
            content="it's me again", lead_id=lead.id, customer_id=placeholder.id,
        )
        db_session.commit()

        # The same real email → merge, don't overwrite.
        surviving = memory.update_customer_details(
            customer_id=placeholder.id, name="Ravi", email="ravi@example.com",
            performed_by="staff", allow_merge=True,
        )
        db_session.commit()

        assert surviving.id == existing.id
        # Lead + message repointed onto the existing customer.
        assert memory.get_lead(lead.id).customer_id == existing.id
        ctx = memory.get_lead_context(lead.id)
        assert ctx["customer"].id == existing.id
        assert any(m.customer_id == existing.id for m in ctx["messages"])

        # The merge is distinctly auditable on both sides.
        actions = {log.action for log in memory.list_audit_logs(entity_type="customer")}
        assert "merged_into" in actions
        assert "absorbed_customer" in actions

    def test_collision_without_allow_merge_keeps_the_customer_apart(self, memory, db_session):
        """The default: nothing merged, the other customer's email not copied, and the
        claim noted for staff on the lead's timeline."""
        existing = memory.get_or_create_customer(email="ravi@example.com", name="Ravi")
        placeholder = memory.create_placeholder_customer()
        lead = memory.create_lead(customer_id=placeholder.id)
        db_session.commit()

        surviving = memory.update_customer_details(
            customer_id=placeholder.id, name="Ravi", email="ravi@example.com",
            performed_by="sales_agent", lead_id=lead.id,
        )
        db_session.commit()

        assert surviving.id == placeholder.id
        assert is_placeholder_email(surviving.email)
        assert surviving.name == "Ravi"
        assert memory.get_lead(lead.id).customer_id == placeholder.id
        actions = {log.action for log in memory.list_audit_logs(entity_type="customer")}
        assert "merged_into" not in actions and "possible_returning_customer" in actions
        notes = [e.description for e in memory.get_lead_context(lead.id)["timeline"]]
        assert any(f"Possible returning customer #{existing.id} (unverified" in (n or "") for n in notes)

    def test_new_email_no_collision_updates_in_place(self, memory, db_session):
        placeholder = memory.get_or_create_customer(
            email="pending-solo@pending.gridnova.internal", name=None,
        )
        db_session.commit()
        surviving = memory.update_customer_details(
            customer_id=placeholder.id, name="Solo", email="solo@example.com",
            performed_by="sales_agent",
        )
        db_session.commit()
        assert surviving.id == placeholder.id
        assert surviving.email == "solo@example.com"
