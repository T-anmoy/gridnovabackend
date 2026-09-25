import logging
from functools import lru_cache
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from app.config import settings
from app.memory.db import get_db

router = APIRouter()
logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[3]


@router.get("/api/v1/health")
def health():
    """Liveness: the process is up. Touches nothing else."""
    return {
        "status": "ok",
        "environment": settings.environment,
        "llm_mode": "mock" if settings.llm_mock_mode else "live",
    }


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
    """Readiness: the database answers and its schema is at this build's Alembic
    head. 503 names the failing check; nothing about the connection is disclosed."""
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
    return {"status": "ready", "db": "ok", "schema": "at_head"}
