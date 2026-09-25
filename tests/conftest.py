"""
Shared test fixtures.
By default every test uses an in-memory SQLite database — no external services required.

Opt-in PostgreSQL mode: set TEST_DATABASE_URL (e.g.
postgresql://postgres:postgres@localhost:5432/gridnova_test) and every test runs
against that database instead, with drop_all + create_all around each test. Point it
at a DEDICATED throwaway database — every table in it is dropped repeatedly.

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
from sqlalchemy.orm import close_all_sessions, sessionmaker
from sqlalchemy.pool import NullPool, StaticPool

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "").strip()

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
from app.memory import reflection as app_reflection
import app.memory.models  # noqa: F401 — ensure all models register on Base


def _make_test_engine():
    if TEST_DATABASE_URL:
        # A real connection per session, like production. NullPool so nothing is
        # held open between tests that could block the next test's drop_all.
        engine = create_engine(TEST_DATABASE_URL, poolclass=NullPool)
        Base.metadata.drop_all(bind=engine)
        return engine
    return create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


@pytest.fixture(autouse=True)
def _isolated_db(monkeypatch):
    """Give every test a private engine + sessionmaker.

    SQLite (default): StaticPool keeps the single in-memory DBAPI connection shared
    across sessions WITHIN this test (so the schema is visible to every session), but
    the engine itself is created and disposed per test — nothing survives into the
    next one. Postgres (TEST_DATABASE_URL): every table is dropped and recreated
    around each test instead.
    Yields the per-test sessionmaker for the fixtures below to use.
    """
    engine = _make_test_engine()
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

    # Same reasoning for milestone reflection: it normally runs on a background
    # thread with its own session, which isn't safe against the shared test
    # connection. Run it inline instead, so tests see the memories it writes.
    monkeypatch.setattr(
        "app.memory.reflection.start_reflection",
        lambda lead_id, trigger, performed_by="sales_agent": (
            app_reflection._reflect_with_own_session(lead_id, trigger, performed_by)
        ),
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
        if TEST_DATABASE_URL:
            # A session a test forgot to close would otherwise keep its transaction
            # (and its table locks) open, and drop_all would wait on it forever.
            close_all_sessions()
            Base.metadata.drop_all(bind=engine)
        engine.dispose()


@pytest.fixture(autouse=True)
def reset_chat_inmemory_state():
    """Clear the chat endpoint's process-level rate-limit and dedup stores between
    tests so results don't depend on test ordering (e.g. the rate-limit test
    otherwise poisons later chat tests in the same process)."""
    import app.api.routes.chat as chat
    import app.api.routes.pricing as pricing
    import app.api.routes.public_intake as public_intake
    import app.api.routes.public_booking as public_booking
    import app.api.routes.public_proposals as public_proposals
    chat._request_log.clear()
    chat._recent_messages.clear()
    public_proposals._accept_request_log.clear()
    public_booking._availability_request_log.clear()
    public_booking._booking_request_log.clear()
    public_booking._recent_emails.clear()
    pricing._instant_estimate_request_log.clear()
    public_intake._request_log.clear()
    public_intake._recent_emails.clear()
    yield
    chat._request_log.clear()
    chat._recent_messages.clear()
    public_proposals._accept_request_log.clear()
    public_booking._availability_request_log.clear()
    public_booking._booking_request_log.clear()
    public_booking._recent_emails.clear()
    pricing._instant_estimate_request_log.clear()
    public_intake._request_log.clear()
    public_intake._recent_emails.clear()


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


# ── Proposal content that can actually be sent ───────────────────────────────
# config/proposal_content.yaml is still all placeholder copy with 0 rates, so any
# engine-drafted proposal is (correctly) blocked from being sent to a customer. Tests
# that exercise a real send use this fixture: the real config's structure with every
# placeholder string replaced and every rate made positive. The YAML itself is
# never modified.

TEST_RATE_PER_KW = 50_000


def _without_placeholders(node, key="text"):
    if isinstance(node, dict):
        return {
            k: (TEST_RATE_PER_KW if k == "rate_per_kw" else _without_placeholders(v, k))
            for k, v in node.items()
        }
    if isinstance(node, list):
        return [_without_placeholders(v, key) for v in node]
    if isinstance(node, str) and "[PLACEHOLDER" in node:
        return f"Test copy for {key}."
    return node


@pytest.fixture
def sendable_proposal_content(monkeypatch):
    from app.proposals import content_engine

    real = content_engine.load_content_config()
    monkeypatch.setattr(content_engine, "_CONTENT_CONFIG", _without_placeholders(real))
    return content_engine._CONTENT_CONFIG


# ── Employees referenced by id ────────────────────────────────────────────────
# In production an employee row always exists before anything points at it:
# get_current_employee provisions it on authentication, before any write. Tests
# that write an employee id directly through MemoryManager or an agent skip that
# step, which SQLite never notices (it does not enforce foreign keys) but
# PostgreSQL rejects. Such tests create the rows they reference.

KNOWN_TEST_EMPLOYEE_IDS = ("emp-1", "emp-admin-1", "emp-sales-1", "emp-sales-2", "emp-other")


def _role_for_test_id(employee_id: str) -> str:
    for marker, role in (("admin", "admin"), ("mgmt", "management"), ("eng", "engineer")):
        if marker in employee_id:
            return role
    return "sales"


def seed_employees(session, *employee_ids: str) -> None:
    """Insert an Employee row for each id that doesn't have one yet. Deliberately a
    plain insert — no audit entry — so tests that count audit rows are unaffected."""
    from app.memory.models import Employee, EmployeeRole

    for employee_id in employee_ids:
        if session.get(Employee, employee_id) is None:
            session.add(Employee(
                id=employee_id,
                name=employee_id,
                email=f"{employee_id}@test.local",
                role=EmployeeRole(_role_for_test_id(employee_id)),
            ))
    session.commit()


@pytest.fixture
def known_employees(_isolated_db):
    """Seed KNOWN_TEST_EMPLOYEE_IDS. Opt in per module with
    `pytestmark = pytest.mark.usefixtures("known_employees")`."""
    session = _isolated_db()
    try:
        seed_employees(session, *KNOWN_TEST_EMPLOYEE_IDS)
    finally:
        session.close()


# ── Auth header helpers ────────────────────────────────────────────────────────

def admin_headers(employee_id: str = "emp-admin-1") -> dict:
    return {"X-Employee-Id": employee_id, "X-Employee-Role": "admin"}


def sales_headers(employee_id: str = "emp-sales-1") -> dict:
    return {"X-Employee-Id": employee_id, "X-Employee-Role": "sales"}


def management_headers(employee_id: str = "emp-mgmt-1") -> dict:
    return {"X-Employee-Id": employee_id, "X-Employee-Role": "management"}


def engineer_headers(employee_id: str = "emp-eng-1") -> dict:
    return {"X-Employee-Id": employee_id, "X-Employee-Role": "engineer"}
