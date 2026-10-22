"""Phase 3A Step 1 — associate-led customer onboarding."""

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.auth import dependencies as deps
from app.onboarding.service import attribution
from app.memory.models import (
    AuditLog, Customer, CustomerOnboarding, Lead, LeadSource, OnboardingAttributionFlag, OnboardingStatus, Project,
    ProposalInputs, TimelineEvent,
)
from tests.onboarding_support import (  # noqa: F401 — fixtures
    ADMIN, ALIPURDUAR, ALIPURDUAR_I, ALIPURDUAR_II, ARARIA, ARARIA_BLOCK, BASE, DASH, ENG, KOLKATA, MGMT, SALES,
    headers, kyc_ready, make_associate, onboarding_body, onboarding_ready, territories, upload,
    upload_required_documents, complete_survey, confirm_site_location,
)


def _create(client, gna, **kw):
    r = client.post(BASE, json=onboarding_body(**kw), headers=headers(gna))
    assert r.status_code == 201, r.text
    return r.json()


def _codes(body) -> set:
    return {w["code"] for w in body["warnings"]}


def _submit_and_verify(client, gna, oid, note="Spoke to the customer; details confirmed."):
    upload_required_documents(client, gna, oid)
    confirm_site_location(client, gna, oid)
    complete_survey(client, gna, oid)
    assert client.post(f"{BASE}/{oid}/submit", headers=headers(gna)).status_code == 200
    return client.post(f"{DASH}/onboardings/{oid}/verification-call",
                       json={"verification_call_completed": True, "note": note}, headers=ADMIN)


class TestCreate:
    def test_creates_customer_lead_project_and_assessment(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        body = _create(client, gna, customer_email="Ravi@Example.test")
        o = db_session.get(CustomerOnboarding, body["onboarding_id"])
        lead = db_session.get(Lead, o.lead_id)
        inputs = db_session.get(ProposalInputs, o.proposal_inputs_id)
        assert lead.source == LeadSource.associate and lead.assigned_employee_id is None
        assert (inputs.state, inputs.district) == ("WEST BENGAL", "Alipurduar")
        assert db_session.get(Project, o.project_id).reference_id.startswith("GNE-WB-ROF-")
        assert lead.collected_fields["name"] == "Customer 1" and lead.collected_fields["phone"] == "9830000001"
        assert lead.collected_fields["email"] == "ravi@example.test"
        assert body["status"] == "draft" and body["can_edit"] is True
        assert body["notice"]["recorded_by"] == f"associate:{gna}" and body["notice"]["version"]
        assert body["territory"]["block"]["lgd_code"] == ALIPURDUAR_I
        audit = db_session.execute(select(AuditLog).where(AuditLog.entity_type == "customer_onboarding",
                                                          AuditLog.action == "created")).scalar_one()
        assert audit.performed_by == f"associate:{gna}"

    def test_preferred_associate_source(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session, level="district_head", territory=ALIPURDUAR)
        o = _create(client, gna)
        assert db_session.get(Lead, o["lead_id"]).source == LeadSource.preferred_associate

    def test_email_optional(self, client, db_session, onboarding_ready):
        o = _create(client, make_associate(db_session))
        assert o["customer"]["email"] is None
        assert db_session.get(Customer, db_session.get(CustomerOnboarding, o["onboarding_id"]).customer_id).email

    def test_gps_and_equipment_package(self, client, db_session, onboarding_ready):
        o = _create(client, make_associate(db_session), site_latitude=26.49, site_longitude=89.52,
                    equipment_package="nova_signature")
        assert (o["site_latitude"], o["site_longitude"]) == (26.49, 89.52)
        assert o["site_assessment"]["equipment_tier"] == "premium"

    @pytest.mark.parametrize("override, field", [
        ({"notice_version": "old"}, "notice_version"), ({"notice_acknowledged": False}, "notice_acknowledged"),
        ({"site_latitude": 26.4}, None), ({"site_latitude": 95, "site_longitude": 1}, "site_latitude"),
        ({"customer_mobile": "12345"}, "customer_mobile"), ({"pin_code": "0123"}, "pin_code"),
        ({"equipment_package": "gold"}, "equipment_package"), ({"desired_output_kw": -1}, "desired_output_kw"),
    ])
    def test_rejects_invalid_input(self, client, db_session, onboarding_ready, override, field):
        r = client.post(BASE, json=onboarding_body(**override), headers=headers(make_associate(db_session)))
        assert r.status_code == 422
        if field:
            assert field in r.text

    def test_block_required_where_the_district_has_blocks(self, client, db_session, onboarding_ready):
        r = client.post(BASE, json=onboarding_body(block=None), headers=headers(make_associate(db_session)))
        assert r.status_code == 422 and r.json()["detail"]["code"] == "invalid_territory"

    def test_kolkata_needs_no_block(self, client, db_session, onboarding_ready):
        o = _create(client, make_associate(db_session), district=KOLKATA, block=None)
        assert o["territory"]["block"] is None and "pin_district_mismatch" not in _codes(o)


class TestWarnings:
    def test_non_dcr_pm_surya_ghar(self, client, db_session, onboarding_ready):
        o = _create(client, make_associate(db_session), scheme_linked=True, scheme_name="pm_surya_ghar",
                    dcr_panels_required=False)
        assert "non_dcr_pm_surya_ghar" in _codes(o)

    def test_no_dcr_warning_when_dcr(self, client, db_session, onboarding_ready):
        o = _create(client, make_associate(db_session), scheme_linked=True, scheme_name="pm_surya_ghar")
        assert "non_dcr_pm_surya_ghar" not in _codes(o)

    def test_capacity_checks(self, client, db_session, onboarding_ready):
        o = _create(client, make_associate(db_session), desired_output_kw=10, sanctioned_load_kw=5,
                    avg_monthly_units_kwh=300)
        assert {"kw_exceeds_sanctioned_load", "kw_high_for_consumption"} <= _codes(o)
        assert o["status"] == "draft"  # warnings never block

    def test_bill_check_skipped_without_a_confirmed_tariff(self, client, db_session, onboarding_ready):
        o = _create(client, make_associate(db_session), desired_output_kw=10, avg_monthly_units_kwh=None,
                    avg_monthly_bill_inr=500)
        assert "kw_high_for_consumption" not in _codes(o)

    def test_pin_checks(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        assert "pin_district_mismatch" in _codes(_create(client, gna, n=1, pin_code="700001"))
        assert "pin_not_in_directory" in _codes(_create(client, gna, n=2, pin_code="999999"))
        # Alipurduar was carved from Jalpaiguri: its 2017-era PINs are still accepted.
        assert not {"pin_district_mismatch", "pin_not_in_directory"} & _codes(_create(client, gna, n=3))


class TestDuplicatesAndAttribution:
    def test_same_associate_duplicate_refused(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        _create(client, gna, n=1)
        r = client.post(BASE, json=onboarding_body(n=2, customer_mobile="9830000001"), headers=headers(gna))
        assert r.status_code == 409 and r.json()["detail"]["code"] == "duplicate_onboarding"

    def _conflict(self, client, db_session):
        a, b = make_associate(db_session), make_associate(db_session)
        first = _create(client, a, n=1)
        second = _create(client, b, n=2, consumer_number="cn 000001")  # same consumer number, normalised
        return a, b, first, second

    def test_other_associate_is_flagged_not_rejected(self, client, db_session, onboarding_ready):
        a, b, first, second = self._conflict(client, db_session)
        assert "attribution_review" in _codes(second)
        assert a not in str(second["warnings"])  # the other associate is never named
        flag = db_session.execute(select(OnboardingAttributionFlag)).scalar_one()
        assert (flag.onboarding_id, flag.existing_onboarding_id) == (second["onboarding_id"], first["onboarding_id"])
        assert flag.matched_on == ["consumer_number"] and flag.existing_was_locked is False
        events = db_session.execute(select(TimelineEvent).where(TimelineEvent.lead_id == second["lead_id"])).scalars()
        assert "attribution_flagged" in {e.event_type.value for e in events}

    def test_open_flag_blocks_verification_of_both(self, client, db_session, onboarding_ready):
        a, b, first, second = self._conflict(client, db_session)
        for gna, o in ((a, first), (b, second)):
            r = _submit_and_verify(client, gna, o["onboarding_id"])
            assert r.status_code == 409 and r.json()["detail"]["code"] == "attribution_conflict"

    def test_keep_existing(self, client, db_session, onboarding_ready):
        a, b, first, second = self._conflict(client, db_session)
        flag_id = client.get(f"{DASH}/attribution-flags", headers=ADMIN).json()["items"][0]["flag_id"]
        r = client.post(f"{DASH}/attribution-flags/{flag_id}/resolve",
                        json={"resolution": "keep_existing", "note": "A met the customer first."}, headers=ADMIN)
        assert r.status_code == 200 and r.json()["status"] == "resolved"
        assert db_session.get(CustomerOnboarding, second["onboarding_id"]).status == OnboardingStatus.closed
        assert _submit_and_verify(client, a, first["onboarding_id"]).json()["status"] == "verified"
        # The closed record is kept, and can no longer be edited.
        r = client.patch(f"{BASE}/{second['onboarding_id']}", json={"floors": 2}, headers=headers(b))
        assert r.status_code == 409

    def test_award_new_ends_an_existing_lock(self, client, db_session, onboarding_ready):
        a, b = make_associate(db_session), make_associate(db_session)
        first = _create(client, a, n=1)
        assert _submit_and_verify(client, a, first["onboarding_id"]).status_code == 200
        second = _create(client, b, n=2, customer_mobile="9830000001")
        flag = db_session.execute(select(OnboardingAttributionFlag)).scalar_one()
        assert flag.existing_was_locked is True and flag.matched_on == ["mobile"]
        r = client.post(f"{DASH}/attribution-flags/{flag.id}/resolve",
                        json={"resolution": "award_new", "note": "Customer asked for B."}, headers=ADMIN)
        assert r.status_code == 200
        db_session.expire_all()
        old = db_session.get(CustomerOnboarding, first["onboarding_id"])
        assert old.status == OnboardingStatus.closed
        assert attribution(db_session, old) == {"locked": False, "released_reason": "closed"}
        assert "Customer asked for B." in old.closed_reason
        assert _submit_and_verify(client, b, second["onboarding_id"]).json()["status"] == "verified"

    def test_resolution_rules(self, client, db_session, onboarding_ready):
        self._conflict(client, db_session)
        flag_id = db_session.execute(select(OnboardingAttributionFlag.id)).scalar_one()
        url = f"{DASH}/attribution-flags/{flag_id}/resolve"
        assert client.post(url, json={"resolution": "keep_existing", "note": "x"}, headers=SALES).status_code == 403
        assert client.get(f"{DASH}/attribution-flags", headers=SALES).status_code == 403
        assert client.post(url, json={"resolution": "keep_existing", "note": " "}, headers=ADMIN).status_code == 422
        assert client.post(url, json={"resolution": "keep_existing", "note": "ok"}, headers=ADMIN).status_code == 200
        assert client.post(url, json={"resolution": "award_new", "note": "again"}, headers=ADMIN).status_code == 409


class TestUpdate:
    def test_owner_updates_and_submitted_returns_to_draft(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        oid = _create(client, gna)["onboarding_id"]
        upload_required_documents(client, gna, oid)
        confirm_site_location(client, gna, oid)
        complete_survey(client, gna, oid)
        assert client.post(f"{BASE}/{oid}/submit", headers=headers(gna)).json()["status"] == "submitted"
        r = client.patch(f"{BASE}/{oid}", json={"floors": 3, "customer_name": "Renamed"}, headers=headers(gna))
        assert r.status_code == 200 and r.json()["status"] == "draft"
        assert r.json()["site_assessment"]["floors"] == 3 and r.json()["customer"]["name"] == "Renamed"
        entry = db_session.execute(select(AuditLog).where(
            AuditLog.entity_type == "customer_onboarding", AuditLog.action == "updated")
            .order_by(AuditLog.id.desc()).limit(1)).scalar_one()
        assert entry.before_value["status"] == "submitted" and entry.after_value["floors"] == 3
        # floors is a survey field: the signed survey must be signed again.
        assert entry.after_value["signatures"] == "cleared_after_edit"

    def test_changing_the_mobile_to_another_associates_customer_flags_it(self, client, db_session, onboarding_ready):
        a, b = make_associate(db_session), make_associate(db_session)
        _create(client, a, n=1)
        oid = _create(client, b, n=2)["onboarding_id"]
        r = client.patch(f"{BASE}/{oid}", json={"customer_mobile": "9830000001"}, headers=headers(b))
        assert r.status_code == 200 and "attribution_review" in _codes(r.json())

    def test_cannot_clear_a_required_field(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        oid = _create(client, gna)["onboarding_id"]
        assert client.patch(f"{BASE}/{oid}", json={"pin_code": None}, headers=headers(gna)).status_code == 422

    def test_verified_is_not_editable(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        oid = _create(client, gna)["onboarding_id"]
        _submit_and_verify(client, gna, oid)
        r = client.patch(f"{BASE}/{oid}", json={"floors": 1}, headers=headers(gna))
        assert r.status_code == 409 and r.json()["detail"]["code"] == "not_editable"


class TestFiles:
    def test_document_upload_records_server_time_and_gps(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        oid = _create(client, gna)["onboarding_id"]
        r = upload(client, gna, oid, document_type="electricity_bill", latitude="26.49", longitude="89.52",
                   captured_at="2026-09-27T10:00:00+05:30")
        assert r.status_code == 201
        doc = r.json()["documents"][0]
        assert doc["document_type"] == "electricity_bill" and doc["received_at"]
        assert (doc["latitude"], doc["longitude"]) == (26.49, 89.52)
        assert doc["client_captured_at"] == "2026-09-27T10:00:00+05:30"
        assert "electricity_bill" in r.json()["checklist"]["present"]
        # Staff open it through the existing site-assessment file endpoint.
        inputs_id = r.json()["site_assessment"]["proposal_inputs_id"]
        link = client.get(f"/api/v1/proposal-inputs/{inputs_id}/files/{doc['file_ref']}", headers=ADMIN)
        assert link.status_code == 200
        assert client.get(f"{BASE}/{oid}/files/{doc['file_ref']}", headers=headers(gna)).status_code == 200

    def test_photo_slot_and_image_only(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        oid = _create(client, gna)["onboarding_id"]
        assert upload(client, gna, oid, photo_slot="wiring_path").json()["photos"][0]["photo_slot"] == "wiring_path"
        from tests.onboarding_support import PDF
        assert upload(client, gna, oid, photo_slot="wiring_path", content=PDF).status_code == 422
        assert upload(client, gna, oid, photo_slot="roof").status_code == 422
        assert upload(client, gna, oid, document_type="passport").status_code == 422
        assert upload(client, gna, oid, document_type="noc", latitude="10").status_code == 422

    def test_oversized_file(self, client, db_session, onboarding_ready, monkeypatch):
        from app.api.routes import associate_onboarding
        monkeypatch.setattr(associate_onboarding, "MAX_FILE_BYTES", 50)
        gna = make_associate(db_session)
        oid = _create(client, gna)["onboarding_id"]
        assert upload(client, gna, oid, document_type="noc").status_code == 413


class TestSubmitAndChecklist:
    def test_submit_requires_every_required_document(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        oid = _create(client, gna)["onboarding_id"]
        r = client.post(f"{BASE}/{oid}/submit", headers=headers(gna))
        assert r.status_code == 422 and r.json()["detail"]["code"] == "incomplete"
        assert r.json()["detail"]["missing_documents"] == ["electricity_bill", "id_proof", "ownership_proof",
                                                            "photo"]
        detail = client.get(f"{BASE}/{oid}", headers=headers(gna)).json()
        assert detail["checklist"]["required"] == ["electricity_bill", "id_proof", "ownership_proof", "photo"]

    def test_conditional_documents(self, client, db_session, onboarding_ready):
        o = _create(client, make_associate(db_session), scheme_linked=True, scheme_name="pm_surya_ghar",
                    property_in_customer_name=False)
        assert {"subsidy_bank_details", "noc"} <= set(o["checklist"]["required"])

    def test_submit(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        oid = _create(client, gna)["onboarding_id"]
        upload_required_documents(client, gna, oid)
        confirm_site_location(client, gna, oid)
        complete_survey(client, gna, oid)
        r = client.post(f"{BASE}/{oid}/submit", headers=headers(gna))
        assert r.status_code == 200 and r.json()["status"] == "submitted" and r.json()["submitted_at"]
        assert client.post(f"{BASE}/{oid}/submit", headers=headers(gna)).status_code == 409


class TestVerificationCall:
    def test_verifies_and_starts_the_lock(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        oid = _create(client, gna)["onboarding_id"]
        r = _submit_and_verify(client, gna, oid)
        assert r.status_code == 200 and r.json()["status"] == "verified"
        # Associate Terms A7: locked until the project is completed or closed — no date.
        assert r.json()["attribution"] == {"locked": True, "released_reason": None, "under_review": False,
                                           "open_flags": []}
        assert client.get(f"{BASE}/{oid}", headers=headers(gna)).json()["attribution"]["locked"] is True
        assert r.json()["verification"]["note"] == "Spoke to the customer; details confirmed."
        # Associates never see the staff note.
        assert "note" not in client.get(f"{BASE}/{oid}", headers=headers(gna)).json()["verification"]

    def test_rules(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        oid = _create(client, gna)["onboarding_id"]
        url = f"{DASH}/onboardings/{oid}/verification-call"
        ok = {"verification_call_completed": True, "note": "fine"}
        assert client.post(url, json=ok, headers=ADMIN).status_code == 409  # still draft
        upload_required_documents(client, gna, oid)
        confirm_site_location(client, gna, oid)
        complete_survey(client, gna, oid)
        client.post(f"{BASE}/{oid}/submit", headers=headers(gna))
        assert client.post(url, json={**ok, "verification_call_completed": False}, headers=ADMIN).status_code == 422
        assert client.post(url, json={**ok, "note": " "}, headers=ADMIN).status_code == 422
        for role in (MGMT, ENG):
            assert client.post(url, json=ok, headers=role).status_code == 403
        assert client.post(url, json=ok, headers=SALES).status_code == 200  # unassigned lead: visible to Sales

    def test_database_refuses_verified_without_a_call(self, client, db_session, onboarding_ready):
        o = db_session.get(CustomerOnboarding, _create(client, make_associate(db_session))["onboarding_id"])
        o.status = OnboardingStatus.verified
        with pytest.raises(IntegrityError):
            db_session.flush()
        db_session.rollback()

    def test_staff_list_and_detail(self, client, db_session, onboarding_ready):
        _create(client, make_associate(db_session))
        assert client.get(f"{DASH}/onboardings", headers=MGMT).json()["total"] == 1
        assert client.get(f"{DASH}/onboardings", headers=ENG).status_code == 403
        assert client.get(f"{DASH}/onboardings", params={"flagged": True}, headers=ADMIN).json()["total"] == 0


class TestTerritoryVisibility:
    def test_associate_cannot_see_another_associates_customer(self, client, db_session, onboarding_ready):
        a, b = make_associate(db_session), make_associate(db_session)
        oid = _create(client, a)["onboarding_id"]
        assert client.get(f"{BASE}/{oid}", headers=headers(b)).status_code == 403
        assert client.get(BASE, headers=headers(b)).json()["total"] == 0
        assert client.patch(f"{BASE}/{oid}", json={"floors": 1}, headers=headers(b)).status_code == 403
        assert upload(client, b, oid, document_type="noc").status_code == 403

    def test_district_head_sees_own_district_not_a_sibling(self, client, db_session, onboarding_ready):
        head = make_associate(db_session, level="district_head", territory=ALIPURDUAR)
        worker = make_associate(db_session)
        mine_district = _create(client, worker, n=1)["onboarding_id"]
        sibling = _create(client, worker, n=2, district=KOLKATA, block=None)["onboarding_id"]
        assert client.get(f"{BASE}/{mine_district}", headers=headers(head)).status_code == 200
        assert client.get(f"{BASE}/{sibling}", headers=headers(head)).status_code == 403
        listing = client.get(BASE, headers=headers(head)).json()
        assert [i["onboarding_id"] for i in listing["items"]] == [mine_district]
        assert listing["items"][0]["can_edit"] is False
        # Seeing is not editing.
        assert client.patch(f"{BASE}/{mine_district}", json={"floors": 1}, headers=headers(head)).status_code == 403

    def test_state_head_sees_their_state_only(self, client, db_session, onboarding_ready):
        head = make_associate(db_session, level="state_head", territory=19)
        worker = make_associate(db_session)
        wb = _create(client, worker, n=1)["onboarding_id"]
        bihar = _create(client, worker, n=2, district=ARARIA, block=ARARIA_BLOCK)["onboarding_id"]
        assert client.get(f"{BASE}/{wb}", headers=headers(head)).status_code == 200
        assert client.get(f"{BASE}/{bihar}", headers=headers(head)).status_code == 403

    def test_block_level_preferred_sees_their_block_only(self, client, db_session, onboarding_ready):
        head = make_associate(db_session, level="block", territory=ALIPURDUAR_I)
        worker = make_associate(db_session)
        same = _create(client, worker, n=1)["onboarding_id"]
        sibling_block = _create(client, worker, n=2, block=ALIPURDUAR_II)["onboarding_id"]
        assert client.get(f"{BASE}/{same}", headers=headers(head)).status_code == 200
        assert client.get(f"{BASE}/{sibling_block}", headers=headers(head)).status_code == 403
        assert client.get(BASE, params={"mine_only": True}, headers=headers(head)).json()["total"] == 0

    def test_staff_token_rejected_on_associate_routes(self, client, monkeypatch, onboarding_ready):
        monkeypatch.setattr(deps, "_verify_clerk_jwt",
                            lambda t: {"sub": "u", "public_metadata": {"role": "admin"}})
        assert client.get(BASE, headers={"Authorization": "Bearer t"}).status_code == 403
        assert client.get(BASE, headers=ADMIN).status_code == 401

    def test_associate_token_rejected_on_staff_routes(self, client, db_session, monkeypatch, onboarding_ready):
        gna = make_associate(db_session)
        monkeypatch.setattr(deps, "_verify_clerk_jwt", lambda t: {
            "sub": "u", "public_metadata": {"principal": "associate", "associate_id": gna}})
        assert client.get(f"{DASH}/onboardings", headers={"Authorization": "Bearer t"}).status_code == 403

    def test_options(self, client, db_session, onboarding_ready):
        body = client.get(f"{BASE}/options", headers=headers(make_associate(db_session))).json()
        assert {p["key"] for p in body["equipment_packages"]} == {"nova_assured", "nova_signature", "as_recommended"}
        assert "[PLACEHOLDER" in body["notice"]["text"] and "attribution_lock_days" not in body
