"""Customer deduplication — verified BROKEN by live investigation before this
fix, now regression-tested.

Before this fix, MemoryManager.get_or_create_customer() matched only on an
exact-string email comparison (no case/whitespace normalization) and never
considered phone at all. Live reproduction confirmed two real duplication
bugs: "CaseTest@Example.com" vs "casetest@example.com" created two Customer
rows, and the same phone number submitted twice (with different/placeholder
emails) also created two rows. Fixed in get_or_create_customer() (email
normalization + phone as a fallback identity key) and — in a follow-up fix,
see TestUpdateCustomerDetailsPhoneMatching below — update_customer_details()
(the Sales Agent's mid-conversation path), which initially only got the
email-case fix and still had the same phone-dedup gap. Both functions now
share the matching logic via MemoryManager._find_matching_customer().
"""
import uuid

import pytest

from app.memory.models import Customer
from app.memory.memory_manager import PLACEHOLDER_EMAIL_DOMAIN, _normalize_email, _normalize_phone


def _placeholder_email() -> str:
    # Customer.email is NOT NULL, so "no real identity yet" is represented the
    # same way create_placeholder_customer() represents it — a placeholder
    # address, not an empty string.
    return f"visitor_{uuid.uuid4().hex}@{PLACEHOLDER_EMAIL_DOMAIN}"


class TestEmailCaseAndWhitespaceNormalization:
    def test_different_case_resolves_to_the_same_customer(self, memory, db_session):
        c1 = memory.get_or_create_customer(email="CaseTest@Example.com", name="Case Test")
        db_session.commit()
        c2 = memory.get_or_create_customer(email="casetest@example.com", name="Case Test")
        db_session.commit()
        assert c1.id == c2.id

    def test_surrounding_whitespace_resolves_to_the_same_customer(self, memory, db_session):
        c1 = memory.get_or_create_customer(email="whitespace@example.com", name="W")
        db_session.commit()
        c2 = memory.get_or_create_customer(email="  whitespace@example.com  ", name="W")
        db_session.commit()
        assert c1.id == c2.id

    def test_new_customers_are_stored_in_normalized_form(self, memory, db_session):
        c = memory.get_or_create_customer(email="Stored@Example.com", name="Stored")
        db_session.commit()
        assert c.email == "stored@example.com"

    def test_across_two_real_endpoints_public_intake_then_chat(self, client, memory):
        r1 = client.post(
            "/api/v1/public/estimate-request",
            json={"name": "Endpoint Test", "email": "EndpointTest@Example.com"},
        )
        assert r1.status_code == 200
        r2 = client.post(
            "/api/v1/chat/message",
            json={"message": "hi", "email": "endpointtest@example.com", "name": "Endpoint Test"},
        )
        assert r2.status_code == 200
        # The email (any casing) is recognised as the existing customer's — and,
        # because a chat cannot prove who is typing, recorded as a claim rather than
        # attached (BE-1 S2): the chat gets its own placeholder customer.
        from tests.chat_support import customer_of
        from app.memory.memory_manager import is_placeholder_email
        assert is_placeholder_email(customer_of(memory._db, r2.json()["session_token"]).email)

        rows = memory._db.execute(
            __import__("sqlalchemy").select(Customer).where(
                Customer.email == "endpointtest@example.com"
            )
        ).scalars().all()
        assert len(rows) == 1  # exactly one customer, not two


class TestPhoneFallbackMatching:
    def test_same_phone_no_conflicting_real_email_resolves_to_one_customer(self, memory, db_session):
        """The email side is 'weak' on the second call (no email at all), so
        phone alone is enough to recognize the same person."""
        c1 = memory.get_or_create_customer(
            email="phone-a@example.com", name="Phone Test", phone="9876500001",
        )
        db_session.commit()
        c2 = memory.get_or_create_customer(
            email=_placeholder_email(), name="Phone Test", phone="9876500001",
        )
        db_session.commit()
        assert c1.id == c2.id

    def test_country_code_prefix_vs_bare_number_still_matches(self, memory, db_session):
        c1 = memory.get_or_create_customer(
            email="cc-prefix@example.com", name="A", phone="+91 98765 00002",
        )
        db_session.commit()
        c2 = memory.get_or_create_customer(
            email=_placeholder_email(), name="A", phone="9876500002",
        )
        db_session.commit()
        assert c1.id == c2.id

    def test_a_placeholder_customer_matched_and_completed_by_phone(self, memory, db_session):
        """The realistic shape of the bug: chat starts anonymous (placeholder
        email), a real phone+email arrives later through a different path."""
        placeholder = memory.create_placeholder_customer()
        db_session.commit()
        memory.update_customer(placeholder.id, performed_by="test", phone="9876500003")
        db_session.commit()

        resolved = memory.get_or_create_customer(
            email="late-identity@example.com", name="Late Identity", phone="9876500003",
        )
        db_session.commit()
        assert resolved.id == placeholder.id


class TestPhoneMatchingDoesNotOverMatch:
    """The edge case the task explicitly asks for: two GENUINELY different
    people whose phone numbers normalize to the same digits (one with a
    country-code prefix, one without) must not be silently merged when both
    sides carry a real, distinct email — a shared family/office line is a real
    possibility, and over-merging two people's leads/history is worse than
    leaving them as two records."""

    def test_two_real_distinct_emails_sharing_a_normalized_phone_stay_separate(
        self, memory, db_session
    ):
        person_one = memory.get_or_create_customer(
            email="person-one@example.com", name="Person One", phone="+919876511111",
        )
        db_session.commit()
        person_two = memory.get_or_create_customer(
            email="person-two@example.com", name="Person Two", phone="9876511111",
        )
        db_session.commit()
        assert person_one.id != person_two.id

    def test_live_through_the_public_intake_endpoint_twice(self, client, memory):
        r1 = client.post(
            "/api/v1/public/estimate-request",
            json={"name": "Person One", "email": "live-person-one@example.com", "phone": "+919876522222"},
        )
        r2 = client.post(
            "/api/v1/public/estimate-request",
            json={"name": "Person Two", "email": "live-person-two@example.com", "phone": "9876522222"},
        )
        assert r1.status_code == 200 and r2.status_code == 200
        lead1 = memory.get_lead(r1.json()["lead_id"])
        lead2 = memory.get_lead(r2.json()["lead_id"])
        assert lead1.customer_id != lead2.customer_id


class TestNormalizationHelpers:
    def test_normalize_email(self):
        assert _normalize_email("  Foo@Bar.COM ") == "foo@bar.com"
        assert _normalize_email(None) is None
        assert _normalize_email("") is None

    def test_normalize_phone_strips_formatting_and_country_code(self):
        assert _normalize_phone("+91 98765 00001") == "9876500001"
        assert _normalize_phone("919876500001") == "9876500001"
        assert _normalize_phone("9876500001") == "9876500001"
        assert _normalize_phone(None) is None
        assert _normalize_phone("") is None

    def test_normalize_phone_does_not_strip_a_genuine_12_digit_number_incorrectly(self):
        # A 12-digit string NOT starting with the India country code is left
        # alone — only the specific 91-prefix + 10-digit shape is stripped.
        assert _normalize_phone("123456789012") == "123456789012"


class TestUpdateCustomerDetailsPhoneMatching:
    """The same phone-fallback matching Phase 1 added to get_or_create_customer,
    applied to update_customer_details() too. A match merges only on the staff
    paths (allow_merge=True); the chat records it as a claim instead (BE-1 S2)."""

    def test_placeholder_customer_matched_by_phone_during_update(self, memory, db_session):
        known = memory.get_or_create_customer(
            email="known-phone@example.com", name="Known Person", phone="9876500010",
        )
        db_session.commit()
        placeholder = memory.create_placeholder_customer()
        db_session.commit()

        surviving = memory.update_customer_details(
            customer_id=placeholder.id, name="Known Person", email=None,
            phone="9876500010", performed_by="staff", allow_merge=True,
        )
        db_session.commit()
        assert surviving is not None
        assert surviving.id == known.id

    def test_country_code_prefix_vs_bare_number_still_matches_on_update(self, memory, db_session):
        known = memory.get_or_create_customer(
            email="cc-prefix-update@example.com", name="A", phone="9876500011",
        )
        db_session.commit()
        placeholder = memory.create_placeholder_customer()
        db_session.commit()

        surviving = memory.update_customer_details(
            customer_id=placeholder.id, name="A", email=None,
            phone="+91 98765 00011", performed_by="staff", allow_merge=True,
        )
        db_session.commit()
        assert surviving.id == known.id

    def test_two_real_distinct_emails_sharing_a_phone_are_not_merged_on_update(self, memory, db_session):
        """The same safety case as get_or_create_customer: person_two already
        has a real, distinct email of their own, so an update that merely adds
        a phone matching person_one's must NOT merge them."""
        person_one = memory.get_or_create_customer(
            email="update-person-one@example.com", name="Person One", phone="9876500012",
        )
        db_session.commit()
        person_two = memory.get_or_create_customer(
            email="update-person-two@example.com", name="Person Two",
        )
        db_session.commit()

        surviving = memory.update_customer_details(
            customer_id=person_two.id, name="Person Two", email=None,
            phone="9876500012", performed_by="staff", allow_merge=True,
        )
        db_session.commit()
        assert surviving is not None
        assert surviving.id == person_two.id  # unchanged — no merge happened
        assert person_one.id != person_two.id

    def test_live_through_the_real_chat_endpoint(self, client, memory):
        """The actual call site: app/agents/sales_agent.py passes the phone through
        to update_customer_details(), which recognises the match — and, from the
        anonymous chat, records it for staff instead of attaching (BE-1 S2)."""
        r1 = client.post(
            "/api/v1/public/estimate-request",
            json={"name": "Chat Phone Match", "email": "chat-phone-match@example.com", "phone": "9876500013"},
        )
        assert r1.status_code == 200
        known_customer_id = memory.get_lead(r1.json()["lead_id"]).customer_id

        # A brand-new, anonymous chat conversation that only ever gives a
        # phone number matching the customer above — no email at all.
        r2 = client.post(
            "/api/v1/chat/message",
            json={"message": "hi, my number is 9876500013, thinking about solar"},
        )
        assert r2.status_code == 200
        from tests.chat_support import customer_of, lead_of
        token = r2.json()["session_token"]
        assert customer_of(memory._db, token).id != known_customer_id
        notes = [e.description for e in memory.get_lead_context(lead_of(memory._db, token).id)["timeline"]]
        assert any(f"Possible returning customer #{known_customer_id} (unverified" in (n or "") for n in notes)


class TestCalculatorFieldsOnEstimateRequest:
    """BE-1 C7: the configurator-lead mirror's calculator fields are kept."""

    def test_calculator_fields_land_in_collected_fields(self, client, memory):
        body = {"name": "Calc", "email": "calc@example.com", "pin_code": "700091", "discom": "CESC",
                "monthly_units": 450, "sizing_method": "by_units", "floors": 2, "access_difficulty": "easy",
                "shading": "partial", "subsidy_interest": "yes", "indicative_total_inr": 245000.0}
        r = client.post("/api/v1/public/estimate-request", json=body)
        assert r.status_code == 200, r.text
        fields = memory.get_lead(r.json()["lead_id"]).collected_fields
        for key in ("pin_code", "discom", "monthly_units", "sizing_method", "floors", "access_difficulty",
                    "shading", "subsidy_interest", "indicative_total_inr"):
            assert fields[key] == body[key], key

    @pytest.mark.parametrize("field, value", [("pin_code", "7000"), ("subsidy_interest", "maybe"),
                                              ("discom", "x" * 101), ("indicative_total_inr", -1)])
    def test_bounded(self, client, field, value):
        r = client.post("/api/v1/public/estimate-request",
                        json={"name": "Calc", "email": "calc2@example.com", field: value})
        assert r.status_code == 422
