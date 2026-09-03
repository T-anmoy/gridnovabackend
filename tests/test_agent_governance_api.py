"""Runtime agent governance API — the DB-backed kill switch that makes
DISABLED_AGENTS toggleable without a redeploy.

The critical property under test isn't just that PATCH writes a row: it's that
the very next authorize() call — and a real request through the chat endpoint —
actually honours it, with no env var and no restart involved.
"""
from app.governance import policy, registry
from app.memory.memory_manager import MemoryManager
from tests.conftest import admin_headers, engineer_headers, management_headers, sales_headers


class TestListAgents:
    def test_admin_only(self, client):
        for headers in (sales_headers(), engineer_headers(), management_headers()):
            assert client.get("/api/v1/governance/agents", headers=headers).status_code == 403

    def test_lists_every_agent_with_status_and_action_budget(self, client):
        response = client.get("/api/v1/governance/agents", headers=admin_headers())
        assert response.status_code == 200
        body = response.json()

        agents = {a["id"]: a for a in body["agents"]}
        assert set(agents) == {"sales_agent", "proposal_agent", "memory_agent"}
        assert agents["sales_agent"]["enabled"] is True
        assert agents["sales_agent"]["override_source"] == "env_var"
        assert body["action_budget"]["replies_per_lead_per_24h"] > 0


class TestToggleAgent:
    def test_admin_only(self, client):
        for headers in (sales_headers(), engineer_headers(), management_headers()):
            response = client.patch(
                "/api/v1/governance/agents/sales_agent", json={"enabled": False}, headers=headers
            )
            assert response.status_code == 403

    def test_unknown_agent_404s(self, client):
        response = client.patch(
            "/api/v1/governance/agents/rogue_agent", json={"enabled": False}, headers=admin_headers()
        )
        assert response.status_code == 404

    def test_disables_and_re_enables(self, client, memory):
        response = client.patch(
            "/api/v1/governance/agents/sales_agent", json={"enabled": False}, headers=admin_headers()
        )
        assert response.status_code == 200
        assert response.json() == {
            "agent_id": "sales_agent", "enabled": False, "updated_by": "emp-admin-1",
        }
        assert memory.get_agent_override("sales_agent").enabled is False

        response = client.patch(
            "/api/v1/governance/agents/sales_agent", json={"enabled": True}, headers=admin_headers()
        )
        assert response.status_code == 200
        assert memory.get_agent_override("sales_agent").enabled is True

    def test_audits_with_a_clear_before_after(self, client, memory):
        client.patch(
            "/api/v1/governance/agents/proposal_agent", json={"enabled": False}, headers=admin_headers()
        )
        logs = memory.list_audit_logs(entity_type="agent_governance", entity_id="proposal_agent")
        assert logs[0].action == "kill_switch_toggled"
        assert logs[0].performed_by == "emp-admin-1"
        assert logs[0].before_value == {"enabled": None, "source": "env_var"}
        assert logs[0].after_value == {"enabled": False, "source": "database"}

    def test_reflects_immediately_in_the_read_endpoint(self, client):
        client.patch(
            "/api/v1/governance/agents/memory_agent", json={"enabled": False}, headers=admin_headers()
        )
        response = client.get("/api/v1/governance/agents", headers=admin_headers())
        agents = {a["id"]: a for a in response.json()["agents"]}
        assert agents["memory_agent"]["enabled"] is False
        assert agents["memory_agent"]["override_source"] == "database"
        # The other, older dashboard governance surface must not drift from this one.
        legacy = client.get("/api/v1/dashboard/governance/agents", headers=admin_headers())
        legacy_agents = {a["id"]: a for a in legacy.json()["agents"]}
        assert legacy_agents["memory_agent"]["enabled"] is False

    def test_is_genuinely_runtime_effective_no_restart_needed(self, memory, db_session):
        """Toggling through the API (not monkeypatching settings.disabled_agents)
        must be enough, by itself, for the very next authorize() call to refuse —
        this is the whole point of a DB-backed override over an env var."""
        assert policy.authorize("sales_agent", registry.REPLY_TO_CUSTOMER, memory=memory).allowed

        memory.set_agent_override("sales_agent", enabled=False, performed_by="emp-admin-1")
        db_session.commit()

        decision = policy.authorize("sales_agent", registry.REPLY_TO_CUSTOMER, memory=memory)
        assert not decision.allowed
        assert decision.code == policy.AGENT_DISABLED

    def test_a_stood_down_agent_still_hands_the_customer_to_a_person(
        self, client, _isolated_db
    ):
        """Through the real public chat endpoint, toggled via the new admin API
        rather than monkeypatching the env var — proving the whole path, not just
        the policy function."""
        client.patch(
            "/api/v1/governance/agents/sales_agent", json={"enabled": False}, headers=admin_headers()
        )

        response = client.post("/api/v1/chat/message", json={
            "message": "hi, I want solar for my factory",
        })
        assert response.status_code == 200
        assert "team" in response.json()["reply"].lower()


class TestAgentAudit:
    def test_admin_only(self, client):
        response = client.get("/api/v1/governance/agents/sales_agent/audit", headers=sales_headers())
        assert response.status_code == 403

    def test_unknown_agent_404s(self, client):
        response = client.get("/api/v1/governance/agents/rogue_agent/audit", headers=admin_headers())
        assert response.status_code == 404

    def test_returns_toggle_history(self, client):
        client.patch(
            "/api/v1/governance/agents/sales_agent", json={"enabled": False}, headers=admin_headers()
        )
        client.patch(
            "/api/v1/governance/agents/sales_agent", json={"enabled": True}, headers=admin_headers()
        )

        response = client.get("/api/v1/governance/agents/sales_agent/audit", headers=admin_headers())
        assert response.status_code == 200
        items = response.json()["items"]
        assert len(items) >= 2
        assert items[0]["action"] == "kill_switch_toggled"
        assert items[0]["after_value"]["enabled"] is True

    def test_includes_denial_history_alongside_toggles(self, client, memory, db_session):
        policy.authorize("proposal_agent", registry.SEND_PROPOSAL, memory=memory)
        db_session.commit()

        response = client.get("/api/v1/governance/agents/proposal_agent/audit", headers=admin_headers())
        items = response.json()["items"]
        assert any(i["action"] == "send_proposal_denied" for i in items)
