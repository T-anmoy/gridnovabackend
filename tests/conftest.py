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
# Readiness answers fresh in tests (its cache has its own test in tests/test_readiness.py).
os.environ.setdefault("READINESS_CACHE_SECONDS", "0")
# Sentry is forced off, not defaulted: app.main calls sentry_sdk.init() at import time
# whenever SENTRY_DSN is set, and a test run must never report to a real project —
# whether the DSN comes from .env or from the shell.
os.environ["SENTRY_DSN"] = ""

# Background work (app/jobs/runner.py) runs inline under tests: whatever a request
# started has finished when the test client returns.
import app.jobs.runner as _runner  # noqa: E402
_runner.INLINE = True

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
    # GET /health/ready caches its answer per process; every test starts without one.
    from app.api.routes.health import reset_readiness_cache
    reset_readiness_cache()
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


class _HttpxPostSeam:
    """Stands in for the notifier's shared httpx.Client: forwards to httpx.post, looked
    up at call time, so the tests that intercept email by patching httpx.post keep
    working (and no test can reach Resend through a client built before the patch)."""

    def post(self, *args, **kwargs):
        import httpx
        return httpx.post(*args, **kwargs)


@pytest.fixture(autouse=True)
def _notifier_http_seam(monkeypatch):
    import app.notifications.notifier as notifier_module
    monkeypatch.setattr(notifier_module, "http_client", lambda: _HttpxPostSeam())


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
    import app.api.routes.public_associates as public_associates
    for log in (public_associates._apply_request_log, public_associates._verify_request_log,
                public_associates._lookup_request_log):
        log.clear()
    chat._request_log.clear()
    chat._recent_messages.clear()
    public_proposals._accept_request_log.clear()
    public_proposals._pdf_request_log.clear()
    public_booking._availability_request_log.clear()
    public_booking._booking_request_log.clear()
    public_booking._recent_emails.clear()
    for log in (pricing._instant_estimate_request_log, pricing._estimate_request_log,
                pricing._packages_request_log):
        log.clear()
    pricing._packages_body.cache_clear()
    public_intake._request_log.clear()
    public_intake._recent_emails.clear()
    public_intake._contact_request_log.clear()
    public_intake._recent_contact_emails.clear()
    import app.api.routes.associate_equipment as associate_equipment
    import app.api.routes.public_receipts as public_receipts
    associate_equipment._request_log.clear()
    public_receipts._receipt_request_log.clear()
    from app.records import store as nova_records_store
    nova_records_store._minute_log.clear()
    yield
    for log in (public_associates._apply_request_log, public_associates._verify_request_log,
                public_associates._lookup_request_log):
        log.clear()
    chat._request_log.clear()
    chat._recent_messages.clear()
    public_proposals._accept_request_log.clear()
    public_proposals._pdf_request_log.clear()
    public_booking._availability_request_log.clear()
    public_booking._booking_request_log.clear()
    public_booking._recent_emails.clear()
    for log in (pricing._instant_estimate_request_log, pricing._estimate_request_log,
                pricing._packages_request_log):
        log.clear()
    pricing._packages_body.cache_clear()
    public_intake._request_log.clear()
    public_intake._recent_emails.clear()
    public_intake._contact_request_log.clear()
    public_intake._recent_contact_emails.clear()


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


# ── Private storage (R2) with boto3 mocked ────────────────────────────────────

class FakeS3:
    """Stands in for boto3's S3 client: records what was signed or uploaded."""

    def __init__(self, **client_kwargs):
        self.client_kwargs = client_kwargs
        self.presigned: list[dict] = []
        self.put: list[dict] = []

    def generate_presigned_url(self, operation, Params, ExpiresIn):
        self.presigned.append({"operation": operation, "params": Params, "expires_in": ExpiresIn})
        return f"https://signed.example/{Params['Key']}?X-Amz-Expires={ExpiresIn}&X-Amz-Signature=test"

    def put_object(self, **kwargs):
        self.put.append(kwargs)
        _FAKE_BUCKET[kwargs["Key"]] = kwargs["Body"]

    def get_object(self, Bucket, Key):
        import io
        return {"Body": io.BytesIO(_FAKE_BUCKET[Key])}


# Objects "uploaded" through FakeS3 during one test, readable back via get_object.
_FAKE_BUCKET: dict[str, bytes] = {}


@pytest.fixture
def fake_storage(monkeypatch):
    """Configured STORAGE_* settings and a recording fake S3 client. Returns the list
    of clients created, newest last."""
    import boto3
    from app.config import settings

    for field, value in (
        ("storage_endpoint_url", "https://acct.r2.cloudflarestorage.com"),
        ("storage_bucket", "gridnova-test"),
        ("storage_access_key", "AKIA-TEST"),
        ("storage_secret_key", "SECRET-TEST-DO-NOT-LEAK"),
    ):
        monkeypatch.setattr(settings, field, value)
    clients: list[FakeS3] = []

    def _client(service, **kwargs):
        assert service == "s3"
        clients.append(FakeS3(**kwargs))
        return clients[-1]

    monkeypatch.setattr(boto3, "client", _client)
    _FAKE_BUCKET.clear()
    # app/storage/r2.py caches one client per process: start (and end) without it, so
    # this test's fake is the one in use and never outlives the test.
    from app.storage import r2
    r2.reset_client()
    yield clients
    r2.reset_client()


STORAGE_URL_PREFIX = "https://acct.r2.cloudflarestorage.com/gridnova-test/"


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


# ── Moving a lead's history into the past (stale-lead tests) ──────────────────

def backdate_lead(session, lead_id: int, when) -> None:
    """Make everything recorded about a lead so far — its creation, timeline, audit
    trail and last customer message — look as if it happened at `when`."""
    from app.memory.models import AuditLog, Lead, TimelineEvent

    lead = session.get(Lead, lead_id)
    lead.created_at = when
    if lead.last_customer_message_at is not None:
        lead.last_customer_message_at = when
    for event in session.query(TimelineEvent).filter_by(lead_id=lead_id):
        event.created_at = when
    for entry in session.query(AuditLog).filter_by(entity_type="lead", entity_id=str(lead_id)):
        entry.created_at = when
    session.commit()


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


# ── Live LLM evaluation (BE-7 N4) ─────────────────────────────────────────────
# Tests marked live_llm call the real configured model and cost money. CI stays
# mock-only: they run only when RUN_LIVE_LLM_EVALS=1 is set deliberately.

def pytest_collection_modifyitems(config, items):
    if os.environ.get("RUN_LIVE_LLM_EVALS") == "1":
        return
    skip = pytest.mark.skip(reason="live LLM evaluation: set RUN_LIVE_LLM_EVALS=1 (and LLM_API_KEY) to run")
    for item in items:
        if "live_llm" in item.keywords:
            item.add_marker(skip)
