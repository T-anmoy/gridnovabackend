"""Evaluate Nova against the real model — BE-7 N4.

    python scripts/eval_nova.py --suite customer --live     # the real model (needs LLM_API_KEY)
    python scripts/eval_nova.py --suite staff --live
    python scripts/eval_nova.py --suite customer --mock     # harness smoke run, no model
    python scripts/eval_nova.py --suite records --live      # Nova Records (BE-9), staff assistant

Every test in tests/ runs in mock mode, so the configured model's actual behaviour is
only measured here. Run it on the staging key before Gate 1 sign-off and before
go-live: a launch requires 100% pass on blocking cases.

It drives the real HTTP routes (POST /api/v1/chat/message, POST
/api/v1/dashboard/memory/ask, POST /api/v1/dashboard/nova/ask) in-process against a THROWAWAY SQLite database in a
temporary directory — never DATABASE_URL — prints a pass/fail table, writes
eval_reports/<timestamp>.json (git-ignored) and exits non-zero on any failure.

--live refuses to run without an LLM_API_KEY (environment or .env). Neither --live
nor --mock refuses to run at all, so nobody mistakes a mock run for a real one.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

SUITES = {"customer": REPO_ROOT / "evals" / "customer_nova.yaml",
          "staff": REPO_ROOT / "evals" / "staff_memory.yaml",
          "records": REPO_ROOT / "evals" / "staff_records.yaml"}
RECORDS_URL = "/api/v1/dashboard/nova/ask"
RECORDS_ROLES = ("admin", "management", "accounts", "operations", "engineer", "sales")
RECORDS_STATUSES = ("answered", "not_on_record", "needs_clarification", "refused")
REPORT_DIR = REPO_ROOT / "eval_reports"
CHAT_URL = "/api/v1/chat/message"
ASK_URL = "/api/v1/dashboard/memory/ask"
ADMIN = {"X-Employee-Id": "emp-eval-admin", "X-Employee-Role": "admin"}
LOCALES = ("en", "hi", "bn")
_NOT_ON_RECORD = re.compile(r"not on record|nothing is on record|records found|matching records", re.I)


class SuiteError(ValueError):
    pass


# ── Suites ─────────────────────────────────────────────────────────────────────

def load_suite(name: str) -> list[dict]:
    """The suite's cases with defaults applied and every regex compiled once (an
    invalid suite fails here, not halfway through a paid run)."""
    import yaml

    data = yaml.safe_load(SUITES[name].read_text(encoding="utf-8"))
    defaults = data.get("defaults") or {}
    cases, seen = [], set()
    for raw in data.get("cases") or []:
        case = {**defaults, **raw}
        cid = case.get("id")
        if not cid or cid in seen:
            raise SuiteError(f"missing or duplicate case id: {cid!r}")
        seen.add(cid)
        if not case.get("category"):
            raise SuiteError(f"{cid}: category is required")
        if name == "customer":
            if case.get("locale") not in LOCALES:
                raise SuiteError(f"{cid}: locale must be one of {LOCALES}")
            if case.get("long_turn"):
                case["turns"] = [case["long_turn"]["text"] * int(case["long_turn"]["repeat"])]
            if not case.get("turns") or not all(isinstance(t, str) and t for t in case["turns"]):
                raise SuiteError(f"{cid}: turns must be a non-empty list of messages")
        elif name == "records":
            if case.get("role") not in RECORDS_ROLES or not case.get("question"):
                raise SuiteError(f"{cid}: role (one of {RECORDS_ROLES}) and question are required")
            if case.get("status") not in RECORDS_STATUSES:
                raise SuiteError(f"{cid}: status must be one of {RECORDS_STATUSES}")
            case["cites"] = list(case.get("cites") or [])
            case["truth"] = list(case.get("truth") or [])
        else:
            if not (case.get("seed") or {}).get("customer") or not case.get("question"):
                raise SuiteError(f"{cid}: seed.customer and question are required")
        for key in ("must_not", "must_any"):
            case[key] = list(case.get(key) or [])
            for pattern in case[key]:
                try:
                    re.compile(pattern, re.I)
                except re.error as exc:
                    raise SuiteError(f"{cid}: bad {key} regex {pattern!r}: {exc}") from exc
        cases.append(case)
    return cases


def _matches(patterns: list, text: str) -> list[str]:
    return [p for p in patterns if re.search(p, text or "", re.I)]


# ── Customer cases ─────────────────────────────────────────────────────────────

def _reply_invariants(reply: str, customer_text: str) -> list[str]:
    """What every customer reply must satisfy, whatever the case: the deterministic
    guardrail checks re-run on what was actually SENT (Hindi/Bengali included)."""
    from app.agents import guardrails
    from app.llm.company_facts import COMPANY_FACTS

    if not (reply or "").strip():
        return ["empty reply"]
    allowed = f"{customer_text}\n{COMPANY_FACTS}"
    found = [guardrails.check_money(reply, allowed), guardrails.check_times(reply, allowed),
             guardrails.check_personal_data(reply, customer_text, COMPANY_FACTS), guardrails.check_leaks(reply)]
    return [f"invariant {f['code']}: {f['detail']}" for f in found if f]


def _lead_guardrail_codes(db, token: str) -> list[str]:
    from sqlalchemy import select

    from app.memory.memory_manager import MemoryManager
    from app.memory.models import ChatSession

    db.expire_all()
    row = db.execute(select(ChatSession).where(
        ChatSession.token_hash == MemoryManager.hash_chat_token(token))).scalar_one_or_none()
    if row is None:
        return []
    codes = []
    for entry in MemoryManager(db).list_audit_logs(entity_type="lead", entity_id=str(row.lead_id)):
        if "guardrail" in entry.action:
            codes += [v.get("code") for v in (entry.after_value or {}).get("violations", [])]
    return codes


def run_customer_case(client, db, case: dict, *, mock: bool) -> dict:
    failures, replies, token = [], [], None
    for turn in case["turns"]:
        body = {"message": turn, "locale": case["locale"], **({"session_token": token} if token else {})}
        response = client.post(CHAT_URL, json=body)
        if response.status_code != 200:
            failures.append(f"HTTP {response.status_code}: {response.text[:200]}")
            break
        token = response.json()["session_token"]
        reply = response.json()["reply"]
        replies.append(reply)
        customer_text = "\n".join(case["turns"])
        failures += _reply_invariants(reply, customer_text)
        failures += [f"must_not matched {p!r}" for p in _matches(case["must_not"], reply)]
    skipped = []
    if replies and case["must_any"]:
        if mock:
            skipped.append("must_any (mock replies)")
        elif not _matches(case["must_any"], replies[-1]):
            failures.append(f"final reply matched none of must_any {case['must_any']}")
    return {"id": case["id"], "category": case["category"], "locale": case["locale"],
            "blocking": bool(case.get("blocking", True)), "passed": not failures, "failures": failures,
            "skipped_checks": skipped, "replies": replies,
            "guardrail_codes": _lead_guardrail_codes(db, token) if token else []}


# ── Staff cases ────────────────────────────────────────────────────────────────

def _seed_customer(memory, seed: dict) -> int:
    from app.memory.models import MemoryKind, MemorySource

    who = seed["customer"]
    customer = memory.get_or_create_customer(email=who["email"], name=who["name"], phone=who.get("phone"),
                                             performed_by="eval")
    if who.get("company"):
        customer.company = who["company"]
    lead = memory.create_lead(customer_id=customer.id, performed_by="eval")
    if seed.get("fields"):
        memory.update_lead_fields(lead_id=lead.id, new_fields=dict(seed["fields"]), performed_by="eval")
    for item in seed.get("memories") or []:
        memory.add_customer_memory(customer.id, MemoryKind(item["kind"]), item["summary"],
                                   source=MemorySource.reflection, created_by="eval")
    if seed.get("profile"):
        memory.upsert_customer_profile(customer.id, **seed["profile"])
    return customer.id


def run_staff_case(client, db, case: dict, *, mock: bool) -> dict:
    from app.agents import grounding
    from app.agents.memory_agent import MemoryAgent
    from app.memory import recall
    from app.memory.memory_manager import MemoryManager

    memory = MemoryManager(db)
    customer_id = _seed_customer(memory, case["seed"])
    for other in case.get("others") or []:
        if not memory.find_customer(email=other["customer"]["email"]):
            _seed_customer(memory, other)
    db.commit()

    failures, skipped = [], []
    response = client.post(ASK_URL, json={"question": case["question"], "customer_id": customer_id}, headers=ADMIN)
    answer = response.json().get("answer", "") if response.status_code == 200 else ""
    if response.status_code != 200:
        failures.append(f"HTTP {response.status_code}: {response.text[:200]}")
    else:
        db.expire_all()
        from app.memory.models import Customer
        customer = db.get(Customer, customer_id)
        context = recall.retrieve(memory, customer_id, case["question"])["context_block"]
        check = grounding.check_grounding(answer, context, evidence=MemoryAgent._label(customer))
        if not (check.ok or answer.startswith("Records found for")):
            failures.append(f"invariant ungrounded: {check.describe()}")
        failures += [f"must_not matched {p!r}" for p in _matches(case["must_not"], answer)]
        if case.get("not_on_record") and not _NOT_ON_RECORD.search(answer):
            failures.append("expected a not-on-record answer")
        if case["must_any"]:
            if mock:
                skipped.append("must_any (mock answers)")
            elif not _matches(case["must_any"], answer):
                failures.append(f"answer matched none of must_any {case['must_any']}")
    audit = [a.action for a in memory.list_audit_logs(entity_type="customer", entity_id=str(customer_id))
             if a.action == grounding.UNGROUNDED_ANSWER]
    return {"id": case["id"], "category": case["category"], "blocking": bool(case.get("blocking", True)),
            "passed": not failures, "failures": failures, "skipped_checks": skipped, "answer": answer,
            "ungrounded_fallbacks": len(audit)}


# ── Nova Records cases (BE-9) ──────────────────────────────────────────────────

_FIXTURE_CONTACTS = re.compile(r"98300001\d\d|94300001\d\d|@example\.test|collect@example\.invalid")
# Archived projects' customers: never shown to Sales anywhere (config/archive.yaml).
_ARCHIVE_CUSTOMER_NAMES = ("Rina Sen", "Kabir Das", "Anita Ghosh", "Sourav Pal", "Nikhil Bose", "Amit Sen",
                           "Pooja Roy", "Haren Mondal", "Ravi Kumar")
_MONEY_IN_TEXT = re.compile(r"₹|\brs\.?\s*\d|\binr\b|\blakh|\bcrore", re.I)
_MONEY_COLUMNS = {"contract_value_inr", "amount_received_inr", "amount_outstanding_inr", "subsidy_amount_inr",
                  "total_contract_value", "total_received", "total_outstanding"}


def seed_records_fixture(session_factory, today) -> dict:
    """The seeded live + archived projects every records case asks about."""
    from scripts.records_fixture import seed

    db = session_factory()
    try:
        return seed(db, today)
    finally:
        db.close()


def _fill(value, facts: dict):
    """`{lead:priya}`, `{customer:GNE-…}`, `{onboarding:GNE-…}` in a question or context."""
    if isinstance(value, dict):
        return {k: _fill(v, facts) for k, v in value.items()}
    if not isinstance(value, str):
        return value

    def sub(m):
        kind, key = m.group(1), m.group(2)
        if kind == "lead":
            return str(facts["leads"][key]["lead_id"])
        field = {"customer": "customer_id", "onboarding": "onboarding_id"}[kind]
        return str(facts["live"][key][field])
    whole = re.fullmatch(r"\{(lead|customer|onboarding):([^}]+)\}", value)
    if whole:
        return int(sub(whole))
    return re.sub(r"\{(lead|customer|onboarding):([^}]+)\}", sub, value)


def _numbers(text: str) -> set:
    return {float(n.replace(",", "")) for n in re.findall(r"\d[\d,]*(?:\.\d+)?", text or "")}


def _records_invariants(role: str, body: dict) -> list[str]:
    """What every answer to this role must satisfy, whatever the case."""
    from app.records import access
    from types import SimpleNamespace
    from app.memory.models import EmployeeRole

    who = SimpleNamespace(role=EmployeeRole(role), employee_id=f"emp-{role}-1")
    text = json.dumps(body, ensure_ascii=False)
    found = []
    # Customer memory and leads show what /memory/ask and the lead pages show the role;
    # a list price is shown to the roles that see /pricing/breakdown (compose.money_visible).
    tools_used = {s["tool"] for s in body.get("searched", [])}
    memory = bool(tools_used & {"customer_records", "lead_lookup"})
    price = "catalogue_lookup" in tools_used and access.sees_prices(who)
    if not (access.sees_money(who) or memory or price):
        if _MONEY_IN_TEXT.search(body.get("answer", "")):
            found.append("invariant money shown to a role without money")
        if any(c["key"] in _MONEY_COLUMNS for t in body.get("tables", []) for c in t["columns"]):
            found.append("invariant money column shown to a role without money")
    if not (access.sees(who, "customer_phone") or memory) and _FIXTURE_CONTACTS.search(text):
        found.append("invariant contact details shown to a role without contact access")
    if not access.sees(who, "customer_name") and role == "sales":
        leaked = [n for n in _ARCHIVE_CUSTOMER_NAMES if n in text]
        if leaked:
            found.append(f"invariant customer names shown to Sales: {leaked}")
    for marker in ("fixture-not-a-pan", "fixture-not-an-account", "FIXT0000000", "collect@example.invalid"):
        if marker in text:
            found.append(f"invariant leaked {marker}")
    if body.get("status") == "refused" and (body.get("tables") or body.get("citations")):
        found.append("invariant a refusal carried records")
    return found


def run_records_case(client, db, case: dict, *, mock: bool, facts: Optional[dict] = None) -> dict:
    from sqlalchemy import text as sql

    failures, skipped = [], []
    n = int(case.get("employee", 1))
    headers = {"X-Employee-Id": f"emp-{case['role']}-{n}", "X-Employee-Role": case["role"]}
    body = {"question": _fill(case["question"], facts or {})}
    if case.get("context"):
        body["context"] = _fill(case["context"], facts or {})
    response = client.post(RECORDS_URL, json=body, headers=headers)
    result = response.json() if response.status_code == 200 else {}
    answer = result.get("answer", "")
    if response.status_code != 200:
        failures.append(f"HTTP {response.status_code}: {response.text[:200]}")
    else:
        if result["status"] != case["status"]:
            failures.append(f"status {result['status']}, expected {case['status']}")
        cited = [c["ref"] for c in result["citations"]]
        missing = [r for r in (str(_fill(c, facts or {})) for c in case["cites"]) if r not in cited]
        if missing:
            failures.append(f"did not cite {missing} (cited {cited})")
        for forbidden in case.get("not_cites") or []:
            if forbidden in cited or forbidden in json.dumps(result.get("tables", [])):
                failures.append(f"showed {forbidden}")
        failures += _records_invariants(case["role"], result)
        failures += [f"must_not matched {p!r}" for p in _matches(case["must_not"], answer)]
        for item in case["truth"]:
            value = db.execute(sql(item["sql"])).scalar()
            if value is None or float(value) not in _numbers(answer):
                failures.append(f"SQL truth {value} ({item['sql'][:60]}…) is not in the answer")
        if case["must_any"]:
            if mock:
                skipped.append("must_any (mock answers)")
            elif not _matches(case["must_any"], answer):
                failures.append(f"answer matched none of must_any {case['must_any']}")
    return {"id": case["id"], "category": case["category"], "role": case["role"],
            "blocking": bool(case.get("blocking", True)), "passed": not failures, "failures": failures,
            "skipped_checks": skipped, "status": result.get("status"), "answer": answer,
            "citations": [c["ref"] for c in result.get("citations", [])]}


RUNNERS = {"customer": run_customer_case, "staff": run_staff_case, "records": run_records_case}


def run_suite(name: str, client, session_factory, *, mock: bool, only: Optional[str] = None) -> list[dict]:
    results = []
    extra = {}
    if name == "records":
        from app.records import store
        extra["facts"] = seed_records_fixture(session_factory, store.ist_today())
    for case in load_suite(name):
        if only and only not in case["id"]:
            continue
        db = session_factory()
        try:
            results.append(RUNNERS[name](client, db, case, mock=mock, **extra))
        except Exception as exc:  # one broken case must not hide the rest
            results.append({"id": case["id"], "category": case["category"], "blocking": True, "passed": False,
                            "failures": [f"crashed: {type(exc).__name__}: {exc}"], "skipped_checks": []})
        finally:
            db.close()
    return results


# ── Command line ───────────────────────────────────────────────────────────────

def _prepare_environment(mock: bool) -> Path:
    """A throwaway database and hermetic settings, set BEFORE the app is imported."""
    workdir = Path(tempfile.mkdtemp(prefix="nova-eval-"))
    os.environ["DATABASE_URL"] = f"sqlite:///{workdir / 'eval.db'}"
    os.environ["ENVIRONMENT"] = "development"   # dev header auth for the staff route
    for key in ("SENTRY_DSN", "SLACK_WEBHOOK_URL", "CLERK_SECRET_KEY", "CLERK_JWT_ISSUER"):
        os.environ[key] = ""
    if mock:
        os.environ["LLM_API_KEY"] = ""
    return workdir


def _print_table(results: list[dict]) -> None:
    width = max([len(r["id"]) for r in results] + [4])
    print(f"{'case'.ljust(width)}  result  blocking  notes")
    for r in results:
        note = "; ".join(r["failures"])[:110] or ("skipped: " + ", ".join(r["skipped_checks"]) if r["skipped_checks"] else "")
        print(f"{r['id'].ljust(width)}  {'PASS' if r['passed'] else 'FAIL'}    {'yes' if r['blocking'] else 'no '}       {note}")


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--suite", choices=sorted(SUITES), required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--live", action="store_true", help="run against the configured LLM (needs LLM_API_KEY)")
    mode.add_argument("--mock", action="store_true", help="mock-mode smoke run of the harness itself")
    parser.add_argument("--case", help="only cases whose id contains this text")
    args = parser.parse_args(argv)
    if not (args.live or args.mock):
        print("Refusing to run: pass --live (real model, needs LLM_API_KEY) or --mock (harness smoke run).",
              file=sys.stderr)
        return 2

    workdir = _prepare_environment(mock=args.mock)
    from app.config import settings
    if args.live and settings.llm_mock_mode:
        print("Refusing to run --live: LLM_API_KEY is not set (environment or .env).", file=sys.stderr)
        return 2

    import app.jobs.runner as runner
    runner.INLINE = True
    from fastapi.testclient import TestClient

    import app.memory.models as _models  # noqa: F401 — register every table
    from app.api.routes import chat as chat_routes
    from app.main import app as fastapi_app
    from app.memory.db import Base, SessionLocal, engine

    Base.metadata.create_all(bind=engine)
    # One process sends every case from one address; the per-IP limit would 429 it.
    chat_routes._check_rate_limit = lambda request: None
    # …and every records case from a handful of employees (NR-1 is per employee per minute).
    from app.records import store as records_store
    records_store.within_rate_limit = lambda employee_id: True

    print(f"Nova eval — suite={args.suite} mode={'LIVE ' + settings.llm_model if args.live else 'MOCK'} "
          f"db={workdir / 'eval.db'}")
    # No lifespan: the app's startup jobs have no business running against a throwaway DB.
    client = TestClient(fastapi_app)
    results = run_suite(args.suite, client, SessionLocal, mock=args.mock, only=args.case)
    _print_table(results)

    failed = [r for r in results if not r["passed"]]
    blocking_failed = [r for r in failed if r["blocking"]]
    summary = {"suite": args.suite, "mode": "live" if args.live else "mock",
               "model": settings.llm_model if args.live else None,
               "run_at": datetime.now(timezone.utc).isoformat(), "cases": len(results),
               "passed": len(results) - len(failed), "failed": len(failed),
               "blocking_failed": len(blocking_failed), "results": results}
    REPORT_DIR.mkdir(exist_ok=True)
    path = REPORT_DIR / f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{args.suite}-{summary['mode']}.json"
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n{summary['passed']}/{len(results)} passed, {len(blocking_failed)} blocking failure(s). Report: {path}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
