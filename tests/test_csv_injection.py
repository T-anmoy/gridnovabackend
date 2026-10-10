"""BE-1 S4 — every CSV the API writes neutralises spreadsheet formulas.

/dashboard/leads/export and /dashboard/proposals/export wrote public-form names into
Excel unescaped; the payments exports already used a safe-cell helper, now shared
(app/security/csv_cells.py) and applied to every cell of every CSV writer.
"""
import csv
import io

import pytest

from app.memory.models import PipelineStage
from app.security.csv_cells import safe_cell
from tests.conftest import admin_headers

PREFIXES = ["=", "+", "-", "@", "\t", "\r"]


@pytest.mark.parametrize("prefix", PREFIXES)
def test_a_dangerous_text_cell_is_quoted(prefix):
    assert safe_cell(f"{prefix}HYPERLINK(\"http://x\")") == f"'{prefix}HYPERLINK(\"http://x\")"


@pytest.mark.parametrize("value, expected", [
    ("-1500.00", "-1500.00"), ("42", "42"), (None, ""), ("Asha", "Asha"), ("-1+2", "'-1+2"), (3.5, "3.5")])
def test_numbers_and_plain_text_are_left_alone(value, expected):
    assert safe_cell(value) == expected


def _rows(response) -> list[list[str]]:
    return list(csv.reader(io.StringIO(response.text)))


@pytest.mark.parametrize("prefix", PREFIXES)
def test_leads_export_neutralises_names(client, memory, db_session, prefix):
    customer = memory.get_or_create_customer(email=f"csv{PREFIXES.index(prefix)}@example.com",
                                             name=f"{prefix}cmd|' /C calc'!A0")
    memory.create_lead(customer_id=customer.id)
    db_session.commit()
    r = client.get("/api/v1/dashboard/leads/export", headers=admin_headers())
    assert r.status_code == 200
    names = [row[1] for row in _rows(r)[1:]]
    assert names == [f"'{prefix}cmd|' /C calc'!A0"]


@pytest.mark.parametrize("prefix", PREFIXES)
def test_proposals_export_neutralises_names(client, memory, db_session, prefix):
    customer = memory.get_or_create_customer(email=f"csvp{PREFIXES.index(prefix)}@example.com",
                                             name=f"{prefix}SUM(1+1)")
    lead = memory.create_lead(customer_id=customer.id)
    memory.update_lead_fields(lead_id=lead.id, new_fields={"name": "x"}, performed_by="t",
                              stage=PipelineStage.qualified)
    memory.create_proposal_draft(lead_id=lead.id, content={}, total_value=1000.0,
                                 requires_manager_signoff=False, performed_by="t")
    db_session.commit()
    r = client.get("/api/v1/dashboard/proposals/export", headers=admin_headers())
    assert r.status_code == 200
    rows = _rows(r)
    assert rows[1][2] == f"'{prefix}SUM(1+1)"
    assert rows[1][5] == "1000.0"  # a number stays a number
