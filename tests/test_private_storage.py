"""Private storage: the bucket is never linked to directly. Staff and customers get
short-lived presigned GET links (boto3 mocked by the fake_storage fixture)."""
import io
import logging
from datetime import datetime, timedelta, timezone

import pytest

from app.config import settings
from app.memory.memory_manager import MemoryManager
from app.memory.models import ProposalStatus
from app.storage import r2
from tests.conftest import STORAGE_URL_PREFIX, admin_headers, engineer_headers

DASH = "/api/v1/dashboard"
SECRET = "SECRET-TEST-DO-NOT-LEAK"


def _proposal(memory, db_session, *, pdf_url=None, email="st@example.com", send=True):
    customer = memory.get_or_create_customer(email=email, name="S")
    lead = memory.create_lead(customer_id=customer.id)
    p = memory.create_proposal_draft(lead_id=lead.id, content={}, total_value=100_000,
                                     requires_manager_signoff=False, performed_by="emp-1")
    memory.transition_proposal_status(proposal_id=p.id, new_status=ProposalStatus.approved,
                                      performed_by="emp-1", approved_by_employee_id="emp-admin-1",
                                      pdf_url=pdf_url)
    token = None
    if send:  # tokens exist only for a sent proposal (BE-1 S5: nothing else is on offer)
        memory.transition_proposal_status(proposal_id=p.id, new_status=ProposalStatus.sent, performed_by="emp-1",
                                          date_sent=datetime.now(timezone.utc))
        token = memory.generate_accept_token(p.id, performed_by="emp-1")
    db_session.commit()
    return p.id, token


class TestSigning:
    def test_signed_url_uses_the_configured_ttl_and_only_get(self, fake_storage, monkeypatch):
        monkeypatch.setattr(settings, "storage_signed_url_ttl_seconds", 900)
        before = datetime.now(timezone.utc)
        url, expires_at = r2.signed_get_url("proposals/7/a.pdf", filename="P-7.pdf")
        [client] = fake_storage
        [call] = client.presigned
        assert call["operation"] == "get_object" and call["expires_in"] == 900
        assert call["params"]["Bucket"] == "gridnova-test" and call["params"]["Key"] == "proposals/7/a.pdf"
        assert call["params"]["ResponseContentDisposition"] == 'inline; filename="P-7.pdf"'
        assert client.client_kwargs["region_name"] == "auto"
        assert client.client_kwargs["config"].signature_version == "s3v4"
        assert timedelta(seconds=899) <= expires_at - before <= timedelta(seconds=902)
        assert SECRET not in url

    def test_attachment_disposition_and_filename_sanitised(self, fake_storage):
        r2.signed_get_url("k", filename='bad"name\\\n.pdf', as_attachment=True)
        disposition = fake_storage[-1].presigned[-1]["params"]["ResponseContentDisposition"]
        assert disposition == 'attachment; filename="badname.pdf"'

    def test_unconfigured_storage_raises(self, monkeypatch):
        monkeypatch.setattr(settings, "storage_bucket", "")
        with pytest.raises(r2.StorageNotConfiguredError):
            r2.signed_get_url("k")

    def test_ttl_setting_is_bounded(self):
        from pydantic import ValidationError
        from app.config import Settings
        for bad in (10, 700_000):
            with pytest.raises(ValidationError):
                Settings(_env_file=None, storage_signed_url_ttl_seconds=bad)


class TestKeyDerivation:
    def test_key_from_a_legacy_url(self, fake_storage):
        assert r2.key_from_url(STORAGE_URL_PREFIX + "proposals/3/abc.pdf") == "proposals/3/abc.pdf"

    @pytest.mark.parametrize("url", ["https://elsewhere.example/gridnova-test/proposals/3/abc.pdf",
                                     STORAGE_URL_PREFIX, "https://r2/proposals/x.pdf"])
    def test_non_matching_url_is_left_alone_and_logged(self, fake_storage, caplog, url):
        with caplog.at_level(logging.WARNING, logger="app.storage.r2"):
            assert r2.key_from_url(url) is None
        assert any("does not match" in r.getMessage() for r in caplog.records)

    def test_new_pdfs_store_the_key(self, memory, db_session, fake_storage, known_employees):
        pid, _ = _proposal(memory, db_session, pdf_url=STORAGE_URL_PREFIX + "proposals/9/n.pdf")
        db_session.expire_all()
        assert memory.get_proposal(pid).pdf_storage_key == "proposals/9/n.pdf"

    def test_legacy_rows_are_derived_at_read_time(self, memory, db_session, fake_storage, known_employees):
        pid, _ = _proposal(memory, db_session, pdf_url=STORAGE_URL_PREFIX + "proposals/9/old.pdf")
        p = memory.get_proposal(pid)
        p.pdf_storage_key = None
        db_session.commit()
        assert MemoryManager.proposal_pdf_key(p) == "proposals/9/old.pdf"


@pytest.mark.usefixtures("known_employees")
class TestStaffPdf:
    def test_returns_a_fresh_signed_link_with_expiry(self, client, memory, db_session, fake_storage):
        stored = STORAGE_URL_PREFIX + "proposals/1/x.pdf"
        pid, _ = _proposal(memory, db_session, pdf_url=stored)
        r = client.get(f"{DASH}/proposals/{pid}/pdf", headers=admin_headers())
        assert r.status_code == 200
        body = r.json()
        assert set(body) == {"pdf_url", "proposal_id", "expires_at"}
        assert body["pdf_url"].startswith("https://signed.example/proposals/1/x.pdf")
        assert body["pdf_url"] != stored and SECRET not in r.text
        assert datetime.fromisoformat(body["expires_at"]) > datetime.now(timezone.utc)

    def test_pending_shape_unchanged(self, client, memory, db_session, fake_storage):
        pid, _ = _proposal(memory, db_session)
        assert client.get(f"{DASH}/proposals/{pid}/pdf", headers=admin_headers()).json() == {
            "status": "pdf_pending", "proposal_id": pid}

    def test_unknown_location_is_409(self, client, memory, db_session, fake_storage):
        pid, _ = _proposal(memory, db_session, pdf_url="https://somewhere-else.example/x.pdf")
        r = client.get(f"{DASH}/proposals/{pid}/pdf", headers=admin_headers())
        assert r.status_code == 409 and r.json()["detail"]["code"] == "pdf_location_unknown"

    def test_storage_not_configured_is_503(self, client, memory, db_session, fake_storage, monkeypatch):
        pid, _ = _proposal(memory, db_session, pdf_url=STORAGE_URL_PREFIX + "proposals/1/x.pdf")
        monkeypatch.setattr(settings, "storage_secret_key", "")
        r = client.get(f"{DASH}/proposals/{pid}/pdf", headers=admin_headers())
        assert r.status_code == 503 and r.json()["detail"]["code"] == "storage_not_configured"

    def test_engineer_still_blocked(self, client, memory, db_session, fake_storage):
        pid, _ = _proposal(memory, db_session, pdf_url=STORAGE_URL_PREFIX + "proposals/1/x.pdf")
        assert client.get(f"{DASH}/proposals/{pid}/pdf", headers=engineer_headers()).status_code == 403

    def test_detail_and_summary_flag_pdf_availability(self, client, memory, db_session, fake_storage):
        with_pdf, _ = _proposal(memory, db_session, pdf_url=STORAGE_URL_PREFIX + "p.pdf", email="a@example.com")
        without, _ = _proposal(memory, db_session, email="b@example.com")
        assert client.get(f"{DASH}/proposals/{with_pdf}", headers=admin_headers()).json()["pdf_available"] is True
        rows = {r["id"]: r for r in client.get(f"{DASH}/proposals", headers=admin_headers()).json()["items"]}
        assert rows[with_pdf]["pdf_available"] is True and rows[without]["pdf_available"] is False


@pytest.mark.usefixtures("known_employees")
class TestPublicPdf:
    def test_valid_token_redirects_to_a_signed_attachment(self, client, memory, db_session, fake_storage):
        pid, token = _proposal(memory, db_session, pdf_url=STORAGE_URL_PREFIX + "proposals/5/p.pdf")
        r = client.get(f"/api/v1/public/proposals/{token}/pdf", follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"].startswith("https://signed.example/proposals/5/p.pdf")
        disposition = fake_storage[-1].presigned[-1]["params"]["ResponseContentDisposition"]
        assert disposition == f'attachment; filename="Grid-Nova-Proposal-{pid}-v1.pdf"'
        assert SECRET not in r.headers["location"]

    @pytest.mark.parametrize("token", ["nope", "a" * 43, "../../etc"])
    def test_unknown_token_is_404(self, client, fake_storage, token):
        assert client.get(f"/api/v1/public/proposals/{token}/pdf", follow_redirects=False).status_code == 404

    def test_pending_pdf_is_409(self, client, memory, db_session, fake_storage):
        _, token = _proposal(memory, db_session)
        assert client.get(f"/api/v1/public/proposals/{token}/pdf", follow_redirects=False).status_code == 409

    def test_rate_limited(self, client, memory, db_session, fake_storage):
        from app.api.routes import public_proposals
        _, token = _proposal(memory, db_session, pdf_url=STORAGE_URL_PREFIX + "p.pdf")
        for _ in range(public_proposals._ACCEPT_RATE_LIMIT):
            assert client.get(f"/api/v1/public/proposals/{token}/pdf", follow_redirects=False).status_code == 302
        assert client.get(f"/api/v1/public/proposals/{token}/pdf", follow_redirects=False).status_code == 429

    def test_public_view_reports_pdf_availability(self, client, memory, db_session, fake_storage):
        _, with_pdf = _proposal(memory, db_session, pdf_url=STORAGE_URL_PREFIX + "p.pdf", email="c@example.com")
        _, without = _proposal(memory, db_session, email="d@example.com")
        assert client.get(f"/api/v1/proposals/accept/{with_pdf}").json()["pdf_available"] is True
        assert client.get(f"/api/v1/proposals/accept/{without}").json()["pdf_available"] is False


def _assessment(client, memory, db_session, **extra):
    customer = memory.get_or_create_customer(email="sa@example.com", name="SA")
    lead = memory.create_lead(customer_id=customer.id)
    db_session.commit()
    body = {"lead_id": lead.id, "customer_type": "individual", "project_family": "rooftop_solar",
            "state": "West Bengal", "district": "Kolkata", "pin_code": "700001",
            "site_type": "rooftop_residential", "connection_type": "on_grid", **extra}
    r = client.post("/api/v1/proposal-inputs/", json=body, headers=admin_headers())
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _upload(client, inputs_id, name="bill.pdf", target="document", **form):
    data = {"target": target, **form}
    if target == "document":
        data.setdefault("document_type", "electricity_bill")
    return client.post(f"/api/v1/proposal-inputs/{inputs_id}/upload", headers=admin_headers(), data=data,
                       files={"file": (name, io.BytesIO(b"%PDF-1.4 test"), "application/pdf")})


class TestAssessmentFiles:
    def test_uploaded_file_is_listed_and_downloadable(self, client, memory, db_session, fake_storage):
        inputs_id = _assessment(client, memory, db_session)
        uploaded = _upload(client, inputs_id).json()
        [f] = uploaded["files"]
        assert set(f) == {"file_ref", "target", "filename", "document_type"}
        assert f["filename"] == "bill.pdf" and len(f["file_ref"]) == 32

        r = client.get(f"/api/v1/proposal-inputs/{inputs_id}/files/{f['file_ref']}", headers=admin_headers())
        assert r.status_code == 200
        body = r.json()
        assert body["url"].startswith(f"https://signed.example/proposal-inputs/{inputs_id}/document/")
        assert body["filename"] == "bill.pdf" and body["target"] == "document"
        assert SECRET not in r.text

    def test_photo_and_signature_files_too(self, client, memory, db_session, fake_storage):
        from app.api.routes.proposal_inputs import INSTALLATION_PHOTO_SLOTS
        inputs_id = _assessment(client, memory, db_session)
        _upload(client, inputs_id, name="roof.pdf", target="installation_photo", photo_slot=INSTALLATION_PHOTO_SLOTS[0])
        files = _upload(client, inputs_id, name="sig.pdf", target="staff_signature").json()["files"]
        assert {f["target"] for f in files} == {"installation_photo", "staff_signature"}
        for f in files:
            assert client.get(f"/api/v1/proposal-inputs/{inputs_id}/files/{f['file_ref']}",
                              headers=admin_headers()).status_code == 200

    def test_client_supplied_storage_key_is_never_signed(self, client, memory, db_session, fake_storage):
        forged = [{"type": "bill", "filename": "x.pdf", "storage_key": "proposals/1/someone-elses.pdf"},
                  {"type": "bill", "filename": "y.pdf", "storage_key": "proposal-inputs/999/document/" + "a" * 32 + "_y.pdf"}]
        inputs_id = _assessment(client, memory, db_session, document_refs=forged)
        assert client.get(f"/api/v1/proposal-inputs/{inputs_id}", headers=admin_headers()).json()["files"] == []
        assert client.get(f"/api/v1/proposal-inputs/{inputs_id}/files/{'a' * 32}",
                          headers=admin_headers()).status_code == 404
        assert all(not c.presigned for c in fake_storage)

    def test_unknown_file_and_unknown_assessment_are_404(self, client, memory, db_session, fake_storage):
        inputs_id = _assessment(client, memory, db_session)
        assert client.get(f"/api/v1/proposal-inputs/{inputs_id}/files/{'b' * 32}",
                          headers=admin_headers()).status_code == 404
        assert client.get(f"/api/v1/proposal-inputs/99999/files/{'b' * 32}",
                          headers=admin_headers()).status_code == 404

    def test_requires_authentication(self, client, memory, db_session, fake_storage):
        inputs_id = _assessment(client, memory, db_session)
        ref = _upload(client, inputs_id).json()["files"][0]["file_ref"]
        assert client.get(f"/api/v1/proposal-inputs/{inputs_id}/files/{ref}").status_code == 401
