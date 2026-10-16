"""Evaluate Nova against the real model — BE-7 N4.

    python scripts/eval_nova.py --suite customer --live     # the real model (needs LLM_API_KEY)
    python scripts/eval_nova.py --suite staff --live
    python scripts/eval_nova.py --suite customer --mock     # harness smoke run, no model

Every test in tests/ runs in mock mode, so the configured model's actual behaviour is
only measured here. Run it on the staging key before Gate 1 sign-off and before
go-live: a launch requires 100% pass on blocking cases.

It drives the real HTTP routes (POST /api/v1/chat/message, POST
/api/v1/dashboard/memory/ask) in-process against a THROWAWAY SQLite database in a
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
          "staff": REPO_ROOT / "evals" / "staff_memory.yaml"}
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


RUNNERS = {"customer": run_customer_case, "staff": run_staff_case}


def run_suite(name: str, client, session_factory, *, mock: bool, only: Optional[str] = None) -> list[dict]:
    results = []
    for case in load_suite(name):
        if only and only not in case["id"]:
            continue
        db = session_factory()
        try:
            results.append(RUNNERS[name](client, db, case, mock=mock))
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
