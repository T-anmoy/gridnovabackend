"""
NeMo Guardrails output rail — stopping internal information reaching a customer.

These drive the REAL NeMo pipeline (config, policy prompt, rail execution, result
parsing) with a stubbed language model standing in for the verdict, so everything
except the model's judgement is genuinely exercised. Stubbing only the verdict is
the point: whether the model answers "Yes" correctly is its business, but whether a
"Yes" actually stops the reply is ours.
"""
import pathlib

import pytest

pytest.importorskip("nemoguardrails", reason="NeMo Guardrails is an optional dependency")

from langchain_core.language_models.fake import FakeListLLM  # noqa: E402

from app.agents import guardrails as g  # noqa: E402
from app.agents import nemo_guard  # noqa: E402
from app.memory.models import MessageRole  # noqa: E402


# A leak with NO pattern in it: no figure, no known id, no fixed phrase. This is
# precisely the kind layer 1 cannot catch and layer 2 exists for — and it has to
# stay pattern-free, or the deterministic layer blocks it first and the rail is
# never consulted at all.
LEAK = "Rajesh from our Surat team handles accounts like yours."
# A leak layer 1 catches on its own, used to prove the rail is not consulted then.
LEAK_WITH_A_FIGURE = "Anything above 5 lakh needs our manager to sign it off."
CLEAN = "Whereabouts is the site, and what's driving this — cost or going greener?"


def _adapter(verdict: str):
    """The stubbed model, wrapped the way the library expects."""
    from nemoguardrails.integrations.langchain.llm_adapter import LangChainLLMAdapter

    return LangChainLLMAdapter(FakeListLLM(responses=[verdict] * 8))


@pytest.fixture
def rail(monkeypatch):
    """A loaded rail whose verdict the test controls."""
    monkeypatch.setattr("app.config.settings.nemo_guardrails_enabled", True)
    monkeypatch.setattr("app.config.settings.llm_api_key", "sk-test")
    monkeypatch.setattr("app.config.settings.nemo_guardrails_fail_closed", False)

    rails = nemo_guard._get_rails()
    assert rails is not None, "the rail should load from the checked-in policy"

    def _verdict(answer: str):
        rails.update_llm(_adapter(answer))

    return _verdict


# ── It is off unless deliberately turned on ────────────────────────────────────

class TestDisabledByDefault:
    def test_off_unless_enabled(self):
        from app.config import settings

        assert settings.nemo_guardrails_enabled is False
        assert nemo_guard.available() is False

    def test_no_call_is_made_when_disabled(self, monkeypatch):
        """The whole suite runs with the rail off, so this asserts the thing that
        keeps every other test honest: disabled really means no LLM call."""
        called = []
        monkeypatch.setattr(nemo_guard, "_get_rails", lambda: called.append(1))
        assert nemo_guard.check_output("anything at all") is None
        assert called == []

    def test_mock_mode_disables_it_even_when_enabled(self, monkeypatch):
        """The rail is an LLM call. With no model configured there is nothing to
        call, and it must not pretend otherwise."""
        monkeypatch.setattr("app.config.settings.nemo_guardrails_enabled", True)
        monkeypatch.setattr("app.config.settings.llm_api_key", "")
        assert nemo_guard.available() is False

    def test_an_empty_reply_is_not_sent_to_the_rail(self, monkeypatch):
        monkeypatch.setattr("app.config.settings.nemo_guardrails_enabled", True)
        monkeypatch.setattr("app.config.settings.llm_api_key", "sk-test")
        assert nemo_guard.check_output("   ") is None


# ── The rail itself ────────────────────────────────────────────────────────────

class TestRailVerdicts:
    def test_a_disclosure_verdict_blocks(self, rail):
        rail("Yes")
        result = nemo_guard.check_output(LEAK, user_message="how fast can you turn this around?")
        assert result is not None
        assert result["code"] == nemo_guard.INTERNAL_DISCLOSURE

    def test_an_allow_verdict_passes(self, rail):
        rail("No")
        assert nemo_guard.check_output(CLEAN, user_message="hi") is None

    def test_the_policy_reaches_the_model(self, rail):
        """The checked-in policy file is what the rail actually runs on — if the
        prompt were not wired through, the rail would be judging on NeMo's generic
        default instead of Grid Nova's disclosure rules."""
        rails = nemo_guard._get_rails()
        prompts = {p.task: p.content for p in rails.config.prompts}
        assert "self_check_output" in prompts
        policy = prompts["self_check_output"]
        for expected in ("Grid Nova", "NAMED OR IDENTIFIABLE EMPLOYEE",
                         "ANOTHER CUSTOMER", "INTERNAL APPROVAL MACHINERY",
                         "{{ bot_response }}"):
            assert expected in policy, expected

    def test_the_policy_leaves_money_to_the_deterministic_layer(self):
        """Found by running the rail against the real model: the policy used to ask
        it whether a figure was "stated by the customer", which it cannot know — the
        rail sees only the reply and the latest message, not the conversation. It
        guessed, and blocked a customer's own bill being repeated back. Money is
        layer 1's job, where the evidence actually is."""
        policy = (pathlib.Path(__file__).resolve().parents[1]
                  / "app" / "agents" / "nemo" / "output_policy.md").read_text(encoding="utf-8")
        assert "Money and meeting times are checked elsewhere" in policy
        assert "always allow" in policy
        # The old failing clause must not come back.
        assert "the customer did not themselves state" not in policy


# ── Failure posture ────────────────────────────────────────────────────────────

class TestFailureHandling:
    def _explode(self, *args, **kwargs):
        raise RuntimeError("rail backend is down")

    def test_fails_open_by_default(self, rail, monkeypatch):
        """An outage must not silence every conversation. The deterministic
        guardrails have already run on this reply either way."""
        rails = nemo_guard._get_rails()
        monkeypatch.setattr(rails, "check", self._explode)
        assert nemo_guard.check_output(CLEAN) is None

    def test_fails_closed_when_configured(self, rail, monkeypatch):
        rails = nemo_guard._get_rails()
        monkeypatch.setattr(rails, "check", self._explode)
        monkeypatch.setattr("app.config.settings.nemo_guardrails_fail_closed", True)

        result = nemo_guard.check_output(CLEAN)
        assert result is not None
        assert "fail-closed" in result["detail"]

    def test_a_timeout_does_not_hang_the_reply(self, rail, monkeypatch):
        import time

        rails = nemo_guard._get_rails()
        monkeypatch.setattr("app.config.settings.nemo_guardrails_timeout_seconds", 0.2)
        monkeypatch.setattr(rails, "check", lambda **kw: time.sleep(5))

        started = time.time()
        assert nemo_guard.check_output(CLEAN) is None
        assert time.time() - started < 2, "the reply must not wait on a stalled rail"

    def test_an_unrecognised_result_shape_allows_the_reply(self):
        """This is the one place the library's result type touches our code. A
        rename upstream should degrade to allowing, never raise mid-conversation."""
        assert nemo_guard._is_blocked(object()) is False
        assert nemo_guard._is_blocked(None) is False


# ── Composition with the deterministic layer ───────────────────────────────────

class TestComposesWithDeterministicGuardrails:
    def test_a_rail_block_replaces_the_reply(self, rail):
        rail("Yes")
        result = g.enforce(
            LEAK, allowed_text="how fast can you turn this around?",
            user_message="how fast?", fallback="SAFE FALLBACK",
        )
        assert result.blocked
        assert nemo_guard.INTERNAL_DISCLOSURE in result.codes
        assert result.reply == "SAFE FALLBACK"

    def test_the_rail_cannot_override_a_deterministic_block(self, rail):
        """Layered on top, never in place of. An allow verdict from the model must
        not rescue a reply the deterministic checks already rejected."""
        rail("No")
        result = g.enforce(
            "We can do this for 8 lakh, all in.",
            allowed_text="nothing about money here",
            user_message="what would it cost?", fallback="SAFE",
        )
        assert result.blocked
        assert g.NEW_MONEY in result.codes

    def test_the_rail_is_not_consulted_once_layer_1_has_blocked(self, rail):
        """A second opinion that cannot change the outcome is a bill, not a control.
        The reply is blocked either way; only the spend differs."""
        called = []
        rail("Yes")
        original = nemo_guard.check_output

        def _spy(*args, **kwargs):
            called.append(1)
            return original(*args, **kwargs)

        import app.agents.nemo_guard as ng
        saved, ng.check_output = ng.check_output, _spy
        try:
            result = g.enforce(
                LEAK_WITH_A_FIGURE, allowed_text="nothing about money here",
                user_message="how fast?", fallback="SAFE",
            )
        finally:
            ng.check_output = saved

        assert result.blocked
        assert g.NEW_MONEY in result.codes
        assert called == [], "the rail must not be called once the reply is blocked"

    def test_a_clean_reply_survives_both_layers(self, rail):
        rail("No")
        result = g.enforce(
            CLEAN, allowed_text="hi there", user_message="hi", fallback="SAFE"
        )
        assert not result.blocked
        assert result.reply == CLEAN


# ── End to end through the agent ───────────────────────────────────────────────

class TestThroughTheSalesAgent:
    def test_a_paraphrased_threshold_leak_never_reaches_the_customer(
        self, memory, db_session, monkeypatch, rail
    ):
        """The case the deterministic layer cannot catch: an internal approval
        threshold explained in ordinary words, matching no pattern at all."""
        from app.agents.sales_agent import sales_agent

        rail("Yes")
        customer = memory.get_or_create_customer(
            email="priya@sunrise.in", name="Priya", performed_by="test"
        )
        lead = memory.create_lead(customer_id=customer.id, performed_by="test")
        db_session.commit()

        monkeypatch.setattr("app.llm.client.llm_client.generate_reply", lambda **kw: LEAK)
        result = sales_agent.handle_message(
            memory=memory, customer_id=customer.id, lead_id=lead.id,
            message="how fast can you turn this around?",
        )
        db_session.commit()

        assert "Rajesh" not in result["reply_text"]
        logged = [
            m.content for m in memory.get_lead_context(lead.id)["messages"]
            if m.role == MessageRole.agent
        ]
        assert logged and "Rajesh" not in logged[-1]

        entries = [
            a for a in memory.list_audit_logs(entity_type="lead", entity_id=str(lead.id))
            if a.action == "reply_blocked_by_guardrail"
        ]
        assert len(entries) == 1
        codes = [v["code"] for v in entries[0].after_value["violations"]]
        assert nemo_guard.INTERNAL_DISCLOSURE in codes
