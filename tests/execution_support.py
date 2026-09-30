"""Builders for the Phase 4A execution tests.

Customers are onboarded through the real API; the sales stages up to agreement_signed
are then recorded through app/onboarding/progress.record — the same path the real
actions use, and the one that starts execution — so each test does not have to replay
the whole proposal → acceptance → stamp-paper flow (tests/test_proposal_request_agreement.py
covers that flow; TestStart below also drives the real signed-agreement upload once).
"""
from __future__ import annotations

import io
from datetime import datetime, timedelta

import pytest
from PIL import Image

from app.memory.models import AssociateCapability, CustomerOnboarding, ProjectStage
from app.onboarding import progress
from tests.onboarding_support import (  # noqa: F401 — fixtures re-exported
    ADMIN, ALIPURDUAR, ALIPURDUAR_I, ALIPURDUAR_II, ARARIA, ARARIA_BLOCK, BASE, DASH, ENG, KOLKATA, MGMT, SALES,
    headers, kyc_ready, make_associate, onboarding_body, onboarding_ready, territories,
)

OPS = {"X-Employee-Id": "emp-ops-1", "X-Employee-Role": "operations"}
ACCOUNTS = {"X-Employee-Id": "emp-acc-1", "X-Employee-Role": "accounts"}
QUEUE = f"{DASH}/execution"
_n = iter(range(1, 10_000))


def exec_url(oid: int) -> str:
    return f"{BASE}/{oid}/execution"


def staff_exec_url(oid: int) -> str:
    return f"{DASH}/onboardings/{oid}/execution"


def new_customer(client, gna: str, **kw) -> int:
    r = client.post(BASE, json=onboarding_body(n=next(_n), **kw), headers=headers(gna))
    assert r.status_code == 201, r.text
    return r.json()["onboarding_id"]


def advance_to(db, oid: int, stage: ProjectStage, by: str = "emp-admin-1") -> None:
    """Record every applicable stage up to and including `stage`, in order."""
    onboarding = db.get(CustomerOnboarding, oid)
    stages = progress.applicable(db, onboarding)
    done = progress.recorded(db, onboarding.project_id)
    for s in stages[: stages.index(stage) + 1]:
        if s not in done:
            progress.record(db, onboarding, s, by, evidence={"test_setup": True})
    db.commit()


def associate_in_execution(client, db, *, capability=AssociateCapability.execution, level=None, territory=None,
                           **customer) -> tuple[str, int]:
    gna = make_associate(db, capability=capability, level=level, territory=territory)
    oid = new_customer(client, gna, **customer)
    advance_to(db, oid, ProjectStage.agreement_signed)
    return gna, oid


def jpeg(*, taken_at: datetime | None = None, gps: tuple[float, float] | None = None, seed: int = 0) -> bytes:
    """A tiny real JPEG, optionally with EXIF DateTimeOriginal and GPS. `seed` changes
    the pixels so two images hash differently."""
    img = Image.new("RGB", (8, 8), (seed % 256, (seed * 7) % 256, 90))
    exif = Image.Exif()
    if taken_at is not None:
        exif.get_ifd(0x8769)[36867] = taken_at.strftime("%Y:%m:%d %H:%M:%S")
    if gps is not None:
        def dms(v):
            v = abs(v)
            d = int(v)
            m = int((v - d) * 60)
            s = round(((v - d) * 60 - m) * 60, 2)
            return (float(d), float(m), float(s))
        exif.get_ifd(0x8825).update({1: "N" if gps[0] >= 0 else "S", 2: dms(gps[0]),
                                     3: "E" if gps[1] >= 0 else "W", 4: dms(gps[1])})
    out = io.BytesIO()
    img.save(out, format="JPEG", exif=exif.tobytes() if (taken_at or gps) else b"")
    return out.getvalue()


PDF = b"%PDF-1.4\n" + b"%" * 64
CAMERA = {"capture_method": "camera", "latitude": "26.49", "longitude": "89.52",
          "captured_at": "2026-09-29T10:00:00+05:30"}


def upload(client, who: dict, url: str, *, photo_slot=None, document_type=None, content=None, **form):
    kind = "photo" if photo_slot else "document"
    data = {"kind": kind, **({"photo_slot": photo_slot} if photo_slot else {"document_type": document_type}), **form}
    body = content if content is not None else (jpeg(seed=sum(map(ord, photo_slot))) if photo_slot else PDF)
    name = "file.pdf" if body.startswith(b"%PDF") else "file.jpg"
    return client.post(f"{url}/files", data=data, files={"file": (name, body, "application/octet-stream")},
                       headers=who)


def days_ago(db, oid: int, stage: ProjectStage, days: int) -> None:
    from sqlalchemy import select
    from app.memory.models import ProjectStageEvent

    onboarding = db.get(CustomerOnboarding, oid)
    event = db.execute(select(ProjectStageEvent).where(ProjectStageEvent.project_id == onboarding.project_id,
                                                       ProjectStageEvent.stage == stage)).scalar_one()
    event.recorded_at = event.recorded_at - timedelta(days=days)
    db.commit()


class Recorder:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def notify(self, event_type, payload):
        self.events.append((event_type, payload))

    def send_associate_email(self, **kwargs):
        self.events.append(("associate_email", kwargs))
        return True

    def send_receipt_to_customer(self, **kwargs):
        self.events.append(("receipt_customer_email", kwargs))
        return True, None

    def __getattr__(self, name):   # the named helpers (new_lead, …) all route to notify
        return lambda *a, **k: None

    def of(self, event_type):
        return [p for e, p in self.events if e == event_type]


@pytest.fixture
def notifications(monkeypatch):
    from app.notifications import notifier as notifier_module
    rec = Recorder()
    monkeypatch.setattr(notifier_module, "notifier", rec)
    return rec
