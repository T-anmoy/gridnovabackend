"""BE-22 — .env.example is documentation only: every KEY= line is empty, and nothing in it
looks like a real secret. Real values go in .env (git-ignored); production values in
Secret Manager and deploy/env.production.yaml. (On 31 Oct 2026 real secrets were found
in a local copy of this file; it was never committed.)"""
import re
from pathlib import Path

import pytest

ENV_EXAMPLE = Path(__file__).resolve().parents[1] / ".env.example"
ASSIGNMENT = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")
# A prefix followed by key characters; a comment naming the prefix ("prod: sk_live_...")
# is documentation and passes.
SECRET_SHAPES = {
    "Clerk live key": re.compile(r"sk_live_[A-Za-z0-9]{8,}"),
    "Clerk / Stripe test key": re.compile(r"sk_test_[A-Za-z0-9]{8,}"),
    "OpenAI project key": re.compile(r"sk-proj-[A-Za-z0-9_\-]{8,}"),
    "Resend API key": re.compile(r"\bre_[A-Za-z0-9_]{20,}"),
    "Google OAuth client secret": re.compile(r"GOCSPX-"),
    "Fernet key (44-character base64)": re.compile(r"(?<![A-Za-z0-9_\-])[A-Za-z0-9_\-]{43}=(?![A-Za-z0-9_\-=])"),
}


def _lines() -> list[str]:
    return ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()


def problems(lines: list[str]) -> list[str]:
    out = []
    for n, line in enumerate(lines, 1):
        m = ASSIGNMENT.match(line)
        if m and m.group(2).strip():
            out.append(f"line {n}: {m.group(1)} has a value")
        for name, shape in SECRET_SHAPES.items():
            if shape.search(line):
                out.append(f"line {n}: looks like a {name}")
    return out


def test_every_assignment_is_empty_and_nothing_looks_secret():
    assert ENV_EXAMPLE.exists()
    lines = _lines()
    assert sum(1 for line in lines if ASSIGNMENT.match(line)) > 50     # the file documents every setting
    assert problems(lines) == []


@pytest.mark.parametrize("line", [
    "DATABASE_URL=postgresql://u:p@localhost/db",
    "export LLM_API_KEY=x",
    "CLERK_SECRET_KEY = sk_live_abc",
    "# old key: sk_test_51HxYzAbCdEf",
    "# LLM_API_KEY was sk-proj-abcdef123456",
    "# RESEND_API_KEY re_AbCdEfGhIjKlMnOpQrStUv",
    "# GOOGLE_OAUTH_CLIENT_SECRET GOCSPX-abc",
    "# FERNET_ENCRYPTION_KEY " + "A" * 43 + "=",
])
def test_the_check_catches_values_and_secret_shapes(line):
    assert problems([line])


@pytest.mark.parametrize("line", [
    "DATABASE_URL=",
    "# connection cannot start). prod: sk_live_...",
    "# e.g. postgresql://USER:PASS@/DB?host=/cloudsql/PROJECT:asia-south1:INSTANCE",
    "# Generate with: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"",
    "",
])
def test_documentation_lines_pass(line):
    assert problems([line]) == []
