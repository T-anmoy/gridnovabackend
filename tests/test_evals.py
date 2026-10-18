"""BE-7 N4 — the Nova evaluation suites (evals/*.yaml) and their runner
(scripts/eval_nova.py). Here they run in mock mode through the test app, which proves
the harness and the deterministic invariants; the real model is measured by the
live_llm tests below, skipped unless RUN_LIVE_LLM_EVALS=1."""
import os
import subprocess
import sys
from collections import Counter

import pytest

from scripts import eval_nova

REQUIRED_CUSTOMER_CATEGORIES = {
    "price_fishing", "per_kw_rate", "savings_promise", "warranty", "subsidy_certainty", "subsidy_ineligibility",
    "dcr", "model_number", "competitor", "past_project", "legal_form", "other_state", "fake_meeting",
    "other_customer", "injection", "hidden_injection", "abusive", "emoji", "very_long",
}
REQUIRED_STAFF_CATEGORIES = {"grounded_fact", "not_on_record", "opinion_vs_fact", "injection", "cross_customer"}
REQUIRED_RECORDS_CATEGORIES = {"lookup_ref", "lookup_name", "lookup_district", "aggregate", "near", "warranty",
                               "outstanding", "not_on_record", "scope_sales", "scope_money", "pii", "injection",
                               "write_request", "clarification"}


def test_customer_suite_shape():
    cases = eval_nova.load_suite("customer")
    assert len(cases) >= 60
    assert set(Counter(c["locale"] for c in cases)) == {"en", "hi", "bn"}
    assert REQUIRED_CUSTOMER_CATEGORIES <= {c["category"] for c in cases}
    # Every category is covered in all three languages.
    by_category = {}
    for c in cases:
        by_category.setdefault(c["category"], set()).add(c["locale"])
    assert all(locales == {"en", "hi", "bn"} for locales in by_category.values()), by_category
    assert all(c["must_not"] or c["must_any"] for c in cases)
    assert any(len(t) > 1500 for c in cases for t in c["turns"])


def test_staff_suite_shape():
    cases = eval_nova.load_suite("staff")
    assert len(cases) >= 20
    assert REQUIRED_STAFF_CATEGORIES <= {c["category"] for c in cases}


def test_records_suite_shape():
    cases = eval_nova.load_suite("records")
    assert len(cases) >= 50
    assert REQUIRED_RECORDS_CATEGORIES <= {c["category"] for c in cases}
    assert {c["role"] for c in cases} == set(eval_nova.RECORDS_ROLES)
    assert sum(1 for c in cases if c["truth"]) >= 8          # aggregates checked against hand-written SQL
    assert {c["status"] for c in cases} == set(eval_nova.RECORDS_STATUSES)


def test_records_suite_passes_in_mock_mode(client, _isolated_db, monkeypatch):
    from app.records import store
    monkeypatch.setattr(store, "within_rate_limit", lambda employee_id: True)
    results = eval_nova.run_suite("records", client, _isolated_db, mock=True)
    failed = [(r["id"], r["failures"]) for r in results if not r["passed"]]
    assert not failed, failed
    assert len(results) >= 50


def test_records_invariants_catch_a_leak():
    leak = {"status": "answered", "answer": "Contract ₹310000.00, call 9830000101", "tables": [], "citations": [],
            "searched": [{"tool": "project_dossier"}]}
    assert len(eval_nova._records_invariants("operations", leak)) == 2
    assert eval_nova._records_invariants("admin", leak) == []
    assert eval_nova._records_invariants("sales", {**leak, "answer": "Rina Sen"})


@pytest.fixture
def no_chat_rate_limit(monkeypatch):
    from app.api.routes import chat as chat_routes
    monkeypatch.setattr(chat_routes, "_check_rate_limit", lambda request: None)


def test_customer_suite_passes_in_mock_mode(client, _isolated_db, no_chat_rate_limit):
    results = eval_nova.run_suite("customer", client, _isolated_db, mock=True)
    failed = [(r["id"], r["failures"]) for r in results if not r["passed"]]
    assert not failed, failed
    assert len(results) >= 60


def test_staff_suite_passes_in_mock_mode(client, _isolated_db):
    results = eval_nova.run_suite("staff", client, _isolated_db, mock=True)
    failed = [(r["id"], r["failures"]) for r in results if not r["passed"]]
    assert not failed, failed


def test_the_invariants_catch_a_rogue_reply():
    """The runner's own checks are real: an invented amount, time or contact detail in
    a sent reply (any language) fails the case even if no must_not names it."""
    assert eval_nova._reply_invariants("यह आठ लाख में हो जाएगा।", "price?")
    assert eval_nova._reply_invariants("How about Thursday at 11:00?", "when?")
    assert eval_nova._reply_invariants("Call Priya on +91 91234 56789.", "her number?")
    assert eval_nova._reply_invariants("", "hi") == ["empty reply"]
    assert eval_nova._reply_invariants("Your bill of 4,000 a month is a good start.", "bill is 4,000") == []


@pytest.mark.parametrize("argv", [["--suite", "customer"], ["--suite", "staff"], ["--suite", "records"]])
def test_refuses_without_a_mode(argv, capsys):
    assert eval_nova.main(argv) == 2
    assert "Refusing to run" in capsys.readouterr().err


def test_live_refuses_without_a_key(tmp_path):
    env = {**os.environ, "LLM_API_KEY": ""}
    result = subprocess.run([sys.executable, "scripts/eval_nova.py", "--suite", "customer", "--live"],
                            cwd=eval_nova.REPO_ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert result.returncode == 2 and "LLM_API_KEY is not set" in result.stderr


@pytest.mark.live_llm
@pytest.mark.parametrize("suite", ["customer", "staff", "records"])
def test_live_suite(suite):
    result = subprocess.run([sys.executable, "scripts/eval_nova.py", "--suite", suite, "--live"],
                            cwd=eval_nova.REPO_ROOT, capture_output=True, text=True, timeout=3600)
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]
