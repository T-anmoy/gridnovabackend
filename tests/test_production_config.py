"""Startup guard for ENVIRONMENT=production (app/config.py check_production_config)."""
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
)


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **{**GOOD, **overrides})


class TestErrors:
    def test_complete_production_config_passes(self):
        assert production_config_problems(_settings()) == ([], [])
        check_production_config(_settings())

    @pytest.mark.parametrize("overrides, variable", [
        ({"database_url": "sqlite:///./gridnova_dev.db"}, "DATABASE_URL"),
        ({"clerk_jwt_issuer": ""}, "CLERK_JWT_ISSUER"),
        ({"fernet_encryption_key": ""}, "FERNET_ENCRYPTION_KEY"),
        ({"allowed_origins": ["https://www.gridnova.in", "http://localhost:3000"]}, "ALLOWED_ORIGINS"),
        ({"allowed_origins": ["http://127.0.0.1:3000"]}, "ALLOWED_ORIGINS"),
    ])
    def test_each_required_setting_fails_naming_the_variable(self, overrides, variable):
        with pytest.raises(RuntimeError, match=variable):
            check_production_config(_settings(**overrides))

    def test_all_problems_are_reported_together(self):
        with pytest.raises(RuntimeError) as exc:
            check_production_config(_settings(database_url="sqlite://", clerk_jwt_issuer=""))
        assert "DATABASE_URL" in str(exc.value) and "CLERK_JWT_ISSUER" in str(exc.value)


class TestWarnings:
    @pytest.mark.parametrize("overrides, variable", [
        ({"storage_bucket": ""}, "STORAGE_BUCKET"),
        ({"storage_secret_key": ""}, "STORAGE_SECRET_KEY"),
        ({"llm_api_key": ""}, "LLM_API_KEY"),
        ({"google_oauth_client_id": ""}, "GOOGLE_OAUTH_CLIENT_ID"),
        ({"frontend_base_url": "http://localhost:3000"}, "FRONTEND_BASE_URL"),
    ])
    def test_degraded_features_warn_but_do_not_fail(self, overrides, variable, caplog):
        with caplog.at_level(logging.WARNING, logger="app.config"):
            check_production_config(_settings(**overrides))
        assert any(variable in r.getMessage() for r in caplog.records)


class TestNonProduction:
    @pytest.mark.parametrize("environment", ["development", "test", "staging"])
    def test_nothing_is_checked_outside_production(self, environment):
        s = _settings(environment=environment, database_url="sqlite://", clerk_jwt_issuer="",
                      fernet_encryption_key="", allowed_origins=["http://localhost:3000"])
        assert production_config_problems(s) == ([], [])
        check_production_config(s)


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
