"""BE-6 C4 — optional `locale` (en | hi | bn) on POST /chat/message steers reply language
only; unknown values mean English; identity and session behaviour are unchanged."""
import pytest

from app.llm.client import LLMClient
from app.llm.locale import reply_locale
from tests.chat_support import chat, lead_of


@pytest.fixture
def prompts(monkeypatch):
    built = []
    original = LLMClient._build_reply_system_prompt

    def capture(*args, **kwargs):
        text = original(*args, **kwargs)
        built.append(text)
        return text

    monkeypatch.setattr(LLMClient, "_build_reply_system_prompt", staticmethod(capture))
    return built


@pytest.mark.parametrize("value, expected", [
    ("hi", "hi"), ("BN", "bn"), ("hi-IN", "hi"), ("bn_BD", "bn"), ("en", "en"),
    ("fr", "en"), ("", "en"), (None, "en"), ("klingon", "en"),
])
def test_reply_locale_normalisation(value, expected):
    assert reply_locale(value) == expected


@pytest.mark.parametrize("locale, language", [("hi", "Hindi"), ("bn", "Bengali")])
def test_prompt_carries_the_language_line(client, prompts, locale, language):
    # BE-7: Hindi/Bengali replies are written in English, guarded, then translated
    # (tests/test_translation_guard.py) — the line now says so.
    assert chat(client, "hi, thinking about solar", locale=locale).status_code == 200
    assert f"The customer reads {language}" in prompts[-1]
    assert "Write your reply in English anyway" in prompts[-1]
    assert f"Write your reply in {language}" not in prompts[-1]


@pytest.mark.parametrize("extra", [{}, {"locale": "en"}, {"locale": "xx"}, {"locale": ""}])
def test_english_and_unknown_add_no_language_line(client, prompts, extra):
    assert chat(client, "hi, thinking about solar", **extra).status_code == 200
    assert "LANGUAGE:" not in prompts[-1]


def test_locale_does_not_touch_session_or_identity(client, db_session):
    first = chat(client, "hi, thinking about solar", locale="hi").json()
    again = chat(client, "rooftop in Pune, about 20 kW", token=first["session_token"], locale="bn")
    assert again.status_code == 200 and again.json()["session_token"] == first["session_token"]
    assert lead_of(db_session, first["session_token"]).id == lead_of(db_session, again.json()["session_token"]).id
