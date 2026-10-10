"""BE-1 A3 — the Docker build context (COPY . .) never carries secrets, local
databases or virtualenvs, and keeps everything the app reads at runtime or deploy.

The matcher follows .dockerignore's rules: patterns are matched against the path from
the context root (and a pattern matching a directory excludes everything under it);
`**` matches any number of directories; a later `!pattern` re-includes; last match wins.
"""
import fnmatch
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _patterns() -> list[tuple[bool, str]]:
    out = []
    for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        negate = line.startswith("!")
        out.append((negate, line[1:] if negate else line))
    return out


def _match(pattern: str, path: str) -> bool:
    pattern = pattern.rstrip("/")
    regex = re.escape(pattern).replace(r"\*\*/", "(?:.*/)?").replace(r"\*\*", ".*")
    regex = regex.replace(r"\*", "[^/]*").replace(r"\?", "[^/]")
    parts = path.split("/")
    # A pattern that matches a parent directory excludes the whole subtree.
    return any(re.fullmatch(regex, "/".join(parts[:i])) for i in range(1, len(parts) + 1)) \
        or fnmatch.fnmatch(path, pattern)


def excluded(path: str) -> bool:
    result = False
    for negate, pattern in _patterns():
        if _match(pattern, path):
            result = not negate
    return result


@pytest.mark.parametrize("path", [
    ".env", ".env.local", ".env.production", "gridnova_dev.db", "gridnova_dev.db.bak-1788503628",
    "gridnova_dev.db-journal", "x.sqlite", ".venv/bin/python", ".venv_qa/lib/x.py", ".venv-win/x",
    ".git/HEAD", ".claude/settings.json", ".ruff_cache/x", "uvicorn.log", "app/__pycache__/main.cpython-313.pyc",
    "tests/conftest.py", ".pytest_cache/v/x",
])
def test_never_in_the_image(path):
    assert excluded(path), path


@pytest.mark.parametrize("path", [
    "app/main.py", "app/agents/nemo/output_policy.md", "alembic/alembic.ini", "alembic/versions/0045_be1_security.py",
    "config/sales_policy.yaml", "data/territories/states.csv", "data/pincodes/README.md",
    "scripts/bootstrap_db.py", "requirements.txt",
])
def test_everything_the_app_reads_is_kept(path):
    assert not excluded(path), path


def test_the_matcher_follows_negation():
    assert _match(".env.*", ".env.example")


def test_env_example_is_committable_and_complete():
    """BE-1 A6: .gitignore's `.env.*` must not hide .env.example, and every Settings
    field is documented in it."""
    import re
    import subprocess

    from app.config import Settings
    if (ROOT / ".git").exists():  # a plain copy of the tree (no repository) cannot answer this part
        r = subprocess.run(["git", "check-ignore", "-q", "--no-index", ".env.example"], cwd=ROOT)
        assert r.returncode == 1, ".env.example is git-ignored"
    documented = set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]+)=", (ROOT / ".env.example").read_text(), re.M))
    assert {f.upper() for f in Settings.model_fields} <= documented
