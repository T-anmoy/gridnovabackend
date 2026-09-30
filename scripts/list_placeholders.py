"""List every entry in the placeholder registry and whether it is still a dummy.

    python scripts/list_placeholders.py            # a table
    python scripts/list_placeholders.py --json     # machine-readable
    python scripts/list_placeholders.py --open     # unresolved entries only

Entries checked against the database (count_query, manual) use DATABASE_URL; without
a reachable database they show as "unknown".
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _rows(db) -> list[dict]:
    from app.placeholders import registry

    rows = []
    for entry, state in registry.evaluate_all(db):
        status = "resolved" if state["resolved"] is True else ("unknown" if state["resolved"] is None else "OPEN")
        rows.append({"id": entry.id, "area": entry.area, "blocking": entry.blocking, "status": status,
                     "location": entry.location_label(), "dummy_value": entry.dummy_value,
                     "needed_from_grid_nova": entry.needed_from_grid_nova, "probe_error": state["error"]})
    return rows


def main(argv: list[str]) -> int:
    from app.memory.db import SessionLocal

    try:
        with SessionLocal() as db:
            rows = _rows(db)
    except Exception:
        rows = _rows(None)
    if "--open" in argv:
        rows = [r for r in rows if r["status"] != "resolved"]
    if "--json" in argv:
        print(json.dumps(rows, indent=2, ensure_ascii=False, default=str))
        return 0
    for r in rows:
        print(f"{r['id']:<7} {r['status']:<9} {r['blocking']:<15} {r['area']:<11} {r['location']}")
        print(f"        {r['needed_from_grid_nova']}")
        if r["probe_error"]:
            print(f"        probe error: {r['probe_error']}")
    open_launch = sum(1 for r in rows if r["status"] != "resolved" and r["blocking"] == "blocks_launch")
    print(f"\n{len(rows)} entries; {open_launch} launch-blocking still open.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
