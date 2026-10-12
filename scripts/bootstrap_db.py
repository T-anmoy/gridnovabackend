"""Bring the database at DATABASE_URL to the current schema — safe to run on every deploy.

    python scripts/bootstrap_db.py

Why this exists: the Alembic chain has no baseline. 0001 (down_revision = None)
only ALTERs tables that Base.metadata.create_all() was assumed to have created, so
`alembic upgrade head` against an empty database fails immediately — and on
PostgreSQL several historical revisions cannot run at all (0004's revision id is
longer than alembic_version.version_num's VARCHAR(32); 0010/0011/0020 create their
enum types explicitly and then op.create_table creates them again). A brand-new
database is therefore built from the models and stamped at head; every later deploy
is an ordinary `alembic upgrade head`.

Three cases, decided only from what is actually in the database:

  * no application tables and no alembic_version  ->  create_all(), then `alembic stamp head`
  * application tables AND alembic_version          ->  `alembic upgrade head`
  * anything else                                   ->  stop, exit 2, change nothing

It never guesses a revision for an unversioned database: stamping the wrong revision
would silently skip (or re-run) migrations against real data.

On PostgreSQL the whole run holds a session-level advisory lock (BOOTSTRAP_LOCK_KEY) on a
connection of its own, so two executions of the migration job (a retry, two deploys at
once) serialise: the second waits, then finds the schema at head and runs a no-op
`alembic upgrade head`.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from sqlalchemy import create_engine, inspect, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

import app.memory.models  # noqa: E402,F401 — registers every model on Base.metadata
from app.config import settings  # noqa: E402
from app.memory.db import Base  # noqa: E402

EXIT_OK = 0
EXIT_REFUSED = 2

# pg_advisory_lock key for the bootstrap/migration job (an arbitrary constant, also used
# by tests/test_migration_job_lock.py). The scheduled jobs' keys live in app/jobs/locks.py.
BOOTSTRAP_LOCK_KEY = 0x6772_6964_6E6F_7661  # "gridnova"


def _alembic_config() -> Config:
    cfg = Config(str(REPO_ROOT / "alembic" / "alembic.ini"))
    # alembic.ini's script_location is relative to the working directory; pin it so
    # the script behaves the same wherever it is launched from.
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    # alembic/env.py reads settings.database_url itself — the same URL used here.
    return cfg


def _current_revision(engine) -> list[str]:
    with engine.connect() as conn:
        return [row[0] for row in conn.execute(text("SELECT version_num FROM alembic_version"))]


def bootstrap(database_url: str) -> int:
    safe_url = make_url(database_url).render_as_string(hide_password=True)
    print(f"bootstrap_db: target {safe_url}")

    engine = create_engine(database_url)
    try:
        if engine.dialect.name != "postgresql":
            return _bootstrap(engine)
        with engine.connect() as lock_conn:
            print("bootstrap_db: waiting for the migration lock")
            lock_conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": BOOTSTRAP_LOCK_KEY})
            lock_conn.commit()
            print("bootstrap_db: migration lock held")
            try:
                return _bootstrap(engine)
            finally:
                lock_conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": BOOTSTRAP_LOCK_KEY})
                lock_conn.commit()
    finally:
        engine.dispose()


def _bootstrap(engine) -> int:
    existing = set(inspect(engine).get_table_names())
    app_tables = set(Base.metadata.tables)
    present = sorted(existing & app_tables)
    has_version_table = "alembic_version" in existing

    if not present and not has_version_table:
        print(f"bootstrap_db: empty database — creating {len(app_tables)} tables from the models")
        Base.metadata.create_all(bind=engine)
        print("bootstrap_db: stamping alembic_version at head")
        command.stamp(_alembic_config(), "head")
        print(f"bootstrap_db: done — alembic_version = {', '.join(_current_revision(engine))}")
        return EXIT_OK

    if present and has_version_table:
        before = _current_revision(engine)
        if not before:
            print(
                "bootstrap_db: REFUSED — alembic_version exists but is empty, so the "
                "schema's revision is unknown. Nothing was changed.",
                file=sys.stderr,
            )
            return EXIT_REFUSED
        print(f"bootstrap_db: existing schema at {', '.join(before)} — running alembic upgrade head")
        command.upgrade(_alembic_config(), "head")
        print(f"bootstrap_db: done — alembic_version = {', '.join(_current_revision(engine))}")
        return EXIT_OK

    if present:
        print(
            "bootstrap_db: REFUSED — application tables exist "
            f"({', '.join(present)}) but there is no alembic_version table, so the "
            "schema's revision is unknown. Nothing was changed. Inspect the database "
            "and `alembic stamp <revision>` it by hand once you know which revision it matches.",
            file=sys.stderr,
        )
    else:
        print(
            "bootstrap_db: REFUSED — alembic_version exists but none of the application "
            "tables do. Nothing was changed.",
            file=sys.stderr,
        )
    return EXIT_REFUSED


def main() -> int:
    return bootstrap(settings.database_url)


if __name__ == "__main__":
    sys.exit(main())
