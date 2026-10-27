"""BE-18 — the Proposal Studio's engine: schema-2 figures for every project type, itemised
GST, the discount line, the savings maths, the package comparison, and the proof-check (one
test per error and per warning, the words ↔ number round trip, the rendered-PDF checks on
deliberately broken renders). Version-1 (lead) documents render exactly as before BE-18.

Expected figures are worked by hand in the comments; every name and address is invented.
"""
from __future__ import annotations

import hashlib
import html
import json
import random
import re
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.pricing import price_list
from app.pricing.amount_words import amount_in_words
from app.proposals import document as d
from app.proposals import document_render
from app.studio import checks
from app.studio import config as studio_config
from tests.proposal_document_support import SAMPLE_DAY, compact_document, detailed_document, require_weasyprint
from tests.studio_support import (
    SAMPLES, commercial_52kw, ground_mounted_1mw, housing_society_60kw, industrial_250kw, residential_3kw_pmsg,
    residential_5kw_comparison, street_lighting_40,
)

GOLDEN = Path(__file__).parent / "golden"
TODAY = date(2026, 10, 27)
VERIFY = "https://www.gridnova.in/verify/p/AB12CD34EF56"


# ── Version-1 documents are untouched ────────────────────────────────────────────

class TestVersionOneUnchanged:
    @pytest.mark.parametrize("name,make,pages", [("compact", compact_document, 3), ("detailed", detailed_document, 8)])
    def test_lead_documents_render_byte_identical_html(self, name, make, pages):
        """The HTML (and so the PDF) of both BE-13 samples is exactly what it was before BE-18."""
        page = document_render.build_html(make(), reference="GN/23/10/2026/01", doc_date=SAMPLE_DAY, draft=False,
                                          pages=pages)
        assert hashlib.sha256(page.encode()).hexdigest() == json.loads(
            (GOLDEN / "proposal_v1_html.sha256.json").read_text())[name]
        body = re.sub(r"<style>.*?</style>", "", page, flags=re.S)
        lines = [" ".join(x.split()) for x in html.unescape(re.sub(r"<[^>]+>", "\n", body)).splitlines()]
        assert "\n".join(x for x in lines if x) + "\n" == (GOLDEN / f"proposal_v1_{name}.txt").read_text()

    def test_a_lead_document_still_parses_as_version_one(self):
        doc = d.parse_any(d.dump(compact_document()))
        assert type(doc) is d.ProposalDocument and "schema_version" not in d.figures(doc)

    def test_a_studio_document_parses_as_version_two(self):
        doc = d.parse_any(d.dump(residential_3kw_pmsg()))
        assert isinstance(doc, d.StudioDocument) and d.figures(doc)["schema_version"] == 2


# ── Figures for every project type ───────────────────────────────────────────────

class TestFigures:
    def test_residential_3kw_pm_surya_ghar(self):
        f = d.figures(residential_3kw_pmsg())
        # The price master: 1,85,000 + 8.9 % = 2,01,465; CFA 2 × 30,000 + 1 × 18,000 = 78,000.
        assert (f["layout"], f["basic"], f["gst"], f["total"]) == ("compact", "185000.00", "16465.00", "201465.00")
        assert (f["subsidy"]["total"], f["subsidy"]["effective_cost"]) == ("78000.00", "123465.00")
        assert [p["amount"] for p in f["payments"]] == ["40293.00", "120879.00", "40293.00"]
        assert f["list_basic"] == "185000.00" and f["below_price_master"] is None

    def test_commercial_52kw(self):
        f = d.figures(commercial_52kw())
        # 28,60,000 + 2,54,540 = 31,14,540; 25 / 60 / 15.
        assert (f["layout"], f["total"]) == ("detailed", "3114540.00")
        assert [p["amount"] for p in f["payments"]] == ["778635.00", "1868724.00", "467181.00"]

    def test_housing_society_60kw_24_houses(self):
        f = d.figures(housing_society_60kw())
        # 60 × 52,000 = 31,20,000 + 2,77,680 = 33,97,680. Eligible min(60, 3 × 24 = 72, 500) = 60 kW
        # × 18,000 = 10,80,000 → effective 23,17,680.
        assert (f["basic"], f["gst"], f["total"]) == ("3120000.00", "277680.00", "3397680.00")
        sub = f["subsidy"]
        assert (sub["kind"], sub["eligible_kw"], sub["limited_by"], sub["total"], sub["effective_cost"]) == (
            "housing_society", "60", "installed", "1080000.00", "2317680.00")

    def test_housing_society_capped_by_houses_and_by_500kw(self):
        doc = housing_society_60kw()
        doc.plant.capacity_kwp, doc.project.houses = Decimal(100), 10        # 3 × 10 = 30 kW
        assert d.figures(doc)["subsidy"]["eligible_kw"] == "30"
        doc.plant.capacity_kwp, doc.project.houses = Decimal(800), 400       # 500 kW cap
        assert d.figures(doc)["subsidy"]["total"] == "9000000.00"

    def test_housing_society_special_category_rate(self):
        doc = housing_society_60kw()
        doc.site.state = "Sikkim"
        assert d.figures(doc)["subsidy"]["total"] == "1188000.00"          # 60 × 19,800

    def test_housing_society_without_houses_shows_no_subsidy_and_needs_them(self):
        doc = housing_society_60kw()
        doc.project.houses = None
        f = d.figures(doc)
        assert f["subsidy"]["applies"] is False
        assert "project.houses" in {m["field"] for m in d.studio_missing_required(doc, f)}

    def test_industrial_250kw_with_discount(self):
        f = d.figures(industrial_250kw())
        # 1,25,00,000 − 5,00,000 = 1,20,00,000 + 10,68,000 = 1,30,68,000.
        assert (f["basic"], f["gst"], f["total"]) == ("12000000.00", "1068000.00", "13068000.00")
        assert f["discount"] == {"price_before_discount": "12500000.00", "amount": "500000.00",
                                 "reason": "Order placed this quarter"}

    def test_ground_mounted_1mwp(self):
        f = d.figures(ground_mounted_1mw())
        assert (f["layout"], f["basic"], f["total"]) == ("detailed", "38000000.00", "41382000.00")

    @pytest.mark.parametrize("ptype,registry", [("ground_mounted", {"PRO-7"}),
                                                ("government_institutional", {"PRO-8", "PRO-11"}),
                                                ("pm_kusum", {"PRO-10"}), ("generic", {"PRO-12"})])
    def test_uncovered_types_compute_but_are_not_ready(self, ptype, registry):
        doc = commercial_52kw().model_copy(update={"project": d.StudioProject(project_type=ptype)})
        d.apply_project_type(doc)
        assert d.figures(doc)["total"] == "3114540.00"
        assert {b["registry"] for b in d.content_blockers(doc)} == registry

    @pytest.mark.parametrize("ptype", ["residential_rooftop", "housing_society", "commercial_rooftop",
                                       "industrial_rooftop"])
    def test_covered_types_have_no_blockers(self, ptype):
        doc = commercial_52kw().model_copy(update={"project": d.StudioProject(project_type=ptype)})
        d.apply_project_type(doc)
        assert d.content_blockers(doc) == []

    def test_layout_by_type(self):
        small = residential_3kw_pmsg()
        assert d.layout(small) == "compact"
        small.project.project_type = "housing_society"
        assert d.layout(small) == "detailed"                                 # always detailed
        assert d.layout(street_lighting_40()) == "itemised"

    def test_defaults_payment_terms_by_size_not_layout(self):
        doc = d.new_studio_document("housing_society")
        doc.plant.capacity_kwp = Decimal(8)
        d.refresh_defaults(doc)
        assert [t.percent for t in doc.payment_terms] == [20, 60, 20]       # ≤ 10 kW, though detailed
        doc.plant.capacity_kwp = Decimal(12)
        d.refresh_defaults(doc)
        assert [t.percent for t in doc.payment_terms] == [25, 60, 15]

    def test_validity_comes_from_the_document(self):
        doc = commercial_52kw()
        doc.validity_days = 30
        assert d.figures(doc)["validity_days"] == 30


class TestItemised:
    def test_gst_grouped_by_rate_and_totals(self):
        f = d.figures(street_lighting_40())
        # 40 × 18,500 = 7,40,000 and 40 × 6,200 = 2,48,000 at 5 %; 60,000 at 18 %.
        assert [x["amount"] for x in f["items"]] == ["740000.00", "248000.00", "60000.00"]
        assert f["gst_groups"] == [{"rate": "0.05", "percent": "5", "taxable": "988000.00", "gst": "49400.00"},
                                   {"rate": "0.18", "percent": "18", "taxable": "60000.00", "gst": "10800.00"}]
        assert (f["basic"], f["gst"], f["total"]) == ("1048000.00", "60200.00", "1108200.00")
        assert sum(Decimal(p["amount"]) for p in f["payments"]) == Decimal(f["total"])
        assert f["words"]["total"] == "Rupees Eleven Lakh Eight Thousand Two Hundred Only"

    def test_rounding_per_line(self):
        doc = street_lighting_40()
        doc.items = [d.ItemRow(description="LED", make="Branded", quantity=Decimal("3"), unit="Nos",
                               unit_rate=Decimal("333.33"), category="solar_street_light")]
        f = d.figures(doc)
        assert (f["items"][0]["amount"], f["items"][0]["gst"], f["total"]) == ("999.99", "50.00", "1049.99")

    def test_unknown_category_or_unit_refused(self):
        with pytest.raises(ValueError):
            d.ItemRow(description="x", make="y", quantity=1, unit="Nos", unit_rate=1, category="fireworks")
        with pytest.raises(ValueError):
            d.ItemRow(description="x", make="y", quantity=1, unit="crates", unit_rate=1, category="solar_street_light")

    def test_issue_blocked_until_rates_and_warranty_confirmed(self):
        assert {b["registry"] for b in d.content_blockers(street_lighting_40())} == {"STU-6", "STU-7", "STU-8"}


class TestDiscountLine:
    def _html(self, doc):
        return document_render.build_html(doc, reference="GN/1", doc_date=TODAY, draft=False, pages=9,
                                          verify_url=VERIFY)

    def test_on(self):
        page = self._html(industrial_250kw())
        assert "Less: Special discount" in page and "1,25,00,000" in page and "1,20,00,000" in page

    def test_off_prints_only_the_offered_price(self):
        doc = industrial_250kw()
        doc.display.show_discount_line = False
        page = self._html(doc)
        assert "Less: Special discount" not in page and "1,25,00,000" not in page and "1,20,00,000" in page

    def test_a_discount_never_on_a_total(self):
        from app.studio.service import StudioError, _priced
        with pytest.raises(StudioError) as exc:
            _priced(commercial_52kw(), {"mode": "staff_total", "total": "3000000",
                                        "discount": {"amount": "1000", "reason": "test"}})
        assert exc.value.field == "price.discount"

    def test_below_the_master_needs_a_reason(self):
        from app.studio.service import StudioError, _priced
        doc = residential_3kw_pmsg()
        with pytest.raises(StudioError) as exc:
            _priced(doc, {"mode": "staff_basic", "basic": "180000"})
        assert exc.value.code == "reason_required"
        new = _priced(doc, {"mode": "staff_basic", "basic": "180000", "reason": "Matched a competitor"})
        assert d.figures(new)["below_price_master"] == {"list_basic": "185000.00", "basic": "180000.00",
                                                        "percent": "2.70"}


# ── Savings ──────────────────────────────────────────────────────────────────────

class TestSavings:
    def test_hand_checked_250kw(self):
        s = d.figures(industrial_250kw())["savings"]
        # 250 kWp × 120 × 12 = 3,60,000 units, all self-consumed (48,0000 a year consumed) × 8.50.
        assert (s["year1_generation_units"], s["year1_savings"]) == (360000, "3060000")
        rows = {r["year"]: r for r in s["table"]}
        # Year 5: 1 − 0.02 − 3 × 0.0055 = 0.9635 → 3,46,860 units → 29,48,310.
        assert (rows[5]["generation_units"], rows[5]["savings"]) == (346860, "2948310")
        # Year 25: 1 − 0.02 − 23 × 0.0055 = 0.8535 → 3,07,260 units → 26,11,710.
        assert (rows[25]["generation_units"], rows[25]["savings"]) == (307260, "2611710")
        assert [r["year"] for r in s["table"]] == [1, 5, 10, 15, 20, 25]
        # Payback on the effective cost: 1,30,68,000 ÷ 30,60,000 = 4.27 → 4.3 years.
        assert s["payback_years"] == "4.3"
        expected_total = sum(round(360000 * (1 if y == 1 else 1 - 0.02 - (y - 2) * 0.0055)) * Decimal("8.5")
                             for y in range(1, 26))
        assert Decimal(s["total_savings_25_years"]) == expected_total

    def test_export_and_bill_based_consumption(self):
        doc = residential_5kw_comparison()
        # 5 kWp → 600 units a month, 7,200 a year. Bill 4,000 − fixed 200 at 8 → 475 units a month.
        doc.savings = d.SavingsInputs(monthly_bill=Decimal(4000), fixed_charges=Decimal(200),
                                      tariff_per_unit=Decimal(8), export_rate=Decimal(3))
        s = d.figures(doc)["savings"]
        assert s["monthly_consumption_units"] == 475
        # 5,700 self-consumed × 8 = 45,600 + 1,500 exported × 3 = 4,500 → 50,100.
        assert s["year1_savings"] == "50100"
        assert s["table"][0]["exported_units"] == 1500

    def test_no_export_credit_by_default(self):
        doc = residential_5kw_comparison()
        doc.savings = d.SavingsInputs(monthly_units=Decimal(300), tariff_per_unit=Decimal(8))
        assert d.figures(doc)["savings"]["year1_savings"] == "28800"            # 3,600 × 8 only

    def test_topcon_degrades_less(self):
        doc = industrial_250kw()
        doc.system.module_technology = "TOPCon"
        rows = {r["year"]: r for r in d.figures(doc)["savings"]["table"]}
        assert rows[5]["generation_units"] == round(360000 * (1 - 0.01 - 3 * 0.004))

    def test_payback_on_the_cost_after_subsidy(self):
        doc = residential_3kw_pmsg()
        doc.savings = d.SavingsInputs(monthly_units=Decimal(400), tariff_per_unit=Decimal(8))
        s = d.figures(doc)["savings"]
        # 4,320 units × 8 = 34,560; effective 1,23,465 ÷ 34,560 = 3.57 → 3.6.
        assert (s["effective_cost"], s["payback_years"]) == ("123465.00", "3.6")

    def test_no_page_without_a_tariff(self):
        doc = industrial_250kw()
        doc.savings = d.SavingsInputs(monthly_units=Decimal(100))
        assert d.figures(doc)["savings"] is None
        assert "savings" in {m["field"] for m in d.studio_missing_required(doc)}

    def test_the_fixed_note_and_no_tax_claim(self):
        s = d.figures(industrial_250kw())["savings"]
        assert s["note"].startswith("Indicative estimate based on the tariff and consumption you shared")
        assert s["depreciation_line"] is None
        assert "tax" not in studio_config.load()["savings"]["note"].lower()


# ── Package comparison ───────────────────────────────────────────────────────────

class TestPackageComparison:
    def test_every_price_master_row(self):
        """All 36 price rows (18 sizes × 2 packages) come out of the comparison exactly as the
        price master quotes them; Signature = Assured × 1.12 before GST."""
        data = price_list.load()
        seen = 0
        for phase, sizes in data["assured_basic_inr"].items():
            for kw in sizes:
                doc = residential_5kw_comparison()
                doc.connection.phase = phase.lower()
                doc.plant.capacity_kwp = Decimal(kw)
                rows = {r["package"]: r for r in d.package_comparison(doc)}
                for package in price_list.PACKAGES:
                    q = price_list.quote(package, phase, kw)
                    assert (rows[package]["basic"], rows[package]["gst"], rows[package]["total"]) == (
                        str(q.basic), str(q.gst), str(q.final))
                    seen += 1
                assert Decimal(rows["NOVA_SIGNATURE"]["basic"]) == d.money(
                    Decimal(rows["NOVA_ASSURED"]["basic"]) * Decimal("1.12"))
        assert seen == 36

    def test_only_up_to_10kw_and_list_priced(self):
        doc = commercial_52kw()
        assert d.figures(doc)["package_comparison"] is None
        doc = residential_5kw_comparison()
        doc.connection.phase = "three"
        doc.plant.capacity_kwp = Decimal(2)                         # no 2 kW three-phase row
        assert d.package_comparison(doc) is None


# ── Proof-check: words, GSTIN ────────────────────────────────────────────────────

class TestWordsRoundTrip:
    @pytest.mark.parametrize("amount", ["0.50", "1", "100000", "9999999999.99", "100000000000", "1000000000",
                                        "12345678901.01", "10000000", "99.99", "1100.10"])
    def test_edges(self, amount):
        assert checks.words_to_amount(amount_in_words(Decimal(amount))) == Decimal(amount)

    def test_hundred_random_amounts(self):
        rng = random.Random(18)
        for _ in range(100):
            amount = Decimal(rng.randrange(1, 10 ** 12)) / 100
            assert checks.words_to_amount(amount_in_words(amount)) == amount

    @pytest.mark.parametrize("bad", ["Two Lakh Only", "Rupees Two Lakh", "Rupees Banana Only",
                                     "Rupees Ten and Fifty Only", "Rupees Twenty-Twelve Only"])
    def test_refuses_what_it_cannot_read(self, bad):
        with pytest.raises(checks.WordsError):
            checks.words_to_amount(bad)


class TestGstin:
    @pytest.mark.parametrize("gstin,ok", [("19ABEFG7809A1ZI", True), ("27AAPFU0939F1ZV", True),
                                          ("27AAPFU0939F1ZW", False), ("27AAPFU0939F1Z", False),
                                          ("XXAAPFU0939F1ZV", False), ("", False)])
    def test_checksum(self, gstin, ok):
        assert checks.gstin_valid(gstin) is ok


# ── Proof-check: one test per error ──────────────────────────────────────────────

def _ids(items):
    return {c["id"] for c in items}


def _doc_checks(doc, figs=None, **kw):
    return checks.document_checks(doc, figs or d.figures(doc), today=TODAY, **kw)


class _Customer:
    def __init__(self, gstin):
        self.gstin = gstin


class TestErrors:
    def test_clean_samples_have_no_errors(self):
        for name, make in SAMPLES.items():
            doc = make()
            errors = [c for c in _doc_checks(doc) if c["level"] == "error" and c["id"] != "content_not_ready"]
            assert errors == [], (name, errors)

    def test_missing_required(self):
        doc = d.new_studio_document("commercial_rooftop")
        assert "missing_required" in _ids(_doc_checks(doc))

    def test_content_not_ready(self):
        assert "content_not_ready" in _ids(_doc_checks(ground_mounted_1mw()))

    def test_total_mismatch(self):
        doc = commercial_52kw()
        f = d.figures(doc)
        f["total"] = "3114541.00"
        assert "total_mismatch" in _ids(_doc_checks(doc, f))

    def test_milestones_mismatch(self):
        doc = commercial_52kw()
        f = d.figures(doc)
        f["payments"][0]["amount"] = "1.00"
        assert "milestones_mismatch" in _ids(_doc_checks(doc, f))

    def test_payment_percentages(self):
        doc = commercial_52kw()
        doc.payment_terms[0].percent = Decimal(30)                       # assignment is not validated
        assert "payment_percent" in _ids(_doc_checks(doc))

    def test_words_mismatch(self):
        doc = commercial_52kw()
        f = d.figures(doc)
        f["words"]["total"] = "Rupees Thirty-One Lakh Only"
        assert "words_mismatch" in _ids(_doc_checks(doc, f))

    def test_subsidy_shown_when_ineligible(self):
        doc = residential_3kw_pmsg()
        f = d.figures(doc)
        doc.plant.dcr = False
        assert "subsidy_ineligible" in _ids(_doc_checks(doc, f))

    def test_subsidy_above_cap(self):
        doc = residential_3kw_pmsg()
        f = d.figures(doc)
        f["subsidy"]["total"] = "90000.00"
        assert "subsidy_above_cap" in _ids(_doc_checks(doc, f))

    def test_mobile(self):
        doc = residential_3kw_pmsg()
        doc.addressee.mobile = "12345"
        assert "mobile_invalid" in _ids(_doc_checks(doc))

    def test_gstin(self):
        assert "gstin_invalid" in _ids(_doc_checks(commercial_52kw(), customer=_Customer("27AAPFU0939F1ZW")))
        assert "gstin_invalid" not in _ids(_doc_checks(commercial_52kw(), customer=_Customer("27AAPFU0939F1ZV")))

    def test_validity_in_the_past(self):
        assert "validity_past" in _ids(_doc_checks(commercial_52kw(), valid_until=date(2026, 10, 26)))

    def test_make_not_in_catalogue(self, db_session):
        doc = commercial_52kw()
        doc.boq[0].make = "Acme Panels"
        assert "make_not_in_catalogue" in _ids(_doc_checks(doc, db=db_session))


# ── Proof-check: the rendered PDF ────────────────────────────────────────────────

def _render_html(page: str) -> bytes:
    from weasyprint import HTML

    from app.pdf.fetcher import safe_url_fetcher
    return HTML(string=page, base_url=None, url_fetcher=safe_url_fetcher()).write_pdf()


class TestRenderedChecks:
    @pytest.mark.parametrize("name", sorted(SAMPLES))
    def test_every_sample_passes(self, name):
        require_weasyprint()
        doc = SAMPLES[name]()
        pdf = document_render.render(doc, reference="GN/27/10/2026/01", doc_date=TODAY, draft=False,
                                     verify_url=VERIFY, pdf_a=True)
        assert checks.pdf_checks(doc, pdf, qr_size=document_render.qr_png_size(VERIFY)) == []

    def _page(self, doc):
        return document_render.build_html(doc, reference="GN/1", doc_date=TODAY, draft=False, pages=3,
                                          verify_url=VERIFY)

    def test_capacity_mismatch(self):
        require_weasyprint()
        doc = residential_3kw_pmsg()
        page = self._page(doc).replace("Habra CCC", "Habra CCC 5 kWp")
        found = checks.pdf_checks(doc, _render_html(page), qr_size=document_render.qr_png_size(VERIFY))
        assert [c for c in found if c["id"] == "capacity_mismatch"][0]["message"].startswith("The PDF prints 5 kWp")

    def test_leftover_text(self):
        require_weasyprint()
        doc = residential_3kw_pmsg()
        page = self._page(doc).replace("Habra CCC", "undefined")
        assert "leftover_text" in _ids(checks.pdf_checks(doc, _render_html(page)))

    def test_footer_missing(self):
        require_weasyprint()
        doc = residential_3kw_pmsg()
        page = re.sub(r'<div class="pagefoot">.*?</div>', "", self._page(doc), count=1, flags=re.S)
        assert "footer_missing" in _ids(checks.pdf_checks(doc, _render_html(page)))

    def test_letterhead_and_qr_missing_from_the_last_page(self):
        require_weasyprint()
        doc = residential_3kw_pmsg()
        page = re.sub(r"@page :nth\(3\) \{.*?\}\s*\}", "", self._page(doc), flags=re.S)
        found = _ids(checks.pdf_checks(doc, _render_html(page), qr_size=document_render.qr_png_size(VERIFY)))
        assert {"letterhead_missing", "qr_missing", "footer_on_last_page"} <= found

    def test_letterhead_on_an_earlier_page(self):
        require_weasyprint()
        doc = residential_3kw_pmsg()
        page = self._page(doc).replace("@page :nth(3)", "@page :nth(1)")
        assert "letterhead_misplaced" in _ids(checks.pdf_checks(doc, _render_html(page),
                                                                qr_size=document_render.qr_png_size(VERIFY)))

    def test_page_count(self):
        require_weasyprint()
        doc = residential_3kw_pmsg()
        page = self._page(doc).replace('<div class="lastspace">', '<div style="height:2400pt"></div>'
                                                                  '<div class="lastspace">')
        found = checks.pdf_checks(doc, _render_html(page))
        assert [c for c in found if c["id"] == "page_count"][0]["message"].endswith("a compact proposal has 3–4.")


# ── Proof-check: one test per warning ────────────────────────────────────────────

class TestWarnings:
    def test_capacity_above_sanctioned(self):
        doc = residential_3kw_pmsg()
        doc.connection.sanctioned_load_kw = Decimal(2)
        assert "capacity_above_sanctioned" in _ids(_doc_checks(doc))

    def test_single_phase_above_limit(self):
        doc = residential_5kw_comparison()
        doc.connection.phase, doc.plant.capacity_kwp = "single", Decimal(8)
        assert "single_phase_large" in _ids(_doc_checks(doc))
        doc.plant.capacity_kwp = Decimal(7)
        assert "single_phase_large" not in _ids(_doc_checks(doc))

    def test_below_price_master_with_the_percentage(self):
        from app.studio.service import _priced
        doc = _priced(residential_3kw_pmsg(), {"mode": "staff_basic", "basic": "175750", "reason": "Festive offer"})
        found = [c for c in _doc_checks(doc) if c["id"] == "below_price_master"]
        assert found and "5.00% below" in found[0]["message"]

    def test_price_per_kwp_band(self, monkeypatch):
        cfg = studio_config.load()
        assert "price_per_kwp_band" not in _ids(_doc_checks(commercial_52kw()))        # no band supplied
        monkeypatch.setitem(cfg["price_per_kwp_band"], "commercial_rooftop", {"low": "40000", "high": "50000"})
        assert "price_per_kwp_band" in _ids(_doc_checks(commercial_52kw()))            # 55,000 a kWp

    def test_roof_area_short(self):
        doc = commercial_52kw()
        doc.project.roof_area_sqft = Decimal(3000)                                     # needs 4,300
        assert "roof_area_short" in _ids(_doc_checks(doc))

    def test_state_outside_service_area(self):
        doc = commercial_52kw()
        doc.site.state = "Odisha"
        assert "state_outside_service" in _ids(_doc_checks(doc))

    def test_spelling_per_word_and_ignore(self):
        doc = commercial_52kw()
        doc.notes_free = ["Recieve the materail at site; galvanised colour-coded cables, approx. 40 m."]
        found = [c for c in _doc_checks(doc) if c["id"] == "spelling"]
        assert {c["word"] for c in found} == {"recieve", "materail"}
        assert all(c["level"] == "warning" for c in found)
        assert {c["word"] for c in _doc_checks(doc, spell_ignore=["materail"]) if c["id"] == "spelling"} == {
            "recieve"}

    def test_untouched_defaults_are_not_spell_checked(self):
        assert not [c for c in _doc_checks(commercial_52kw()) if c["id"] == "spelling"]

    def test_fingerprint_is_stable_and_specific(self):
        a = checks.check("spelling", "warning", "notes_free[0]", "'x' may be misspelt.")
        b = dict(a, message="'y' may be misspelt.")
        assert checks.fingerprint(a) == checks.fingerprint(dict(a)) != checks.fingerprint(b)
