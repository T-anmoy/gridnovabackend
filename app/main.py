from contextlib import asynccontextmanager

import sentry_sdk
from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routes.chat import router as chat_router
from app.api.routes.dashboard import router as dashboard_router
from app.api.routes.health import router as health_router
from app.api.routes.public_proposals import router as public_proposals_router
from app.config import settings
from app.memory.db import Base, engine


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create all tables on startup (Alembic handles production migrations)
    Base.metadata.create_all(bind=engine)
    # Fail loudly at startup if the proposal content config is structurally invalid
    # (overlapping pricing tiers, a rule referencing a missing section, etc.) rather
    # than discovering it mid-request while drafting a proposal.
    from app.proposals.content_engine import load_content_config
    load_content_config(force_reload=True)
    yield


if settings.sentry_dsn:
    sentry_sdk.init(dsn=settings.sentry_dsn, environment=settings.environment)

app = FastAPI(
    title="Grid Nova AI Operations Platform",
    description="AI-powered lead qualification and proposal management for Grid Nova Energy",
    version="1.0.0",
    lifespan=lifespan,
)

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


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    import logging
    logging.getLogger(__name__).error("Unhandled exception: %s", exc, exc_info=True)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "An internal error occurred."},
    )
