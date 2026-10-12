"""Reply languages the website chat supports."""
from __future__ import annotations

from typing import Optional

SUPPORTED_LOCALES = ("en", "hi", "bn")
_LANGUAGE_NAMES = {"hi": "Hindi", "bn": "Bengali"}


def reply_locale(value: Optional[str]) -> str:
    """Normalise a client-sent locale; unknown values fall back to English."""
    code = (value or "").strip().lower().replace("_", "-").split("-")[0]
    return code if code in SUPPORTED_LOCALES else "en"


def language_instruction(locale: Optional[str]) -> str:
    """The system-prompt line that steers reply language ("" for English)."""
    name = _LANGUAGE_NAMES.get(reply_locale(locale))
    if not name:
        return ""
    return (
        f"\n\nLANGUAGE: Write your reply in {name}, in a natural, conversational register. "
        "This changes the language only: every rule above and below still applies, and "
        "names, email addresses, numbers, equipment terms and the exact meeting times you "
        "were given stay exactly as provided."
    )
