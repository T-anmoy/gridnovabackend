"""
Shared test fixtures.
All tests use an in-memory SQLite database — no external services required.

Each test gets its OWN fresh engine + connection (function-scoped `_isolated_db`),
so no SQLAlchemy state leaks across test boundaries. A shared, process-wide
StaticPool engine previously caused intermittent StaleDataError on the 'leads'
table when the test's session, the request's get_db session, and the orchestrator's
internal SessionLocal all touched the same rows across test order boundaries.
"""
import os
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("ENVIRONMENT", "development")
os.environ.setdefault("LLM_API_KEY", "")  # forces mock mode
# Keep tests hermetic: never hit real external services even if a developer's local
# .env carries real credentials. (os.environ takes precedence over the .env file.)
os.environ.setdefault("CLERK_SECRET_KEY", "")
os.environ.setdefault("CLERK_JWT_ISSUER", "")
os.environ.setdefault("SLACK_WEBHOOK_URL", "")

from app.main import app as fastapi_app
from app.memory.db import Base, get_db
from app.memory.memory_manager import MemoryManager
import app.memory.models  # noqa: F401 — ensure all models register on Base


@pytest.fixture(autouse=True)
def _isolated_db(monkeypatch):
    """Give every test a private in-memory engine + sessionmaker.

    StaticPool keeps the single in-memory DBAPI connection shared across sessions
    WITHIN this test (so the schema is visible to every session), but the engine
    itself is created and disposed per test — nothing survives into the next one.
    Yields the per-test sessionmaker for the fixtures below to use.
    """
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

    # Orchestrator graph nodes and the PDF background thread do
    # `from app.memory.db import SessionLocal` inside function bodies at runtime —
    # monkeypatch the module attribute so they use this test's engine too.
    # (monkeypatch auto-restores at teardown.)
    monkeypatch.setattr("app.memory.db.SessionLocal", SessionLocal)

    # The PDF background thread opens its own session against this same StaticPool
    # engine — a single raw SQLite DBAPI connection shared by every session in this
    # test. That connection isn't safe for two threads to use concurrently (real
    # deployments give each request its own connection; this sharing is purely a
    # test-harness convenience), so under tests it runs synchronously instead of on
    # a real background thread. Production is unaffected — this only patches the
    # test process's view of the seam in app/api/routes/dashboard.py.
    monkeypatch.setattr(
        "app.api.routes.dashboard._start_pdf_generation",
        lambda generate_fn, proposal_id: generate_fn(),
    )

    def _override_get_db():
        session = SessionLocal()
        try:
            yield session
        finally:
            session.close()

    fastapi_app.dependency_overrides[get_db] = _override_get_db
    try:
        yield SessionLocal
    finally:
        fastapi_app.dependency_overrides.pop(get_db, None)
        engine.dispose()


@pytest.fixture(autouse=True)
def reset_chat_inmemory_state():
    """Clear the chat endpoint's process-level rate-limit and dedup stores between
    tests so results don't depend on test ordering (e.g. the rate-limit test
    otherwise poisons later chat tests in the same process)."""
    import app.api.routes.chat as chat
    import app.api.routes.public_proposals as public_proposals
    chat._request_log.clear()
    chat._recent_messages.clear()
    public_proposals._accept_request_log.clear()
    yield
    chat._request_log.clear()
    chat._recent_messages.clear()
    public_proposals._accept_request_log.clear()


@pytest.fixture
def db_session(_isolated_db):
    session = _isolated_db()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def memory(db_session):
    return MemoryManager(db_session)


@pytest.fixture
def client(_isolated_db):
    return TestClient(fastapi_app)


# ── Auth header helpers ────────────────────────────────────────────────────────

def admin_headers(employee_id: str = "emp-admin-1") -> dict:
    return {"X-Employee-Id": employee_id, "X-Employee-Role": "admin"}


def sales_headers(employee_id: str = "emp-sales-1") -> dict:
    return {"X-Employee-Id": employee_id, "X-Employee-Role": "sales"}


def management_headers(employee_id: str = "emp-mgmt-1") -> dict:
    return {"X-Employee-Id": employee_id, "X-Employee-Role": "management"}


def engineer_headers(employee_id: str = "emp-eng-1") -> dict:
    return {"X-Employee-Id": employee_id, "X-Employee-Role": "engineer"}
