"""Tests for the site-assessment field expansion on ProposalInputs — a real
gap analysis against a professional rooftop-solar site-survey checklist.
Confirms the new fields actually save and retrieve correctly through the
real API (POST then GET), not just that the model/migration exist.
"""
from app.memory.models import LeadSource

from tests.conftest import sales_headers


def _new_lead(memory, db_session, email="site-assessment-test@example.com"):
    customer = memory.get_or_create_customer(email=email, name="Site Assessment Test")
    db_session.commit()
    lead = memory.create_lead(customer_id=customer.id, source=LeadSource.website_chat)
    db_session.commit()
    return lead.id


def _valid_payload(lead_id: int, **overrides) -> dict:
    payload = {
        "lead_id": lead_id,
        "customer_type": "individual",
        "project_family": "rooftop_solar",
        "state": "West Bengal",
        "district": "Kolkata",
        "pin_code": "700001",
        "site_type": "rooftop_residential",
        "connection_type": "on_grid",
    }
    payload.update(overrides)
    return payload


EXPANDED_FIELDS = {
    "roof_ownership_confirmed": True,
    "roofs_available_count": 2,
    "roofs_considered_count": 1,
    "future_roof_expansion_planned": False,
    "roof_age_years": 8.5,
    "consumption_history": [
        {"month": "2026-02", "units_kwh": 410.0, "bill_inr": 3280.0},
        {"month": "2026-03", "units_kwh": 425.0, "bill_inr": 3400.0},
        {"month": "2026-04", "units_kwh": 460.0, "bill_inr": 3680.0},
        {"month": "2026-05", "units_kwh": 500.0, "bill_inr": 4000.0},
        {"month": "2026-06", "units_kwh": 520.0, "bill_inr": 4160.0},
        {"month": "2026-07", "units_kwh": 480.0, "bill_inr": 3840.0},
    ],
    "sanctioned_load_kw": 6.0,
    "discom_consumer_category": "domestic",
    "consumer_priority_class": "vip",
    "installation_photo_refs": {
        "panel_installation_area": {"filename": "panel_area.jpg", "storage_key": None},
        "acdb_dcdb_placement": {"filename": "acdb.jpg", "storage_key": None},
        "earthing_location": {"filename": "earthing.jpg", "storage_key": None},
        "inverter_installation_area": {"filename": "inverter.jpg", "storage_key": None},
        "wiring_path": {"filename": "wiring.jpg", "storage_key": None},
        "panel_to_inverter_distance": {"filename": "p2i.jpg", "storage_key": None},
        "inverter_to_lt_panel_distance": {"filename": "i2lt.jpg", "storage_key": None},
    },
    "staff_signature_ref": {"filename": "staff_sig.png", "storage_key": None, "signed_at": "2026-09-07T10:00:00+00:00"},
    "customer_signature_ref": {"filename": "cust_sig.png", "storage_key": None, "signed_at": "2026-09-07T10:05:00+00:00"},
}


class TestExpandedFieldsSaveAndRetrieve:
    def test_all_new_fields_round_trip_through_the_real_api(self, client, memory, db_session):
        lead_id = _new_lead(memory, db_session, "expanded-fields@example.com")
        r = client.post(
            "/api/v1/proposal-inputs/",
            json=_valid_payload(lead_id, **EXPANDED_FIELDS),
            headers=sales_headers(),
        )
        assert r.status_code == 200, r.text
        inputs_id = r.json()["id"]

        get_r = client.get(f"/api/v1/proposal-inputs/{inputs_id}", headers=sales_headers())
        assert get_r.status_code == 200, get_r.text
        body = get_r.json()

        assert body["roof_ownership_confirmed"] is True
        assert body["roofs_available_count"] == 2
        assert body["roofs_considered_count"] == 1
        assert body["future_roof_expansion_planned"] is False
        assert body["roof_age_years"] == 8.5
        assert len(body["consumption_history"]) == 6
        assert body["consumption_history"][0] == {"month": "2026-02", "units_kwh": 410.0, "bill_inr": 3280.0}
        assert body["sanctioned_load_kw"] == 6.0
        assert body["discom_consumer_category"] == "domestic"
        assert body["consumer_priority_class"] == "vip"
        assert set(body["installation_photo_refs"].keys()) == {
            "panel_installation_area", "acdb_dcdb_placement", "earthing_location",
            "inverter_installation_area", "wiring_path",
            "panel_to_inverter_distance", "inverter_to_lt_panel_distance",
        }
        assert body["installation_photo_refs"]["panel_installation_area"]["filename"] == "panel_area.jpg"
        assert body["staff_signature_ref"]["filename"] == "staff_sig.png"
        assert body["customer_signature_ref"]["filename"] == "cust_sig.png"

    def test_all_new_fields_are_optional_backward_compatible(self, client, memory, db_session):
        """A submission with none of the new fields (the pre-expansion shape)
        must still work exactly as before."""
        lead_id = _new_lead(memory, db_session, "no-expanded-fields@example.com")
        r = client.post(
            "/api/v1/proposal-inputs/", json=_valid_payload(lead_id), headers=sales_headers(),
        )
        assert r.status_code == 200, r.text
        get_r = client.get(f"/api/v1/proposal-inputs/{r.json()['id']}", headers=sales_headers())
        body = get_r.json()
        assert body["consumption_history"] is None
        assert body["installation_photo_refs"] is None
        assert body["staff_signature_ref"] is None
        assert body["roof_ownership_confirmed"] is None
        # consumer_priority_class defaults to "normal" when not supplied.
        assert body["consumer_priority_class"] == "normal"

    def test_consumer_account_number_already_existed_not_duplicated(self, client, memory, db_session):
        """Consumer Account Number was already covered by the pre-existing
        consumer_number field (see 0010_proposal_inputs) — this test guards
        against ever silently adding a second, competing field for the same
        real-world concept."""
        lead_id = _new_lead(memory, db_session, "consumer-number@example.com")
        r = client.post(
            "/api/v1/proposal-inputs/",
            json=_valid_payload(lead_id, consumer_number="123456789"),
            headers=sales_headers(),
        )
        assert r.status_code == 200, r.text
        get_r = client.get(f"/api/v1/proposal-inputs/{r.json()['id']}", headers=sales_headers())
        assert get_r.json()["consumer_number"] == "123456789"


class TestInstallationPhotoSlotValidation:
    def test_unknown_photo_slot_rejected(self, client, memory, db_session):
        lead_id = _new_lead(memory, db_session, "bad-photo-slot@example.com")
        r = client.post(
            "/api/v1/proposal-inputs/",
            json=_valid_payload(
                lead_id,
                installation_photo_refs={"not_a_real_slot": {"filename": "x.jpg"}},
            ),
            headers=sales_headers(),
        )
        assert r.status_code == 422

    def test_a_single_known_slot_is_accepted(self, client, memory, db_session):
        lead_id = _new_lead(memory, db_session, "one-photo-slot@example.com")
        r = client.post(
            "/api/v1/proposal-inputs/",
            json=_valid_payload(
                lead_id,
                installation_photo_refs={"earthing_location": {"filename": "e.jpg"}},
            ),
            headers=sales_headers(),
        )
        assert r.status_code == 200, r.text


class TestDiscomConsumerClassification:
    def test_domestic_and_industrial_both_accepted(self, client, memory, db_session):
        for category in ("domestic", "industrial"):
            lead_id = _new_lead(memory, db_session, f"discom-{category}@example.com")
            r = client.post(
                "/api/v1/proposal-inputs/",
                json=_valid_payload(lead_id, discom_consumer_category=category),
                headers=sales_headers(),
            )
            assert r.status_code == 200, r.text

    def test_unknown_category_rejected(self, client, memory, db_session):
        lead_id = _new_lead(memory, db_session, "discom-bad@example.com")
        r = client.post(
            "/api/v1/proposal-inputs/",
            json=_valid_payload(lead_id, discom_consumer_category="agricultural"),
            headers=sales_headers(),
        )
        assert r.status_code == 422

    def test_vip_and_normal_both_accepted(self, client, memory, db_session):
        for priority in ("normal", "vip"):
            lead_id = _new_lead(memory, db_session, f"priority-{priority}@example.com")
            r = client.post(
                "/api/v1/proposal-inputs/",
                json=_valid_payload(lead_id, consumer_priority_class=priority),
                headers=sales_headers(),
            )
            assert r.status_code == 200, r.text
