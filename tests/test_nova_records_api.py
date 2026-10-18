"""BE-9 — the Nova Records API (/api/v1/dashboard/nova): contract shapes, conversation
ownership, feedback, limits, retention, and end-to-end questions in mock mode against the
seeded fixture with the exact refs each must cite."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.memory.models import AuditLog, NovaConversation, NovaTurn
from app.records import config, store
from tests.records_support import ASK, BASE, ask, records, today, who  # noqa: F401 — fixtures

MONEY_KEYS = {"amount", "amount_due", "total_value", "price", "percent", "balance", "commission",
              "contract_value_inr", "amount_received_inr", "amount_outstanding_inr", "subsidy_amount_inr"}


def _keys(node) -> set:
    if isinstance(node, dict):
        return set(node) | set().union(*(_keys(v) for v in node.values())) if node else set()
    if isinstance(node, list):
        return set().union(*(_keys(v) for v in node)) if node else set()
    return set()


class TestContract:
    def test_shape(self, client, records):
        body = ask(client, "Show me every 5 kW+ project we commissioned in Howrah in 2025 and the inverters used")
        assert set(body) == {"conversation_id", "turn_id", "status", "answer", "citations", "tables", "searched",
                             "suggestions", "generated_at"}
        assert body["status"] == "answered" and len(body["conversation_id"]) == 32 and len(body["turn_id"]) == 32
        assert datetime.fromisoformat(body["generated_at"]).utcoffset() == timedelta(0)
        cite = body["citations"][0]
        assert set(cite) == {"ref", "kind", "label", "path"} and cite["kind"] == "archive_project"
        assert cite["path"].startswith("/dashboard/archive/")
        table = body["tables"][0]
        assert {c["key"]: c["type"] for c in table["columns"]}["capacity_kw"] == "kw"
        assert [r["ref"] for r in table["rows"]] == ["GN-ARC-0102", "GN-ARC-0101"]
        assert body["searched"][0]["tool"] == "search_projects" and "district Howrah" in body["searched"][0]["summary"]
        assert all(set(s) == {"label", "question", "path"} for s in body["suggestions"])

    @pytest.mark.parametrize("body", [{"question": ""}, {"question": "x" * 1001},
                                      {"question": "hi", "context": {"lead": 1}},
                                      {"question": "hi", "conversation_id": "not-an-id"}, {"question": "hi", "x": 1}])
    def test_validation(self, client, body):
        assert client.post(ASK, json=body, headers=who("admin")).status_code == 422

    def test_blank_question(self, client):
        r = client.post(ASK, json={"question": "   "}, headers=who("admin"))
        assert r.status_code == 422 and r.json()["detail"]["code"] == "empty_question"

    def test_associates_are_refused(self, client):
        assert client.post(ASK, json={"question": "hi"}, headers={"X-Associate-Id": "GNA-WB-26-00001"}).status_code == 401

    def test_operations_sees_no_money_anywhere(self, client, records):
        for q in ("Show me GN-ARC-0101", "How many projects by district?",
                  "How much is outstanding on projects that passed inspection?", "Show me GNE-WB-RES-2026-00125"):
            body = ask(client, q, role="operations")
            assert not (_keys(body) & MONEY_KEYS), q
            assert "₹" not in body["answer"] and "310000" not in str(body)

    def test_the_turn_is_stored_with_how_it_was_made(self, client, db_session, records):
        body = ask(client, "Projects in Howrah", role="engineer")
        turn = db_session.get(NovaTurn, body["turn_id"])
        assert turn.status == "answered" and turn.planner == "keyword" and turn.composer == "template"
        assert turn.tool_calls[0]["tool"] == "search_projects" and turn.tool_calls[0]["ok"] is True
        assert "GN-ARC-0101" in turn.refs and turn.role == "engineer" and turn.latency_ms >= 0
        assert turn.response["answer"] == body["answer"] and turn.run_id

    def test_refusal_is_answered_and_audited(self, client, db_session, records):
        body = ask(client, "Send the warranty card to Amit Sen")
        assert body["status"] == "refused" and body["tables"] == [] and body["citations"] == []
        rows = db_session.execute(select(AuditLog).where(AuditLog.action == "nova_records_refused",
                                                         AuditLog.entity_id == body["turn_id"])).scalars().all()
        assert len(rows) == 1

    def test_a_switched_off_assistant_is_503(self, client, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "disabled_agents", ["nova_records"])
        r = client.post(ASK, json={"question": "Projects in Howrah"}, headers=who("admin"))
        assert r.status_code == 503 and r.json()["detail"]["code"] == "assistant_disabled"


class TestConversations:
    def test_continue_and_read_back(self, client, records):
        first = ask(client, "Projects in Howrah", role="sales")
        second = ask(client, "Which warranties expire in the next 90 days?", role="sales",
                     conversation_id=first["conversation_id"])
        assert second["conversation_id"] == first["conversation_id"]
        listing = client.get(f"{BASE}/conversations", headers=who("sales")).json()["items"]
        assert [c["conversation_id"] for c in listing] == [first["conversation_id"]]
        assert listing[0]["turn_count"] == 2 and listing[0]["title"] == "Projects in Howrah"
        detail = client.get(f"{BASE}/conversations/{first['conversation_id']}", headers=who("sales")).json()
        assert [t["turn_id"] for t in detail["turns"]] == [first["turn_id"], second["turn_id"]]
        assert detail["turns"][1]["answer"] == second["answer"] and detail["turns"][0]["feedback"]["helpful"] is None

    def test_history_reaches_the_planner(self, client, records, monkeypatch):
        seen = []
        from app.agents import records_agent as module
        original = module.router.plan
        monkeypatch.setattr(module.llm_client, "records_plan", lambda **kw: seen.append(kw["history"]) or None)
        monkeypatch.setattr(module.router, "plan", original)
        first = ask(client, "Projects in Howrah")
        ask(client, "And in Nadia?", conversation_id=first["conversation_id"])
        assert seen[0] == [] and seen[1][0]["question"] == "Projects in Howrah"
        assert "GN-ARC-0101" in seen[1][0]["refs"]

    def test_another_employees_conversation_is_404(self, client, records):
        mine = ask(client, "Projects in Howrah", role="sales")
        r = client.post(ASK, json={"question": "more", "conversation_id": mine["conversation_id"]},
                        headers=who("sales", 2))
        assert r.status_code == 404 and r.json()["detail"]["code"] == "conversation_not_found"
        for role in ("sales", "management", "accounts"):
            n = 2 if role == "sales" else 1
            assert client.get(f"{BASE}/conversations/{mine['conversation_id']}", headers=who(role, n)).status_code == 404
        assert client.get(f"{BASE}/conversations", headers=who("sales", 2)).json()["items"] == []

    def test_admin_may_read_any_for_audit(self, client, db_session, records):
        mine = ask(client, "Projects in Howrah", role="sales")
        r = client.get(f"{BASE}/conversations/{mine['conversation_id']}", headers=who("admin"))
        assert r.status_code == 200 and r.json()["employee_id"] == "emp-sales-1"
        audit = db_session.execute(select(AuditLog).where(
            AuditLog.action == "nova_conversation_read_for_audit")).scalar_one()
        assert audit.performed_by == "emp-admin-1" and audit.after_value == {"owner": "emp-sales-1"}

    def test_feedback(self, client, db_session, records):
        body = ask(client, "Projects in Howrah", role="engineer")
        url = f"{BASE}/turns/{body['turn_id']}/feedback"
        assert client.post(url, json={"helpful": True}, headers=who("engineer", 2)).status_code == 404
        r = client.post(url, json={"helpful": False, "note": "  missing ARC-3  "}, headers=who("engineer"))
        assert r.status_code == 200 and r.json()["helpful"] is False and r.json()["note"] == "missing ARC-3"
        turn = db_session.get(NovaTurn, body["turn_id"])
        db_session.refresh(turn)
        assert turn.feedback_helpful is False and turn.feedback_at is not None
        assert client.post(url, json={"note": "x"}, headers=who("engineer")).status_code == 422


class TestLimits:
    def test_rate_limit(self, client, records, monkeypatch):
        monkeypatch.setitem(config.load()["limits"], "requests_per_minute", 2)
        for _ in range(2):
            ask(client, "Projects in Howrah")
        r = client.post(ASK, json={"question": "Projects in Howrah"}, headers=who("admin"))
        assert r.status_code == 429 and r.json()["detail"]["code"] == "rate_limited" and r.headers["Retry-After"] == "60"
        ask(client, "Projects in Howrah", role="sales")   # per employee

    def test_daily_cap(self, client, records, monkeypatch):
        monkeypatch.setitem(config.load()["limits"], "daily_questions", 1)
        ask(client, "Projects in Howrah")
        r = client.post(ASK, json={"question": "Projects in Howrah"}, headers=who("admin"))
        assert r.status_code == 429 and r.json()["detail"]["code"] == "daily_limit_reached"

    def test_yesterdays_questions_do_not_count(self, client, db_session, records, monkeypatch):
        monkeypatch.setitem(config.load()["limits"], "daily_questions", 1)
        body = ask(client, "Projects in Howrah")
        turn = db_session.get(NovaTurn, body["turn_id"])
        turn.created_at = datetime.now(timezone.utc) - timedelta(days=2)
        db_session.commit()
        ask(client, "Projects in Howrah")


class TestRetention:
    URL = f"{BASE}/internal/check-nova-records-retention"

    def test_purges_only_expired_conversations(self, client, db_session, records):
        old = ask(client, "Projects in Howrah")
        new = ask(client, "Projects in Nadia")
        row = db_session.get(NovaConversation, old["conversation_id"])
        row.updated_at = datetime.now(timezone.utc) - timedelta(days=config.retention_days() + 1)
        db_session.commit()
        listing = client.get(f"{BASE}/conversations", headers=who("admin")).json()["items"]
        assert [c["conversation_id"] for c in listing] == [new["conversation_id"]]
        from tests.conftest import seed_employees
        seed_employees(db_session, "emp-admin-1")
        r = client.post(self.URL, headers=who("admin"))
        assert r.status_code == 200 and r.json()["conversations_deleted"] == 1 and r.json()["turns_deleted"] == 1
        db_session.expire_all()
        assert db_session.get(NovaConversation, old["conversation_id"]) is None
        assert db_session.get(NovaTurn, old["turn_id"]) is None
        assert db_session.get(NovaConversation, new["conversation_id"]) is not None

    @pytest.mark.parametrize("role", ["management", "sales", "engineer", "operations", "accounts"])
    def test_admin_only(self, client, role):
        assert client.post(self.URL, headers=who(role)).status_code == 403


# ── End to end, mock mode: 15 questions, exact refs ───────────────────────────────

E2E = [
    ("admin", "Show me every 5 kW+ project we commissioned in Howrah in 2025 and the inverters used", "answered",
     ["GN-ARC-0102", "GN-ARC-0101"]),
    ("admin", "What did we install for Mr Sen in Salt Lake, and when does the warranty end?", "answered",
     ["GN-ARC-0106"]),
    ("accounts", "How much is outstanding on projects that passed inspection?", "answered", ["GNE-WB-RES-2026-00125"]),
    ("management", "Which associate brought the most customers in Nadia this quarter?", "answered",
     ["GNA-WB-26-90001"]),
    ("admin", "Is there a project near Kasba with Waaree modules?", "answered", ["GN-ARC-0105"]),
    ("engineer", "Which warranties expire in the next 90 days?", "answered", ["GNE-WB-RES-2026-00125", "GN-ARC-0101"]),
    ("sales", "Show me GNE-WB-RES-2026-00125", "answered", ["GNE-WB-RES-2026-00125"]),
    ("sales", "Show me GNE-WB-RES-2026-00126", "not_on_record", []),
    ("operations", "Open service requests", "answered", ["SR-1", "GNE-WB-RES-2026-00125"]),
    ("admin", "Projects for customer Sen", "needs_clarification", ["GN-ARC-0101", "GN-ARC-0106"]),
    ("operations", "What is the total contract value of projects in Howrah?", "refused", []),
    ("admin", "Mark GN-ARC-0101 as handed over", "refused", []),
    ("admin", "What is Amit Sen's PAN number?", "refused", []),
    ("engineer", "Projects in Patna with Waaree modules", "not_on_record", []),
    ("sales", "Approved inverter makes in Nova Signature", "answered", ["PKG-NOVA_SIGNATURE"]),
]


@pytest.mark.parametrize("role, question, status, cited", E2E, ids=[q for _, q, _, _ in E2E])
def test_end_to_end(client, records, role, question, status, cited):
    body = ask(client, question, role=role)
    assert body["status"] == status, body["answer"]
    assert [c["ref"] for c in body["citations"]] == cited, body["answer"]


def test_end_to_end_lead_of_another_rep(client, records):
    lead = records["leads"]["priya"]["lead_id"]
    hidden = ask(client, f"Show me lead {lead}", role="sales")
    assert hidden["status"] == "not_on_record" and "Priya" not in str(hidden)
    shown = ask(client, f"Show me lead {lead}", role="sales", n=2)
    assert [c["ref"] for c in shown["citations"]][:1] == [f"L{lead}"]


def test_context_from_the_page(client, records):
    body = ask(client, "When does the warranty end?", role="operations",
               context={"project_ref": "GNE-WB-RES-2026-00125"})
    assert body["status"] == "answered" and body["citations"][0]["ref"] == "GNE-WB-RES-2026-00125"
    assert body["citations"][0]["path"] == f"/dashboard/operations/{records['live']['GNE-WB-RES-2026-00125']['onboarding_id']}"


def test_store_today_is_ist(monkeypatch):
    # 20:00 UTC on 31 Dec is already 1 Jan in India.
    assert store.ist_today(datetime(2026, 12, 31, 20, 0, tzinfo=timezone.utc)).isoformat() == "2027-01-01"
