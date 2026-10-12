"""BE-6 C3 — server-side lead filters (stage, score, IST created-day) on the list and
the CSV export, applied inside the role scope and before pagination."""
import csv
import io
from datetime import datetime, timezone

import pytest

from app.memory.models import LeadScore, LeadSource, PipelineStage
from tests.conftest import admin_headers, sales_headers

LEADS = "/api/v1/dashboard/leads"
EXPORT = "/api/v1/dashboard/leads/export"


def _utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


@pytest.fixture
def pool(memory, db_session):
    memory.get_or_create_employee("emp-sales-1", "S1", "s1@gridnova.in", "sales")
    memory.get_or_create_employee("emp-sales-2", "S2", "s2@gridnova.in", "sales")
    # (name, stage, score, owner, created_at UTC)
    rows = [
        ("A", PipelineStage.new, LeadScore.low, None, _utc(2026, 9, 1, 10)),
        ("B", PipelineStage.qualified, LeadScore.medium, "emp-sales-1", _utc(2026, 9, 2, 10)),
        ("C", PipelineStage.qualified, LeadScore.high, "emp-sales-2", _utc(2026, 9, 3, 10)),
        ("D", PipelineStage.won, LeadScore.high, "emp-sales-1", _utc(2026, 9, 4, 10)),
        ("E", PipelineStage.lost, None, None, _utc(2026, 9, 5, 10)),
        # 2026-09-06 19:00 UTC is 00:30 IST on 09-07
        ("F", PipelineStage.new, LeadScore.low, None, _utc(2026, 9, 6, 19, 0)),
        ("=cmd|' /C calc'!A0", PipelineStage.new, LeadScore.low, None, _utc(2026, 9, 6, 10)),
    ]
    out = {}
    for name, stage, score, owner, created in rows:
        c = memory.get_or_create_customer(email=f"{abs(hash(name))}@example.com", name=name)
        lead = memory.create_lead(customer_id=c.id, source=LeadSource.referral)
        lead.pipeline_stage, lead.lead_score = stage, score
        lead.assigned_employee_id, lead.created_at = owner, created
        out[name] = lead
    db_session.commit()
    return out


def _names(client, memory, params, headers=None):
    body = client.get(LEADS, params=params, headers=headers or admin_headers()).json()
    return {memory.get_lead(i["id"]).customer.name for i in body["items"]}, body


def test_stage_single_repeated_and_comma(client, memory, pool):
    assert _names(client, memory, {"stage": "won"})[0] == {"D"}
    assert _names(client, memory, [("stage", "won"), ("stage", "lost")])[0] == {"D", "E"}
    assert _names(client, memory, {"stage": "won,lost"})[0] == {"D", "E"}


def test_unknown_stage_is_a_structured_422(client, pool):
    r = client.get(LEADS, params={"stage": "bogus"}, headers=admin_headers())
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "unknown_stage" and r.json()["detail"]["field"] == "stage"
    assert client.get(EXPORT, params={"stage": "bogus"}, headers=admin_headers()).status_code == 422


def test_score_range(client, memory, pool):
    assert _names(client, memory, {"min_score": "high"})[0] == {"C", "D"}
    assert _names(client, memory, {"max_score": "medium"})[0] >= {"A", "B", "F"}
    assert "E" not in _names(client, memory, {"max_score": "high"})[0]  # unscored never match
    assert _names(client, memory, {"min_score": "medium", "max_score": "medium"})[0] == {"B"}
    bad = client.get(LEADS, params={"min_score": "high", "max_score": "low"}, headers=admin_headers())
    assert bad.status_code == 422 and bad.json()["detail"]["code"] == "invalid_score_range"
    assert client.get(LEADS, params={"min_score": "9"}, headers=admin_headers()).json()["detail"]["code"] == "unknown_score"


def test_created_days_are_ist_and_inclusive(client, memory, pool):
    got, _ = _names(client, memory, {"created_from": "2026-09-02", "created_to": "2026-09-03"})
    assert got == {"B", "C"}
    # F was created 00:30 IST on the 7th, not the 6th
    assert "F" in _names(client, memory, {"created_from": "2026-09-07", "created_to": "2026-09-07"})[0]
    assert "F" not in _names(client, memory, {"created_from": "2026-09-06", "created_to": "2026-09-06"})[0]
    r = client.get(LEADS, params={"created_from": "09/02/2026"}, headers=admin_headers())
    assert r.status_code == 422 and r.json()["detail"]["code"] == "invalid_date"
    r = client.get(LEADS, params={"created_from": "2026-09-05", "created_to": "2026-09-01"}, headers=admin_headers())
    assert r.json()["detail"]["code"] == "invalid_date_range"


def test_combined_filters_and_total_before_paging(client, memory, pool):
    got, body = _names(client, memory, {"stage": "qualified", "min_score": "high", "page_size": 1})
    assert got == {"C"} and body["total"] == 1
    body = client.get(LEADS, params={"stage": "new", "page_size": 1, "page": 2}, headers=admin_headers()).json()
    assert body["total"] == 3 and len(body["items"]) == 1


def test_sales_scope_still_holds(client, memory, pool):
    got, body = _names(client, memory, {"stage": ["qualified", "won"]}, sales_headers("emp-sales-1"))
    assert got == {"B", "D"}  # C belongs to sales-2
    assert body["total"] == 2


def _export_rows(client, params):
    r = client.get(EXPORT, params=params, headers=admin_headers())
    assert r.status_code == 200
    return list(csv.reader(io.StringIO(r.text)))[1:]


def test_export_matches_the_table(client, memory, pool):
    params = {"stage": "new", "max_score": "low", "created_from": "2026-09-01", "created_to": "2026-09-06"}
    table_ids = {i["id"] for i in client.get(LEADS, params=params, headers=admin_headers()).json()["items"]}
    assert {int(r[0]) for r in _export_rows(client, params)} == table_ids
    assert len(table_ids) == 2  # A and the injection row; F is the 7th in IST


def test_export_keeps_cells_safe(client, pool):
    rows = _export_rows(client, {"stage": "new"})
    cell = next(r[1] for r in rows if "calc" in r[1])
    assert not cell.startswith("=")
