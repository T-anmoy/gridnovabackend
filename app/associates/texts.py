"""Grid Nova-supplied wording for the associate programme (config/associate_documents.yaml):
the versioned KYC consent notice and the welcome-letter body."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

_PATH = Path(__file__).resolve().parents[2] / "config" / "associate_documents.yaml"
PLACEHOLDER_MARKER = "[PLACEHOLDER"


@lru_cache(maxsize=1)
def _load() -> dict:
    with _PATH.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    current = data.get("current_kyc_consent_version")
    versions = data.get("kyc_consent_versions") or {}
    if not current or current not in versions:
        raise RuntimeError(f"{_PATH.name}: current_kyc_consent_version must name one of kyc_consent_versions")
    return data


def current_consent() -> tuple[str, str]:
    """(version, text) of the consent notice applicants accept today."""
    data = _load()
    version = data["current_kyc_consent_version"]
    return version, str(data["kyc_consent_versions"][version]["text"]).strip()


def consent_is_placeholder() -> bool:
    return PLACEHOLDER_MARKER in current_consent()[1]


def welcome_letter_body() -> str:
    return str(_load().get("welcome_letter_body") or "").strip()


def welcome_letter_is_placeholder() -> bool:
    return PLACEHOLDER_MARKER in welcome_letter_body()
