"""One command to get a local database ready — schema, territories, optionally dummy data.

    python scripts/dev_setup.py                 # schema + territories
    python scripts/dev_setup.py --dummy-data    # ... and the development-only equipment catalogue

Safe to run again at any time:
  * a new or empty database is built with scripts/bootstrap_db.py;
  * an existing database behind the current schema is upgraded (`alembic upgrade head`),
    and what ran is printed;
  * territories are upserted (never deleted) by scripts/seed_territories.py;
  * it NEVER deletes or overwrites a database. If it cannot continue it stops and prints
    what to do by hand.

Refuses to run when ENVIRONMENT=production: production's schema belongs to
scripts/bootstrap_db.py and Alembic on deploy, and its data is never seeded from here.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import create_engine, inspect, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.config import settings  # noqa: E402
from scripts import bootstrap_db, seed_dummy_data, seed_territories  # noqa: E402

EXIT_OK, EXIT_FAILED, EXIT_REFUSED = 0, 1, 2
API_URL = "http://localhost:8000"


def _revision(url: str) -> Optional[str]:
    """The schema revision in the database, or None when there is none yet."""
    engine = create_engine(url)
    try:
        if "alembic_version" not in inspect(engine).get_table_names():
            return None
        with engine.connect() as conn:
            rows = [r[0] for r in conn.execute(text("SELECT version_num FROM alembic_version"))]
        return ", ".join(rows) or None
    finally:
        engine.dispose()


def _backup_hint(url: str) -> str:
    parsed = make_url(url)
    if parsed.get_backend_name() == "sqlite" and parsed.database:
        path = Path(parsed.database)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        return (f"  1. Keep a copy:   cp {path} {path}.bak-{stamp}\n"
                f"  2. See the error: .venv/bin/alembic -c alembic/alembic.ini upgrade head\n"
                f"  3. Only if this is a throwaway local database and you want to start over, MOVE it aside\n"
                f"     (do not delete it):  mv {path} {path}.old-{stamp}   then run this script again.")
    return ("  1. Take a backup first (for PostgreSQL: pg_dump).\n"
            "  2. See the error: .venv/bin/alembic -c alembic/alembic.ini upgrade head\n"
            "  3. Fix what it reports, then run this script again.")


def _stop(why: str, url: str, code: int) -> int:
    print(f"\ndev_setup: STOPPED — {why}", file=sys.stderr)
    print("Your database has NOT been deleted, and nothing further was changed. To continue safely:\n"
          + _backup_hint(url), file=sys.stderr)
    return code


def _schema(url: str) -> Optional[int]:
    """Step (a). Returns an exit code to stop with, or None to carry on."""
    before = _revision(url)
    try:
        code = bootstrap_db.bootstrap(url)
    except Exception as exc:  # a failing migration: report it, never touch the database further
        return _stop(f"the schema step failed: {type(exc).__name__}: {exc}", url, EXIT_FAILED)
    if code != bootstrap_db.EXIT_OK:
        return _stop("the database has tables but no recorded schema revision (typically a database "
                     "the app created itself on an earlier start, before any setup), so its revision "
                     "cannot be known safely.", url, EXIT_REFUSED)
    print(f"dev_setup: schema {before or '(new database)'} → {_revision(url)}")
    return None


def _counts(url: str) -> dict:
    from app.associates.territory_seed import territory_counts
    engine = create_engine(url)
    try:
        with Session(engine) as db:
            return territory_counts(db)
    finally:
        engine.dispose()


def main(argv: Optional[list[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    unknown = [a for a in argv if a != "--dummy-data"]
    if unknown:
        print(f"dev_setup: unknown option(s): {' '.join(unknown)}\nusage: python scripts/dev_setup.py [--dummy-data]",
              file=sys.stderr)
        return EXIT_REFUSED
    if settings.is_production_like:
        print(f"dev_setup: REFUSED — ENVIRONMENT={settings.environment}. This script is for local development "
              "only; staging and production are migrated by scripts/bootstrap_db.py on deploy.", file=sys.stderr)
        return EXIT_REFUSED

    os.chdir(REPO_ROOT)   # a relative sqlite path in DATABASE_URL then means the same here as for uvicorn
    url = settings.database_url
    print(f"dev_setup: database {make_url(url).render_as_string(hide_password=True)}")

    stop = _schema(url)
    if stop is not None:
        return stop

    code = seed_territories.main(url)
    if code != 0:
        return _stop("the territory data could not be loaded (see above).", url, EXIT_FAILED)

    if "--dummy-data" in argv:
        code = seed_dummy_data.main()
        if code != 0:
            return _stop("the dummy data could not be loaded (see above).", url, EXIT_FAILED)

    counts = _counts(url)
    print(f"""
dev_setup: ready.
  schema revision : {_revision(url)}
  territories     : {counts['states']} states, {counts['districts']} districts, {counts['blocks']} blocks

Start the API (from this folder, with the virtualenv active):
  uvicorn app.main:app --reload --port 8000

In the frontend's .env.local:
  NEXT_PUBLIC_API_URL={API_URL}
""")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
