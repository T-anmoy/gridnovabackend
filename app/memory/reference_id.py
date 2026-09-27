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

# Every Indian state and union territory, keyed by lower-case name, mapped to its
# ISO 3166-2:IN subdivision code (without the "IN-" prefix) as currently published
# — including the 23 Nov 2023 recoding (Odisha OD, Chhattisgarh CG, Telangana TS,
# Uttarakhand UK) and the 2020 merger of Dadra and Nagar Haveli with Daman and Diu
# (DH). Former names and spellings still in common use are listed as aliases so
# they resolve to the same code. Anything that is not a recognised name or code
# becomes UNKNOWN_STATE_CODE — there is no guessing from the name's letters, which
# collided with real codes (Uttar Pradesh -> "UT").
STATE_CODES: dict[str, str] = {
    "andaman and nicobar islands": "AN",
    "andhra pradesh": "AP",
    "arunachal pradesh": "AR",
    "assam": "AS",
    "bihar": "BR",
    "chandigarh": "CH",
    "chhattisgarh": "CG",
    "dadra and nagar haveli and daman and diu": "DH",
    "delhi": "DL",
    "goa": "GA",
    "gujarat": "GJ",
    "haryana": "HR",
    "himachal pradesh": "HP",
    "jammu and kashmir": "JK",
    "jharkhand": "JH",
    "karnataka": "KA",
    "kerala": "KL",
    "ladakh": "LA",
    "lakshadweep": "LD",
    "madhya pradesh": "MP",
    "maharashtra": "MH",
    "manipur": "MN",
    "meghalaya": "ML",
    "mizoram": "MZ",
    "nagaland": "NL",
    "odisha": "OD",
    "puducherry": "PY",
    "punjab": "PB",
    "rajasthan": "RJ",
    "sikkim": "SK",
    "tamil nadu": "TN",
    "telangana": "TS",
    "tripura": "TR",
    "uttar pradesh": "UP",
    "uttarakhand": "UK",
    "west bengal": "WB",
}
STATE_NAME_ALIASES: dict[str, str] = {
    "andaman and nicobar": "AN",
    "chattisgarh": "CG",
    "dadra and nagar haveli": "DH",
    "daman and diu": "DH",
    "dadra and nagar haveli and diu and daman": "DH",
    "nct of delhi": "DL",
    "national capital territory of delhi": "DL",
    "orissa": "OD",
    "pondicherry": "PY",
    "uttaranchal": "UK",
}
_KNOWN_CODES = frozenset(STATE_CODES.values())


def _normalize_state(state: str) -> str:
    import unicodedata

    folded = unicodedata.normalize("NFKD", state).encode("ascii", "ignore").decode()
    return " ".join(folded.replace("&", " and ").lower().split())


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
    """ISO 3166-2:IN code for a state/UT name (or for the code itself, with or
    without "IN-"), else UNKNOWN_STATE_CODE."""
    if not state or not state.strip():
        return UNKNOWN_STATE_CODE
    normalized = _normalize_state(state)
    if normalized in STATE_CODES:
        return STATE_CODES[normalized]
    if normalized in STATE_NAME_ALIASES:
        return STATE_NAME_ALIASES[normalized]
    code = normalized.upper().removeprefix("IN-")
    if code in _KNOWN_CODES:
        return code
    return UNKNOWN_STATE_CODE


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
