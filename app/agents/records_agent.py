"""
Nova Records — the internal assistant that answers staff questions from records (BE-9).

ARCHITECTURAL INVARIANT: like every other agent here, this module never imports another
agent. It reads records through app/records/tools.py and shares only libraries
(app/agents/grounding.py, app/projects/view.py, app/memory/recall.py).

WHO IT TALKS TO
Signed-in staff only, through /api/v1/dashboard/nova. It reads across every record a role
may see, so it is never wired into the public chat. Each answer is scoped to the asking
employee: the tools apply app/auth/scoping.py and the config/archive.yaml field map
before anything reaches a prompt.

THE MODEL NEVER TOUCHES THE DATABASE AND NEVER DOES ARITHMETIC
1. Screen (app/records/safety.py): requests to change data, send messages, reveal identity
   documents or the instructions are refused deterministically.
2. Plan: the model picks at most three read-only tools (operation records_plan); a plan
   that fails validation gets one repair attempt, then the employee is asked to rephrase.
   Mock mode or a failed call → the deterministic keyword router (app/records/router.py).
3. Execute: tools run with row caps and a statement timeout (Postgres), each in a savepoint.
4. Compose: the model phrases a short answer from the results (operation records_answer);
   tables are built from the rows, never written by the model.
5. Verify (grounding.check_records_answer): cited refs exist, every fact is in the
   results, every total/count is one a tool computed, nothing the role may not see.
   Failure → one retry with the violations → else the deterministic templated answer,
   and the attempt is audit-logged (records_answer_unverified).
Nothing it does is ever an action: there are no write tools.
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import date
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.agents import grounding
from app.llm.client import llm_client
from app.memory.models import AuditLog
from app.records import access, compose, router, safety, tools
from app.records import config as records_config

logger = logging.getLogger(__name__)

ANSWERED = "answered"
NOT_ON_RECORD = "not_on_record"
NEEDS_CLARIFICATION = "needs_clarification"
REFUSED = "refused"

MAX_QUESTION_CHARS = 1000
GENERIC_CLARIFICATION = ("I couldn't work out how to look that up. Could you rephrase it with a project reference, "
                         "a lead number, a customer name, a place, a make or a date range?")


class PlannedCall(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tool: str
    args: dict = Field(default_factory=dict)


class Plan(BaseModel):
    model_config = ConfigDict(extra="ignore")
    intent: str = Field("lookup", max_length=64)
    tool_calls: list[PlannedCall] = Field(default_factory=list)
    clarification: Optional[str] = Field(None, max_length=500)


def _strip_fences(raw: str) -> str:
    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.S)
    return raw.strip()


def _numbers(text_: str) -> set:
    return {float(n) for n in re.findall(r"-?\d+(?:\.\d+)?", text_ or "")}


def validate_plan(raw: str, question: str, max_calls: int) -> tuple[Optional[dict], list[str]]:
    """Parse and validate a model plan. Returns (plan, errors); plan is None on errors."""
    errors: list[str] = []
    try:
        data = json.loads(_strip_fences(raw))
        plan = Plan.model_validate(data)
    except (ValueError, ValidationError) as exc:
        return None, [f"the plan is not valid JSON of the required shape: {str(exc)[:300]}"]
    if len(plan.tool_calls) > max_calls:
        errors.append(f"at most {max_calls} tool_calls")
    calls = []
    asked = _numbers(question)
    for i, call in enumerate(plan.tool_calls[:max_calls]):
        spec = tools.TOOLS.get(call.tool)
        if spec is None:
            errors.append(f"tool_calls[{i}]: unknown tool {call.tool!r}; use one of {sorted(tools.TOOLS)}")
            continue
        try:
            args = spec["args"].model_validate(call.args).model_dump(mode="json", exclude_none=True)
        except ValidationError as exc:
            errors.append(f"tool_calls[{i}] ({call.tool}): bad args: {exc.errors()[:3]}")
            continue
        near = (args.get("filters") or {}).get("near") or {}
        for key in ("lat", "lng"):
            if key in near and not any(abs(near[key] - n) < 1e-6 for n in asked):
                errors.append(f"tool_calls[{i}]: coordinates must come from the question; use near.place instead")
                break
        calls.append({"tool": call.tool, "args": args})
    if not calls and not plan.clarification and not errors:
        errors.append("no tool_calls and no clarification")
    if errors:
        return None, errors
    clarification = plan.clarification
    if clarification and any(grounding.extract_facts(clarification)[k] for k in ("money", "emails", "phones")):
        clarification = GENERIC_CLARIFICATION   # a clarifying question states no facts
    return {"intent": plan.intent, "tool_calls": calls, "clarification": clarification if not calls else None}, []


class RecordsAgent:
    def answer(self, db: Session, viewer, *, question: str, context: Optional[dict] = None,
               history: Optional[list] = None, today: date, turn_id: str = "") -> dict:
        """Authorisation happens in the harness before this method is entered."""
        started = time.perf_counter()
        question = (question or "").strip()[:MAX_QUESTION_CHARS]
        usage = {"model": None, "prompt_tokens": None, "completion_tokens": None}
        out = self._answer(db, viewer, question, context or {}, history or [], today, turn_id, usage)
        results = out.pop("results")
        out.update(usage)
        out.update({
            "citations": compose.citations(out["answer"], results),
            "tables": compose.tables(results) if out["status"] != REFUSED else [],
            "searched": compose.searched_list(results),
            "suggestions": compose.suggestions(results, viewer, out["status"]) if out["status"] != REFUSED else [],
            "latency_ms": int((time.perf_counter() - started) * 1000),
        })
        return out

    # ── The pipeline ──────────────────────────────────────────────────────────────

    def _answer(self, db, viewer, question, context, history, today, turn_id, usage) -> dict:
        category = safety.screen(question)
        if category:
            self._audit(db, viewer, turn_id, "nova_records_refused", {"category": category})
            return self._reply(REFUSED, safety.ANSWERS[category], intent=f"refused:{category}", planner=None)

        plan, planner = self._plan(db, viewer, question, context, history, today, usage)
        if not plan["tool_calls"]:
            return self._reply(NEEDS_CLARIFICATION, plan.get("clarification") or GENERIC_CLARIFICATION,
                               intent=plan["intent"], planner=planner)

        results, log, errors = self._execute(db, viewer, plan["tool_calls"], today)
        results, log = self._follow_up(db, viewer, results, log, today)
        base = {"intent": plan["intent"], "planner": planner, "tool_calls": log}

        if not results:
            refusals = [e for e in errors if e["code"] in ("not_permitted", "filter_not_permitted")]
            if refusals:
                self._audit(db, viewer, turn_id, "nova_records_refused",
                            {"category": "role_scope", "errors": refusals})
                return self._reply(REFUSED, " ".join(dict.fromkeys(e["message"] for e in refusals)), **base)
            message = next((e["message"] for e in errors if e["code"] == "invalid_argument"), GENERIC_CLARIFICATION)
            return self._reply(NEEDS_CLARIFICATION, message, **base)

        candidates = self._ambiguity(results)
        if candidates is not None:
            answer = compose.clarification_for(candidates)
            return self._reply(NEEDS_CLARIFICATION, answer, results=results, **base)

        if all(compose.is_empty(r) for r in results):
            return self._reply(NOT_ON_RECORD, compose.not_on_record_sentence(results), results=results, **base)

        answer, composer, verifier = self._compose(db, viewer, question, results, today, turn_id, usage)
        notes = [e["message"] for e in errors]
        if notes:
            answer += "\n\n" + " ".join(dict.fromkeys(notes))
        return self._reply(ANSWERED, answer, results=results, composer=composer, verifier=verifier, **base)

    def _plan(self, db, viewer, question, context, history, today, usage) -> tuple[dict, str]:
        max_calls = records_config.limit("max_tool_calls")
        kwargs = dict(question=question, role=access.role_of(viewer).value, today=today.isoformat(), context=context,
                      catalogue=tools.schemas(), history=history[-records_config.limit("history_turns"):])
        raw = llm_client.records_plan(**kwargs)
        if raw is None:
            return router.plan(db, question, viewer, context, today), "keyword"
        self._add_usage(usage, raw)
        plan, errors = validate_plan(raw["text"], question, max_calls)
        if plan is not None:
            return plan, "model"
        repaired = llm_client.records_plan(**kwargs, feedback=errors)
        if repaired is None:
            return router.plan(db, question, viewer, context, today), "keyword"
        self._add_usage(usage, repaired)
        plan, errors = validate_plan(repaired["text"], question, max_calls)
        if plan is not None:
            return plan, "model"
        logger.warning("Records plan invalid after one repair", extra={"errors": errors[:5]})
        return {"intent": "invalid_plan", "tool_calls": [], "clarification": GENERIC_CLARIFICATION}, "model"

    def _execute(self, db: Session, viewer, calls: list, today: date) -> tuple[list, list, list]:
        timeout = records_config.limit("tool_timeout_seconds")
        pg = db.get_bind().dialect.name == "postgresql"
        results, log, errors = [], [], []
        if pg:
            db.execute(text(f"SET LOCAL statement_timeout = {int(timeout * 1000)}"))
        try:
            for call in calls[:records_config.limit("max_tool_calls")]:
                entry = self._run_one(db, viewer, call, today, timeout)
                log.append(entry["log"])
                if entry.get("result") is not None:
                    results.append(entry["result"])
                if entry.get("error"):
                    errors.append(entry["error"])
        finally:
            if pg:
                db.execute(text("SET LOCAL statement_timeout TO DEFAULT"))
        return results, log, errors

    def _run_one(self, db, viewer, call, today, timeout) -> dict:
        tool, args = call["tool"], call.get("args") or {}
        clock = time.perf_counter()
        entry: dict[str, Any] = {"log": {"tool": tool, "args": args}}
        try:
            with db.begin_nested():
                result = tools.run(db, viewer, tool, args, today)
        except tools.ToolError as exc:
            entry["error"] = {"tool": tool, "code": exc.code, "message": exc.message, "field": exc.field}
        except ValidationError as exc:
            entry["error"] = {"tool": tool, "code": "invalid_argument", "message": GENERIC_CLARIFICATION,
                              "field": str(exc.errors()[:1])}
        except Exception as exc:   # a statement timeout, or a bug in one tool: never the whole answer
            logger.error("Records tool failed", exc_info=True, extra={"tool": tool})
            entry["error"] = {"tool": tool, "code": "tool_failed", "field": None,
                              "message": f"One lookup ({tool}) could not be completed: "
                                         f"{'it took too long' if 'timeout' in str(exc).lower() else 'it failed'}."}
        else:
            elapsed = time.perf_counter() - clock
            if elapsed > timeout:
                entry["error"] = {"tool": tool, "code": "tool_failed", "field": None,
                                  "message": f"One lookup ({tool}) took too long and was left out."}
            else:
                entry["result"] = result
        ms = int((time.perf_counter() - clock) * 1000)
        entry["log"].update({"ok": "result" in entry, "ms": ms,
                             **({"error": entry["error"]["code"]} if "error" in entry else {}),
                             **({"refs": len(entry["result"]["refs"])} if "result" in entry else {})})
        return entry

    def _follow_up(self, db, viewer, results: list, log: list, today: date) -> tuple[list, list]:
        """One deterministic step: a customer-name search that found exactly one project
        also fetches that project's dossier ("what did we install for Mr Sen…")."""
        if len(log) >= records_config.limit("max_tool_calls"):
            return results, log
        for r in results:
            if r["tool"] == "search_projects" and r["searched"].get("customer_name") and len(r["rows"]) == 1:
                ref = r["rows"][0]["ref"]
                if any(x["tool"] == "project_dossier" and x["searched"].get("ref") == ref for x in results):
                    break
                entry = self._run_one(db, viewer, {"tool": "project_dossier", "args": {"ref": ref}}, today,
                                      records_config.limit("tool_timeout_seconds"))
                log.append(entry["log"])
                if entry.get("result") is not None:
                    results.append(entry["result"])
                break
        return results, log

    @staticmethod
    def _ambiguity(results: list) -> Optional[list]:
        for r in results:
            if r["kind"] == "candidates" and len(r["candidates"]) > 1:
                return r["candidates"]
            if r["tool"] == "search_projects" and r["searched"].get("customer_name"):
                names = {(row.get("customer_name") or "").strip().lower() for row in r["rows"]}
                if len(names) > 1:
                    return [{"ref": row["ref"], "name": f"{row.get('customer_name')} ({row.get('district') or '—'})"}
                            for row in r["rows"]]
        return None

    def _compose(self, db, viewer, question, results, today, turn_id, usage) -> tuple[str, str, dict]:
        template = compose.template_answer(results, viewer)
        evidence = compose.evidence(results)
        computed, counts = compose.computed_values(results)
        known = {ref["ref"] for r in results for ref in r["refs"]}
        searched_text = json.dumps([r["searched"] for r in results], default=str)
        check_kwargs = dict(evidence=evidence, known_refs=known, computed=computed, counts=counts,
                            searched_text=searched_text, sees_money=compose.money_visible(viewer, results),
                            sees_contact=compose.contact_visible(viewer, results),
                            allow_no_citation=not known)
        attempts, failures, feedback = [], [], None
        for _ in range(2):
            generated = llm_client.records_answer(question=question, role=access.role_of(viewer).value,
                                                  today=today.isoformat(), evidence=evidence,
                                                  untrusted=compose.untrusted_block(results), feedback=feedback)
            if generated is None:
                break
            self._add_usage(usage, generated)
            answer = (generated["text"] or "").strip()
            check = grounding.check_records_answer(answer, **check_kwargs)
            if check.ok and answer:
                return answer, "model", {"checked": True, "ok": True, "attempts": len(attempts) + 1, "violations": []}
            attempts.append(answer[:2000])
            failures.append(check.violations)
            feedback = check.describe() or ["the answer was empty"]
        if attempts:
            logger.warning("Records answer failed verification — sending the templated answer",
                           extra={"violations": feedback})
            self._audit(db, viewer, turn_id, grounding.RECORDS_UNVERIFIED,
                        {"question": question[:500], "attempts": attempts, "violations": failures})
            return template, "template", {"checked": True, "ok": False, "attempts": len(attempts),
                                          "violations": [v for f in failures for v in f][:20]}
        return template, "template", {"checked": False, "ok": True, "attempts": 0, "violations": []}

    # ── Helpers ───────────────────────────────────────────────────────────────────

    @staticmethod
    def _add_usage(usage: dict, call: dict) -> None:
        usage["model"] = call.get("model") or usage["model"]
        for key in ("prompt_tokens", "completion_tokens"):
            if call.get(key) is not None:
                usage[key] = (usage[key] or 0) + call[key]

    @staticmethod
    def _audit(db: Session, viewer, turn_id: str, action: str, after: dict) -> None:
        db.add(AuditLog(entity_type="nova_turn", entity_id=turn_id or "-", action=action,
                        performed_by=getattr(viewer, "employee_id", "employee"),
                        after_value={"role": access.role_of(viewer).value, **after}))

    @staticmethod
    def _reply(status: str, answer: str, *, results: Optional[list] = None, intent: str = "", planner=None,
               composer: Optional[str] = None, verifier: Optional[dict] = None, tool_calls=None) -> dict:
        results = results or []
        return {
            "status": status,
            "answer": answer,
            "results": results,
            "intent": intent,
            "planner": planner,
            "composer": composer or ("template" if results else None),
            "verifier": verifier,
            "tool_calls": tool_calls or [],
            "refs": list(dict.fromkeys(ref["ref"] for r in results for ref in r["refs"])),
        }


records_agent = RecordsAgent()
