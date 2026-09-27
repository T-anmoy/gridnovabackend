"""Load the State → District → Block master from data/territories/*.csv.

Idempotent: every row is matched on its LGD code and inserted or updated in place.
Nothing is ever deleted — a territory that has left the files keeps its row (and
every application or seat that references it), and is switched off with is_active
by an Admin if it should no longer be offered. See data/territories/README.md for
where the files came from and how to add a state.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.memory.models import AuditLog, TerritoryBlock, TerritoryDistrict, TerritoryState

DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "territories"


class TerritoryDataError(ValueError):
    """The data files are inconsistent; nothing was written."""


@dataclass
class SeedResult:
    inserted: dict = field(default_factory=lambda: {"states": 0, "districts": 0, "blocks": 0})
    updated: dict = field(default_factory=lambda: {"states": 0, "districts": 0, "blocks": 0})


def _read(path: Path, columns: tuple[str, ...]) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        missing = [c for c in columns if c not in (reader.fieldnames or [])]
        if missing:
            raise TerritoryDataError(f"{path.name}: missing column(s) {', '.join(missing)}")
        rows = []
        for line_no, row in enumerate(reader, start=2):
            values = {c: (row.get(c) or "").strip() for c in columns}
            if not all(values.values()):
                raise TerritoryDataError(f"{path.name}:{line_no}: every column needs a value")
            rows.append(values)
        return rows


def _int(value: str, where: str) -> int:
    try:
        return int(value)
    except ValueError:
        raise TerritoryDataError(f"{where}: {value!r} is not an LGD code")


def load_files(data_dir: Path = DATA_DIR) -> tuple[list[dict], list[dict], list[dict]]:
    """Read and cross-check all three files before anything touches the database."""
    states = _read(data_dir / "states.csv", ("state_lgd_code", "iso_code", "state_name"))
    districts = _read(data_dir / "districts.csv", ("state_lgd_code", "district_lgd_code", "district_name"))
    blocks = _read(data_dir / "blocks.csv", ("district_lgd_code", "block_lgd_code", "block_name"))

    state_codes = {_int(s["state_lgd_code"], "states.csv") for s in states}
    if len(state_codes) != len(states):
        raise TerritoryDataError("states.csv: duplicate state_lgd_code")
    if len({s["iso_code"] for s in states}) != len(states):
        raise TerritoryDataError("states.csv: duplicate iso_code")
    for s in states:
        if len(s["iso_code"]) != 2 or not s["iso_code"].isalpha() or not s["iso_code"].isupper():
            raise TerritoryDataError(f"states.csv: iso_code {s['iso_code']!r} must be two upper-case letters")

    district_codes = set()
    for d in districts:
        code = _int(d["district_lgd_code"], "districts.csv")
        if code in district_codes:
            raise TerritoryDataError(f"districts.csv: duplicate district_lgd_code {code}")
        if _int(d["state_lgd_code"], "districts.csv") not in state_codes:
            raise TerritoryDataError(f"districts.csv: district {code} names a state not in states.csv")
        district_codes.add(code)

    block_codes = set()
    for b in blocks:
        code = _int(b["block_lgd_code"], "blocks.csv")
        if code in block_codes:
            raise TerritoryDataError(f"blocks.csv: duplicate block_lgd_code {code}")
        if _int(b["district_lgd_code"], "blocks.csv") not in district_codes:
            raise TerritoryDataError(f"blocks.csv: block {code} names a district not in districts.csv")
        block_codes.add(code)
    return states, districts, blocks


def seed_territories(db: Session, data_dir: Path = DATA_DIR, performed_by: str = "territory_seed") -> SeedResult:
    """Upsert the files into the database. Does not commit — the caller does, so a
    failure part-way leaves nothing behind."""
    states, districts, blocks = load_files(data_dir)
    result = SeedResult()

    def _upsert(model, key: str, code: int, values: dict, bucket: str):
        row = db.execute(select(model).where(model.lgd_code == code)).scalar_one_or_none()
        if row is None:
            row = model(lgd_code=code, **values)
            db.add(row)
            db.flush()
            result.inserted[bucket] += 1
            return row
        changed = {k: v for k, v in values.items() if getattr(row, k) != v}
        if changed:
            before = {k: getattr(row, k) for k in changed}
            for k, v in changed.items():
                setattr(row, k, v)
            db.flush()
            db.add(AuditLog(entity_type=f"territory_{key}", entity_id=str(row.id), action="seed_updated",
                            performed_by=performed_by, before_value=before, after_value=changed))
            result.updated[bucket] += 1
        return row

    state_ids = {}
    for s in states:
        code = int(s["state_lgd_code"])
        row = _upsert(TerritoryState, "state", code,
                      {"iso_code": s["iso_code"], "name": s["state_name"]}, "states")
        state_ids[code] = row.id
    district_ids = {}
    for d in districts:
        code = int(d["district_lgd_code"])
        row = _upsert(TerritoryDistrict, "district", code,
                      {"state_id": state_ids[int(d["state_lgd_code"])], "name": d["district_name"]}, "districts")
        district_ids[code] = row.id
    for b in blocks:
        _upsert(TerritoryBlock, "block", int(b["block_lgd_code"]),
                {"district_id": district_ids[int(b["district_lgd_code"])], "name": b["block_name"]}, "blocks")

    total_inserted = sum(result.inserted.values())
    if total_inserted:
        db.add(AuditLog(entity_type="territory_master", entity_id=str(data_dir.name), action="seeded",
                        performed_by=performed_by, after_value={"inserted": result.inserted}))
    return result
