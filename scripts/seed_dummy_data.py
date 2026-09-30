"""Seed the DUMMY equipment catalogue for development and tests — never production.

    python scripts/seed_dummy_data.py

Grid Nova has not supplied its real module and inverter catalogue yet (placeholder
registry entry D1). These five rows let the serial register and execution stages be
built and tried out meanwhile. Every row has a brand beginning DUMMY and
is_placeholder = true, so it is marked in the dashboard, never named by Nova, and
never put in front of a customer. Running it twice changes nothing more.

Refuses to run when ENVIRONMENT=production.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# (category, brand, model_name, specification, is_dcr_compliant)
DUMMY_EQUIPMENT = (
    ("module", "DUMMY Solar", "DUMMY-M540-DCR", "DUMMY — 540 Wp mono PERC, DCR, 25-year performance warranty", True),
    ("module", "DUMMY Solar", "DUMMY-M575-DCR", "DUMMY — 575 Wp TOPCon, DCR, 30-year performance warranty", True),
    ("module", "DUMMY Import", "DUMMY-M550-NDCR", "DUMMY — 550 Wp mono PERC, non-DCR, 25-year performance warranty", False),
    ("inverter", "DUMMY Power", "DUMMY-INV-5K", "DUMMY — 5 kW single-phase on-grid inverter, 10-year warranty", False),
    ("inverter", "DUMMY Power", "DUMMY-INV-10K", "DUMMY — 10 kW three-phase on-grid inverter, 10-year warranty", False),
)


class RefusedInProduction(RuntimeError):
    pass


def seed(db, environment: str) -> int:
    """Insert whichever dummy rows are missing. Returns how many were added."""
    from sqlalchemy import select
    from app.memory.models import Equipment, EquipmentCategory

    if environment == "production":
        raise RefusedInProduction("seed_dummy_data refuses to run with ENVIRONMENT=production.")
    added = 0
    for category, brand, model_name, specification, dcr in DUMMY_EQUIPMENT:
        exists = db.execute(select(Equipment.id).where(Equipment.brand == brand,
                                                       Equipment.model_name == model_name)).first()
        if exists:
            continue
        db.add(Equipment(category=EquipmentCategory(category), brand=brand, model_name=model_name,
                         specification=specification, is_dcr_compliant=dcr, is_active=True, is_placeholder=True))
        added += 1
    db.commit()
    return added


def main() -> int:
    from app.config import settings
    from app.memory.db import SessionLocal

    try:
        with SessionLocal() as db:
            added = seed(db, settings.environment)
    except RefusedInProduction as exc:
        print(exc, file=sys.stderr)
        return 2
    print(f"Added {added} DUMMY equipment row(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
