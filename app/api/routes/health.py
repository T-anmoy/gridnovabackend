import logging
from functools import lru_cache
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from app.config import settings
from app.memory.db import get_db

router = APIRouter()
logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[3]


@router.get("/api/v1/health")
def health(request: Request):
    """Liveness: the process is up. Touches nothing else. `environment` and
    `llm_mode` are included only for a caller with a valid X-Internal-Key — they tell
    an outsider which deployment (and whether it has a model) they are probing."""
    from app.security.internal_key import has_valid_internal_key

    body = {"status": "ok"}
    if has_valid_internal_key(request):
        body.update(environment=settings.environment, llm_mode="mock" if settings.llm_mock_mode else "live")
    return body


@lru_cache(maxsize=1)
def _script_directory():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config(str(_REPO_ROOT / "alembic" / "alembic.ini"))
    cfg.set_main_option("script_location", str(_REPO_ROOT / "alembic"))
    return ScriptDirectory.from_config(cfg)


def _schema_state(current: list[str]) -> str:
    script = _script_directory()
    head = script.get_current_head()
    if current == [head]:
        return "at_head"
    if len(current) == 1:
        try:
            ancestors = {rev.revision for rev in script.walk_revisions(base="base", head=head)}
        except Exception:
            ancestors = set()
        if current[0] in ancestors:
            return "behind"
    return "unknown_revision"


@router.get("/api/v1/health/ready")
def ready(db: Session = Depends(get_db)):
    """Readiness: the database answers, its schema is at this build's Alembic head and —
    in production — the territory master is loaded. 503 names the failing check;
    nothing about the connection is disclosed. `territories` reports what is loaded."""
    try:
        db.execute(text("SELECT 1"))
    except Exception:
        logger.exception("Readiness check: database unreachable")
        return JSONResponse(status_code=503,
                            content={"status": "not_ready", "db": "unreachable", "schema": "unknown"})

    schema: Optional[str]
    try:
        if not inspect(db.get_bind()).has_table("alembic_version"):
            schema = "not_initialized"
        else:
            current = [row[0] for row in db.execute(text("SELECT version_num FROM alembic_version"))]
            schema = _schema_state(current) if current else "not_initialized"
    except Exception:
        logger.exception("Readiness check: could not read the schema version")
        schema = "unknown"

    if schema != "at_head":
        return JSONResponse(status_code=503, content={"status": "not_ready", "db": "ok", "schema": schema})

    from app.associates.territory_seed import territory_counts
    try:
        territories = territory_counts(db)
    except Exception:
        logger.exception("Readiness check: could not count the territory master")
        territories = {"seeded": False, "states": 0, "districts": 0, "blocks": 0}
    body = {"status": "ready", "db": "ok", "schema": "at_head", "territories": territories}
    if settings.is_production_like and not territories["seeded"]:
        # A deploy must not go live with empty registration forms. Development stays
        # ready (and says seeded: false) so a half-set-up machine still starts.
        return JSONResponse(status_code=503, content={**body, "status": "not_ready"})
    return body
