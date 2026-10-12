"""P3 (BE-3): list endpoints and scheduled jobs whose query count used to grow with the
data. Each test counts the SQL statements one call executes (an SQLAlchemy
before_cursor_execute listener on the test engine) and requires the count to stay the
same as the data grows, under a fixed budget."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from sqlalchemy import event

from app.memory.models import AuditLog, Employee, EmployeeRole, Meeting, Proposal, ProposalStatus
from tests.payments_support import territories  # noqa: F401 — fixture

SALES = {"X-Employee-Id": "emp-sales-1", "X-Employee-Role": "sales"}
ADMIN = {"X-Employee-Id": "emp-admin-1", "X-Employee-Role": "admin"}


@contextmanager
def count_queries():
    from app.memory import db as db_module
    engine = db_module.SessionLocal.kw["bind"]
    statements: list[str] = []

    def count(conn, cursor, statement, *args):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", count)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", count)


# ── GET /dashboard/activity/recent (Sales) ────────────────────────────────────────

def _seed_activity(db, memory, n_leads: int) -> None:
    """n_leads leads (a third the caller's, a third someone else's, a third unassigned),
    each with audit entries about the lead, its customer, a proposal and a meeting."""
    for emp_id in ("emp-sales-1", "emp-sales-2"):
        if db.get(Employee, emp_id) is None:
            db.add(Employee(id=emp_id, name=emp_id, email=f"{emp_id}@example.com", role=EmployeeRole.sales))
    db.commit()
    now = datetime.now(timezone.utc)
    for i in range(n_leads):
        customer = memory.get_or_create_customer(email=f"c{i}-{n_leads}@example.com", name=f"C{i}", performed_by="t")
        lead = memory.create_lead(customer_id=customer.id, performed_by="t")
        lead.assigned_employee_id = ("emp-sales-1", "emp-sales-2", None)[i % 3]
        proposal = Proposal(lead_id=lead.id, version=1, status=ProposalStatus.draft, content={})
        meeting = Meeting(lead_id=lead.id, employee_id="emp-sales-2", scheduled_at=now + timedelta(days=i + 1))
        db.add_all([proposal, meeting])
        db.flush()
        for kind, entity in (("lead", lead.id), ("customer", customer.id), ("proposal", proposal.id),
                             ("meeting", meeting.id)):
            db.add(AuditLog(entity_type=kind, entity_id=str(entity), action="touched", performed_by="emp-admin-1"))
    db.commit()


def _activity_queries(client, db, memory, n_leads: int) -> int:
    _seed_activity(db, memory, n_leads)
    client.get("/api/v1/dashboard/activity/recent", headers=SALES)   # provision the caller first
    with count_queries() as statements:
        r = client.get("/api/v1/dashboard/activity/recent", headers=SALES)
    assert r.status_code == 200
    return len(statements), r.json()["items"]


def test_sales_activity_feed_has_a_constant_query_count(client, db_session, memory):
    few, items = _activity_queries(client, db_session, memory, 6)
    assert items, "the caller's own and unassigned leads appear"
    many, items = _activity_queries(client, db_session, memory, 60)
    assert len(items) == 15
    assert many == few, (few, many)
    assert many <= 15, many   # was ~2 per scanned entry (up to ~1,000 for 500 entries)


def test_sales_activity_feed_never_shows_another_reps_lead(client, db_session, memory):
    _seed_activity(db_session, memory, 9)
    items = client.get("/api/v1/dashboard/activity/recent", headers=SALES).json()["items"]
    from app.memory.models import Lead
    from app.memory.memory_manager import MemoryManager
    entries = {(e.entity_type, e.entity_id): e for e in db_session.query(AuditLog)}
    mm = MemoryManager(db_session)
    for item in items:
        lead_ids = mm.lead_ids_for_audit_entry(entries[(item["entity_type"], item["entity_id"])])
        assert any(db_session.get(Lead, i).assigned_employee_id in (None, "emp-sales-1") for i in lead_ids)


# ── GET /dashboard/associate-applications ─────────────────────────────────────────

def _applications_queries(client, db, n: int) -> tuple[int, dict]:
    from tests.onboarding_support import ALIPURDUAR, make_associate
    for i in range(n):   # both programmes, interleaved
        make_associate(db, **({"level": "district_head", "territory": ALIPURDUAR} if i % 2 else {}))
    client.get("/api/v1/dashboard/associate-applications", headers=ADMIN)
    with count_queries() as statements:
        r = client.get("/api/v1/dashboard/associate-applications", params={"page_size": 5}, headers=ADMIN)
    assert r.status_code == 200, r.text
    return len(statements), r.json()


def test_applications_are_paginated_in_sql(client, db_session, territories):
    few, body = _applications_queries(client, db_session, 6)
    assert body["total"] == 6 and len(body["items"]) == 5
    many, body = _applications_queries(client, db_session, 30)
    assert body["total"] == 36 and len(body["items"]) == 5
    assert many == few, (few, many)
    times = [i["submitted_at"] for i in body["items"]]
    assert times == sorted(times, reverse=True)


# ── Scheduled jobs: only open work is loaded ──────────────────────────────────────

def test_payment_alerts_skip_projects_with_nothing_left_to_alert(client, db_session, territories):
    from app.jobs import payment_alerts
    from app.memory.models import PaymentMilestone, ProjectStage
    from tests.payments_support import accepted_customer

    _, open_oid = accepted_customer(client, db_session, stage=ProjectStage.accepted)
    _, alerted_oid = accepted_customer(client, db_session, stage=ProjectStage.accepted)
    assert set(payment_alerts._open_onboarding_ids(db_session)) == {open_oid, alerted_oid}
    for m in db_session.query(PaymentMilestone).filter_by(onboarding_id=alerted_oid):
        m.overdue_alerted_at = datetime.now(timezone.utc)
    db_session.commit()
    assert payment_alerts._open_onboarding_ids(db_session) == [open_oid]


def test_execution_alerts_skip_handed_over_projects(client, db_session, territories, monkeypatch):
    from app.jobs import execution_alerts
    from app.memory.models import ProjectStage
    from app.onboarding import progress
    from tests.execution_support import advance_to, associate_in_execution

    _, running = associate_in_execution(client, db_session)
    _, finished = associate_in_execution(client, db_session)
    advance_to(db_session, running, ProjectStage.installed)
    advance_to(db_session, finished, ProjectStage.handover_complete)
    loaded = []
    real = progress.applicable
    monkeypatch.setattr(progress, "applicable", lambda db, o: loaded.append(o.id) or real(db, o))
    execution_alerts.check_execution_alerts(db_session, datetime.now(timezone.utc))
    assert running in loaded and finished not in loaded
