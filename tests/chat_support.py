"""Helpers for tests that drive the public chat endpoint (BE-1 S2: a conversation is
identified by its session_token, never by lead/customer ids from the browser)."""
from sqlalchemy import select

from app.memory.memory_manager import MemoryManager
from app.memory.models import ChatSession, Customer, Lead

CHAT_URL = "/api/v1/chat/message"


def chat(client, message: str, token: str = None, **extra):
    body = {"message": message, **extra}
    if token:
        body["session_token"] = token
    return client.post(CHAT_URL, json=body)


def session_row(db_session, token: str) -> ChatSession:
    db_session.expire_all()
    return db_session.execute(select(ChatSession).where(
        ChatSession.token_hash == MemoryManager.hash_chat_token(token))).scalar_one()


def lead_of(db_session, token: str) -> Lead:
    return db_session.get(Lead, session_row(db_session, token).lead_id)


def customer_of(db_session, token: str) -> Customer:
    return db_session.get(Customer, session_row(db_session, token).customer_id)


def token_for(memory, db_session, lead_id: int) -> str:
    """A chat session for a lead the test created directly — what a returning
    browser would hold after its first message."""
    lead = memory.get_lead(lead_id)
    token = memory.create_chat_session(lead_id=lead.id, customer_id=lead.customer_id, performed_by="test")
    db_session.commit()
    return token
