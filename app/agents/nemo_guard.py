"""
NeMo Guardrails — a semantic output rail over the Sales Agent's replies.

WHAT THIS ADDS THAT THE DETERMINISTIC LAYER CANNOT
app/agents/guardrails.py catches what can be enumerated: a figure the conversation
cannot justify, a time nobody offered, a prompt marker, an employee id. It cannot
catch internal information that has been *paraphrased* — "the deal only needs one
signature under five lakh, so this should be quick" leaks the approval threshold
without matching any pattern. That is what this rail is for.

HOW THE TWO RELATE
The deterministic layer is the primary control and always runs. This one is an
additional, optional pass layered on top; it never replaces a deterministic check
and never overrides an allow decision made there. If this module is switched off,
missing, or unreachable, the deterministic guarantees are exactly what they were.

COST, HONESTLY
This is an extra LLM call on every customer reply — latency and spend per message —
and it is probabilistic, so it will occasionally miss and occasionally misfire. That
is why it is off by default, why the policy is written to prefer allowing an
ambiguous message, and why it can never be the only thing standing between an
internal figure and a customer.

FAIL-OPEN BY DEFAULT
If the rail errors or times out the reply is sent, matching this codebase's posture
that a broken LLM degrades the conversation rather than breaking it — the
deterministic checks have already run either way. A deployment that would rather
lose the reply than risk a leak can set MEMORY-style flag NEMO_GUARDRAILS_FAIL_CLOSED
to true, and every failure then falls back to the safe deterministic reply instead.
"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from pathlib import Path
from typing import Optional

from app.config import settings

logger = logging.getLogger(__name__)

INTERNAL_DISCLOSURE = "internal_disclosure"

_POLICY_PATH = Path(__file__).parent / "nemo" / "output_policy.md"

# Loading the rails takes a couple of seconds, so it happens once, lazily, on the
# first reply that needs it rather than at import or at app boot.
_lock = threading.Lock()
_rails = None
_load_failed = False

# One long-lived worker so a per-message timeout does not mean a thread per message.
# A timed-out call cannot be killed in Python, so it is abandoned and the worker
# picks up the next one when it finishes.
_executor: Optional[ThreadPoolExecutor] = None


def _build_rails():
    """Compose the rails config from the policy file plus this app's LLM settings.

    The policy lives in its own file so it reads as a document the sales team can
    review and amend; only the model wiring is assembled here.
    """
    import yaml
    from nemoguardrails import LLMRails, RailsConfig

    model_config = {"type": "main", "engine": "openai", "model": settings.llm_model}
    # Only pass a base_url when it is genuinely custom — the OpenAI engine defaults
    # correctly, and passing the default explicitly breaks some proxies.
    if settings.llm_base_url and "api.openai.com" not in settings.llm_base_url:
        model_config["parameters"] = {"base_url": settings.llm_base_url}

    config = RailsConfig.from_content(
        yaml_content=yaml.safe_dump({
            "models": [model_config],
            "rails": {"output": {"flows": ["self check output"]}},
            "prompts": [{
                "task": "self_check_output",
                "content": _POLICY_PATH.read_text(encoding="utf-8"),
            }],
        })
    )
    return LLMRails(config)


def available() -> bool:
    """Whether the rail can run at all: switched on, library present, model configured.

    Mock mode disables it — the rail is an LLM call, and there is no LLM.
    """
    if not settings.nemo_guardrails_enabled or settings.llm_mock_mode:
        return False
    if _load_failed:
        return False
    try:
        import nemoguardrails  # noqa: F401
    except ImportError:
        return False
    return True


def _get_rails():
    global _rails, _load_failed, _executor
    if _rails is not None:
        return _rails
    with _lock:
        if _rails is None and not _load_failed:
            try:
                import os

                # The OpenAI engine reads the key from the environment. Set it from
                # settings so there is one place credentials are configured.
                if settings.llm_api_key and not os.environ.get("OPENAI_API_KEY"):
                    os.environ["OPENAI_API_KEY"] = settings.llm_api_key
                _rails = _build_rails()
                _executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="nemo-rail")
                logger.info("NeMo Guardrails output rail loaded")
            except Exception:
                _load_failed = True
                logger.exception(
                    "NeMo Guardrails failed to load — continuing with the "
                    "deterministic guardrails only"
                )
    return _rails


def check_output(bot_message: str, user_message: str = "") -> Optional[dict]:
    """Run the disclosure rail over one reply.

    Returns a violation dict when the reply must not be sent, or None to allow it.
    Never raises: on error or timeout it honours the fail-open/closed setting.
    """
    if not available() or not (bot_message or "").strip():
        return None

    rails = _get_rails()
    if rails is None or _executor is None:
        return None

    try:
        from nemoguardrails.rails.llm.options import RailType

        future = _executor.submit(
            rails.check,
            messages=[
                {"role": "user", "content": user_message or ""},
                {"role": "assistant", "content": bot_message},
            ],
            rail_types=[RailType.OUTPUT],
        )
        result = future.result(timeout=settings.nemo_guardrails_timeout_seconds)
    except FutureTimeout:
        return _on_failure("timed out")
    except Exception as exc:
        return _on_failure(f"{type(exc).__name__}: {exc}")

    if _is_blocked(result):
        rail_name = getattr(result, "rail", None) or "self check output"
        logger.warning(
            "NeMo Guardrails blocked a reply as an internal disclosure",
            extra={"rail": rail_name},
        )
        return {
            "code": INTERNAL_DISCLOSURE,
            "detail": f"semantic disclosure rail blocked the reply ({rail_name})",
        }
    return None


def _is_blocked(result) -> bool:
    """True when the rails result says the message must not be sent.

    Read defensively: this is the one place the library's result shape reaches our
    code, and a rename upstream should degrade to allowing the reply rather than
    raising inside a customer conversation.
    """
    status = getattr(result, "status", None)
    return str(getattr(status, "value", status)).lower() == "blocked"


def _on_failure(reason: str) -> Optional[dict]:
    logger.warning(
        "NeMo Guardrails rail unavailable for this reply (%s) — %s",
        reason,
        "blocking, fail-closed is on" if settings.nemo_guardrails_fail_closed
        else "allowing, deterministic checks still applied",
    )
    if settings.nemo_guardrails_fail_closed:
        return {
            "code": INTERNAL_DISCLOSURE,
            "detail": f"disclosure rail unavailable ({reason}) and fail-closed is enabled",
        }
    return None


def reset_for_tests() -> None:
    """Drop the cached rails so a test can change settings and reload."""
    global _rails, _load_failed, _executor
    with _lock:
        if _executor is not None:
            _executor.shutdown(wait=False)
        _rails = None
        _executor = None
        _load_failed = False
