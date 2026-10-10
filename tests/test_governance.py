"""
Agent governance — what each agent may do, enforced rather than documented.

Two halves. The runtime half asserts that a prohibited or disabled action is
actually refused at the moment it is attempted. The structural half checks the
architecture invariants that were previously only prose at the top of each module:
that agents do not import each other, and that the Proposal Agent has no route to
sending anything. Those were the rules most likely to be broken by a future change
made in good faith, precisely because nothing was checking them.
"""
import ast
import pathlib

from app.governance import policy, registry
from app.memory.memory_manager import MemoryManager
from app.memory.models import MessageRole, PipelineStage
from tests.conftest import admin_headers, engineer_headers, sales_headers


APP_ROOT = pathlib.Path(__file__).resolve().parents[1] / "app"


def _source(module_path: str) -> str:
    return (APP_ROOT / module_path).read_text(encoding="utf-8")


def _tree(module_path: str):
    return ast.parse(_source(module_path))


def _imported_modules(module_path: str) -> set:
    """Every module name imported anywhere in the file, including inside function
    bodies — this codebase imports tools lazily inside methods on purpose, so a
    top-of-file-only check would miss exactly the coupling worth catching."""
    names = set()
    for node in ast.walk(_tree(module_path)):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            names.add(base)
            names.update(f"{base}.{alias.name}" for alias in node.names)
    return names


def _identifiers(module_path: str) -> set:
    """Names and attributes actually referenced in code. Docstrings and comments are
    not code, which is the whole point — several of these modules describe their own
    invariant in prose using the very words being checked for."""
    found = set()
    for node in ast.walk(_tree(module_path)):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
    return found


def _uses_attribute(module_path: str, owner: str, attr: str) -> bool:
    """True when the file contains `owner.attr` as real code."""
    for node in ast.walk(_tree(module_path)):
        if (isinstance(node, ast.Attribute) and node.attr == attr
                and isinstance(node.value, ast.Name) and node.value.id == owner):
            return True
    return False


# ── The registry itself ────────────────────────────────────────────────────────

class TestRegistry:
    def test_every_agent_module_exists(self):
        for spec in registry.all_specs():
            path = APP_ROOT.parent / (spec.module.replace(".", "/") + ".py")
            assert path.exists(), f"{spec.id} points at a module that isn't there: {spec.module}"

    def test_no_agent_both_permits_and_forbids_an_action(self):
        """A spec that contradicts itself would make every authorisation decision
        depend on check order rather than intent."""
        for spec in registry.all_specs():
            overlap = set(spec.capabilities) & set(spec.prohibitions)
            assert not overlap, f"{spec.id} both permits and forbids {overlap}"

    def test_the_three_rules_the_design_rests_on(self):
        assert registry.SEND_PROPOSAL in registry.PROPOSAL_AGENT.prohibitions
        assert registry.APPROVE_PROPOSAL in registry.PROPOSAL_AGENT.prohibitions
        assert registry.REPLY_TO_CUSTOMER in registry.PROPOSAL_AGENT.prohibitions

    def test_the_sales_agent_may_not_price_or_send(self):
        assert registry.SET_PROPOSAL_PRICE in registry.SALES_AGENT.prohibitions
        assert registry.SEND_PROPOSAL in registry.SALES_AGENT.prohibitions

    def test_the_memory_agent_never_faces_a_customer(self):
        """It can read every customer's history, so a customer-facing route would
        be a cross-customer data leak."""
        assert registry.MEMORY_AGENT.audience == "employees"
        assert registry.REPLY_TO_CUSTOMER in registry.MEMORY_AGENT.prohibitions

    def test_may_reflects_both_lists(self):
        assert registry.SALES_AGENT.may(registry.REPLY_TO_CUSTOMER)
        assert not registry.SALES_AGENT.may(registry.SEND_PROPOSAL)
        assert not registry.SALES_AGENT.may("an_action_nobody_defined")


# ── Authorisation ──────────────────────────────────────────────────────────────

class TestAuthorize:
    def test_permits_a_declared_capability(self):
        assert policy.authorize("sales_agent", registry.REPLY_TO_CUSTOMER).allowed

    def test_refuses_a_prohibited_action(self, memory, db_session):
        decision = policy.authorize("proposal_agent", registry.SEND_PROPOSAL, memory=memory)
        assert not decision.allowed
        assert decision.code == policy.PROHIBITED

    def test_refuses_an_action_nobody_granted(self, memory):
        decision = policy.authorize("sales_agent", registry.ANSWER_EMPLOYEE_QUESTION, memory=memory)
        assert not decision.allowed
        # Distinct from PROHIBITED on purpose: "nobody granted this" and "somebody
        # forbade this" read very differently in an incident review.
        assert decision.code == policy.NOT_PERMITTED

    def test_refuses_an_unregistered_agent(self):
        decision = policy.authorize("rogue_agent", registry.REPLY_TO_CUSTOMER)
        assert not decision.allowed
        assert decision.code == policy.UNKNOWN_AGENT

    def test_decision_is_truthy_when_allowed(self):
        assert bool(policy.authorize("sales_agent", registry.BOOK_MEETING))
        assert not bool(policy.authorize("sales_agent", registry.SEND_PROPOSAL, record=False))

    def test_a_broken_check_allows_rather_than_crashes(self, monkeypatch):
        """A governance layer that can crash the thing it governs is worse than
        none — the business rules and approval gates still stand either way."""
        def _explode(*args, **kwargs):
            raise RuntimeError("registry unavailable")

        monkeypatch.setattr(policy, "_evaluate", _explode)
        assert policy.authorize("sales_agent", registry.REPLY_TO_CUSTOMER).allowed

    def test_a_denial_is_written_to_the_audit_trail(self, memory, db_session):
        policy.authorize("proposal_agent", registry.SEND_PROPOSAL, memory=memory)
        db_session.commit()

        entries = memory.list_audit_logs(entity_type="agent", entity_id="proposal_agent")
        assert entries
        assert entries[-1].after_value["code"] == policy.PROHIBITED
        assert entries[-1].after_value["allowed"] is False


# ── The kill switch ────────────────────────────────────────────────────────────

class TestKillSwitch:
    def test_a_disabled_agent_is_refused(self, monkeypatch, memory):
        monkeypatch.setattr("app.config.settings.disabled_agents", ["sales_agent"])
        decision = policy.authorize("sales_agent", registry.REPLY_TO_CUSTOMER, memory=memory)
        assert not decision.allowed
        assert decision.code == policy.AGENT_DISABLED

    def test_disabling_one_agent_leaves_the_others_running(self, monkeypatch, memory):
        monkeypatch.setattr("app.config.settings.disabled_agents", ["sales_agent"])
        assert policy.authorize("proposal_agent", registry.DRAFT_PROPOSAL, memory=memory).allowed

    def test_a_stood_down_agent_hands_the_customer_to_a_person(
        self, client, _isolated_db, monkeypatch
    ):
        """Through the real public endpoint, because that is where it matters: the
        customer must not get silence or a stack trace, and the team must find out.
        A kill switch that just drops conversations on the floor is not a kill switch.
        """
        monkeypatch.setattr("app.config.settings.disabled_agents", ["sales_agent"])

        response = client.post("/api/v1/chat/message", json={
            "message": "hi, I want solar for my factory",
        })
        assert response.status_code == 200
        body = response.json()
        assert body["reply"], "a stood-down agent still answers the customer"
        assert "team" in body["reply"].lower()

        session = _isolated_db()
        try:
            mem = MemoryManager(session)
            from tests.chat_support import lead_of
            messages = mem.get_lead_context(lead_of(session, body["session_token"]).id)["messages"]
            # The customer's own words are never lost because the agent stood down.
            assert any(
                m.role == MessageRole.customer and "factory" in m.content for m in messages
            )
        finally:
            session.close()

    def test_a_stood_down_agent_does_not_touch_the_lead(
        self, client, _isolated_db, monkeypatch
    ):
        """An agent that has been switched off should not still be quietly
        extracting fields and moving the pipeline stage."""
        monkeypatch.setattr("app.config.settings.disabled_agents", ["sales_agent"])

        response = client.post("/api/v1/chat/message", json={
            "message": "my name is Raj, 50kW rooftop in Pune, raj@example.com",
        })
        assert response.status_code == 200
        token = response.json()["session_token"]

        session = _isolated_db()
        try:
            from tests.chat_support import lead_of
            lead = lead_of(session, token)
            assert dict(lead.collected_fields or {}) == {}
            assert lead.pipeline_stage == PipelineStage.new
        finally:
            session.close()

    def test_the_denial_is_recorded_even_though_nothing_was_committed(
        self, client, _isolated_db, monkeypatch
    ):
        """The harness writes a denied run on its own session precisely so the
        record survives whatever the caller does with its transaction."""
        monkeypatch.setattr("app.config.settings.disabled_agents", ["sales_agent"])
        client.post("/api/v1/chat/message", json={"message": "hello"})

        session = _isolated_db()
        try:
            runs = MemoryManager(session).list_audit_logs(entity_type="agent_run")
            denied = [r for r in runs if (r.after_value or {}).get("code") == "denied"]
            assert denied, "a denied run must leave a record"
            assert denied[-1].after_value["governance_code"] == policy.AGENT_DISABLED
        finally:
            session.close()

    def test_a_disabled_proposal_agent_refuses_to_draft(self, _isolated_db, monkeypatch):
        from app.orchestrator.orchestrator import orchestrator

        session = _isolated_db()
        try:
            mem = MemoryManager(session)
            customer = mem.get_or_create_customer(
                email="p@example.com", name="P", performed_by="test"
            )
            lead = mem.create_lead(customer_id=customer.id, performed_by="test")
            mem.update_lead_fields(
                lead_id=lead.id, performed_by="test", stage=PipelineStage.qualified,
                new_fields={"name": "P", "email": "p@example.com",
                            "project_type": "rooftop", "location": "Pune",
                            "capacity_kw": 50},
            )
            session.commit()
            lead_id, customer_id = lead.id, customer.id
        finally:
            session.close()

        monkeypatch.setattr("app.config.settings.disabled_agents", ["proposal_agent"])
        result = orchestrator.request_proposal_draft(
            lead_id=lead_id, customer_id=customer_id, performed_by="emp-sales-1"
        )
        assert result.get("proposal_id") is None
        assert "switched off" in (result.get("error") or "")


# ── Runaway protection ─────────────────────────────────────────────────────────

class TestBudget:
    def _lead_with_replies(self, memory, db_session, count):
        from app.memory.models import Channel

        customer = memory.get_or_create_customer(
            email="loop@example.com", name="Loop", performed_by="test"
        )
        lead = memory.create_lead(customer_id=customer.id, performed_by="test")
        for _ in range(count):
            memory.log_message(
                channel=Channel.website, role=MessageRole.agent, content="reply",
                lead_id=lead.id, customer_id=customer.id, agent_name="Nova",
            )
        db_session.commit()
        return lead

    def test_stands_down_after_the_daily_limit(self, memory, db_session, monkeypatch):
        monkeypatch.setattr("app.config.settings.agent_daily_action_limit_per_lead", 3)
        lead = self._lead_with_replies(memory, db_session, 3)

        decision = policy.authorize(
            "sales_agent", registry.REPLY_TO_CUSTOMER, memory=memory, lead_id=lead.id
        )
        assert not decision.allowed
        assert decision.code == policy.BUDGET_EXCEEDED

    def test_under_the_limit_is_fine(self, memory, db_session, monkeypatch):
        monkeypatch.setattr("app.config.settings.agent_daily_action_limit_per_lead", 10)
        lead = self._lead_with_replies(memory, db_session, 3)
        assert policy.authorize(
            "sales_agent", registry.REPLY_TO_CUSTOMER, memory=memory, lead_id=lead.id
        ).allowed

    def test_zero_disables_the_limit(self, memory, db_session, monkeypatch):
        monkeypatch.setattr("app.config.settings.agent_daily_action_limit_per_lead", 0)
        lead = self._lead_with_replies(memory, db_session, 50)
        assert policy.authorize(
            "sales_agent", registry.REPLY_TO_CUSTOMER, memory=memory, lead_id=lead.id
        ).allowed

    def test_the_limit_does_not_apply_to_human_triggered_work(
        self, memory, db_session, monkeypatch
    ):
        """Drafting and recall are started by a person, so a person is already the
        rate limit. Counting them here would stop real work for no safety gain."""
        monkeypatch.setattr("app.config.settings.agent_daily_action_limit_per_lead", 1)
        lead = self._lead_with_replies(memory, db_session, 20)
        assert policy.authorize(
            "proposal_agent", registry.DRAFT_PROPOSAL, memory=memory, lead_id=lead.id
        ).allowed


# ── Structural invariants ──────────────────────────────────────────────────────

class TestArchitecturalInvariants:
    """These were prose at the top of each module. Prose does not fail a build.

    Every check here parses the module rather than grepping it. The first version of
    these tests matched raw text and failed on the very docstrings that STATE the
    invariant ("this module must never import from agents.proposal_agent") — a
    structural rule has to be checked structurally or it reports on its own comments.
    """

    def test_agents_never_import_each_other(self):
        forbidden_for = {
            "agents/sales_agent.py": {"proposal_agent", "memory_agent"},
            "agents/proposal_agent.py": {"sales_agent", "memory_agent"},
            "agents/memory_agent.py": {"sales_agent", "proposal_agent"},
        }
        for module, forbidden in forbidden_for.items():
            imported = _imported_modules(module)
            leaked = {f for f in forbidden if any(f in name for name in imported)}
            assert not leaked, (
                f"{module} imports {leaked} — agents must communicate only through "
                f"the orchestrator and shared memory"
            )

    def test_no_agent_reaches_another_through_a_dynamic_import(self):
        """The invariant is about coupling, not about the `import` keyword."""
        for module in ("agents/sales_agent.py", "agents/proposal_agent.py",
                       "agents/memory_agent.py"):
            assert "importlib" not in _identifiers(module), (
                f"{module} uses importlib, which would route around the import check"
            )

    def test_the_proposal_agent_has_no_route_to_sending(self):
        used = _identifiers("agents/proposal_agent.py")
        for forbidden in ("ProposalStatus", "generate_accept_token",
                          "transition_proposal_status", "proposal_sent"):
            assert forbidden not in used, (
                f"proposal_agent uses {forbidden!r} — drafting and sending must stay "
                f"separate, with a human in between"
            )

    def test_only_the_sales_agent_writes_customer_facing_messages(self):
        for module in ("agents/proposal_agent.py", "agents/memory_agent.py"):
            assert not _uses_attribute(module, "MessageRole", "agent"), (
                f"{module} writes a customer-facing message; only the Sales Agent may"
            )

    def test_business_rules_never_call_an_llm(self):
        """The thresholds that decide qualification and sign-off are deterministic.
        An LLM reaching in here would be the most consequential regression possible
        in this codebase."""
        assert not any("llm" in name for name in _imported_modules("rules/business_rules.py"))
        assert not any("llm" in ident.lower() for ident in _identifiers("rules/business_rules.py"))

    def test_the_content_engine_never_calls_an_llm(self):
        """Section selection and pricing are composed from YAML, never generated."""
        assert not any("llm" in name for name in _imported_modules("proposals/content_engine.py"))
        assert "llm_client" not in _identifiers("proposals/content_engine.py")

    def test_every_agent_is_invoked_through_the_harness(self):
        """Governance moved out of the agents and into the harness, so the invariant
        moved with it: an agent cannot skip authorisation by forgetting to call it,
        because it never calls it — the harness does, on the way in."""
        orchestrator_src = _source("orchestrator/orchestrator.py")
        assert orchestrator_src.count("harness.run(") == 3, (
            "each of the three agents must be invoked through the harness"
        )
        for spec in registry.all_specs():
            assert spec.id in orchestrator_src, f"{spec.id} is never run by the orchestrator"

    def test_the_harness_authorises_every_run(self):
        assert "authorize" in _identifiers("agents/harness.py")

    def test_no_agent_bypasses_the_harness_by_calling_governance_itself(self):
        """The Sales Agent still authorises `book_meeting` mid-turn, which is correct
        — that is a sub-action, not the run. What no agent may do is re-authorise its
        own entry point, which would double-log every denial."""
        for module, primary in (
            ("agents/sales_agent.py", registry.REPLY_TO_CUSTOMER),
            ("agents/proposal_agent.py", registry.DRAFT_PROPOSAL),
            ("agents/memory_agent.py", registry.ANSWER_EMPLOYEE_QUESTION),
        ):
            src = _source(module)
            assert f"registry.{primary.upper()}" not in src, (
                f"{module} authorises its own entry point; the harness already did"
            )


# ── The reporting endpoints ────────────────────────────────────────────────────

class TestGovernanceEndpoints:
    def test_lists_every_agent_with_its_limits(self, client):
        response = client.get("/api/v1/dashboard/governance/agents", headers=admin_headers())
        assert response.status_code == 200
        body = response.json()

        agents = {a["id"]: a for a in body["agents"]}
        assert set(agents) == {"sales_agent", "proposal_agent", "memory_agent"}
        assert "send_proposal" in agents["proposal_agent"]["prohibitions"]
        assert agents["proposal_agent"]["uses_llm"] is False
        assert agents["sales_agent"]["enabled"] is True
        assert body["limits"]["replies_per_lead_per_24h"] > 0

    def test_reflects_the_kill_switch(self, client, monkeypatch):
        monkeypatch.setattr("app.config.settings.disabled_agents", ["sales_agent"])
        response = client.get("/api/v1/dashboard/governance/agents", headers=admin_headers())
        agents = {a["id"]: a for a in response.json()["agents"]}
        assert agents["sales_agent"]["enabled"] is False
        assert agents["proposal_agent"]["enabled"] is True

    def test_management_may_view_governance(self, client):
        from tests.conftest import management_headers

        assert client.get(
            "/api/v1/dashboard/governance/agents", headers=management_headers()
        ).status_code == 200

    def test_sales_and_engineers_may_not(self, client):
        for headers in (sales_headers(), engineer_headers()):
            assert client.get(
                "/api/v1/dashboard/governance/agents", headers=headers
            ).status_code == 403

    def test_activity_counts_denials_and_blocks(self, client, _isolated_db):
        session = _isolated_db()
        try:
            mem = MemoryManager(session)
            policy.authorize("proposal_agent", registry.SEND_PROPOSAL, memory=mem)
            policy.authorize("sales_agent", registry.APPROVE_PROPOSAL, memory=mem)
            session.commit()
        finally:
            session.close()

        response = client.get(
            "/api/v1/dashboard/governance/activity?days=1", headers=admin_headers()
        )
        assert response.status_code == 200
        body = response.json()
        assert body["governance_denials"] == 2
        assert body["by_agent"]["proposal_agent"]["denied"] == 1
        assert body["by_agent"]["proposal_agent"]["denials_by_code"]["action_prohibited"] == 1

    def test_activity_requires_authorisation(self, client):
        assert client.get(
            "/api/v1/dashboard/governance/activity", headers=sales_headers()
        ).status_code == 403
