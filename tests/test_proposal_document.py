"""BE-13 — the proposal document: two fixed layouts, structured data, staff editing, Raju
Paul as the only signatory. app/proposals/document*.py, app/api/routes/proposal_document.py,
config/proposal_document.yaml. Test customers are invented."""
from __future__ import annotations

import re
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.memory.memory_manager import MemoryManager
from app.memory.models import AuditLog, PipelineStage, Proposal, ProposalStatus
from app.pricing.amount_words import amount_in_words
from app.proposals import document as d
from app.proposals import document_config, document_render, reference
from tests.conftest import TEST_DATABASE_URL
from tests.onboarding_support import kyc_ready, onboarding_ready, territories  # noqa: F401 — fixtures
from tests.proposal_document_support import (
    ADMIN, DASH, SAMPLE_DAY, compact_document, complete_document, detailed_document, document, edit, pdf_text,
    require_weasyprint,
)

SALES = {"X-Employee-Id": "emp-sales-1", "X-Employee-Role": "sales"}
OTHER_SALES = {"X-Employee-Id": "emp-sales-2", "X-Employee-Role": "sales"}
ENG = {"X-Employee-Id": "emp-eng-1", "X-Employee-Role": "engineer"}
MGMT = {"X-Employee-Id": "emp-mgmt-1", "X-Employee-Role": "management"}
ROLE_HEADERS = {"admin": ADMIN, "sales": SALES, "engineer": ENG, "management": MGMT}
LEAD_FIELDS = {"project_type": "rooftop", "segment": "residential", "location": "Kolkata", "capacity_kw": 3}


def _chat_lead(_isolated_db, email="doc-test@example.in", **fields):
    session = _isolated_db()
    try:
        memory = MemoryManager(session)
        customer = memory.get_or_create_customer(email=email, name="Test Customer", performed_by="test")
        lead = memory.create_lead(customer_id=customer.id, performed_by="test")
        memory.update_lead_fields(lead_id=lead.id, new_fields={"name": "Test Customer", "email": email,
                                                               **LEAD_FIELDS, **fields},
                                  performed_by="test", stage=PipelineStage.qualified)
        session.commit()
        return customer.id, lead.id
    finally:
        session.close()


def _draft(client, _isolated_db, headers=SALES, **fields):
    customer_id, lead_id = _chat_lead(_isolated_db, **fields)
    r = client.post(f"{DASH}/proposals/draft", json={"lead_id": lead_id, "customer_id": customer_id}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["proposal_id"]


def _approve(client, pid, headers=ADMIN):
    return client.post(f"{DASH}/proposals/{pid}/approve", headers=headers)


# ── Layout rule ──────────────────────────────────────────────────────────────────

class TestLayoutRule:
    @pytest.mark.parametrize("kwp,layout", [("1", "compact"), ("10", "compact"), ("10.00", "compact"),
                                            ("10.01", "detailed"), ("52", "detailed")])
    def test_capacity_decides_the_layout(self, kwp, layout):
        assert document_config.layout_for(Decimal(kwp)) == layout
        doc = d.ProposalDocument(plant=d.Plant(capacity_kwp=Decimal(kwp)))
        assert d.figures(doc)["layout"] == layout

    def test_the_layout_is_not_a_staff_choice(self):
        assert "layout" not in document_config.EDITABLE_SECTIONS
        with pytest.raises(ValueError):
            d.ProposalDocument.model_validate({"layout": "detailed"})


# ── Figures against Grid Nova's two samples ──────────────────────────────────────

class TestFiguresMatchTheSamples:
    def test_compact_three_kwp_pm_surya_ghar(self):
        f = d.figures(compact_document())
        assert (f["basic"], f["gst"], f["total"]) == ("185000.00", "16465.00", "201465.00")
        assert [p["amount"] for p in f["payments"]] == ["40293.00", "120879.00", "40293.00"]
        assert [p["percent"] for p in f["payments"]] == ["20", "60", "20"]
        assert f["subsidy"]["applies"] is True
        assert [(s["kw"], s["rate"], s["amount"]) for s in f["subsidy"]["slabs"]] == [
            ("2", "30000.00", "60000.00"), ("1", "18000.00", "18000.00")]
        assert (f["subsidy"]["total"], f["subsidy"]["effective_cost"]) == ("78000.00", "123465.00")
        assert (f["generation"]["monthly_units"], f["generation"]["annual_units"]) == (360, 4320)
        assert (f["space_required_sqft"]["low"], f["space_required_sqft"]["high"]) == (250, 300)
        assert f["words"]["total"] == "Rupees Two Lakh One Thousand Four Hundred Sixty-Five Only"
        assert f["whole_rupees"] is True and f["validity_days"] == 15

    def test_detailed_fifty_two_kwp(self):
        f = d.figures(detailed_document())
        assert (f["basic"], f["gst"], f["total"]) == ("2860000.00", "254540.00", "3114540.00")
        assert [p["amount"] for p in f["payments"]] == ["778635.00", "1868724.00", "467181.00"]
        assert f["words"] == {"basic": "Rupees Twenty-Eight Lakh Sixty Thousand Only",
                              "gst": "Rupees Two Lakh Fifty-Four Thousand Five Hundred Forty Only",
                              "total": "Rupees Thirty-One Lakh Fourteen Thousand Five Hundred Forty Only"}
        assert (f["generation"]["monthly_units"], f["generation"]["annual_units"]) == (6240, 74880)
        assert f["space_required_sqft"]["single"] == 4300
        assert f["subsidy"]["applies"] is False and f["layout"] == "detailed"

    def test_the_compact_price_is_the_price_list(self):
        from app.pricing import price_list
        q = price_list.quote("NOVA_ASSURED", "SINGLE", 3)
        assert d.figures(compact_document())["basic"] == str(q.basic)

    def test_payments_always_add_up_to_the_total(self):
        doc = detailed_document()
        doc.price = d.Price(mode="staff_basic", basic=Decimal("1234567.89"))
        doc.payment_terms = [d.PaymentTerm(percent=Decimal("33.33"), label="a"),
                             d.PaymentTerm(percent=Decimal("33.33"), label="b"),
                             d.PaymentTerm(percent=Decimal("33.34"), label="c")]
        f = d.figures(doc)
        assert sum(Decimal(p["amount"]) for p in f["payments"]) == Decimal(f["total"])
        assert f["whole_rupees"] is False

    def test_a_rate_per_kwp_times_capacity(self):
        doc = d.set_price(detailed_document(), rate_per_kwp=Decimal("55000"))
        assert d.figures(doc)["basic"] == "2860000.00"

    def test_a_total_set_whole_stays_exact(self):
        doc = d.set_price(detailed_document(), total=Decimal("3114540"))
        f = d.figures(doc)
        assert (f["basic"], f["gst"], f["total"]) == ("2860000.00", "254540.00", "3114540.00")

    @pytest.mark.parametrize("dcr,opted,segment,applies", [
        (True, True, "residential", True), (False, True, "residential", False),
        (True, False, "residential", False), (True, True, "commercial", False)])
    def test_subsidy_only_for_residential_pm_surya_ghar_with_dcr(self, dcr, opted, segment, applies):
        doc = compact_document()
        doc.plant.dcr, doc.pm_surya_ghar.subsidy_opted, doc.segment = dcr, opted, segment
        f = d.figures(doc)
        assert f["subsidy"]["applies"] is applies
        assert (f["subsidy"]["effective_cost"] is not None) is applies

    def test_housing_society_gets_no_residential_subsidy(self):
        doc = compact_document()
        doc.housing_society = True
        assert d.figures(doc)["subsidy"]["applies"] is False


class TestAmountsInWords:
    @pytest.mark.parametrize("amount,words", [
        ("100000", "Rupees One Lakh Only"),
        ("10000000", "Rupees One Crore Only"),
        ("9999999", "Rupees Ninety-Nine Lakh Ninety-Nine Thousand Nine Hundred Ninety-Nine Only"),
        ("100000000", "Rupees Ten Crore Only"),
        ("101000001", "Rupees Ten Crore Ten Lakh One Only"),
        ("201465", "Rupees Two Lakh One Thousand Four Hundred Sixty-Five Only"),
        ("16465.50", "Rupees Sixteen Thousand Four Hundred Sixty-Five and Fifty Paise Only"),
    ])
    def test_indian_system(self, amount, words):
        assert amount_in_words(Decimal(amount)) == words


class TestIndianGrouping:
    @pytest.mark.parametrize("amount,whole,text", [
        ("201465.00", True, "2,01,465"), ("3114540.00", True, "31,14,540"), ("10000000.00", True, "1,00,00,000"),
        ("999.00", True, "999"), ("16465.50", False, "16,465.50"), ("201465.00", False, "2,01,465.00")])
    def test_grouping(self, amount, whole, text):
        assert d.grouped(Decimal(amount), whole) == text


# ── Reference ────────────────────────────────────────────────────────────────────

class TestReference:
    def test_format_keeps_the_samples_look_with_a_sequence(self):
        assert reference.format_reference(date(2026, 10, 23), 1) == "GN/23/10/2026/01"
        assert reference.format_reference(date(2026, 10, 23), 123) == "GN/23/10/2026/123"

    def test_per_day_sequence_is_unique(self, db_session):
        day = date(2026, 10, 23)
        seqs = [reference.next_sequence(db_session, day) for _ in range(5)]
        assert seqs == [1, 2, 3, 4, 5]
        assert reference.next_sequence(db_session, date(2026, 10, 24)) == 1

    def test_ist_day(self):
        # 20:00 UTC on 23 Oct is 01:30 IST on 24 Oct.
        assert reference.today_ist(datetime(2026, 10, 23, 20, 0, tzinfo=timezone.utc)) == date(2026, 10, 24)

    @pytest.mark.skipif(not TEST_DATABASE_URL, reason="needs real concurrent connections (PostgreSQL)")
    def test_concurrent_allocation_never_repeats(self, _isolated_db):
        import threading
        day, results, errors = date(2026, 10, 25), [], []
        barrier = threading.Barrier(8)

        def allocate():
            session = _isolated_db()
            try:
                barrier.wait()
                results.append(reference.next_sequence(session, day))
                session.commit()
            except Exception as exc:  # collected and asserted below
                errors.append(exc)
            finally:
                session.close()

        threads = [threading.Thread(target=allocate) for _ in range(8)]
        [t.start() for t in threads]
        [t.join(timeout=30) for t in threads]
        assert not errors and sorted(results) == list(range(1, 9))

    def test_allocated_at_approval_once_and_revision_suffix(self, client, _isolated_db, db_session):
        pid = _draft(client, _isolated_db)
        complete_document(client, pid)
        assert db_session.get(Proposal, pid).document_reference is None
        assert _approve(client, pid).status_code == 200
        db_session.expire_all()
        first = db_session.get(Proposal, pid)
        assert re.fullmatch(r"GN/\d{2}/\d{2}/\d{4}/\d{2}", first.document_reference)
        assert first.document_date == reference.today_ist()
        # A revision: sent back, drafted again, approved again → the same base with -R1.
        assert client.post(f"{DASH}/proposals/{pid}/request-revision", json={"reason": "new roof"},
                           headers=ADMIN).status_code == 200
        r = client.post(f"{DASH}/proposals/draft", json={"lead_id": first.lead_id,
                                                         "customer_id": first.lead.customer_id}, headers=SALES)
        v2 = r.json()["proposal_id"]
        assert r.json()["version"] == 2
        complete_document(client, v2)
        assert _approve(client, v2).status_code == 200
        db_session.expire_all()
        assert db_session.get(Proposal, v2).document_reference == first.document_reference + "-R1"
        # Never reallocated.
        assert reference.assign(db_session, db_session.get(Proposal, pid)) == first.document_reference

    def test_two_proposals_on_one_day_get_different_references(self, client, _isolated_db, db_session):
        a = _draft(client, _isolated_db, email="one@example.in")
        b = _draft(client, _isolated_db, email="two@example.in")
        for pid in (a, b):
            complete_document(client, pid)
            assert _approve(client, pid).status_code == 200
        db_session.expire_all()
        refs = {db_session.get(Proposal, pid).document_reference for pid in (a, b)}
        assert len(refs) == 2


# ── Aadhaar, signatory, placeholders ─────────────────────────────────────────────

class TestAadhaar:
    @pytest.mark.parametrize("value", ["123456783832", "XX63 3832", "383", "abcd"])
    def test_only_the_last_four_digits_are_accepted(self, value):
        with pytest.raises(ValueError):
            d.Addressee(aadhaar_last4=value)

    def test_rendered_masked_and_omitted_when_unknown(self):
        doc = compact_document()
        html = document_render.build_html(doc, reference="GN/23/10/2026/01", doc_date=SAMPLE_DAY, draft=False)
        assert "XXXX XXXX 3832" in html and "XX63" not in html
        doc.addressee.aadhaar_last4 = None
        html = document_render.build_html(doc, reference="GN/23/10/2026/01", doc_date=SAMPLE_DAY, draft=False)
        assert "Aadhaar:" not in html


class TestSignatory:
    def test_one_config_value_and_not_editable(self):
        cfg = document_config.load()
        assert cfg["signatory"]["name"] == "RAJU PAUL" and cfg["signatory"]["title"] == "Authorised Signatory"
        assert "signatory" not in document_config.EDITABLE_SECTIONS

    @pytest.mark.parametrize("make", [compact_document, detailed_document])
    def test_both_layouts_print_raju_paul_and_no_other_name(self, make):
        require_weasyprint()
        pdf = document_render.render(make(), reference="GN/23/10/2026/01", doc_date=SAMPLE_DAY, draft=False)
        text = "\n".join(pdf_text(pdf))
        assert text.count("RAJU PAUL") == 2   # the letter and the closing sign-off
        assert "Authorised Signatory" in text
        assert "ABHIJIT" not in text.upper() and "BHATTACHARJEE" not in text.upper()

    def test_the_dashboard_cannot_change_it(self, client, _isolated_db):
        pid = _draft(client, _isolated_db)
        r = edit(client, pid, "signatory", {"name": "SOMEONE ELSE"})
        assert r.status_code == 403 and r.json()["detail"]["code"] == "field_not_editable"


class TestRenderedPdf:
    @pytest.mark.parametrize("make,pages", [(compact_document, 3), (detailed_document, 8)])
    def test_page_count_and_no_placeholder_or_none(self, make, pages):
        require_weasyprint()
        pdf = document_render.render(make(), reference="GN/23/10/2026/01", doc_date=SAMPLE_DAY, draft=False)
        texts = pdf_text(pdf)
        assert len(texts) == pages
        whole = "\n".join(texts)
        for bad in ("[PLACEHOLDER", "[UNCONFIRMED", "DUMMY", "{", "}"):
            assert bad not in whole
        assert not re.search(r"\bNone\b", whole)
        # The page-footer line on every page but the last (the letterhead takes its place).
        for n, page in enumerate(texts[:-1], 1):
            assert re.search(rf"Page\s*{n}\s*of\s*{pages}", page)
        assert "of " + str(pages) not in texts[-1].split("Authorised Signatory")[-1]
        assert "DRAFT" not in whole

    def test_compact_pm_surya_ghar_content(self):
        require_weasyprint()
        text = "\n".join(pdf_text(document_render.render(compact_document(), reference="GN/23/10/2026/01",
                                                         doc_date=SAMPLE_DAY, draft=False)))
        for expected in ("PM SURYA GHAR SUBSIDY ELIGIBLE", "Rs. 2,01,465", "Rs. 78,000", "Rs. 1,23,465",
                         "GN/23/10/2026/01", "23.10.2026", "Within West Bengal", "25 years",
                         "Rupees Two Lakh One Thousand Four Hundred Sixty-Five Only", "+91 98300 00001"):
            assert expected in text, expected

    def test_preview_stamp(self):
        require_weasyprint()
        text = "\n".join(pdf_text(document_render.render(compact_document(), reference=None,
                                                         doc_date=SAMPLE_DAY, draft=True)))
        assert "DRAFT — NOT FOR CUSTOMER" in text and "Allocated on approval" in text

    def test_a_placeholder_never_reaches_the_html(self):
        doc = compact_document()
        doc.addressee.name = "Test"
        doc.connection.sub_division = None
        doc.customer_scope = ["Provide {discom} access."]
        html = document_render.build_html(doc, reference="GN/23/10/2026/01", doc_date=SAMPLE_DAY, draft=False)
        assert "Provide WBSEDCL access." in html
        with pytest.raises(ValueError):
            d.ProposalDocument.model_validate({**d.dump(doc), "notes": ["[PLACEHOLDER: fill me]"]})
        with pytest.raises(ValueError):
            d.ProposalDocument.model_validate({**d.dump(doc), "notes": ["Pay {secret_token}"]})

    def test_html_escapes_customer_text(self):
        doc = compact_document()
        doc.addressee.name = "<b>Evil</b> & Co"
        html = document_render.build_html(doc, reference="GN/23/10/2026/01", doc_date=SAMPLE_DAY, draft=False)
        assert "<b>Evil</b>" not in html and "&lt;b&gt;Evil&lt;/b&gt; &amp; Co" in html

    def test_hindi_and_bengali_names_render(self):
        require_weasyprint()
        from tests.test_pdf_fonts import _installed_families
        from app.pdf import fonts
        from scripts import smoke_fonts
        if not {fonts.DEVANAGARI_FAMILY, fonts.BENGALI_FAMILY} <= _installed_families():
            pytest.skip("Noto Devanagari / Bengali fonts are not installed here (the image and CI install them)")
        doc = compact_document()
        doc.addressee.name = "राजेश कुमार"
        doc.site.town = "কলকাতা"
        pdf = document_render.render(doc, reference="GN/23/10/2026/01", doc_date=SAMPLE_DAY, draft=False)
        assert smoke_fonts.missing_families(pdf) == []


# ── Drafting, missing data, approval and sending ─────────────────────────────────

class TestDraftedDocument:
    def test_every_draft_gets_a_document_from_the_data_held(self, client, _isolated_db, db_session):
        pid = _draft(client, _isolated_db)
        view = document(client, pid)
        doc = view["document"]
        assert view["has_document"] is True and view["layout"] == "compact" and view["revision"] == 0
        assert doc["addressee"]["name"] == "Test Customer" and doc["plant"]["capacity_kwp"] == "3"
        assert doc["segment"] == "residential" and doc["coverage"] is None
        assert view["signatory"] == {"name": "RAJU PAUL", "title": "Authorised Signatory",
                                     "for_line": "For Grid Nova Energy"}
        content = db_session.get(Proposal, pid).content
        assert not {s["id"] for s in content["sections"]} & {"cover_letter_intro", "standard_terms", "closing"}

    def test_missing_required_blocks_approval_and_send(self, client, _isolated_db, db_session):
        pid = _draft(client, _isolated_db)
        missing = {m["field"] for m in document(client, pid)["missing_required"]}
        assert {"price", "connection.phase"} <= missing
        r = _approve(client, pid)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "missing_required"
        assert {m["field"] for m in r.json()["detail"]["missing"]} == missing
        # A proposal approved some other way is still not sent.
        from app.proposals.send_guard import placeholder_problem
        assert placeholder_problem(db_session.get(Proposal, pid))["code"] == "missing_required"

    def test_complete_document_approves_and_sends(self, client, _isolated_db, db_session):
        pid = _draft(client, _isolated_db)
        complete_document(client, pid)
        assert _approve(client, pid).status_code == 200
        r = client.post(f"{DASH}/proposals/{pid}/send", headers=ADMIN)
        assert r.status_code == 200, r.text
        token = r.json()["accept_token"]
        page = client.get(f"/api/v1/proposals/accept/{token}").json()
        f = document(client, pid)["figures"]
        assert page["document"]["total"] == f["total"] and page["price"] == float(f["total"])
        assert page["document"]["reference"] == db_session.get(Proposal, pid).document_reference

    @pytest.mark.launch_texts_unapproved      # BE-22: drafted government text, not yet approved
    def test_uncovered_segment_keeps_manual_review(self, client, _isolated_db):
        pid = _draft(client, _isolated_db, segment="government", capacity_kw=40)
        view = document(client, pid)
        assert view["document"]["coverage"] == "government" and view["layout"] == "detailed"
        assert any(m["field"] == "segment_text" for m in view["missing_required"])

    def test_the_associate_formal_proposal_gets_the_same_document(self, client, db_session, onboarding_ready):
        """An associate-sourced up-to-10 kW PM Surya Ghar customer: zero typing."""
        from tests.test_proposal_request_agreement import _prepared
        gna, o, pid = _prepared(client, db_session, connection_phase="single_phase", equipment_package="nova_assured",
                                discom="WBSEDCL", scheme_linked=True, scheme_name="pm_surya_ghar")
        proposal = db_session.get(Proposal, pid)
        view = document(client, pid)
        doc = view["document"]
        assert view["missing_required"] == [], view["missing_required"]
        assert doc["addressee"]["name"] == "Customer 1" and doc["addressee"]["mobile"] == "9830000001"
        assert doc["connection"]["discom"] == "WBSEDCL" and doc["connection"]["phase"] == "single"
        assert doc["plant"] == {"capacity_kwp": "3", "package": "NOVA_ASSURED", "dcr": True}
        assert doc["price"]["mode"] == "price_list" and view["figures"]["total"] == "201465.00"
        assert view["figures"]["subsidy"]["applies"] is True
        assert proposal.total_value == 201465.0
        # Built by the same function as any other draft.
        rebuilt = d.build(db_session, proposal.lead, _list_ctx(db_session, proposal.lead))
        assert d.dump(rebuilt) == doc

    def test_engineer_edits_the_bill_of_quantity_without_seeing_the_price(self, client, _isolated_db):
        pid = _draft(client, _isolated_db)
        rows = [{"description": "Solar PV Modules – {module_technology}", "quantity": "{capacity_kwp} kWp",
                 "make": "Sova Solar / Equivalent", "kind": "module"}]
        r = edit(client, pid, "boq", rows, headers=ENG)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["total_value"] is None and body["requires_manager_signoff"] is None and body["figures"] is None
        assert body["document"]["view"] == "technical" and body["document"]["boq"][0]["make"] == "Sova Solar / Equivalent"

    def test_engineer_sees_only_the_technical_part(self, client, _isolated_db):
        pid = _draft(client, _isolated_db)
        view = document(client, pid, ENG)
        assert view["document"]["view"] == "technical"
        assert set(view["document"]) == {"view", "revision", "plant", "system", "boq"}
        assert view["figures"] is None and view["editable_sections"] == ["system", "boq"]
        detail = client.get(f"{DASH}/proposals/{pid}", headers=ENG).json()
        assert "price" not in str(detail["document"]) and "addressee" not in str(detail["document"])


def _list_ctx(db, lead):
    from app.proposals import price_list_content
    return price_list_content.context_for_lead(db, lead)


# ── Who may change what ──────────────────────────────────────────────────────────

SECTION_VALUES = {
    "addressee": {"customer_type": "individual", "name": "Edited Name", "address_lines": ["1 New Road"]},
    "connection": {"phase": "single", "discom": "WBSEDCL", "consumer_number": "200", "category": "Residential"},
    "site": {"address_lines": ["1 New Road"], "town": "Howrah", "state": "West Bengal"},
    "pm_surya_ghar": {"subsidy_opted": False},
    "plant": {"capacity_kwp": "4", "package": "NOVA_ASSURED", "dcr": False},
    "system": {"module_technology": "TOPCon"},
    "boq": [{"description": "Solar PV Modules – {module_technology}", "quantity": "{capacity_kwp} kWp",
             "make": "Sova Solar / Equivalent", "kind": "module"}],
    "customer_scope": ["Provide roof access."],
    "payment_terms": [{"percent": "50", "label": "On Order"}, {"percent": "50", "label": "Before Commissioning"}],
    "notes": ["Delivery within 30 days of the order."],
}


class TestPermissions:
    @pytest.mark.parametrize("role", ["admin", "sales", "engineer", "management"])
    @pytest.mark.parametrize("section", list(SECTION_VALUES))
    def test_each_role_and_section(self, client, _isolated_db, role, section):
        pid = _draft(client, _isolated_db)
        allowed = role in document_config.edit_roles(section)
        r = edit(client, pid, section, SECTION_VALUES[section], headers=ROLE_HEADERS[role])
        if allowed:
            assert r.status_code == 200, r.text
        else:
            assert r.status_code == 403 and r.json()["detail"]["code"] == "field_not_editable"

    def test_the_default_role_map(self):
        cfg = document_config.load()["edit_permissions"]
        for section in ("addressee", "connection", "site", "pm_surya_ghar", "plant", "customer_scope",
                        "payment_terms", "notes"):
            assert set(cfg[section]) == {"admin", "sales"}
        assert set(cfg["system"]) == set(cfg["boq"]) == {"admin", "sales", "engineer"}

    @pytest.mark.parametrize("section", ["header", "footer", "watermark", "letter", "quality_policy", "price",
                                         "figures", "layout", "gst"])
    def test_fixed_parts_are_not_editable(self, client, _isolated_db, section):
        pid = _draft(client, _isolated_db)
        r = edit(client, pid, section, {"x": 1})
        assert r.status_code == 403 and r.json()["detail"]["code"] == "field_not_editable"

    def test_sales_only_on_own_leads(self, client, _isolated_db):
        pid = _draft(client, _isolated_db)          # drafted by emp-sales-1, who now owns the lead
        r = edit(client, pid, "notes", ["x"], headers=OTHER_SALES)
        assert r.status_code == 403
        assert client.get(f"{DASH}/proposals/{pid}/document", headers=OTHER_SALES).status_code == 403

    @pytest.mark.parametrize("role", ["operations", "accounts"])
    def test_roles_without_lead_access(self, client, _isolated_db, role):
        pid = _draft(client, _isolated_db)
        headers = {"X-Employee-Id": f"emp-{role}-1", "X-Employee-Role": role}
        assert client.get(f"{DASH}/proposals/{pid}/document", headers=headers).status_code == 403
        assert edit(client, pid, "notes", ["x"], headers=headers).status_code == 403


class TestEditing:
    def test_payment_terms_must_total_100(self, client, _isolated_db):
        pid = _draft(client, _isolated_db)
        r = edit(client, pid, "payment_terms", [{"percent": "50", "label": "a"}, {"percent": "40", "label": "b"}])
        assert r.status_code == 422 and r.json()["detail"]["code"] == "payment_terms_total"

    def test_makes_come_from_the_catalogue_or_price_list(self, client, _isolated_db):
        pid = _draft(client, _isolated_db)
        rows = [{"description": "Modules", "quantity": "3 kWp", "make": "Sova Solar / Invented Brand"}]
        r = edit(client, pid, "boq", rows)
        assert r.status_code == 422 and r.json()["detail"] == {
            "code": "make_not_in_catalogue", "field": "boq[0].make",
            "message": "Unknown make(s) ['Invented Brand']: use the equipment catalogue's or the price list's makes, "
                       "or 'Equivalent'."}

    def test_stale_edit_is_refused(self, client, _isolated_db):
        pid = _draft(client, _isolated_db)
        assert edit(client, pid, "notes", ["first"], base_revision=0).status_code == 200
        r = edit(client, pid, "notes", ["second"], base_revision=0)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "stale_version"
        assert r.json()["detail"]["current_revision"] == 1

    def test_every_edit_is_audited_field_by_field(self, client, _isolated_db, db_session):
        pid = _draft(client, _isolated_db)
        r = edit(client, pid, "addressee", {"customer_type": "individual", "name": "New Name",
                                            "address_lines": ["1 Road"], "aadhaar_last4": "1234"}, headers=SALES)
        assert r.status_code == 200
        changes = {c["field"]: (c["before"], c["after"]) for c in r.json()["changes"]}
        assert changes["addressee.name"] == ("Test Customer", "New Name")
        assert changes["addressee.aadhaar_last4"] == (None, "1234")
        row = db_session.execute(select(AuditLog).where(AuditLog.action == "document_edited")).scalar_one()
        assert row.performed_by == "emp-sales-1" and row.entity_id == str(pid) and row.created_at is not None
        assert row.before_value["fields"]["addressee.name"] == "Test Customer"
        assert row.after_value["fields"]["addressee.name"] == "New Name"
        assert (row.before_value["revision"], row.after_value["revision"]) == (0, 1)

    def test_locked_after_approval(self, client, _isolated_db):
        pid = _draft(client, _isolated_db)
        complete_document(client, pid)
        assert _approve(client, pid).status_code == 200
        view = document(client, pid)
        assert view["locked"] is True and view["editable_sections"] == []
        r = edit(client, pid, "notes", ["too late"])
        assert r.status_code == 409 and r.json()["detail"]["code"] == "proposal_locked"

    def test_a_revision_is_a_new_version_that_keeps_the_edits(self, client, _isolated_db, db_session):
        pid = _draft(client, _isolated_db)
        complete_document(client, pid)
        assert edit(client, pid, "notes", ["Kept across the revision."]).status_code == 200
        assert _approve(client, pid).status_code == 200
        assert client.post(f"{DASH}/proposals/{pid}/request-revision", json={"reason": "x"},
                           headers=ADMIN).status_code == 200
        lead = db_session.get(Proposal, pid).lead
        v2 = client.post(f"{DASH}/proposals/draft", json={"lead_id": lead.id, "customer_id": lead.customer_id},
                         headers=SALES).json()["proposal_id"]
        view = document(client, v2)
        assert view["version"] == 2 and view["locked"] is False and view["revision"] == 0
        assert view["document"]["notes"] == ["Kept across the revision."]
        assert view["missing_required"] == []
        assert view["document"]["addressee"]["name"] == "Test Customer"
        assert set(view["document"]["edited"]) >= {"notes", "connection", "site"}

    def test_a_revision_takes_fresh_data_for_sections_staff_never_touched(self, client, _isolated_db, db_session):
        pid = _draft(client, _isolated_db)
        complete_document(client, pid)
        assert edit(client, pid, "addressee", {"customer_type": "individual", "title": "Dr.", "name": "Staff Typed",
                                               "address_lines": ["7 Park Street"]}).status_code == 200
        assert _approve(client, pid).status_code == 200
        client.post(f"{DASH}/proposals/{pid}/request-revision", json={"reason": "bigger plant"}, headers=ADMIN)
        lead = db_session.get(Proposal, pid).lead
        MemoryManager(db_session).update_lead_fields(lead_id=lead.id, new_fields={"capacity_kw": 4}, performed_by="t")
        db_session.commit()
        v2 = client.post(f"{DASH}/proposals/draft", json={"lead_id": lead.id, "customer_id": lead.customer_id},
                         headers=SALES).json()["proposal_id"]
        doc = document(client, v2)["document"]
        assert (doc["addressee"]["title"], doc["addressee"]["name"]) == ("Dr.", "Staff Typed")   # carried
        assert doc["plant"]["capacity_kwp"] == "4"                                              # fresh

    def test_a_capacity_change_reprices_and_re_evaluates_the_tier(self, client, _isolated_db, db_session):
        pid = _draft(client, _isolated_db, capacity_kw=3)
        complete_document(client, pid)
        assert client.patch(f"{DASH}/proposals/{pid}/adjust-pricing", headers=ADMIN,
                            json={"rate_per_kwp": "50000", "reason": "Per-kWp quote"}).status_code == 200
        assert db_session.get(Proposal, pid).requires_manager_signoff is False      # Rs 1.63 lakh
        r = edit(client, pid, "plant", {"capacity_kwp": "12", "package": "NOVA_ASSURED", "dcr": False})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["layout"] == "detailed"
        assert set(body["regenerated_sections"]) >= {"boq", "customer_scope", "payment_terms"}
        assert body["figures"]["basic"] == "600000.00" and body["total_value"] == 653400.0
        assert body["requires_manager_signoff"] is True                              # above Rs 5 lakh
        db_session.expire_all()
        assert db_session.get(Proposal, pid).requires_manager_signoff is True
        audit = db_session.execute(select(AuditLog).where(AuditLog.action == "document_repriced")).scalars().all()
        assert audit[-1].after_value == {"total_value": 653400.0, "requires_manager_signoff": True}
        # The standard approval route now refuses: it needs the manager sign-off.
        assert _approve(client, pid).status_code == 403

    def test_price_list_follows_the_plant(self, client, db_session, onboarding_ready):
        from tests.test_proposal_request_agreement import _prepared
        _, _, pid = _prepared(client, db_session, connection_phase="single_phase", equipment_package="nova_assured",
                              discom="WBSEDCL")
        r = edit(client, pid, "plant", {"capacity_kwp": "5", "package": "NOVA_ASSURED", "dcr": True})
        assert r.status_code == 200
        assert r.json()["figures"]["basic"] == "287000.00" and r.json()["total_value"] == 312543.0

    def test_adjust_pricing_with_a_basic_price(self, client, _isolated_db, db_session):
        pid = _draft(client, _isolated_db, capacity_kw=52, segment="commercial")
        r = client.patch(f"{DASH}/proposals/{pid}/adjust-pricing", headers=ADMIN,
                         json={"basic_price": "2860000", "reason": "Quoted"})
        assert r.status_code == 200, r.text
        assert r.json()["total_value"] == 3114540.0 and r.json()["figures"]["gst"] == "254540.00"
        assert client.patch(f"{DASH}/proposals/{pid}/adjust-pricing", headers=ADMIN,
                            json={"basic_price": "1", "new_value": 2, "reason": "x"}).status_code == 422


# ── Preview ──────────────────────────────────────────────────────────────────────

class TestPreview:
    def test_draft_preview_is_stamped_and_final_is_not(self, client, _isolated_db):
        require_weasyprint()
        pid = _draft(client, _isolated_db)
        complete_document(client, pid)
        r = client.get(f"{DASH}/proposals/{pid}/preview.pdf", headers=SALES)
        assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
        assert "DRAFT — NOT FOR CUSTOMER" in "\n".join(pdf_text(r.content))
        assert _approve(client, pid).status_code == 200
        final = "\n".join(pdf_text(client.get(f"{DASH}/proposals/{pid}/preview.pdf", headers=SALES).content))
        assert "DRAFT" not in final and "GN/" in final and "RAJU PAUL" in final

    def test_engineers_and_unpriced_drafts(self, client, _isolated_db):
        pid = _draft(client, _isolated_db)
        assert client.get(f"{DASH}/proposals/{pid}/preview.pdf", headers=ENG).status_code == 403
        r = client.get(f"{DASH}/proposals/{pid}/preview.pdf", headers=SALES)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "missing_required"

    def test_rate_limited(self, client, _isolated_db, monkeypatch):
        import app.api.routes.proposal_document as route
        monkeypatch.setattr(route, "PREVIEW_LIMIT", 1)
        pid = _draft(client, _isolated_db)
        client.get(f"{DASH}/proposals/{pid}/preview.pdf", headers=SALES)
        r = client.get(f"{DASH}/proposals/{pid}/preview.pdf", headers=SALES)
        assert r.status_code == 429 and r.json()["detail"]["code"] == "rate_limited"


def test_status_values_editable():
    from app.proposals import document_service
    assert set(document_service.EDITABLE_STATUSES) == {ProposalStatus.draft, ProposalStatus.pending_pricing_validation,
                                                      ProposalStatus.pending_approval}
