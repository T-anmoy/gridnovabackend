"""Startup guard for ENVIRONMENT=production and staging (app/config.py check_production_config),
and the ENVIRONMENT value check itself (BE-1 A7)."""
import logging

import pytest
from fastapi.testclient import TestClient

from app.config import Settings, check_production_config, production_config_problems, settings
from app.main import app as fastapi_app
from app.memory.db import Base

GOOD = dict(
    environment="production",
    database_url="postgresql://u:p@10.0.0.5:5432/gridnova",
    clerk_jwt_issuer="https://clerk.gridnova.in",
    fernet_encryption_key="x" * 44,
    allowed_origins=["https://www.gridnova.in"],
    storage_bucket="b", storage_endpoint_url="https://r2.example", storage_access_key="a",
    storage_secret_key="s",
    llm_api_key="sk-test",
    google_oauth_client_id="id", google_oauth_client_secret="secret",
    google_oauth_redirect_uri="https://api.gridnova.in/api/v1/dashboard/employees/me/calendar/callback",
    frontend_base_url="https://www.gridnova.in",
    public_api_base_url="https://api.gridnova.in",
    clerk_secret_key="sk_test_x",
    identity_hmac_key="y" * 44,
    trusted_proxy_hops=1,
    internal_api_key="z" * 43,
    resend_api_key="re_test", notify_from_email="Grid Nova <notifications@gridnova.in>",
    sentry_dsn="https://key@o0.ingest.sentry.io/0",
)


@pytest.fixture
def real_associate_texts(monkeypatch):
    """config/associate_documents.yaml still holds placeholders (correctly warned
    about); tests of an otherwise complete config stand in real texts."""
    from app.associates import texts
    monkeypatch.setattr(texts, "consent_is_placeholder", lambda: False)
    monkeypatch.setattr(texts, "welcome_letter_is_placeholder", lambda: False)
    from app.onboarding import config as onboarding_config
    monkeypatch.setattr(onboarding_config, "notice_is_placeholder", lambda: False)
    monkeypatch.setattr(onboarding_config, "agreement_is_placeholder", lambda: False)
    from app.placeholders import registry
    monkeypatch.setattr(registry, "unresolved_launch_blockers", lambda db=None: 0)


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **{**GOOD, **overrides})


class TestErrors:
    def test_complete_production_config_passes(self, real_associate_texts):
        assert production_config_problems(_settings()) == ([], [])
        check_production_config(_settings())

    @pytest.mark.parametrize("overrides, variable", [
        ({"database_url": "sqlite:///./gridnova_dev.db"}, "DATABASE_URL"),
        ({"clerk_jwt_issuer": ""}, "CLERK_JWT_ISSUER"),
        ({"fernet_encryption_key": ""}, "FERNET_ENCRYPTION_KEY"),
        ({"allowed_origins": ["https://www.gridnova.in", "http://localhost:3000"]}, "ALLOWED_ORIGINS"),
        ({"allowed_origins": ["http://127.0.0.1:3000"]}, "ALLOWED_ORIGINS"),
        ({"llm_api_key": ""}, "LLM_API_KEY"),
        # BE-3 D7
        ({"clerk_secret_key": ""}, "CLERK_SECRET_KEY"),
        ({"identity_hmac_key": ""}, "IDENTITY_HMAC_KEY"),
        ({"trusted_proxy_hops": 0}, "TRUSTED_PROXY_HOPS"),
        ({"internal_api_key": ""}, "INTERNAL_API_KEY"),
        ({"frontend_base_url": "http://localhost:3000"}, "FRONTEND_BASE_URL"),
        ({"public_api_base_url": ""}, "PUBLIC_API_BASE_URL"),
        ({"database_url": "mysql://u:p@db/gridnova"}, "DATABASE_URL"),
        ({"database_url": "postgresql://u:p@10.0.0.5:5432/"}, "DATABASE_URL"),
        ({"database_url": "postgresql://u:p@10.0.0.5/gridnova?sslmode=disable&gssencmode=disable"},
         "DATABASE_URL"),
        ({"allowed_origins": ["https://www.gridnova.in/"]}, "ALLOWED_ORIGINS"),
        ({"allowed_origins": ["https://www.gridnova.in/app"]}, "ALLOWED_ORIGINS"),
        ({"allowed_origins": ["www.gridnova.in"]}, "ALLOWED_ORIGINS"),
    ])
    def test_each_required_setting_fails_naming_the_variable(self, overrides, variable):
        with pytest.raises(RuntimeError, match=variable):
            check_production_config(_settings(**overrides))

    @pytest.mark.parametrize("url", [
        "postgresql://gridnova:pw@/gridnova?host=/cloudsql/proj:asia-south1:gridnova-staging",
        "postgresql+psycopg2://gridnova:pw@/gridnova?host=/cloudsql/proj:asia-south1:gridnova-prod",
        "postgresql://gridnova:pw@10.20.0.3:5432/gridnova?sslmode=require",
    ])
    def test_cloud_sql_url_shapes_are_accepted(self, url, real_associate_texts):
        errors, _ = production_config_problems(_settings(database_url=url))
        assert errors == []

    def test_all_problems_are_reported_together(self):
        with pytest.raises(RuntimeError) as exc:
            check_production_config(_settings(database_url="sqlite://", clerk_jwt_issuer=""))
        assert "DATABASE_URL" in str(exc.value) and "CLERK_JWT_ISSUER" in str(exc.value)


class TestWarnings:
    @pytest.mark.parametrize("overrides, variable", [
        ({"storage_bucket": ""}, "STORAGE_BUCKET"),
        ({"storage_secret_key": ""}, "STORAGE_SECRET_KEY"),
        ({"google_oauth_client_id": ""}, "GOOGLE_OAUTH_CLIENT_ID"),
        ({"resend_api_key": ""}, "RESEND_API_KEY"),
        ({"notify_from_email": ""}, "NOTIFY_FROM_EMAIL"),
        ({"sentry_dsn": ""}, "SENTRY_DSN"),
    ])
    def test_degraded_features_warn_but_do_not_fail(self, overrides, variable, caplog):
        with caplog.at_level(logging.WARNING, logger="app.config"):
            check_production_config(_settings(**overrides))
        assert any(variable in r.getMessage() for r in caplog.records)


    def test_placeholder_associate_texts_warn(self):
        """The KYC notice, welcome letter and (BE-19) customer notice are approved copy now;
        the agreement is still a placeholder."""
        _, warnings = production_config_problems(_settings())
        assert not any("KYC consent notice is a [PLACEHOLDER]" in w for w in warnings)
        assert not any("welcome_letter_body is a [PLACEHOLDER]" in w for w in warnings)
        assert not any("Customer Information Notice is a [PLACEHOLDER]" in w for w in warnings)
        assert any("agreement.body is a [PLACEHOLDER]" in w for w in warnings)

    def test_unresolved_launch_blocking_placeholders_warn_once(self, caplog):
        """One WARNING with the count — never a refusal to boot."""
        from app.placeholders import registry
        _, warnings = production_config_problems(_settings())
        count = registry.unresolved_launch_blockers()
        assert count > 0
        assert [w for w in warnings if "placeholder(s) unresolved" in w] == [
            f"{count} launch-blocking placeholder(s) unresolved — see GET /api/v1/dashboard/placeholders "
            "or scripts/list_placeholders.py"]
        with caplog.at_level(logging.WARNING, logger="app.config"):
            check_production_config(_settings())
        assert sum("placeholder(s) unresolved" in r.getMessage() for r in caplog.records) == 1


class TestNonProduction:
    @pytest.mark.parametrize("environment", ["development", "test"])
    def test_nothing_is_checked_in_development_or_test(self, environment):
        s = _settings(environment=environment, database_url="sqlite://", clerk_jwt_issuer="",
                      fernet_encryption_key="", allowed_origins=["http://localhost:3000"], llm_api_key="")
        assert production_config_problems(s) == ([], [])
        check_production_config(s)


class TestStaging:
    """Staging runs the production guard (BE-1 A7)."""

    def test_staging_refuses_the_same_missing_settings(self):
        with pytest.raises(RuntimeError, match="ENVIRONMENT=staging.*DATABASE_URL"):
            check_production_config(_settings(environment="staging", database_url="sqlite://"))

    def test_staging_without_an_llm_key_only_warns(self, real_associate_texts, caplog):
        errors, warnings = production_config_problems(_settings(environment="staging", llm_api_key=""))
        assert errors == []
        assert any("LLM_API_KEY" in w for w in warnings)

    def test_staging_without_an_internal_key_only_warns(self, real_associate_texts):
        errors, warnings = production_config_problems(_settings(environment="staging", internal_api_key=""))
        assert errors == [] and any("INTERNAL_API_KEY" in w for w in warnings)

    def test_production_without_an_llm_key_refuses(self, real_associate_texts):
        errors, _ = production_config_problems(_settings(llm_api_key=""))
        assert any("LLM_API_KEY" in e for e in errors)

    def test_staging_boot_runs_the_guard(self, monkeypatch):
        monkeypatch.setattr(settings, "environment", "staging")
        monkeypatch.setattr(settings, "database_url", "sqlite:///./gridnova_dev.db")
        with pytest.raises(RuntimeError, match="ENVIRONMENT=staging"):
            with TestClient(fastapi_app):
                pass


class TestEnvironmentValue:
    @pytest.mark.parametrize("value", ["development", "test", "staging", "production"])
    def test_the_four_known_values_are_accepted(self, value):
        assert Settings(_env_file=None, environment=value).environment == value

    @pytest.mark.parametrize("value", ["prod", "Production", "PRODUCTION", "dev", "Staging", "production ", ""])
    def test_any_other_value_stops_the_boot(self, value):
        from pydantic import ValidationError
        with pytest.raises(ValidationError, match="ENVIRONMENT must be exactly one of"):
            Settings(_env_file=None, environment=value)

    def test_boot_fails_on_an_unknown_value(self, monkeypatch):
        """A process started with ENVIRONMENT=prod never gets as far as serving."""
        import subprocess
        import sys
        env = {"ENVIRONMENT": "prod", "PATH": "/usr/bin:/bin", "DATABASE_URL": "sqlite:///:memory:"}
        result = subprocess.run([sys.executable, "-c", "import app.config"], env=env, capture_output=True,
                                text=True, cwd=str(__import__("pathlib").Path(__file__).resolve().parents[1]))
        assert result.returncode != 0
        assert "ENVIRONMENT must be exactly one of" in result.stderr

    @pytest.mark.parametrize("value, production_like, dev_auth", [
        ("development", False, True), ("test", False, True), ("staging", True, False), ("production", True, False)])
    def test_flags(self, value, production_like, dev_auth):
        s = Settings(_env_file=None, environment=value)
        assert s.is_production_like is production_like
        assert s.allows_dev_auth is dev_auth
        assert s.is_production is (value == "production")


class TestStartup:
    def test_production_boot_with_sqlite_refuses_to_start(self, monkeypatch):
        monkeypatch.setattr(settings, "environment", "production")
        monkeypatch.setattr(settings, "database_url", "sqlite:///./gridnova_dev.db")
        with pytest.raises(RuntimeError, match="DATABASE_URL"):
            with TestClient(fastapi_app):
                pass

    def test_production_boot_leaves_the_schema_to_the_bootstrap(self, monkeypatch):
        for field, value in GOOD.items():
            monkeypatch.setattr(settings, field, value)
        calls = []
        monkeypatch.setattr(Base.metadata, "create_all", lambda *a, **k: calls.append(1))
        with TestClient(fastapi_app):
            pass
        assert calls == []

    def test_development_boot_still_creates_tables(self, monkeypatch):
        calls = []
        monkeypatch.setattr(Base.metadata, "create_all", lambda *a, **k: calls.append(1))
        with TestClient(fastapi_app):
            pass
        assert calls == [1]
