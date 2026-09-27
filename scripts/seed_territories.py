"""Load the territory master (State → District → Block) into DATABASE_URL.

    python scripts/seed_territories.py

Run it after scripts/bootstrap_db.py on every deploy — it is idempotent (upsert by
LGD code, never deletes). The data files and their provenance are in
data/territories/ (see its README.md). Until this has run, the public registration
forms have no territories to offer and every registration is refused.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.associates.territory_seed import TerritoryDataError, seed_territories  # noqa: E402
from app.config import settings  # noqa: E402


def main(database_url: str) -> int:
    print(f"seed_territories: target {make_url(database_url).render_as_string(hide_password=True)}")
    engine = create_engine(database_url)
    try:
        with Session(engine) as db:
            try:
                result = seed_territories(db)
            except TerritoryDataError as exc:
                print(f"seed_territories: REFUSED — {exc}. Nothing was changed.", file=sys.stderr)
                return 2
            db.commit()
        print(f"seed_territories: inserted {result.inserted}, updated {result.updated}")
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main(settings.database_url))
