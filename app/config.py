import json
import logging
from typing import List
from urllib.parse import urlparse
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# The only accepted ENVIRONMENT values, exactly as written (case matters: "Production"
# or "prod" stops the boot instead of quietly running with development behaviour).
ENVIRONMENTS = ("development", "test", "staging", "production")
# Where the X-Employee-* / X-Associate-Id header bypass is accepted. Nowhere else.
DEV_AUTH_ENVIRONMENTS = ("development", "test")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    environment: str = "development"
    database_url: str = "sqlite:///./gridnova_dev.db"
    # PostgreSQL connection pool, per instance (app/memory/db.py). Infrastructure sizing,
    # not business values: size them against Cloud SQL max_connections ÷ max instances.
    db_pool_size: int = Field(default=10, ge=1)
    db_max_overflow: int = Field(default=5, ge=0)
    db_pool_timeout_seconds: float = Field(default=10.0, gt=0)
    db_pool_recycle_seconds: int = Field(default=1800, ge=60)

    # Clerk
    clerk_secret_key: str = ""
    clerk_jwt_issuer: str = ""
    # BE-21: offboarding in Clerk. False (Clerk's free plan, where ban is not available):
    # a deactivated employee's or suspended associate's sessions are revoked. True (Pro):
    # the Clerk user is banned and, on reactivation, unbanned. Either way the database
    # refuses them on their next request.
    clerk_ban_enabled: bool = False
    # BE-21: comma-separated addresses that may become the first Admins through the
    # bootstrap path ({"role": "admin"} set by hand on a Clerk user's public metadata) —
    # only with that address verified in Clerk. Blank = bootstrap off.
    bootstrap_admin_emails: str = ""

    # Storage (Cloudflare R2 / S3-compatible)
    storage_bucket: str = ""
    storage_endpoint_url: str = ""
    storage_access_key: str = ""
    storage_secret_key: str = ""

    # LLM
    llm_api_key: str = ""
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-4o-mini"
    # BE-18: the Proposal Studio's optional "suggest from notes" assistant (customer, site and
    # connection fields only — never a price or anything printed as a figure). Off by default.
    studio_ai_fill_enabled: bool = False

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
    # Rail checks that may run at once (one per concurrent customer reply). A single
    # worker serialised every check and one hung call starved the rest.
    nemo_guardrails_max_workers: int = Field(default=4, ge=1, le=32)

    # Background work after the response (app/jobs/runner.py): threads per instance.
    background_workers: int = Field(default=4, ge=1, le=32)

    # Sweepers in the scheduled jobs (app/jobs/sweeper.py): background work older than
    # this many minutes and still unfinished is retried, at most this many per run.
    sweeper_grace_minutes: int = Field(default=15, ge=1)
    sweeper_batch_size: int = Field(default=10, ge=1)
    # check-stale-leads: most quiet-lead reflections (model calls) one run makes inline,
    # so a backlog cannot push the request past its timeout.
    stale_lead_reflections_per_run: int = Field(default=10, ge=0)

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

    # Encryption at rest (Fernet) — employee Google refresh tokens, associate bank
    # account numbers. Comma-separated, newest first: the first key encrypts, all decrypt
    # (rotation: app/security/encryption.py).
    fernet_encryption_key: str = ""
    # Signs associate identity claims (the duplicate-registration guard,
    # app/associates/service.py). Its own secret, so a Fernet rotation never changes it.
    # Required in staging and production. Never rotate it without
    # scripts/rehash_identity_claims.py.
    identity_hmac_key: str = ""

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

    # GET /api/v1/health/ready reuses its answer for this many seconds (0 = never), so
    # probes and uptime checks polling it never add database load.
    readiness_cache_seconds: float = Field(default=15.0, ge=0)

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
    # Where a staff invitation lands after acceptance (BE-17). Blank = Clerk's own sign-up page.
    staff_invite_redirect_url: str = ""

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

    @field_validator("environment")
    @classmethod
    def _known_environment(cls, value: str) -> str:
        if value not in ENVIRONMENTS:
            raise ValueError(f"ENVIRONMENT must be exactly one of {', '.join(ENVIRONMENTS)} (got {value!r})")
        return value

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def is_production_like(self) -> bool:
        """Staging or production: production auth and the production config guard."""
        return self.environment in ("staging", "production")

    @property
    def allows_dev_auth(self) -> bool:
        """The dev header bypass — only when ENVIRONMENT is exactly development or test."""
        return self.environment in DEV_AUTH_ENVIRONMENTS

    @property
    def allows_mock_replies(self) -> bool:
        """The literal "[MOCK] …" model output may reach a customer only in development
        and test; anywhere else a model-less deployment answers with the
        deterministic fallback replies."""
        return self.environment in DEV_AUTH_ENVIRONMENTS

    @property
    def bootstrap_admin_email_set(self) -> frozenset:
        return frozenset(e.strip().lower() for e in self.bootstrap_admin_emails.split(",") if e.strip())

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


def _is_bare_origin(origin: str) -> bool:
    """scheme://host[:port] exactly — what a browser sends as Origin and Clerk as `azp`.
    A path or a trailing slash never matches either."""
    parsed = urlparse(origin)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc) and parsed.path == "" \
        and not parsed.params and not parsed.query and not parsed.fragment


def _database_url_problem(url: str) -> str | None:
    """Why DATABASE_URL is not a usable production PostgreSQL URL, or None."""
    from sqlalchemy.engine import make_url
    try:
        parsed = make_url(url)
    except Exception:
        return "DATABASE_URL is not a valid SQLAlchemy URL"
    if parsed.drivername.startswith("sqlite"):
        return "DATABASE_URL is SQLite — production needs PostgreSQL"
    if not parsed.drivername.startswith("postgresql"):
        return f"DATABASE_URL uses {parsed.drivername!r} — production needs PostgreSQL (postgresql://…)"
    if not parsed.database:
        return "DATABASE_URL names no database"
    socket_host = str(parsed.query.get("host", ""))
    if not parsed.host and not socket_host.startswith("/"):
        return "DATABASE_URL has neither a host nor a Cloud SQL socket (?host=/cloudsql/<connection name>)"
    if parsed.query.get("sslmode") == "disable" and not socket_host.startswith("/"):
        return ("DATABASE_URL disables TLS over TCP (sslmode=disable) — use the Cloud SQL socket "
                "(?host=/cloudsql/<connection name>) or TLS; that flag is a local-laptop workaround only")
    return None


def production_config_problems(s: Settings) -> tuple[list[str], list[str]]:
    """(errors, warnings) for a production or staging boot. Empty otherwise.

    Errors are settings the app cannot run safely without; warnings are features
    that will silently degrade (no PDFs uploaded, no calendar). An empty LLM_API_KEY
    is an error in production (customers would get canned replies) and a warning in
    staging."""
    if not s.is_production_like:
        return [], []
    errors: list[str] = []
    warnings: list[str] = []

    database_problem = _database_url_problem(s.database_url.strip())
    if database_problem:
        errors.append(database_problem)
    if not s.clerk_jwt_issuer.strip():
        errors.append("CLERK_JWT_ISSUER is empty — no employee could authenticate")
    if not s.clerk_secret_key.strip():
        errors.append("CLERK_SECRET_KEY is empty — associate invitations would be silently skipped "
                      "and staff roles could not be synced to Clerk")
    if not s.fernet_encryption_key.strip():
        errors.append("FERNET_ENCRYPTION_KEY is empty — Google refresh tokens cannot be stored encrypted")
    if not s.identity_hmac_key.strip():
        errors.append("IDENTITY_HMAC_KEY is empty — associate duplicate-registration checks need their own key")
    local_origins = [o for o in s.allowed_origins if _is_local_url(o)]
    if local_origins:
        errors.append(f"ALLOWED_ORIGINS contains a localhost origin: {', '.join(local_origins)}")
    malformed = [o for o in s.allowed_origins if not _is_bare_origin(o)]
    if malformed:
        errors.append("ALLOWED_ORIGINS entries must be scheme://host with no path or trailing slash "
                      f"(they are compared with the browser's Origin and Clerk's azp exactly): {', '.join(malformed)}")
    if s.trusted_proxy_hops <= 0:
        errors.append("TRUSTED_PROXY_HOPS is 0 — behind Cloud Run every visitor would share one rate-limit "
                      "bucket (1 directly behind Cloud Run, 2 behind an external HTTPS load balancer)")
    if not s.internal_api_key.strip():
        message = "INTERNAL_API_KEY is empty — Cloud Scheduler's internal jobs cannot authenticate"
        (errors if s.is_production else warnings).append(message)
    if _is_local_url(s.frontend_base_url) or not s.frontend_base_url.strip():
        errors.append(f"FRONTEND_BASE_URL is {s.frontend_base_url or 'empty'} — browser redirects and "
                      "associate email links would point at it")
    if not s.public_api_base_url.strip() or _is_local_url(s.public_api_base_url):
        errors.append("PUBLIC_API_BASE_URL is empty or local — customer PDF/receipt links and associate ID "
                      "card QR codes point at this API's public origin")

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
        if s.is_production:
            errors.append("LLM_API_KEY is empty — agents would run in MOCK mode")
        else:
            warnings.append("LLM_API_KEY is empty — agents run in MOCK mode (customers get the canned fallback replies)")
    if not s.google_calendar_enabled:
        warnings.append(
            "GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET / GOOGLE_OAUTH_REDIRECT_URI "
            "not all set — meeting scheduling is off"
        )
    if not s.resend_api_key.strip() or not s.notify_from_email.strip():
        warnings.append("RESEND_API_KEY / NOTIFY_FROM_EMAIL not both set — no email is sent (customer "
                        "proposals, receipts, staff and associate alerts)")
    if not s.sentry_dsn.strip():
        warnings.append("SENTRY_DSN is empty — unhandled errors are only in Cloud Logging")
    if not s.bootstrap_admin_email_set:
        warnings.append("BOOTSTRAP_ADMIN_EMAILS is empty — the first-Admin bootstrap path is off "
                        "(scripts/grant_admin.py still works)")
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
    """Raise if a production or staging boot is missing something it cannot run
    without; log a WARNING for each feature that will be degraded. No-op in
    development and test."""
    errors, warnings = production_config_problems(s)
    log = logging.getLogger("app.config")
    if s.is_production_like:
        # Cannot be checked from here: it is a setting of the Clerk instance. Without it
        # every staff and associate request is refused (403 session_token_missing_claims).
        log.info("Clerk: the session token must carry public_metadata, email and name "
                 "(Clerk Dashboard → Sessions → Customize session token; deploy/RUNBOOK.md §D8)")
    for message in warnings:
        log.warning("PRODUCTION CONFIG WARNING: %s", message)
    if errors:
        raise RuntimeError(
            f"Refusing to start with ENVIRONMENT={s.environment}: " + "; ".join(errors)
        )


settings = Settings()
