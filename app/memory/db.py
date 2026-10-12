from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase
from app.config import settings


def engine_options(database_url: str) -> dict:
    """create_engine() keyword arguments for DATABASE_URL.

    PostgreSQL: an explicit, env-driven pool. Each instance holds at most
    DB_POOL_SIZE + DB_MAX_OVERFLOW connections, so Cloud SQL's max_connections must cover
    (that sum) × Cloud Run --max-instances, plus the migration job and an admin session
    (deploy/RUNBOOK.md, "Connection budget"). pool_pre_ping replaces a connection Cloud SQL
    closed while it sat idle, and pool_recycle retires connections before any server-side
    idle timeout can. A request that cannot get a connection within DB_POOL_TIMEOUT_SECONDS
    fails instead of hanging.
    """
    if "sqlite" in database_url:
        return {"connect_args": {"check_same_thread": False}}
    return {
        "pool_size": settings.db_pool_size,
        "max_overflow": settings.db_max_overflow,
        "pool_timeout": settings.db_pool_timeout_seconds,
        "pool_recycle": settings.db_pool_recycle_seconds,
        "pool_pre_ping": True,
    }


engine = create_engine(settings.database_url, echo=False, **engine_options(settings.database_url))

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
