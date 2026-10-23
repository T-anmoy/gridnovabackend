"""The contract docs in docs/api/ stay usable: openapi.json matches the code, and every
file the index lists exists."""
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DOCS = REPO / "docs" / "api"


def test_openapi_json_is_up_to_date():
    r = subprocess.run([sys.executable, "scripts/export_openapi.py", "--check"], cwd=REPO,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr + "\nRun: python scripts/export_openapi.py"


def test_index_lists_existing_files():
    index = (DOCS / "README.md").read_text(encoding="utf-8")
    linked = set(re.findall(r"\]\(([a-z0-9_]+\.md)\)", index))
    assert {"phase4a.md", "phase4a_followups.md", "phase3c_site_location.md", "pma_pricing.md",
            "hf_c_equipment.md", "site_survey.md"} <= linked
    for name in linked:
        assert (DOCS / name).exists(), name


def test_documented_new_paths_exist_in_openapi():
    import json
    paths = json.loads((DOCS / "openapi.json").read_text(encoding="utf-8"))["paths"]
    for path in ("/api/v1/associate/equipment",
                 "/api/v1/dashboard/onboardings/{onboarding_id}/execution/units/{unit_id}/match",
                 "/api/v1/pricing/packages",
                 "/api/v1/associate/onboardings/{onboarding_id}/site-location",
                 "/api/v1/dashboard/onboardings/{onboarding_id}/site-location",
                 "/api/v1/associate/onboardings/{onboarding_id}/survey/results",
                 "/api/v1/associate/onboardings/{onboarding_id}/survey/signatures/{party}",
                 "/api/v1/dashboard/onboardings/{onboarding_id}/survey/results",
                 "/api/v1/dashboard/associates/{associate_id}/commission-rate",
                 "/api/v1/dashboard/associates/{associate_id}/performance",
                 "/api/v1/associate/commission-rate"):
        assert path in paths, path
