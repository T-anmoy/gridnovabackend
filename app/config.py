import json
from typing import List
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    environment: str = "development"
    database_url: str = "sqlite:///./gridnova_dev.db"

    # Clerk
    clerk_secret_key: str = ""
    clerk_jwt_issuer: str = ""

    # Storage (Cloudflare R2 / S3-compatible)
    storage_bucket: str = ""
    storage_endpoint_url: str = ""
    storage_access_key: str = ""
    storage_secret_key: str = ""

    # LLM
    llm_api_key: str = ""
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-4o-mini"

    # Business rules
    proposal_value_manager_approval_threshold: float = 500_000.0
    capacity_director_notify_threshold_mw: float = 1.0

    # Analytics — estimated USD cost per AI (agent) reply, used to derive
    # ai_estimated_cost from the number of agent messages. Tune per model/pricing.
    ai_estimated_cost_per_call: float = 0.01

    # Google Calendar (meeting scheduling)
    google_oauth_client_id: str = ""
    google_oauth_client_secret: str = ""
    google_oauth_redirect_uri: str = ""

    # Encryption at rest (Fernet) — for employee Google refresh tokens
    fernet_encryption_key: str = ""

    # Scheduling business hours (IST). Slots are only ever offered inside this window.
    business_hours_start_ist: int = 10  # 10:00
    business_hours_end_ist: int = 18     # 18:00
    business_days: List[int] = [0, 1, 2, 3, 4, 5]  # Mon–Sat (Mon=0)
    business_timezone: str = "Asia/Kolkata"
    scheduling_days_ahead: int = 5
    scheduling_default_duration_minutes: int = 30

    # Notifications — Slack webhook for internal alerts (blank → log-only notifier)
    slack_webhook_url: str = ""

    # Observability
    sentry_dsn: str = ""

    # CORS
    allowed_origins: List[str] = ["http://localhost:3000", "https://www.gridnova.in"]

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def llm_mock_mode(self) -> bool:
        return not bool(self.llm_api_key)

    @property
    def google_calendar_enabled(self) -> bool:
        return all([
            self.google_oauth_client_id,
            self.google_oauth_client_secret,
            self.google_oauth_redirect_uri,
        ])


settings = Settings()
