"""Reply languages the website chat supports."""
from __future__ import annotations

from typing import Optional

SUPPORTED_LOCALES = ("en", "hi", "bn")
_LANGUAGE_NAMES = {"hi": "Hindi", "bn": "Bengali"}


def reply_locale(value: Optional[str]) -> str:
    """Normalise a client-sent locale; unknown values fall back to English."""
    code = (value or "").strip().lower().replace("_", "-").split("-")[0]
    return code if code in SUPPORTED_LOCALES else "en"


def language_name(locale: Optional[str]) -> Optional[str]:
    """"Hindi" / "Bengali" for a translated locale, None for English."""
    return _LANGUAGE_NAMES.get(reply_locale(locale))


def language_instruction(locale: Optional[str]) -> str:
    """The system-prompt line for a Hindi/Bengali conversation ("" for English).

    BE-7: the reply is written in ENGLISH even then. Every guardrail reads English,
    so the reply is checked in English and translated afterwards under its own
    checks (app/agents/translation_check.py). This line exists because a model
    tends to answer in the customer's language, which the checks could not read.
    """
    name = language_name(locale)
    if not name:
        return ""
    return (
        f"\n\nLANGUAGE: The customer reads {name}, and may write in it. Write your reply in "
        f"English anyway — a separate, checked step translates it into {name}. Every rule "
        "above still applies."
    )
