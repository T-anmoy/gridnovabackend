"""
Project reference IDs — GNE-{STATE}-{TYPE}-{YEAR}-{SEQUENCE}, e.g.
GNE-WB-RES-2026-00125.

Generated once, at Project creation, and never recomputed — see
MemoryManager.create_project(). The sequence resets per (state, type, year)
and is race-condition-safe: the increment is one atomic
INSERT ... ON CONFLICT DO UPDATE ... RETURNING statement against
project_sequence_counters, so two concurrent creations for the same bucket can
never receive the same number — there is no read-then-write gap for a second
writer to land in.

IMPORTANT DATA-AVAILABILITY CAVEAT (found during Phase 0 investigation, not
invented here): neither Project nor Lead carries a structured state or
project-family field today — the only place either is structured at all is
ProposalInputs, a separate table that arrives (if ever) after the Project
already exists. Every Project auto-created by MemoryManager.create_lead() (the
dominant, chat-originated path) starts completely empty. That means the state
and type codes below fall back to their "unknown" markers for most Projects
created today, not just a rare edge case — this is a real limitation of the
current data model, not a quality gap in the mapping tables themselves. State
is threaded through explicitly wherever a caller genuinely has it (currently:
app/api/routes/public_intake.py, which collects a raw state string).
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.memory.models import ProjectFamily

UNKNOWN_STATE_CODE = "XX"
UNKNOWN_TYPE_CODE = "GEN"

# Confirmed primary service states (see app/llm/company_facts.py). Anything
# else falls back to the first two letters of the given name, uppercased —
# a judgment call: reasonable as a stand-in, not a real state-code registry,
# and flagged as such in the accompanying report.
STATE_CODES: dict[str, str] = {
    "west bengal": "WB",
    "bihar": "BR",
}

# ProjectFamily -> 3-letter type code. rooftop_solar maps to ROF ("rooftop"),
# not RES ("residential") — ProjectFamily carries no residential/commercial/
# industrial distinction (that's a separate `segment` field elsewhere), so a
# code implying "residential" would misdescribe a commercial or industrial
# rooftop project. This whole table is a judgment call for Raju Paul / the
# team to confirm or adjust — not treated as final here.
TYPE_CODES: dict[ProjectFamily, str] = {
    ProjectFamily.rooftop_solar: "ROF",
    ProjectFamily.ground_mounted: "GRM",
    ProjectFamily.government_institutional: "GOV",
    ProjectFamily.street_lighting: "STL",
    ProjectFamily.water_pump: "PMP",
    ProjectFamily.om_service: "OMS",
    ProjectFamily.consulting: "CON",
}


def state_code(state: Optional[str]) -> str:
    if not state or not state.strip():
        return UNKNOWN_STATE_CODE
    normalized = state.strip().lower()
    if normalized in STATE_CODES:
        return STATE_CODES[normalized]
    fallback = state.strip()[:2].upper()
    return fallback or UNKNOWN_STATE_CODE


def type_code(project_family) -> str:
    if project_family is None:
        return UNKNOWN_TYPE_CODE
    if isinstance(project_family, str):
        try:
            project_family = ProjectFamily(project_family)
        except ValueError:
            return UNKNOWN_TYPE_CODE
    return TYPE_CODES.get(project_family, UNKNOWN_TYPE_CODE)


def next_sequence(db: Session, key: str) -> int:
    """Atomically increment and return the next value for one (state, type,
    year) bucket. A single INSERT ... ON CONFLICT ... RETURNING statement —
    the database enforces the atomicity, not application-level locking, so
    this is safe under real concurrent writers, not just correct in isolation.
    """
    row = db.execute(
        text(
            "INSERT INTO project_sequence_counters (counter_key, value) "
            "VALUES (:key, 1) "
            "ON CONFLICT(counter_key) DO UPDATE "
            "SET value = project_sequence_counters.value + 1 "
            "RETURNING value"
        ),
        {"key": key},
    ).first()
    return int(row[0])


def generate_reference_id(
    db: Session,
    *,
    state: Optional[str],
    project_family,
    year: int,
) -> str:
    st = state_code(state)
    tp = type_code(project_family)
    key = f"{st}-{tp}-{year}"
    seq = next_sequence(db, key)
    return f"GNE-{st}-{tp}-{year}-{seq:05d}"
