import json
import logging
from typing import List
from urllib.parse import urlparse
from pydantic import Field
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

    # Agent harness (app/agents/harness.py). A run slower than this is logged as
    # slow; it is a warning, not a cut-off — see the harness docstring for why it
    # does not pretend to enforce a wall-clock timeout it cannot enforce.
    agent_slow_run_ms: float = 8_000.0
    # Where time is actually bounded: the model client's own request timeout and
    # transport-level retries.
    llm_request_timeout_seconds: float = 30.0
    llm_max_retries: int = 2

    # Agent governance (app/governance/). DISABLED_AGENTS is the kill switch: name
    # any agent id here and it stops acting, through configuration, without a deploy.
    # e.g. DISABLED_AGENTS=["sales_agent"]
    disabled_agents: List[str] = []
    # Runaway protection: most agent replies one lead can receive in 24h before the
    # agent stands down and the conversation is handed to a person. 0 disables it.
    agent_daily_action_limit_per_lead: int = 60

    # NeMo Guardrails — semantic output rail that blocks internal-information
    # disclosure the deterministic checks cannot pattern-match. Off by default: it
    # is an extra LLM call on every customer reply. See app/agents/nemo_guard.py.
    nemo_guardrails_enabled: bool = False
    nemo_guardrails_timeout_seconds: float = 6.0
    # When the rail errors or times out: false sends the reply (the deterministic
    # guardrails have already run), true withholds it. Fail-open is the default
    # because an outage would otherwise silence every conversation.
    nemo_guardrails_fail_closed: bool = False

    # Customer memory (episodic recall + self-reflection)
    # Keyword retrieval is the default and needs nothing extra. Semantic search
    # additionally embeds every memory, so it costs an API call per memory and is
    # off until someone turns it on deliberately.
    memory_semantic_search: bool = False
    memory_embedding_model: str = "text-embedding-3-small"
    memory_recall_top_k: int = 12
    # Reflection is milestone-driven, but a long conversation would otherwise never
    # reflect until it qualified; this forces one every N customer messages.
    memory_reflect_every_n_messages: int = 12

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

    # Client IP for rate limiting (app/security/client_ip.py). How many proxies in front
    # of the app append to X-Forwarded-For. 0 = trust no header and use the socket peer.
    # Cloud Run's own front end appends one entry: 1. An external HTTPS load balancer in
    # front of Cloud Run adds another: 2.
    trusted_proxy_hops: int = 0

    # Server-to-server key (app/security/internal_key.py), sent as X-Internal-Key by the
    # website's lead mirror and by Cloud Scheduler. Blank = feature off, header ignored.
    internal_api_key: str = ""

    # Where browser-facing redirects land (e.g. after Google Calendar consent).
    frontend_base_url: str = "http://localhost:3000"
    # This API's own public origin, for links that must point at it from outside
    # (the customer email's PDF download). Blank = such links are left out.
    public_api_base_url: str = ""

    # Where Clerk sends an invited associate after they accept their invitation (the
    # associate portal's sign-up page). Blank = Clerk's own default for the instance.
    associate_invite_redirect_url: str = ""

    # Lifetime of presigned storage GET links (app/storage/r2.py). R2 caps it at 7 days.
    storage_signed_url_ttl_seconds: int = Field(default=600, ge=60, le=604_800)

    # Email notifications (app/notifications/notifier.py, via Resend's REST API).
    # Blank RESEND_API_KEY = no email; log/Slack notifications are unaffected.
    resend_api_key: str = ""
    # Sender, e.g. "Grid Nova <notifications@gridnova.in>" — the domain must be verified in Resend.
    notify_from_email: str = ""
    # Comma-separated staff recipients for internal alerts. Blank = no staff emails.
    staff_notify_emails: str = ""
    # Role-specific recipients for payment (Accounts) and execution (Operations) alerts.
    # Blank = fall back to STAFF_NOTIFY_EMAILS. Placeholder-registry entry P7.
    accounts_notify_emails: str = ""
    operations_notify_emails: str = ""
    # Email associates about their own customers (proposal ready, stage rejected,
    # payment verified). Placeholder-registry entry A1 — not yet confirmed by Grid Nova.
    notify_associates: bool = True

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def staff_notify_email_list(self) -> List[str]:
        return [e.strip() for e in self.staff_notify_emails.split(",") if e.strip()]

    @property
    def accounts_notify_email_list(self) -> List[str]:
        own = [e.strip() for e in self.accounts_notify_emails.split(",") if e.strip()]
        return own or self.staff_notify_email_list

    @property
    def operations_notify_email_list(self) -> List[str]:
        own = [e.strip() for e in self.operations_notify_emails.split(",") if e.strip()]
        return own or self.staff_notify_email_list

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


_LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}


def _is_local_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in _LOCAL_HOSTS or host.endswith(".localhost")


def production_config_problems(s: Settings) -> tuple[list[str], list[str]]:
    """(errors, warnings) for a production boot. Empty unless ENVIRONMENT=production.

    Errors are settings the app cannot run safely without; warnings are features
    that will silently degrade (mock LLM, no PDFs uploaded, no calendar)."""
    if not s.is_production:
        return [], []
    errors: list[str] = []
    warnings: list[str] = []

    if s.database_url.strip().lower().startswith("sqlite"):
        errors.append("DATABASE_URL is SQLite — production needs PostgreSQL")
    if not s.clerk_jwt_issuer.strip():
        errors.append("CLERK_JWT_ISSUER is empty — no employee could authenticate")
    if not s.fernet_encryption_key.strip():
        errors.append("FERNET_ENCRYPTION_KEY is empty — Google refresh tokens cannot be stored encrypted")
    local_origins = [o for o in s.allowed_origins if _is_local_url(o)]
    if local_origins:
        errors.append(f"ALLOWED_ORIGINS contains a localhost origin: {', '.join(local_origins)}")

    empty_storage = [
        name for name, value in (
            ("STORAGE_BUCKET", s.storage_bucket),
            ("STORAGE_ENDPOINT_URL", s.storage_endpoint_url),
            ("STORAGE_ACCESS_KEY", s.storage_access_key),
            ("STORAGE_SECRET_KEY", s.storage_secret_key),
        ) if not value.strip()
    ]
    if empty_storage:
        warnings.append(f"{', '.join(empty_storage)} empty — proposal PDFs and document uploads will not be stored")
    if s.llm_mock_mode:
        warnings.append("LLM_API_KEY is empty — agents run in MOCK mode")
    if not s.google_calendar_enabled:
        warnings.append(
            "GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET / GOOGLE_OAUTH_REDIRECT_URI "
            "not all set — meeting scheduling is off"
        )
    if _is_local_url(s.frontend_base_url):
        warnings.append(f"FRONTEND_BASE_URL is {s.frontend_base_url} — browser redirects will point at localhost")
    if not s.public_api_base_url.strip():
        warnings.append("PUBLIC_API_BASE_URL is empty — associate ID cards cannot be generated "
                        "(their QR code links to this API's public verification endpoint)")
    from app.associates import texts
    if texts.consent_is_placeholder():
        warnings.append("config/associate_documents.yaml: the current KYC consent notice is a [PLACEHOLDER] "
                        "— associate registrations are refused until Grid Nova's text is added")
    if texts.welcome_letter_is_placeholder():
        warnings.append("config/associate_documents.yaml: welcome_letter_body is a [PLACEHOLDER] "
                        "— welcome letters are issued with placeholder text")
    from app.onboarding import config as onboarding_config
    if onboarding_config.notice_is_placeholder():
        warnings.append("config/associate_onboarding.yaml: the Customer Information Notice is a [PLACEHOLDER] "
                        "— associates record customers' acknowledgement of placeholder text")
    if onboarding_config.agreement_is_placeholder():
        warnings.append("config/associate_onboarding.yaml: agreement.body is a [PLACEHOLDER] "
                        "— customer agreements are generated with placeholder text")
    try:
        from app.memory.db import SessionLocal
        from app.placeholders import registry
        with SessionLocal() as db:
            blockers = registry.unresolved_launch_blockers(db)
        if blockers:
            warnings.append(f"{blockers} launch-blocking placeholder(s) unresolved — "
                            "see GET /api/v1/dashboard/placeholders or scripts/list_placeholders.py")
    except Exception as exc:  # the count is advisory: never let it stop a boot
        warnings.append(f"could not count unresolved placeholders: {type(exc).__name__}: {exc}")
    return errors, warnings


def check_production_config(s: Settings) -> None:
    """Raise if a production boot is missing something it cannot run without; log a
    WARNING for each feature that will be degraded. No-op outside production."""
    errors, warnings = production_config_problems(s)
    log = logging.getLogger("app.config")
    for message in warnings:
        log.warning("PRODUCTION CONFIG WARNING: %s", message)
    if errors:
        raise RuntimeError(
            "Refusing to start with ENVIRONMENT=production: " + "; ".join(errors)
        )


settings = Settings()
