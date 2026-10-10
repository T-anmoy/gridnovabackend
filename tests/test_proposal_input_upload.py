"""Tests for POST /api/v1/proposal-inputs/{id}/upload — the real backend
half of DocumentRef/InstallationPhotoRef/SignatureRef, storing real bytes via
the same R2 client app/pdf/generator.py already uses for proposal PDFs.
"""
from app.memory.models import LeadSource
from app.storage import r2 as r2_module

from tests.conftest import sales_headers, engineer_headers


def _new_lead(memory, db_session, email="upload-test@example.com"):
    customer = memory.get_or_create_customer(email=email, name="Upload Test")
    db_session.commit()
    lead = memory.create_lead(customer_id=customer.id, source=LeadSource.website_chat)
    db_session.commit()
    return lead.id


def _new_inputs(client, memory, db_session, email="upload-test@example.com"):
    lead_id = _new_lead(memory, db_session, email)
    r = client.post(
        "/api/v1/proposal-inputs/",
        json={
            "lead_id": lead_id,
            "customer_type": "individual",
            "project_family": "rooftop_solar",
            "state": "West Bengal",
            "district": "Kolkata",
            "pin_code": "700001",
            "site_type": "rooftop_residential",
            "connection_type": "on_grid",
        },
        headers=sales_headers(),
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


_FAKE_PDF_BYTES = b"%PDF-1.4 fake but real bytes for a real upload test"
# Real magic bytes: the endpoint types a file by its content, not the claimed Content-Type.
_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


class TestSuccessfulUpload:
    def test_document_upload_stores_real_key_and_appends_to_document_refs(
        self, client, memory, db_session, monkeypatch
    ):
        inputs_id = _new_inputs(client, memory, db_session)
        monkeypatch.setattr(r2_module, "upload_bytes", lambda key, body, ct: f"https://r2.example/{key}")

        r = client.post(
            f"/api/v1/proposal-inputs/{inputs_id}/upload",
            files={"file": ("bill.pdf", _FAKE_PDF_BYTES, "application/pdf")},
            data={"target": "document", "document_type": "electricity_bill"},
            headers=sales_headers(),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert len(body["document_refs"]) == 1
        ref = body["document_refs"][0]
        assert ref["type"] == "electricity_bill"
        assert ref["filename"] == "bill.pdf"
        assert ref["storage_key"].startswith(f"proposal-inputs/{inputs_id}/document/")

    def test_installation_photo_upload_lands_in_the_right_slot(self, client, memory, db_session, monkeypatch):
        inputs_id = _new_inputs(client, memory, db_session, "photo-test@example.com")
        monkeypatch.setattr(r2_module, "upload_bytes", lambda key, body, ct: f"https://r2.example/{key}")

        r = client.post(
            f"/api/v1/proposal-inputs/{inputs_id}/upload",
            files={"file": ("earthing.jpg", _JPEG, "image/jpeg")},
            data={"target": "installation_photo", "photo_slot": "earthing_location"},
            headers=sales_headers(),
        )
        assert r.status_code == 200, r.text
        photos = r.json()["installation_photo_refs"]
        assert set(photos.keys()) == {"earthing_location"}
        assert photos["earthing_location"]["filename"] == "earthing.jpg"

    def test_two_photo_uploads_accumulate_without_overwriting_each_other(self, client, memory, db_session, monkeypatch):
        inputs_id = _new_inputs(client, memory, db_session, "photo-accum@example.com")
        monkeypatch.setattr(r2_module, "upload_bytes", lambda key, body, ct: f"https://r2.example/{key}")

        client.post(
            f"/api/v1/proposal-inputs/{inputs_id}/upload",
            files={"file": ("a.jpg", _JPEG, "image/jpeg")},
            data={"target": "installation_photo", "photo_slot": "wiring_path"},
            headers=sales_headers(),
        )
        r2_ = client.post(
            f"/api/v1/proposal-inputs/{inputs_id}/upload",
            files={"file": ("b.jpg", _JPEG, "image/jpeg")},
            data={"target": "installation_photo", "photo_slot": "earthing_location"},
            headers=sales_headers(),
        )
        photos = r2_.json()["installation_photo_refs"]
        assert set(photos.keys()) == {"wiring_path", "earthing_location"}

    def test_staff_signature_upload(self, client, memory, db_session, monkeypatch):
        inputs_id = _new_inputs(client, memory, db_session, "sig-test@example.com")
        monkeypatch.setattr(r2_module, "upload_bytes", lambda key, body, ct: f"https://r2.example/{key}")

        r = client.post(
            f"/api/v1/proposal-inputs/{inputs_id}/upload",
            files={"file": ("sig.png", _PNG, "image/png")},
            data={"target": "staff_signature"},
            headers=sales_headers(),
        )
        assert r.status_code == 200, r.text
        sig = r.json()["staff_signature_ref"]
        assert sig["filename"] == "sig.png"
        assert sig["signed_at"] is not None
        assert r.json()["customer_signature_ref"] is None


class TestRejections:
    def test_wrong_file_type_rejected(self, client, memory, db_session):
        inputs_id = _new_inputs(client, memory, db_session, "wrongtype@example.com")
        r = client.post(
            f"/api/v1/proposal-inputs/{inputs_id}/upload",
            files={"file": ("script.exe", b"MZ fake exe", "application/x-msdownload")},
            data={"target": "document", "document_type": "other"},
            headers=sales_headers(),
        )
        assert r.status_code == 422

    def test_content_is_sniffed_not_the_claimed_type(self, client, memory, db_session, monkeypatch):
        """BE-1 S10: an executable labelled image/jpeg is refused by its bytes."""
        monkeypatch.setattr(r2_module, "upload_bytes", lambda key, body, ct: f"https://r2.example/{key}")
        inputs_id = _new_inputs(client, memory, db_session, "sniff@example.com")
        r = client.post(
            f"/api/v1/proposal-inputs/{inputs_id}/upload",
            files={"file": ("photo.jpg", b"MZ\x90\x00 not an image", "image/jpeg")},
            data={"target": "installation_photo", "photo_slot": "earthing_location"},
            headers=sales_headers(),
        )
        assert r.status_code == 422
        assert r.json()["detail"]["code"] == "invalid_file"

    def test_stored_type_and_filename_come_from_the_server(self, client, memory, db_session, monkeypatch):
        stored = []
        monkeypatch.setattr(r2_module, "upload_bytes", lambda key, body, ct: stored.append((key, ct)) or key)
        inputs_id = _new_inputs(client, memory, db_session, "sanitise@example.com")
        r = client.post(
            f"/api/v1/proposal-inputs/{inputs_id}/upload",
            files={"file": ("../../etc/<bill>.pdf", _FAKE_PDF_BYTES, "image/png")},
            data={"target": "document", "document_type": "electricity_bill"},
            headers=sales_headers(),
        )
        assert r.status_code == 200, r.text
        key, content_type = stored[0]
        assert content_type == "application/pdf"
        assert "/../" not in key and "<" not in key and key.endswith("_bill_.pdf")

    def test_unknown_document_type_rejected(self, client, memory, db_session):
        inputs_id = _new_inputs(client, memory, db_session, "doctype@example.com")
        r = client.post(
            f"/api/v1/proposal-inputs/{inputs_id}/upload",
            files={"file": ("f.pdf", _FAKE_PDF_BYTES, "application/pdf")},
            data={"target": "document", "document_type": "anything_at_all"},
            headers=sales_headers(),
        )
        assert r.status_code == 422
        assert r.json()["detail"]["code"] == "unknown_document_type"

    def test_oversized_file_rejected(self, client, memory, db_session, monkeypatch):
        from app.api.routes import proposal_inputs as route_module

        monkeypatch.setattr(route_module, "MAX_UPLOAD_SIZE_BYTES", 10)  # tiny, for a fast test
        inputs_id = _new_inputs(client, memory, db_session, "oversized@example.com")
        r = client.post(
            f"/api/v1/proposal-inputs/{inputs_id}/upload",
            files={"file": ("big.pdf", _FAKE_PDF_BYTES, "application/pdf")},
            data={"target": "document", "document_type": "other"},
            headers=sales_headers(),
        )
        assert r.status_code == 413
        assert "MB" in r.json()["detail"]["message"]

    def test_empty_file_rejected(self, client, memory, db_session):
        inputs_id = _new_inputs(client, memory, db_session, "empty@example.com")
        r = client.post(
            f"/api/v1/proposal-inputs/{inputs_id}/upload",
            files={"file": ("empty.pdf", b"", "application/pdf")},
            data={"target": "document", "document_type": "other"},
            headers=sales_headers(),
        )
        assert r.status_code == 422

    def test_unknown_target_rejected(self, client, memory, db_session):
        inputs_id = _new_inputs(client, memory, db_session, "badtarget@example.com")
        r = client.post(
            f"/api/v1/proposal-inputs/{inputs_id}/upload",
            files={"file": ("f.pdf", _FAKE_PDF_BYTES, "application/pdf")},
            data={"target": "not_a_real_target"},
            headers=sales_headers(),
        )
        assert r.status_code == 422

    def test_unknown_photo_slot_rejected(self, client, memory, db_session):
        inputs_id = _new_inputs(client, memory, db_session, "badslot@example.com")
        r = client.post(
            f"/api/v1/proposal-inputs/{inputs_id}/upload",
            files={"file": ("f.jpg", b"x", "image/jpeg")},
            data={"target": "installation_photo", "photo_slot": "not_a_real_slot"},
            headers=sales_headers(),
        )
        assert r.status_code == 422

    def test_document_without_document_type_rejected(self, client, memory, db_session):
        inputs_id = _new_inputs(client, memory, db_session, "notype@example.com")
        r = client.post(
            f"/api/v1/proposal-inputs/{inputs_id}/upload",
            files={"file": ("f.pdf", _FAKE_PDF_BYTES, "application/pdf")},
            data={"target": "document"},
            headers=sales_headers(),
        )
        assert r.status_code == 422

    def test_missing_inputs_id_returns_404(self, client):
        r = client.post(
            "/api/v1/proposal-inputs/999999/upload",
            files={"file": ("f.pdf", _FAKE_PDF_BYTES, "application/pdf")},
            data={"target": "document", "document_type": "other"},
            headers=sales_headers(),
        )
        assert r.status_code == 404

    def test_requires_auth(self, client, memory, db_session):
        inputs_id = _new_inputs(client, memory, db_session, "noauth@example.com")
        r = client.post(
            f"/api/v1/proposal-inputs/{inputs_id}/upload",
            files={"file": ("f.pdf", _FAKE_PDF_BYTES, "application/pdf")},
            data={"target": "document", "document_type": "other"},
        )
        assert r.status_code == 401

    def test_role_gated(self, client, memory, db_session):
        inputs_id = _new_inputs(client, memory, db_session, "wrongrole@example.com")
        r = client.post(
            f"/api/v1/proposal-inputs/{inputs_id}/upload",
            files={"file": ("f.pdf", _FAKE_PDF_BYTES, "application/pdf")},
            data={"target": "document", "document_type": "other"},
            headers=engineer_headers(),
        )
        assert r.status_code == 403


class TestHonestFailureWhenStorageNotConfigured:
    def test_real_unconfigured_storage_fails_clearly_not_silently(self, client, memory, db_session):
        """No monkeypatching here — STORAGE_* is genuinely blank in this test
        environment (see .env), so this exercises the REAL upload_bytes() and
        its REAL StorageNotConfiguredError path, proving the endpoint never
        fakes success when storage isn't actually configured."""
        inputs_id = _new_inputs(client, memory, db_session, "noconfig@example.com")
        r = client.post(
            f"/api/v1/proposal-inputs/{inputs_id}/upload",
            files={"file": ("f.pdf", _FAKE_PDF_BYTES, "application/pdf")},
            data={"target": "document", "document_type": "other"},
            headers=sales_headers(),
        )
        assert r.status_code == 503
        assert r.json()["detail"]["code"] == "storage_not_configured"
        # And nothing was silently written to document_refs.
        get_r = client.get(f"/api/v1/proposal-inputs/{inputs_id}", headers=sales_headers())
        assert get_r.json()["document_refs"] is None
