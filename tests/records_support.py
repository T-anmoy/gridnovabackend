"""Builders for the BE-9 Nova Records tests: the seeded fixture (scripts/records_fixture.py),
callers per role, and a fake model for the planner/composer paths."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.memory.models import EmployeeRole
from app.records import store

ASK = "/api/v1/dashboard/nova/ask"
BASE = "/api/v1/dashboard/nova"
ROLES = ("admin", "management", "accounts", "operations", "engineer", "sales")


def who(role: str, n: int = 1) -> dict:
    return {"X-Employee-Id": f"emp-{role}-{n}", "X-Employee-Role": role}


def viewer(role: str, n: int = 1):
    return SimpleNamespace(role=EmployeeRole(role), employee_id=f"emp-{role}-{n}")


@pytest.fixture
def today():
    return store.ist_today()


@pytest.fixture
def records(db_session, today):
    from scripts.records_fixture import seed
    return seed(db_session, today)


def ask(client, question: str, role: str = "admin", n: int = 1, **body):
    r = client.post(ASK, json={"question": question, **body}, headers=who(role, n))
    assert r.status_code == 200, r.text
    return r.json()


def run(db, role: str, tool: str, args: dict, today, n: int = 1):
    from app.records import tools
    return tools.run(db, viewer(role, n), tool, args, today)


def refs(result: dict) -> list[str]:
    return [r["ref"] for r in result["refs"]]


class FakeModel:
    """Stands in for records_plan / records_answer: returns the queued texts in order
    (None = the model is unavailable) and records every call."""

    def __init__(self, plans=(), answers=()):
        self.plans, self.answers = list(plans), list(answers)
        self.plan_calls, self.answer_calls = [], []

    def records_plan(self, **kwargs):
        self.plan_calls.append(kwargs)
        text = self.plans.pop(0) if self.plans else None
        return None if text is None else {"text": text, "model": "fake-model", "prompt_tokens": 100,
                                          "completion_tokens": 20}

    def records_answer(self, **kwargs):
        self.answer_calls.append(kwargs)
        text = self.answers.pop(0) if self.answers else None
        return None if text is None else {"text": text, "model": "fake-model", "prompt_tokens": 300,
                                          "completion_tokens": 60}


@pytest.fixture
def fake_model(monkeypatch):
    from app.llm import client as llm

    fake = FakeModel()
    monkeypatch.setattr(llm.llm_client, "records_plan", fake.records_plan)
    monkeypatch.setattr(llm.llm_client, "records_answer", fake.records_answer)
    return fake
