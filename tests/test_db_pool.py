"""P1 (BE-3): an explicit PostgreSQL pool, and no request holding two pooled connections
while the orchestrator works in its own session."""
from __future__ import annotations

import pytest

from app.config import settings
from app.main import app as fastapi_app
from app.memory.db import engine_options, get_db
from app.orchestrator import orchestrator as orchestrator_module

ADMIN = {"X-Employee-Id": "emp-admin-1", "X-Employee-Role": "admin"}


def test_postgres_gets_an_explicit_pool(monkeypatch):
    monkeypatch.setattr(settings, "db_pool_size", 7)
    monkeypatch.setattr(settings, "db_max_overflow", 3)
    monkeypatch.setattr(settings, "db_pool_timeout_seconds", 4.0)
    monkeypatch.setattr(settings, "db_pool_recycle_seconds", 900)
    assert engine_options("postgresql://u:p@/gridnova?host=/cloudsql/p:asia-south1:i") == {
        "pool_size": 7, "max_overflow": 3, "pool_timeout": 4.0, "pool_recycle": 900, "pool_pre_ping": True,
    }


def test_sqlite_keeps_its_thread_flag_only():
    assert engine_options("sqlite:///./x.db") == {"connect_args": {"check_same_thread": False}}


@pytest.fixture
def request_sessions(_isolated_db, monkeypatch):
    """Every session a request is given, newest last."""
    sessions = []

    def _get_db():
        session = _isolated_db()
        sessions.append(session)
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setitem(fastapi_app.dependency_overrides, get_db, _get_db)
    return sessions


def _orchestrator_checks_idle(monkeypatch, method: str, sessions: list, result: dict) -> list:
    seen = []

    def fake(**kwargs):
        seen.append(sessions[-1].in_transaction())
        return result

    monkeypatch.setattr(orchestrator_module.orchestrator, method, fake)
    return seen


def test_a_manual_draft_releases_the_request_connection_first(client, request_sessions, monkeypatch):
    seen = _orchestrator_checks_idle(monkeypatch, "request_proposal_draft", request_sessions,
                                     {"error": "Lead not found"})
    r = client.post("/api/v1/dashboard/proposals/draft", json={"lead_id": 999, "customer_id": 1}, headers=ADMIN)
    assert r.status_code == 400
    assert seen == [False]


def test_a_memory_question_releases_the_request_connection_first(client, request_sessions, monkeypatch):
    seen = _orchestrator_checks_idle(monkeypatch, "answer_customer_question", request_sessions,
                                     {"answer": "Not on file.", "resolved": False})
    r = client.post("/api/v1/dashboard/memory/ask", json={"question": "Who is Anita?"}, headers=ADMIN)
    assert r.status_code == 200, r.text
    assert seen == [False]


def test_the_site_assessment_auto_draft_releases_it_too(db_session, monkeypatch):
    from app.api.routes import proposal_inputs
    from app.memory.memory_manager import MemoryManager

    memory = MemoryManager(db_session)
    customer = memory.get_or_create_customer(email="pool@example.com", name="Pool Test", performed_by="t")
    lead = memory.create_lead(customer_id=customer.id, performed_by="t")
    db_session.commit()
    memory.get_lead(lead.id)                       # the request has read since its last commit
    assert db_session.in_transaction()
    seen = _orchestrator_checks_idle(monkeypatch, "request_proposal_draft", [db_session], {"error": "not ready"})
    out = proposal_inputs._trigger_auto_draft(memory, db_session, lead.id, 1, "emp-admin-1")
    assert out["triggered"] is False and seen == [False]


def test_chat_already_commits_before_the_agent_runs(client, request_sessions, monkeypatch):
    seen = _orchestrator_checks_idle(monkeypatch, "handle_customer_message", request_sessions,
                                     {"reply_text": "Hello", "is_qualified": False, "missing_fields": []})
    r = client.post("/api/v1/chat/message", json={"message": "Hi"})
    assert r.status_code == 200, r.text
    assert seen == [False]
