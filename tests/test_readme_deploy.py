"""D2 (BE-3): the README has ONE deployment section, it lists every setting, and the
wrong "start the app so create_all() runs, then stamp" procedure is gone for good."""
import re
from pathlib import Path

README = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")


def test_one_deployment_section():
    assert len(re.findall(r"^## Deployment", README, re.M)) == 1
    assert "deploy/RUNBOOK.md" in README


def test_the_create_all_on_boot_procedure_is_gone():
    assert "Start the app once" not in README
    assert "let Cloud Run's first boot do" not in README


def test_every_setting_is_in_the_environment_table():
    from app.config import Settings
    table = README[README.index("### Environment variables"):]
    missing = [f.upper() for f in Settings.model_fields if not re.search(rf"\b{f.upper()}\b", table)]
    assert missing == []


def test_the_runbook_exists_with_its_sections():
    runbook = (Path(__file__).resolve().parents[1] / "deploy" / "RUNBOOK.md").read_text(encoding="utf-8")
    for heading in ("APIs", "Artifact Registry", "Cloud SQL", "service account", "Secret Manager",
                    "migration job", "Scheduler", "Clerk", "domain", "Smoke test", "Rollback", "Cost"):
        assert heading.lower() in runbook.lower(), heading
