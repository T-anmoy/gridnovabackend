from contextlib import asynccontextmanager

import sentry_sdk
from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routes.agent_governance import router as agent_governance_router
from app.api.routes.associate_onboarding import router as associate_onboarding_router
from app.api.routes.associate_execution import router as associate_execution_router
from app.api.routes.associate_equipment import router as associate_equipment_router
from app.api.routes.associate_payments import router as associate_payments_router
from app.api.routes.associate_portal import router as associate_portal_router
from app.api.routes.associate_survey import router as associate_survey_router
from app.api.routes.associates_admin import router as associates_admin_router
from app.api.routes.chat import router as chat_router
from app.api.routes.dashboard import router as dashboard_router
from app.api.routes.equipment import router as equipment_router
from app.api.routes.execution_admin import router as execution_admin_router
from app.api.routes.execution_register_admin import router as execution_register_admin_router
from app.api.routes.health import router as health_router
from app.api.routes.onboarding_admin import router as onboarding_admin_router
from app.api.routes.payments_admin import router as payments_admin_router
from app.api.routes.placeholders_admin import router as placeholders_admin_router
from app.api.routes.pricing import router as pricing_router
from app.api.routes.survey_admin import router as survey_admin_router
from app.api.routes.proposal_inputs import router as proposal_inputs_router
from app.api.routes.public_associates import router as public_associates_router
from app.api.routes.public_booking import router as public_booking_router
from app.api.routes.public_intake import router as public_intake_router
from app.api.routes.public_proposals import router as public_proposals_router
from app.api.routes.public_receipts import router as public_receipts_router
from app.config import check_production_config, settings
from app.memory.db import Base, engine
from app.security.upload_limit import UploadSizeLimitMiddleware


@asynccontextmanager
async def lifespan(app: FastAPI):
    # A production boot missing a required setting stops here, naming the variable,
    # instead of serving with SQLite, no auth issuer, or no token encryption.
    check_production_config(settings)
    # Create all tables on startup for local development and tests. In production the
    # schema belongs to scripts/bootstrap_db.py + Alembic alone: a create_all here would
    # (a) build an unversioned schema if the service booted before the bootstrap ran,
    # which the bootstrap then has to refuse, and (b) pre-create a new model's table
    # before its migration runs, so that migration's create_table would fail.
    if not settings.is_production:
        Base.metadata.create_all(bind=engine)
    # Fail loudly at startup if the proposal content config is structurally invalid
    # (overlapping pricing tiers, a rule referencing a missing section, etc.) rather
    # than discovering it mid-request while drafting a proposal.
    from app.proposals.content_engine import load_content_config
    load_content_config(force_reload=True)
    # Same for the associate onboarding config (checklist, thresholds, notice text).
    from app.onboarding.config import load as load_onboarding_config
    load_onboarding_config()
    # And execution/payments (config/execution_payments.yaml).
    from app.execution.config import load as load_execution_config
    load_execution_config()
    # And the on-site survey (config/site_survey.yaml).
    from app.survey.config import load as load_survey_config
    load_survey_config()
    # And Grid Nova's price list + pricing policy (config/price_list.yaml,
    # config/pricing_policy.yaml) — a tampered table must stop the boot.
    from app.pricing.price_list import load as load_price_list
    from app.pricing.policy import load as load_pricing_policy
    load_price_list()
    load_pricing_policy()
    # And the placeholder registry (config/placeholder_registry.yaml).
    from app.placeholders.registry import load as load_placeholder_registry
    load_placeholder_registry()
    yield


if settings.sentry_dsn:
    # No request bodies, and KYC values scrubbed from everything else — see
    # app/security/sentry_scrub.py.
    from app.security.sentry_scrub import sentry_init_options
    sentry_sdk.init(**sentry_init_options(settings.sentry_dsn, settings.environment))

app = FastAPI(
    title="Grid Nova AI Operations Platform",
    description="AI-powered lead qualification and proposal management for Grid Nova Energy",
    version="1.0.0",
    lifespan=lifespan,
)

# Oversized site-assessment uploads are refused while they arrive. Added before CORS
# so CORS stays the outer layer and the 413 still carries CORS headers to the browser.
app.add_middleware(UploadSizeLimitMiddleware)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routes
app.include_router(health_router)
app.include_router(chat_router)
app.include_router(dashboard_router)
app.include_router(public_proposals_router)
app.include_router(proposal_inputs_router)
app.include_router(public_intake_router)
app.include_router(public_booking_router)
app.include_router(agent_governance_router)
app.include_router(equipment_router)
app.include_router(pricing_router)
app.include_router(public_associates_router)
app.include_router(associates_admin_router)
app.include_router(associate_portal_router)
app.include_router(associate_onboarding_router)
app.include_router(onboarding_admin_router)
app.include_router(associate_survey_router)
app.include_router(survey_admin_router)
app.include_router(placeholders_admin_router)
app.include_router(associate_execution_router)
app.include_router(associate_equipment_router)
# Before execution_admin: its POST .../execution/{stage} would otherwise swallow
# .../execution/units and .../execution/dcr-override.
app.include_router(execution_register_admin_router)
app.include_router(execution_admin_router)
app.include_router(associate_payments_router)
app.include_router(payments_admin_router)
app.include_router(public_receipts_router)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    import logging
    logging.getLogger(__name__).error("Unhandled exception: %s", exc, exc_info=True)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "An internal error occurred."},
    )
