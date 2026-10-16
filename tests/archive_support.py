"""Builders for the BE-8 Project Archive tests."""
from __future__ import annotations

import csv
import io

from app.archive.importer import TEMPLATE_COLUMNS
from tests.associate_support import territories  # noqa: F401 — fixture

BASE = "/api/v1/dashboard/archive"
ROLES = ("admin", "management", "accounts", "operations", "engineer", "sales")


def who(role: str) -> dict:
    return {"X-Employee-Id": f"emp-{role}-1", "X-Employee-Role": role}


ADMIN = who("admin")


def row(**over) -> dict:
    base = {"grid_nova_ref": "GN-0001", "scheme_or_segment": "pm_surya_ghar_residential", "system_type": "on_grid",
            "capacity_kw": "3", "state": "West Bengal", "district": "Alipurduar", "commissioning_date": "15/03/2025"}
    base.update(over)
    return base


def csv_bytes(rows: list[dict], columns=None, *, bom=False, comment_row=False) -> bytes:
    columns = list(columns if columns is not None else TEMPLATE_COLUMNS)
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(columns)
    if comment_row:
        writer.writerow(["#EXAMPLE"] + [""] * (len(columns) - 1))
    for r in rows:
        writer.writerow([r.get(c, "") for c in columns])
    data = out.getvalue().encode("utf-8")
    return (b"\xef\xbb\xbf" + data) if bom else data


def upload(client, data: bytes, *, dry_run=True, token=None, who_=None, name="projects.csv"):
    form = {"dry_run": "true" if dry_run else "false"}
    if token:
        form["batch_token"] = token
    return client.post(f"{BASE}/imports", files={"file": (name, data, "text/csv")}, data=form,
                       headers=who_ or ADMIN)


def import_rows(client, rows: list[dict], **kw):
    """Dry run then commit; returns the commit response."""
    data = csv_bytes(rows, **kw)
    dry = upload(client, data)
    assert dry.status_code == 200, dry.text
    done = upload(client, data, dry_run=False, token=dry.json()["batch_token"])
    assert done.status_code == 200, done.text
    return done


def codes(report: dict, kind="errors") -> set:
    return {i["code"] for i in report[kind]}
