"""
Agent harness — the execution wrapper every agent runs inside.

What is under test is the scaffolding, not the agents: that a raised agent becomes a
contained failure rather than an exception in a customer's conversation, that a
denied run still produces something sendable, that a turn has one traceable id, and
that the numbers on the runs report come from the runs themselves.
"""
from datetime import datetime, timedelta, timezone

from app.agents import harness
from app.governance import policy, registry
from app.memory.memory_manager import MemoryManager
from tests.conftest import admin_headers, sales_headers


def _ok(payload=None):
    return lambda: dict(payload or {"done": True})


# ── The envelope ───────────────────────────────────────────────────────────────

class TestRunEnvelope:
    def test_a_successful_run(self, memory, db_session):
        run = harness.run(
            "sales_agent", registry.REPLY_TO_CUSTOMER, _ok({"reply_text": "hi"}),
            memory=memory,
        )
        assert run.ok and run.code == harness.OK
        assert run.result["reply_text"] == "hi"
        assert run.error is None
        assert run.duration_ms >= 0
        assert len(run.run_id) == 12

    def test_every_run_gets_its_own_id(self, memory):
        first = harness.run("sales_agent", registry.REPLY_TO_CUSTOMER, _ok(), memory=memory)
        second = harness.run("sales_agent", registry.REPLY_TO_CUSTOMER, _ok(), memory=memory)
        assert first.run_id != second.run_id

    def test_the_run_id_is_readable_from_inside_the_agent(self, memory):
        """This is what makes one turn traceable across three modules."""
        seen = {}

        def _inner():
            seen["id"] = harness.current_run_id.get()
            return {}

        run = harness.run("sales_agent", registry.REPLY_TO_CUSTOMER, _inner, memory=memory)
        assert seen["id"] == run.run_id

    def test_the_run_id_is_cleared_afterwards(self, memory):
        harness.run("sales_agent", registry.REPLY_TO_CUSTOMER, _ok(), memory=memory)
        assert harness.current_run_id.get() == ""

    def test_an_agent_returning_nothing_is_not_a_failure(self, memory):
        run = harness.run("sales_agent", registry.REPLY_TO_CUSTOMER, lambda: None, memory=memory)
        assert run.ok
        assert run.result == {}


# ── Failure containment ────────────────────────────────────────────────────────

class TestFailureContainment:
    def _boom(self):
        raise ValueError("the agent fell over")

    def test_an_exception_becomes_a_failed_run(self, memory):
        run = harness.run("sales_agent", registry.REPLY_TO_CUSTOMER, self._boom, memory=memory)
        assert not run.ok
        assert run.code == harness.FAILED
        assert "ValueError" in run.error

    def test_the_caller_supplies_what_the_customer_sees(self, memory):
        """The harness has no idea what a safe answer sounds like for a given
        audience, so it never invents one."""
        run = harness.run(
            "sales_agent", registry.REPLY_TO_CUSTOMER, self._boom, memory=memory,
            on_error=lambda exc: {"reply_text": "sorry, try again shortly"},
        )
        assert run.result["reply_text"] == "sorry, try again shortly"

    def test_a_failed_run_is_recorded_on_its_own_session(self, _isolated_db):
        """The caller is about to roll back, and a failure is the run most worth
        having a record of — so it must not be rolled back with everything else."""
        session = _isolated_db()
        try:
            memory = MemoryManager(session)
            harness.run("sales_agent", registry.REPLY_TO_CUSTOMER, self._boom, memory=memory)
            session.rollback()  # exactly what the orchestrator node does
        finally:
            session.close()

        check = _isolated_db()
        try:
            runs = MemoryManager(check).list_audit_logs(entity_type="agent_run")
            assert any((r.after_value or {}).get("code") == "failed" for r in runs)
        finally:
            check.close()

    def test_a_broken_ledger_does_not_break_the_run(self, memory, monkeypatch):
        def _explode(*args, **kwargs):
            raise RuntimeError("audit table is gone")

        monkeypatch.setattr(
            "app.memory.memory_manager.MemoryManager.record_agent_run", _explode
        )
        run = harness.run("sales_agent", registry.REPLY_TO_CUSTOMER, _ok(), memory=memory)
        assert run.ok, "bookkeeping must never take down the work it is recording"


# ── Governance, applied once, here ─────────────────────────────────────────────

class TestGovernanceInTheHarness:
    def test_a_prohibited_action_never_reaches_the_agent(self, memory):
        ran = []
        run = harness.run(
            "proposal_agent", registry.SEND_PROPOSAL,
            lambda: ran.append(1) or {}, memory=memory,
        )
        assert run.code == harness.DENIED
        assert run.governance_code == policy.PROHIBITED
        assert ran == [], "the agent body must not run at all"

    def test_a_denied_run_still_produces_something_sendable(self, memory):
        run = harness.run(
            "proposal_agent", registry.SEND_PROPOSAL, _ok(), memory=memory,
            on_denied=lambda decision: {"reply_text": "let me get a colleague"},
        )
        assert run.result["reply_text"] == "let me get a colleague"

    def test_a_disabled_agent_is_denied(self, memory, monkeypatch):
        monkeypatch.setattr("app.config.settings.disabled_agents", ["sales_agent"])
        run = harness.run("sales_agent", registry.REPLY_TO_CUSTOMER, _ok(), memory=memory)
        assert run.governance_code == policy.AGENT_DISABLED


# ── Accounting ─────────────────────────────────────────────────────────────────

class TestAccounting:
    def test_model_calls_are_attributed_to_the_run(self, memory):
        from app.llm.client import llm_client

        def _three_calls():
            llm_client.extract_fields("hi", [])
            llm_client.extract_meeting_signal("hi", [])
            llm_client.generate_reply("hi", "opening", {}, [], [])
            return {}

        run = harness.run("sales_agent", registry.REPLY_TO_CUSTOMER, _three_calls, memory=memory)
        assert run.llm_calls == 3
        assert run.estimated_cost_usd > 0

    def test_counts_do_not_leak_between_runs(self, memory):
        from app.llm.client import llm_client

        harness.run(
            "sales_agent", registry.REPLY_TO_CUSTOMER,
            lambda: llm_client.extract_fields("hi", []) and {}, memory=memory,
        )
        second = harness.run("sales_agent", registry.REPLY_TO_CUSTOMER, _ok(), memory=memory)
        assert second.llm_calls == 0

    def test_a_slow_run_is_flagged_but_still_succeeds(self, memory, monkeypatch):
        """Flagged, not cut off. See the harness docstring for why it does not
        pretend to enforce a timeout it cannot enforce."""
        monkeypatch.setattr("app.config.settings.agent_slow_run_ms", 0.001)
        import time

        run = harness.run(
            "sales_agent", registry.REPLY_TO_CUSTOMER,
            lambda: time.sleep(0.01) or {}, memory=memory,
        )
        assert run.slow is True
        assert run.ok, "slow is a warning, not a failure"

    def test_the_summary_carries_no_customer_text(self, memory):
        """Run records go to the audit trail; conversation content belongs in the
        conversation log, which has its own access rules."""
        run = harness.run(
            "sales_agent", registry.REPLY_TO_CUSTOMER,
            _ok({"reply_text": "SENSITIVE CUSTOMER TEXT"}), memory=memory,
        )
        assert "SENSITIVE CUSTOMER TEXT" not in str(run.summary())


# ── Through the real agents ────────────────────────────────────────────────────

class TestAgentsRunUnderTheHarness:
    def test_a_chat_turn_records_a_run(self, client, _isolated_db):
        response = client.post("/api/v1/chat/message", json={"message": "hi there"})
        assert response.status_code == 200

        session = _isolated_db()
        try:
            runs = MemoryManager(session).list_audit_logs(entity_type="agent_run")
            sales = [r for r in runs if (r.after_value or {}).get("agent") == "sales_agent"]
            assert sales, "a customer turn must leave a run record"
            payload = sales[-1].after_value
            assert payload["code"] == "ok"
            assert payload["llm_calls"] >= 1
            assert payload["duration_ms"] >= 0
        finally:
            session.close()

    def test_a_failing_agent_does_not_break_the_endpoint(
        self, client, _isolated_db, monkeypatch
    ):
        """The whole point of containment: a customer gets an apology, not a 500."""
        monkeypatch.setattr(
            "app.agents.sales_agent.sales_agent.handle_message",
            lambda **kw: (_ for _ in ()).throw(RuntimeError("agent exploded")),
        )
        response = client.post("/api/v1/chat/message", json={"message": "hello"})
        assert response.status_code == 200
        assert "something went wrong" in response.json()["reply"].lower()

        session = _isolated_db()
        try:
            runs = MemoryManager(session).list_audit_logs(entity_type="agent_run")
            assert any((r.after_value or {}).get("code") == "failed" for r in runs)
        finally:
            session.close()

    def test_the_recall_agent_runs_under_the_harness(self, client, _isolated_db):
        session = _isolated_db()
        try:
            mem = MemoryManager(session)
            customer = mem.get_or_create_customer(
                email="priya@sunrise.in", name="Priya Sharma", performed_by="test"
            )
            session.commit()
            customer_id = customer.id
        finally:
            session.close()

        assert client.post(
            "/api/v1/dashboard/memory/ask",
            json={"question": "what do we know?", "customer_id": customer_id},
            headers=admin_headers(),
        ).status_code == 200

        session = _isolated_db()
        try:
            runs = MemoryManager(session).list_audit_logs(entity_type="agent_run")
            assert any(
                (r.after_value or {}).get("agent") == "memory_agent" for r in runs
            )
        finally:
            session.close()


# ── The runs report ────────────────────────────────────────────────────────────

class TestRunsReport:
    def test_stats_come_from_the_runs_themselves(self, memory, db_session):
        for _ in range(3):
            harness.run("sales_agent", registry.REPLY_TO_CUSTOMER, _ok(), memory=memory)
        harness.run(
            "sales_agent", registry.REPLY_TO_CUSTOMER,
            lambda: (_ for _ in ()).throw(RuntimeError("nope")), memory=memory,
        )
        db_session.commit()

        stats = memory.agent_run_stats(datetime.now(timezone.utc) - timedelta(hours=1))
        sales = stats["by_agent"]["sales_agent"]
        assert sales["runs"] == 4
        assert sales["ok"] == 3
        assert sales["failed"] == 1
        assert "p95_ms" in sales and "median_ms" in sales

    def test_the_endpoint_is_admin_or_management_only(self, client):
        assert client.get(
            "/api/v1/dashboard/governance/runs", headers=admin_headers()
        ).status_code == 200
        assert client.get(
            "/api/v1/dashboard/governance/runs", headers=sales_headers()
        ).status_code == 403

    def test_the_endpoint_reports_a_real_turn(self, client):
        client.post("/api/v1/chat/message", json={"message": "hi"})
        body = client.get(
            "/api/v1/dashboard/governance/runs?days=1", headers=admin_headers()
        ).json()
        assert body["total_runs"] >= 1
        assert "sales_agent" in body["by_agent"]
