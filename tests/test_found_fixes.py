"""Fixes for issues found during review: the background PDF job never changes a
proposal's status; oversized uploads are refused while they arrive; Engineers don't
see commercial fields of a site assessment."""
import asyncio
import io
import json

import pytest

import app.notifications.notifier as notifier_module
from app.memory.models import ProposalStatus
from app.notifications.notifier import EmailNotifier, LogNotifier
from app.security.upload_limit import MULTIPART_OVERHEAD_BYTES, UploadSizeLimitMiddleware
from tests.conftest import (
    STORAGE_URL_PREFIX, admin_headers, engineer_headers, management_headers, sales_headers,
)

DASH = "/api/v1/dashboard"
INPUTS = "/api/v1/proposal-inputs"


# ── a. PDF job vs. status ───────────────────────────────────────────────────────

@pytest.fixture
def held_pdf_job(monkeypatch):
    """Capture the background PDF job instead of running it, so a test can do other
    things 'while it renders' and then let it finish."""
    jobs = []
    monkeypatch.setattr("app.api.routes.dashboard._start_pdf_generation",
                        lambda generate_fn, proposal_id: jobs.append(generate_fn))
    monkeypatch.setattr("app.api.routes.dashboard.generate_and_upload_pdf",
                        lambda **kwargs: STORAGE_URL_PREFIX + f"proposals/{kwargs['proposal_id']}/p.pdf")
    return jobs


def _drafted(memory, db_session, email):
    customer = memory.get_or_create_customer(email=email, name="R")
    lead = memory.create_lead(customer_id=customer.id)
    p = memory.create_proposal_draft(lead_id=lead.id, content={}, total_value=150_000,
                                     requires_manager_signoff=False, performed_by="emp-1")
    db_session.commit()
    return p.id


@pytest.mark.usefixtures("known_employees", "fake_storage")
class TestPdfJobNeverChangesStatus:
    def test_sent_during_generation_stays_sent(self, client, memory, db_session, held_pdf_job, monkeypatch):
        mails = []
        monkeypatch.setattr(notifier_module.httpx, "post",
                            lambda *a, **k: mails.append(k["json"]) or type("R", (), {"status_code": 200})())
        monkeypatch.setattr("app.api.routes.dashboard.notifier", EmailNotifier(
            api_key="re_x", from_email="n@gridnova.in", staff_emails=[], frontend_base_url="https://x",
            inner=LogNotifier()))
        pid = _drafted(memory, db_session, "race@example.com")
        assert client.post(f"{DASH}/proposals/{pid}/approve", headers=admin_headers()).status_code == 200
        assert client.post(f"{DASH}/proposals/{pid}/send", headers=admin_headers()).status_code == 200
        assert len(mails) == 1

        [job] = held_pdf_job
        job()  # the PDF finishes after the send

        db_session.expire_all()
        proposal = memory.get_proposal(pid)
        assert proposal.status == ProposalStatus.sent
        assert proposal.pdf_storage_key == f"proposals/{pid}/p.pdf"
        assert len(mails) == 1  # the job sends nothing
        actions = [l.action for l in memory.list_audit_logs(entity_type="proposal", entity_id=str(pid))]
        assert "pdf_generated" in actions
        # Only the approval itself moved the status to approved — never the PDF job.
        assert sum("status_changed_to" in a and "approved" in a for a in actions) == 1

    def test_revision_requested_during_generation_is_kept(self, client, memory, db_session, held_pdf_job):
        pid = _drafted(memory, db_session, "rev@example.com")
        client.post(f"{DASH}/proposals/{pid}/approve", headers=admin_headers())
        assert client.post(f"{DASH}/proposals/{pid}/request-revision", json={"reason": "wrong roof"},
                           headers=admin_headers()).status_code == 200
        held_pdf_job[0]()
        db_session.expire_all()
        assert memory.get_proposal(pid).status == ProposalStatus.revision_requested

    def test_normal_approval_still_gets_its_pdf(self, client, memory, db_session, held_pdf_job):
        pid = _drafted(memory, db_session, "ok@example.com")
        client.post(f"{DASH}/proposals/{pid}/approve", headers=admin_headers())
        held_pdf_job[0]()
        db_session.expire_all()
        proposal = memory.get_proposal(pid)
        assert proposal.status == ProposalStatus.approved and proposal.pdf_url


# ── b. upload size ──────────────────────────────────────────────────────────────

def _run_middleware(headers, chunks):
    """Drive UploadSizeLimitMiddleware directly; returns (status, body, app_finished)."""
    finished = {"done": False}

    async def app(scope, receive, send):
        while True:
            message = await receive()
            if not message.get("more_body"):
                break
        finished["done"] = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    queue = [{"type": "http.request", "body": c, "more_body": i < len(chunks) - 1} for i, c in enumerate(chunks)]

    async def receive():
        return queue.pop(0)

    out = []

    async def send(message):
        out.append(message)

    scope = {"type": "http", "method": "POST", "path": "/api/v1/proposal-inputs/1/upload",
             "headers": [(k.encode(), v.encode()) for k, v in headers.items()]}
    asyncio.run(UploadSizeLimitMiddleware(app)(scope, receive, send))
    status = next(m["status"] for m in out if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in out if m["type"] == "http.response.body")
    return status, body, finished["done"]


class TestUploadLimit:
    LIMIT = 10 * 1024 * 1024

    def test_declared_content_length_over_the_limit_is_refused_before_reading(self):
        status, body, finished = _run_middleware(
            {"content-length": str(self.LIMIT + MULTIPART_OVERHEAD_BYTES + 1)}, [b"x"])
        assert status == 413 and not finished
        assert json.loads(body) == {"detail": "File exceeds the 10MB limit."}

    def test_undeclared_body_is_cut_off_as_soon_as_it_passes_the_limit(self):
        chunk = b"x" * (1024 * 1024)
        status, _, finished = _run_middleware({}, [chunk] * 12)
        assert status == 413 and not finished

    def test_within_the_limit_passes_through(self):
        status, body, finished = _run_middleware({"content-length": "1000"}, [b"x" * 1000])
        assert status == 200 and finished and body == b"ok"

    def test_other_paths_are_untouched(self):
        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": b""})
        out = []

        async def send(m):
            out.append(m)
        scope = {"type": "http", "method": "POST", "path": "/api/v1/chat/message",
                 "headers": [(b"content-length", b"999999999")]}
        asyncio.run(UploadSizeLimitMiddleware(app)(scope, None, send))
        assert out[0]["status"] == 204

    def _inputs(self, client, memory, db_session):
        customer = memory.get_or_create_customer(email="up@example.com", name="U")
        lead = memory.create_lead(customer_id=customer.id)
        db_session.commit()
        return client.post(f"{INPUTS}/", headers=sales_headers(), json={
            "lead_id": lead.id, "customer_type": "individual", "project_family": "rooftop_solar",
            "state": "West Bengal", "district": "Kolkata", "pin_code": "700001",
            "site_type": "rooftop_residential", "connection_type": "on_grid"}).json()["id"]

    def _upload(self, client, inputs_id, size):
        return client.post(f"{INPUTS}/{inputs_id}/upload", headers=sales_headers(),
                           data={"target": "document", "document_type": "bill"},
                           files={"file": ("f.pdf", io.BytesIO(b"x" * size), "application/pdf")})

    def test_file_one_byte_over_ten_mb_is_413(self, client, memory, db_session, fake_storage):
        r = self._upload(client, self._inputs(client, memory, db_session), self.LIMIT + 1)
        assert r.status_code == 413 and r.json() == {"detail": "File exceeds the 10MB limit."}
        assert not any(c.put for c in fake_storage)

    def test_file_of_exactly_ten_mb_is_accepted(self, client, memory, db_session, fake_storage):
        r = self._upload(client, self._inputs(client, memory, db_session), self.LIMIT)
        assert r.status_code == 200

    def test_far_oversized_body_is_refused_by_content_length(self, client, memory, db_session, fake_storage):
        r = self._upload(client, self._inputs(client, memory, db_session), self.LIMIT + 2 * MULTIPART_OVERHEAD_BYTES)
        assert r.status_code == 413 and r.json() == {"detail": "File exceeds the 10MB limit."}
        assert not any(c.put for c in fake_storage)


# ── c. Engineer view of a site assessment ───────────────────────────────────────

class TestEngineerAssessmentView:
    def _inputs(self, client, memory, db_session):
        customer = memory.get_or_create_customer(email="eng@example.com", name="E")
        lead = memory.create_lead(customer_id=customer.id)
        db_session.commit()
        inputs_id = client.post(f"{INPUTS}/", headers=admin_headers(), json={
            "lead_id": lead.id, "customer_type": "organisation", "organisation_name": "Acme Mills",
            "gst_number": "19ABCDE1234F1Z5", "project_family": "rooftop_solar",
            "state": "West Bengal", "district": "Kolkata", "pin_code": "700001",
            "full_address": "12 Park Street", "site_type": "rooftop_commercial", "connection_type": "on_grid",
            "avg_monthly_bill_inr": 42000, "consumption_history": [{"month": "2026-07", "units_kwh": 4100}],
            }).json()["id"]
        # Staff notes are written during review, not on submission.
        memory.get_proposal_inputs(inputs_id).internal_notes = "Owner negotiating hard on price"
        db_session.commit()
        return inputs_id

    def test_engineer_does_not_see_commercial_fields(self, client, memory, db_session):
        inputs_id = self._inputs(client, memory, db_session)
        for body in (client.get(f"{INPUTS}/{inputs_id}", headers=engineer_headers()).json(),
                     client.get(f"{INPUTS}/", params={"status": "submitted"},
                                headers=engineer_headers()).json()["items"][0]):
            assert "gst_number" not in body and "internal_notes" not in body
            assert "19ABCDE1234F1Z5" not in json.dumps(body) and "negotiating" not in json.dumps(body)
            assert body["full_address"] == "12 Park Street"
            assert body["avg_monthly_bill_inr"] == 42000 and body["consumption_history"]

    @pytest.mark.parametrize("headers", [admin_headers(), management_headers(), sales_headers()])
    def test_other_roles_unchanged(self, client, memory, db_session, headers):
        inputs_id = self._inputs(client, memory, db_session)
        body = client.get(f"{INPUTS}/{inputs_id}", headers=headers).json()
        assert body["gst_number"] == "19ABCDE1234F1Z5"
        assert body["internal_notes"] == "Owner negotiating hard on price"
