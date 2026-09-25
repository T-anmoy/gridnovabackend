"""Test runs never report to Sentry, whatever .env or the shell sets SENTRY_DSN to."""
import os
import subprocess
import sys
from pathlib import Path

import sentry_sdk

from app.config import settings

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_sentry_is_not_initialised_in_tests():
    assert settings.sentry_dsn == ""
    assert not sentry_sdk.is_initialized()


def test_a_real_looking_dsn_in_the_environment_is_still_ignored(tmp_path):
    """Run a real test in a fresh interpreter with a DSN exported, as a developer
    shell might have it: conftest must still keep Sentry off."""
    probe = tmp_path / "test_probe_sentry.py"
    probe.write_text(
        "import sentry_sdk\n"
        "import app.main  # where sentry_sdk.init() runs when a DSN is set\n"
        "def test_probe():\n"
        "    assert not sentry_sdk.is_initialized()\n"
    )
    env = {**os.environ, "SENTRY_DSN": "https://abc123@o0.ingest.sentry.io/0"}
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "--rootdir", str(REPO_ROOT), "-c", str(REPO_ROOT / "pytest.ini"),
         "--confcutdir", str(REPO_ROOT / "tests"), str(probe),
         "-o", f"testpaths={REPO_ROOT / 'tests'}", "--import-mode=importlib",
         "-p", "tests.conftest"],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "Sentry is attempting to send" not in output
