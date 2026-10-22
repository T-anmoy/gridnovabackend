"""BE-1 A5 — Grid Nova's drafted KYC consent notice (policies document 6) and Customer
Information Notice (document 7) are loaded as version draft-v1.1, NOT switched on.

They were generated from the frontend's content/policies/notices.en.json and checked
word for word at the time (every word kept; only the `**` emphasis, the editorial note
and the checkbox label dropped); here the loaded result is pinned."""
from app.associates import texts
from app.onboarding import config as onboarding_config

PLACEHOLDER = "[PLACEHOLDER"


def _kyc() -> str:
    return texts._load()["kyc_consent_versions"]["draft-v1.1"]["text"]


def _notice() -> str:
    return onboarding_config.load()["notice_versions"]["draft-v1.1"]["text"]


def test_both_texts_load_through_the_existing_loaders():
    assert _kyc().startswith("Why we are asking for this information\n\n")
    assert _notice().startswith("Grid Nova Energy — how we use your information\n\n")


def test_no_placeholder_marker_and_no_markdown():
    for text in (_kyc(), _notice()):
        assert PLACEHOLDER not in text and "**" not in text


def test_the_editorial_note_and_checkbox_label_are_not_in_the_text():
    assert "Shown on its own screen" not in _kyc() and "I have read this notice" not in _kyc()
    assert "read out to, the customer" not in _notice() and "has been informed and agrees" not in _notice()


def test_flattened_for_pre_line_display():
    kyc = _kyc()
    assert kyc.count("\n• ") == 5
    for heading in ("What we will collect", "How we will use it", "Who will see it", "How long we keep it",
                    "Your choices"):
        assert f"\n\n{heading}\n\n" in kyc
    assert "store only its last four digits. Please upload a masked Aadhaar (showing only the last four digits)" in kyc
    assert _notice().endswith("info@gridnova.in · +91 98747 74440.")
    assert _notice().count("\n\n") == 4


def test_the_kyc_notice_is_live_and_matches_the_approved_draft():
    version, text = texts.current_consent()
    assert version == "approved-v1" and not texts.consent_is_placeholder()
    assert text == _kyc()
    assert not texts.welcome_letter_is_placeholder()
    assert "+91 98747 74440" in texts.welcome_letter_body()


def test_the_customer_notice_is_still_the_placeholder():
    assert onboarding_config.current_notice()[0] == "placeholder-v0" and onboarding_config.notice_is_placeholder()
