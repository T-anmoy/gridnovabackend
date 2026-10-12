"""T4 (BE-3): the contract every scheduled job (POST /api/v1/dashboard/internal/check-*)
keeps with Cloud Scheduler. The jobs are DISCOVERED from the app's routes, so a job
added later (BE-4, BE-5, …) is held to the same contract without editing this file."""
from __future__ import annotations

import pytest

from app.config import settings
from app.main import app as fastapi_app
from tests.conftest import seed_employees

KEY = "test-internal-key-0123456789abcdef"


def internal_jobs() -> list[str]:
    """From the app's own route table (its OpenAPI schema covers every included router)."""
    return sorted(path for path, ops in fastapi_app.openapi()["paths"].items()
                  if "/internal/check-" in path and "post" in ops)


JOBS = internal_jobs()


def test_the_known_jobs_are_discovered():
    assert {"/api/v1/dashboard/internal/check-stale-leads", "/api/v1/dashboard/internal/check-payment-alerts",
            "/api/v1/dashboard/internal/check-execution-alerts",
            "/api/v1/dashboard/internal/check-onboarding-escalations",
            "/api/v1/dashboard/internal/check-survey-reviews"} <= set(JOBS)


def test_every_job_is_in_the_scheduler_config():
    """deploy/scheduler.yaml schedules every job the app exposes."""
    from pathlib import Path
    import yaml
    config = yaml.safe_load((Path(__file__).resolve().parents[1] / "deploy" / "scheduler.yaml").read_text())
    scheduled = {job["path"] for job in config["jobs"]}
    assert scheduled == set(JOBS)


@pytest.fixture
def internal_key(monkeypatch):
    monkeypatch.setattr(settings, "internal_api_key", KEY)


@pytest.fixture
def sent(monkeypatch):
    """Every notification and email a run sends."""
    import app.notifications.notifier as notifier_module
    from app.notifications.notifier import LogNotifier

    out = []

    class Capture(LogNotifier):
        def notify(self, event_type, payload):
            out.append(event_type)

        def send_associate_email(self, **kwargs):
            out.append(("associate_email", kwargs.get("event_type")))

    monkeypatch.setattr(notifier_module, "notifier", Capture())
    return out


@pytest.mark.parametrize("path", JOBS)
class TestInternalJobContract:
    def test_no_credentials_is_401(self, client, internal_key, path):
        assert client.post(path).status_code == 401

    def test_a_wrong_key_is_401(self, client, internal_key, path):
        assert client.post(path, headers={"X-Internal-Key": KEY + "x"}).status_code == 401
        assert client.post(path, headers={"X-Internal-Key": ""}).status_code == 401

    def test_a_key_is_ignored_when_none_is_configured(self, client, path, monkeypatch):
        monkeypatch.setattr(settings, "internal_api_key", "")
        assert client.post(path, headers={"X-Internal-Key": ""}).status_code == 401

    def test_the_right_key_is_200(self, client, internal_key, path, sent):
        r = client.post(path, headers={"X-Internal-Key": KEY})
        assert r.status_code == 200, r.text
        assert isinstance(r.json(), dict)

    @pytest.mark.parametrize("role", ["management", "sales", "engineer", "operations", "accounts"])
    def test_non_admin_staff_are_403(self, client, db_session, internal_key, path, role):
        r = client.post(path, headers={"X-Employee-Id": f"emp-{role}-1", "X-Employee-Role": role})
        assert r.status_code == 403

    def test_admin_may_run_it(self, client, db_session, path, sent):
        seed_employees(db_session, "emp-admin-1")
        assert client.post(path, headers={"X-Employee-Id": "emp-admin-1",
                                          "X-Employee-Role": "admin"}).status_code == 200

    def test_a_second_run_sends_nothing(self, client, memory, db_session, internal_key, path, sent):
        # Something for the stale-lead job to find; each job's own suite seeds its own data
        # (tests/test_stale_leads.py, test_payments.py, test_execution_stages.py, …).
        from tests.test_stale_leads import _seed_lead
        _seed_lead(memory, db_session, email="contract-quiet@example.com", last_msg_age_hours=50)
        assert client.post(path, headers={"X-Internal-Key": KEY}).status_code == 200
        first = len(sent)
        assert client.post(path, headers={"X-Internal-Key": KEY}).status_code == 200
        assert len(sent) == first, sent[first:]
