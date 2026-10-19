"""The two public registration endpoints: POST /api/v1/public/associates/apply and
POST /api/v1/public/preferred/apply."""
import json

import pytest
from sqlalchemy import select, text

from app.memory.models import (
    AssociateApplication, AssociateIdentityClaim, AuditLog, PreferredAssociateApplication,
)
from tests.associate_support import (  # noqa: F401 — fixtures
    ALIPURDUAR, ALIPURDUAR_I, ARARIA_BLOCK, ASSOCIATE_APPLY, KOLKATA, PDF, PNG, PREFERRED_APPLY, WB,
    aadhaar, adult_dob, associate_form, files, kyc_ready, preferred_form, submit, territories,
)


def _errors(r) -> dict:
    return {e["loc"][-1]: e["msg"] for e in r.json()["detail"]}


class TestAssociateApply:
    def test_submits_and_stores_one_record(self, client, db_session, kyc_ready):
        r = submit(client, ASSOCIATE_APPLY, associate_form())
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["application_type"] == "associate" and body["status"] == "submitted"
        row = db_session.get(AssociateApplication, body["application_id"])
        assert row.capability.value == "sales" and row.pan_last4 == "001F"
        # BE-4: the PAN is encrypted at rest — never stored in clear.
        from app.security.encryption import decrypt
        assert "ABCPE1001F" not in row.pan_encrypted and decrypt(row.pan_encrypted) == "ABCPE1001F"
        assert set(row.documents) == {"masked_aadhaar", "pan_card", "photo", "cancelled_cheque"}
        assert row.documents["photo"]["content_type"] == "image/jpeg"
        assert db_session.execute(select(PreferredAssociateApplication)).first() is None

    def test_full_aadhaar_is_never_persisted(self, client, db_session, kyc_ready):
        form = associate_form()
        full = form["aadhaar_number"]
        r = submit(client, ASSOCIATE_APPLY, form)
        assert r.status_code == 201
        row = db_session.get(AssociateApplication, r.json()["application_id"])
        assert row.aadhaar_last4 == full[-4:]
        # Search every text column of every row in every table this write touched.
        for table in ("associate_applications", "associate_identity_claims", "audit_logs"):
            for record in db_session.execute(text(f"SELECT * FROM {table}")).all():
                assert full not in json.dumps([str(c) for c in record]), table
        assert full not in r.text

    def test_bank_account_is_encrypted_at_rest(self, client, db_session, kyc_ready):
        form = associate_form()
        r = submit(client, ASSOCIATE_APPLY, form)
        row = db_session.get(AssociateApplication, r.json()["application_id"])
        assert form["bank_account_number"] not in row.bank_account_encrypted
        from app.security.encryption import decrypt
        assert decrypt(row.bank_account_encrypted) == form["bank_account_number"]
        assert row.bank_account_last4 == form["bank_account_number"][-4:]

    def test_documents_uploaded_to_private_storage(self, client, kyc_ready):
        r = submit(client, ASSOCIATE_APPLY, associate_form())
        keys = [p["Key"] for c in kyc_ready for p in c.put]
        assert len(keys) == 4
        assert all(k.startswith(f"associate-applications/associate/{r.json()['application_id']}/") for k in keys)

    def test_submission_is_audited_without_sensitive_values(self, client, db_session, kyc_ready):
        form = associate_form()
        r = submit(client, ASSOCIATE_APPLY, form)
        entry = db_session.execute(select(AuditLog).where(AuditLog.entity_type == "associate_application")).scalar_one()
        assert entry.action == "submitted" and entry.entity_id == str(r.json()["application_id"])
        dumped = json.dumps(entry.after_value)
        for secret in (form["pan"], form["bank_account_number"], form["aadhaar_number"]):
            assert secret not in dumped

    def test_capability_required_and_checked(self, client, kyc_ready):
        r = submit(client, ASSOCIATE_APPLY, associate_form(capability="marketing"))
        assert r.status_code == 422 and "capability" in _errors(r)

    def test_district_without_blocks_needs_no_block(self, client, kyc_ready):
        form = associate_form(district_lgd_code=str(KOLKATA), block_lgd_code=None)
        assert submit(client, ASSOCIATE_APPLY, form).status_code == 201

    def test_block_required_where_the_district_has_blocks(self, client, kyc_ready):
        r = submit(client, ASSOCIATE_APPLY, associate_form(block_lgd_code=None))
        assert r.status_code == 422 and r.json()["detail"]["field"] == "block_lgd_code"

    def test_block_must_belong_to_the_district(self, client, kyc_ready):
        r = submit(client, ASSOCIATE_APPLY, associate_form(block_lgd_code=str(ARARIA_BLOCK)))
        assert r.status_code == 422 and r.json()["detail"]["code"] == "invalid_territory"

    def test_unknown_state(self, client, kyc_ready):
        r = submit(client, ASSOCIATE_APPLY, associate_form(state_lgd_code="9"))
        assert r.status_code == 422 and r.json()["detail"]["field"] == "state_lgd_code"


class TestValidationErrors:
    def test_all_field_errors_reported_together_and_values_never_echoed(self, client, kyc_ready):
        bad_aadhaar = aadhaar()[:-1] + str((int(aadhaar()[-1]) + 1) % 10)
        form = associate_form(aadhaar_number=bad_aadhaar, pan="ABCCE1234F", bank_ifsc="SBIN1001234",
                              mobile="12345", date_of_birth="2015-01-01", pin_code="012345")
        r = submit(client, ASSOCIATE_APPLY, form)
        assert r.status_code == 422
        errors = _errors(r)
        assert {"aadhaar_number", "pan", "bank_ifsc", "mobile", "date_of_birth", "pin_code"} <= set(errors)
        assert "checksum" in errors["aadhaar_number"]
        assert "individual" in errors["pan"]
        assert bad_aadhaar not in r.text and "input" not in r.text

    def test_missing_fields_and_files(self, client, kyc_ready):
        r = client.post(ASSOCIATE_APPLY, data={"full_name": "X"})
        errors = _errors(r)
        assert r.status_code == 422
        for field in ("date_of_birth", "aadhaar_number", "pan", "photo", "cancelled_cheque", "kyc_consent_version"):
            assert field in errors

    def test_consent_must_be_accepted(self, client, kyc_ready):
        r = submit(client, ASSOCIATE_APPLY, associate_form(kyc_consent_accepted="false"))
        assert "kyc_consent_accepted" in _errors(r)

    def test_stale_consent_version_rejected(self, client, kyc_ready):
        r = submit(client, ASSOCIATE_APPLY, associate_form(kyc_consent_version="old-version"))
        assert "kyc_consent_version" in _errors(r)

    def test_consent_version_is_stored(self, client, db_session, kyc_ready):
        from app.associates import texts
        r = submit(client, ASSOCIATE_APPLY, associate_form())
        row = db_session.get(AssociateApplication, r.json()["application_id"])
        assert row.kyc_consent_version == texts.current_consent()[0]
        assert row.kyc_consent_accepted_at is not None


class TestUploads:
    def test_non_image_non_pdf_rejected_by_content_not_name(self, client, kyc_ready):
        f = files()
        f["pan_card"] = ("pan.png", b"MZ\x90\x00 not really a png", "image/png")
        r = client.post(ASSOCIATE_APPLY, data=associate_form(), files=f)
        assert r.status_code == 422 and "pan_card" in _errors(r)

    def test_photo_must_be_an_image(self, client, kyc_ready):
        f = files()
        f["photo"] = ("photo.pdf", PDF, "application/pdf")
        r = client.post(ASSOCIATE_APPLY, data=associate_form(), files=f)
        assert r.status_code == 422 and "photo" in _errors(r)

    def test_oversized_file(self, client, kyc_ready, monkeypatch):
        from app.associates import uploads
        monkeypatch.setattr(uploads, "MAX_DOCUMENT_BYTES", 100)
        f = files()
        f["pan_card"] = ("pan.png", PNG + b"\x00" * 200, "image/png")
        r = client.post(ASSOCIATE_APPLY, data=associate_form(), files=f)
        assert r.status_code == 413

    def test_storage_not_configured_is_never_a_fake_success(self, client, db_session, territories, monkeypatch):
        from app.config import settings
        from app.security.encryption import generate_key
        monkeypatch.setattr(settings, "fernet_encryption_key", generate_key())
        r = submit(client, ASSOCIATE_APPLY, associate_form())
        assert r.status_code == 503 and r.json()["detail"]["code"] == "storage_not_configured"
        assert db_session.execute(select(AssociateApplication)).first() is None

    def test_encryption_key_missing(self, client, fake_storage, territories, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "fernet_encryption_key", "")
        r = submit(client, ASSOCIATE_APPLY, associate_form())
        assert r.status_code == 503 and r.json()["detail"]["code"] == "encryption_not_configured"


class TestDuplicates:
    @pytest.mark.parametrize("field, value_from_first", [
        ("pan", True), ("mobile", True), ("bank_account_number", True),
    ])
    def test_same_attribute_blocks_a_second_registration(self, client, kyc_ready, field, value_from_first):
        first = associate_form(1)
        assert submit(client, ASSOCIATE_APPLY, first).status_code == 201
        second = associate_form(2, **{field: first[field]})
        r = submit(client, ASSOCIATE_APPLY, second)
        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "duplicate_registration"
        assert r.json()["detail"]["field"] == field

    def test_bank_account_duplicate_is_per_bank(self, client, kyc_ready):
        first = associate_form(1)
        assert submit(client, ASSOCIATE_APPLY, first).status_code == 201
        other_bank = associate_form(2, bank_account_number=first["bank_account_number"], bank_ifsc="HDFC0001234")
        assert submit(client, ASSOCIATE_APPLY, other_bank).status_code == 201

    def test_aadhaar_last4_dob_and_name_together(self, client, kyc_ready):
        first = associate_form(1)
        assert submit(client, ASSOCIATE_APPLY, first).status_code == 201
        same_person = associate_form(2, aadhaar_number=first["aadhaar_number"],
                                     date_of_birth=first["date_of_birth"], full_name="  test   APPLICANT 1 ")
        r = submit(client, ASSOCIATE_APPLY, same_person)
        assert r.status_code == 409 and r.json()["detail"]["field"] == "aadhaar_number"

    def test_same_aadhaar_last4_with_a_different_dob_is_allowed(self, client, kyc_ready):
        first = associate_form(1)
        assert submit(client, ASSOCIATE_APPLY, first).status_code == 201
        other = associate_form(2, aadhaar_number=first["aadhaar_number"], full_name=first["full_name"],
                               date_of_birth=adult_dob(40))
        assert submit(client, ASSOCIATE_APPLY, other).status_code == 201

    def test_duplicates_blocked_across_both_programmes(self, client, kyc_ready):
        first = associate_form(1)
        assert submit(client, ASSOCIATE_APPLY, first).status_code == 201
        r = submit(client, PREFERRED_APPLY, preferred_form(2, pan=first["pan"]))
        assert r.status_code == 409 and r.json()["detail"]["field"] == "pan"

    def test_claims_are_hashes_not_values(self, client, db_session, kyc_ready):
        form = associate_form()
        submit(client, ASSOCIATE_APPLY, form)
        claims = db_session.execute(select(AssociateIdentityClaim)).scalars().all()
        assert {c.kind for c in claims} == {"pan", "mobile", "bank_account", "aadhaar_dob_name"}
        for c in claims:
            assert len(c.value_hash) == 64 and form["pan"] not in c.value_hash

    def test_database_refuses_a_duplicate_claim(self, db_session, kyc_ready):
        from sqlalchemy.exc import IntegrityError
        from app.memory.models import AssociateType
        for _ in range(2):
            db_session.add(AssociateIdentityClaim(kind="pan", value_hash="a" * 64,
                                                  application_type=AssociateType.associate, application_id=1))
        with pytest.raises(IntegrityError):
            db_session.flush()
        db_session.rollback()


class TestPreferredApply:
    def test_submits_a_distinct_record(self, client, db_session, kyc_ready):
        r = submit(client, PREFERRED_APPLY, preferred_form(gstin="19abcpe1001f1z5"))
        assert r.status_code == 201, r.text
        row = db_session.get(PreferredAssociateApplication, r.json()["application_id"])
        assert row.requested_level.value == "district_head" and row.gstin == "19ABCPE1001F1Z5"
        assert row.team_size == 4 and len(row.references) == 2
        assert row.requested_block_id is None  # a district head's territory is the district
        assert db_session.execute(select(AssociateApplication)).first() is None

    def test_gstin_optional_but_checked(self, client, kyc_ready):
        assert submit(client, PREFERRED_APPLY, preferred_form(1)).status_code == 201
        r = submit(client, PREFERRED_APPLY, preferred_form(2, gstin="NOTAGSTIN"))
        assert r.status_code == 422 and "gstin" in _errors(r)

    def test_block_level_needs_a_block(self, client, kyc_ready):
        r = submit(client, PREFERRED_APPLY, preferred_form(level="block", requested_block_lgd_code=None))
        assert r.status_code == 422 and r.json()["detail"]["field"] == "requested_block_lgd_code"

    def test_references_must_differ_from_each_other_and_the_applicant(self, client, kyc_ready):
        form = preferred_form()
        form["reference_2_mobile"] = form["reference_1_mobile"]
        assert "reference_2_mobile" in _errors(submit(client, PREFERRED_APPLY, form))
        form = preferred_form()
        form["reference_1_mobile"] = form["mobile"]
        assert "reference_1_mobile" in _errors(submit(client, PREFERRED_APPLY, form))

    def test_invalid_applicant_mobile_is_reported(self, client, kyc_ready):
        assert "mobile" in _errors(submit(client, PREFERRED_APPLY, preferred_form(mobile="123")))

    def test_full_seat_rejected_with_a_clear_message(self, client, db_session, kyc_ready):
        from tests.associate_support import approve_flow
        from tests.conftest import seed_employees
        seed_employees(db_session, "emp-admin-1")
        first = submit(client, PREFERRED_APPLY, preferred_form(1)).json()["application_id"]
        with _documents_stubbed():
            assert approve_flow(client, "preferred_associate", first).status_code == 200
        r = submit(client, PREFERRED_APPLY, preferred_form(2))
        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "seat_full"
        assert "District Head seat for Alipurduar is already filled" in r.json()["detail"]["message"]


def _documents_stubbed():
    from unittest.mock import patch
    return patch("app.associates.documents.render_pdf", return_value=b"%PDF-stub")


class TestPublicGuards:
    def test_rate_limited(self, client, kyc_ready):
        codes = [client.post(ASSOCIATE_APPLY, data={}).status_code for _ in range(6)]
        assert codes[:5] == [422] * 5 and codes[5] == 429

    def test_production_refuses_registration_under_placeholder_consent(self, client, kyc_ready, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "environment", "production")
        r = submit(client, ASSOCIATE_APPLY, associate_form())
        assert r.status_code == 503 and r.json()["detail"]["code"] == "kyc_consent_not_configured"

    def test_middleware_refuses_an_oversized_body_early(self, client, kyc_ready, monkeypatch):
        from app.associates import uploads
        monkeypatch.setattr(uploads, "MAX_DOCUMENT_BYTES", 10)
        r = client.post(ASSOCIATE_APPLY, content=b"x" * (200 * 1024),
                        headers={"content-type": "multipart/form-data; boundary=zz"})
        assert r.status_code == 413


class TestReferenceData:
    def test_territory_lists(self, client, territories):
        states = client.get("/api/v1/public/territories/states").json()["items"]
        assert {(s["lgd_code"], s["iso_code"]) for s in states} == {(10, "BR"), (19, "WB")}
        districts = client.get(f"/api/v1/public/territories/states/{WB}/districts").json()["items"]
        by_code = {d["lgd_code"]: d for d in districts}
        assert by_code[KOLKATA]["has_blocks"] is False and by_code[ALIPURDUAR]["has_blocks"] is True
        blocks = client.get(f"/api/v1/public/territories/districts/{ALIPURDUAR}/blocks").json()["items"]
        assert ALIPURDUAR_I in {b["lgd_code"] for b in blocks}
        assert client.get("/api/v1/public/territories/states/99/districts").status_code == 404

    def test_kyc_consent(self, client):
        body = client.get("/api/v1/public/kyc-consent").json()
        assert body["version"] and "[PLACEHOLDER" in body["text"]

    def test_seat_availability(self, client, territories):
        r = client.get("/api/v1/public/preferred/seat-availability",
                       params={"level": "state_head", "state_lgd_code": WB})
        assert r.json() == {"level": "state_head", "capacity": 2, "held": 0, "available": True}
