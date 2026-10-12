"""S1 Step 7 — the survey report PDF: generated after the review, stored privately (a new
file each time), served by audited signed links under the document rule, never
carrying a price, and never turning typed text into markup."""
import pytest
from sqlalchemy import select

from app.memory.models import AuditLog, CustomerOnboarding, ProposalInputs
from app.pdf import fetcher
from app.survey import report
from tests.conftest import _FAKE_BUCKET
from tests.onboarding_support import (  # noqa: F401 — fixtures
    ADMIN, ALIPURDUAR, BASE, DASH, ENG, MGMT, SALES, headers, kyc_ready, make_associate, onboarding_ready,
    review_survey, territories,
)
from tests.test_survey_review import _verified

HOSTILE = '<img src="http://127.0.0.1:9/probe.png"><link rel="attachment" href="file:///etc/hosts"> & co'


def _weasyprint():
    try:
        import weasyprint  # noqa: F401
    except Exception:  # native libraries missing on this machine
        pytest.skip("WeasyPrint's native libraries are not available")


@pytest.fixture
def stub_render(monkeypatch):
    rendered = []
    monkeypatch.setattr(fetcher, "render_pdf", lambda html: rendered.append(html) or b"%PDF-1.7 stub")
    return rendered


def _reviewed(client, db_session, **review):
    gna, oid = _verified(client, db_session)
    r = review_survey(client, oid, **(review or {"outcome": "feasible"}))
    assert r.status_code == 200, r.text
    return gna, oid


def _generate(client, oid, who=ENG):
    return client.post(f"{DASH}/onboardings/{oid}/survey/report", headers=who)


def _inputs(db_session, oid) -> ProposalInputs:
    db_session.expire_all()
    return db_session.get(ProposalInputs, db_session.get(CustomerOnboarding, oid).proposal_inputs_id)


class TestGenerating:
    def test_real_pdf_stored_privately(self, client, db_session, onboarding_ready):
        _weasyprint()
        _, oid = _reviewed(client, db_session)
        r = _generate(client, oid)
        assert r.status_code == 200, r.text
        inputs = _inputs(db_session, oid)
        assert inputs.survey_report_key.startswith(f"proposal-inputs/{inputs.id}/survey_report/")
        assert inputs.survey_report_key.endswith(".pdf")
        assert _FAKE_BUCKET[inputs.survey_report_key].startswith(b"%PDF")
        assert r.json()["filename"].startswith("Survey-Report-GNE-")
        assert r.json()["generated_at"] == inputs.survey_report_generated_at.isoformat()
        audit = db_session.execute(select(AuditLog).where(AuditLog.action == "survey_report_generated")).scalar_one()
        assert audit.after_value["survey_report_key"] == inputs.survey_report_key

    def test_regenerating_keeps_the_old_file(self, client, db_session, onboarding_ready, stub_render):
        _, oid = _reviewed(client, db_session)
        _generate(client, oid)
        first = _inputs(db_session, oid).survey_report_key
        assert _generate(client, oid, who=ADMIN).status_code == 200
        second = _inputs(db_session, oid).survey_report_key
        assert second != first and first in _FAKE_BUCKET and second in _FAKE_BUCKET

    def test_needs_a_review(self, client, db_session, onboarding_ready, stub_render):
        _, oid = _verified(client, db_session)
        r = _generate(client, oid)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "review_required"
        assert stub_render == []

    def test_storage_off_is_503(self, client, db_session, onboarding_ready, stub_render, monkeypatch):
        from app.config import settings
        _, oid = _reviewed(client, db_session)
        monkeypatch.setattr(settings, "storage_bucket", "")
        r = _generate(client, oid)
        assert r.status_code == 503 and r.json()["detail"]["code"] == "storage_not_configured"
        assert _inputs(db_session, oid).survey_report_key is None
        r = client.get(f"{DASH}/onboardings/{oid}/survey/report", headers=ENG)
        assert r.status_code == 404   # nothing generated, so nothing to sign

    @pytest.mark.parametrize("who", [SALES, MGMT])
    def test_only_engineer_and_admin_generate(self, client, db_session, onboarding_ready, stub_render, who):
        _, oid = _reviewed(client, db_session)
        assert _generate(client, oid, who=who).status_code == 403


class TestContents:
    def test_what_it_says(self, client, db_session, onboarding_ready, stub_render):
        _, oid = _reviewed(client, db_session, outcome="feasible_with_conditions",
                           conditions=["Reinforce the parapet"], notes="Checked from the photos")
        assert _generate(client, oid).status_code == 200
        html = stub_render[-1]
        onboarding = db_session.get(CustomerOnboarding, oid)
        from app.onboarding import service
        assert service.project_reference(db_session, onboarding) in html
        for text in ("Site Survey Report", "WBSEDCL", "2026-03", "Main", "feasible with conditions",
                     "Reinforce the parapet", "Checked from the photos", "Suggested size (kW)",
                     "Declared by the customer as: Customer", "Grid Nova Energy", "emp-eng-1"):
            assert text in html, text
        assert html.count('src="data:image/png;base64,') == 2
        # No unsupplied company fact is printed.
        assert "[PLACEHOLDER" not in html and "GSTIN" not in html

    def test_no_prices(self, client, db_session, onboarding_ready, stub_render):
        from app.survey import results
        _, oid = _reviewed(client, db_session)
        _generate(client, oid)
        html = stub_render[-1]
        estimate = results.compute(db_session, db_session.get(CustomerOnboarding, oid))["estimate"]
        amounts = [str(v) for pkg in estimate["packages"].values() for v in pkg.values()
                   if isinstance(v, str) and v.replace(".", "").isdigit() and float(v) > 10000]
        assert amounts, estimate   # the customer IS priced — the report just leaves it out
        for amount in amounts:
            assert amount not in html
        assert "price" not in html.lower() and "subsidy" not in html.lower() and "Rs " not in html

    def test_warnings_in_plain_english(self):
        assert report.warning_text({"code": "requested_above_suggested", "params": {
            "requested_kw": 6, "suggested_kw": 4, "limiting_factor": "area"}}) == (
            "The requested size (6 kW) is above the suggested size (4 kW), which is limited by the shadow-free "
            "roof area.")
        assert report.warning_text({"code": "overhead_line", "params": {}}).startswith("An overhead line")
        # Every code app/survey/results.py documents has its own sentence (not the fallback).
        for code in ("requested_above_suggested", "load_enhancement_needed", "implied_tariff_outside_range",
                     "units_bill_mismatch", "three_phase_below_min", "fragile_roof", "overhead_line",
                     "roof_not_owned_no_noc", "society_noc_missing", "area_implausible", "duplicate_consumer_number"):
            assert code in report.warning_text.__code__.co_consts, code
        assert report.warning_text({"code": "new_code", "params": {}}) == "New code."

    def test_a_placeholder_company_fact_is_left_out(self, monkeypatch):
        from app.proposals import content_engine
        config = dict(content_engine.load_content_config())
        config["company"] = {**config["company"], "address": "[PLACEHOLDER: address]"}
        monkeypatch.setattr(content_engine, "_CONTENT_CONFIG", config)
        assert all("PLACEHOLDER" not in line for line in report._company())
        assert config["company"]["name"] in report._company()


class TestEscaping:
    def _hostile(self, client, db_session):
        gna, oid = _verified(client, db_session)
        inputs = _inputs(db_session, oid)
        inputs.roof_access_note = HOSTILE
        inputs.customer_declaration_name = HOSTILE
        inputs.roof_sections = [{"label": HOSTILE, "length_m": "10.00", "width_m": "6.00", "obstacle_area_sqm": None}]
        db_session.commit()
        r = review_survey(client, oid, "revisit_needed", notes=HOSTILE,
                          revisit_items=[{"section": "roof", "note": HOSTILE}])
        assert r.status_code == 200, r.text
        return oid

    def test_typed_text_is_escaped(self, client, db_session, onboarding_ready):
        oid = self._hostile(client, db_session)
        html = report.build_html(db_session, db_session.get(CustomerOnboarding, oid))
        assert "<link" not in html and 'src="http' not in html
        assert html.count("&lt;img src=&quot;http://127.0.0.1:9/probe.png&quot;&gt;") >= 4
        assert "&amp; co" in html

    def test_real_render_embeds_and_fetches_nothing(self, client, db_session, onboarding_ready, monkeypatch):
        _weasyprint()
        import weasyprint
        oid = self._hostile(client, db_session)
        delegated = []
        real_fetch = weasyprint.URLFetcher.fetch
        monkeypatch.setattr(weasyprint.URLFetcher, "fetch",
                            lambda self, url, headers=None: delegated.append(url) or real_fetch(self, url, headers))
        assert _generate(client, oid).status_code == 200
        pdf = _FAKE_BUCKET[_inputs(db_session, oid).survey_report_key]
        assert pdf.startswith(b"%PDF") and b"EmbeddedFile" not in pdf
        # Only the two signature images, as data: URIs.
        assert delegated and all(url.startswith("data:image/png") for url in delegated)

    def test_a_signature_that_is_not_a_png_is_not_embedded(self, client, db_session, onboarding_ready):
        _, oid = _reviewed(client, db_session)
        inputs = _inputs(db_session, oid)
        _FAKE_BUCKET[inputs.customer_signature_ref["storage_key"]] = b"<svg onload=x>"
        html = report.build_html(db_session, db_session.get(CustomerOnboarding, oid))
        assert html.count('src="data:image/png;base64,') == 1
        assert "(signature image not available)" in html and "<svg" not in html


class TestLinks:
    def test_staff_link_and_audit(self, client, db_session, onboarding_ready, stub_render):
        _, oid = _reviewed(client, db_session)
        r = client.get(f"{DASH}/onboardings/{oid}/survey/report", headers=SALES)
        assert r.status_code == 404 and r.json()["detail"]["code"] == "report_not_generated"
        _generate(client, oid)
        for who in (SALES, ENG, MGMT):
            r = client.get(f"{DASH}/onboardings/{oid}/survey/report", headers=who)
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["url"].startswith("https://signed.example/proposal-inputs/") and body["expires_at"]
            assert body["filename"].startswith("Survey-Report-GNE-")
        links = list(db_session.execute(select(AuditLog).where(AuditLog.action == "link_issued")).scalars())
        assert [a.performed_by for a in links] == ["emp-sales-1", "emp-eng-1", "emp-mgmt-1"]
        assert all(a.after_value["kind"] == "survey_report" for a in links)
        summary = client.get(f"{DASH}/onboardings/{oid}/survey", headers=ENG).json()
        assert summary["report"]["generated_at"] is not None

    def test_associate_owner_and_the_territory_document_rule(self, client, db_session, onboarding_ready,
                                                             stub_render, monkeypatch):
        gna, oid = _reviewed(client, db_session)
        url = f"{BASE}/{oid}/survey/report"
        assert client.get(url, headers=headers(gna)).json()["detail"]["code"] == "report_not_generated"
        _generate(client, oid)
        r = client.get(url, headers=headers(gna))
        assert r.status_code == 200 and r.json()["url"].startswith("https://signed.example/")
        audit = db_session.execute(select(AuditLog).where(AuditLog.action == "link_issued")).scalar_one()
        assert audit.performed_by == f"associate:{gna}" and audit.after_value["kind"] == "survey_report"
        head = make_associate(db_session, level="district_head", territory=ALIPURDUAR)
        r = client.get(url, headers=headers(head))
        assert r.status_code == 403
        from app.auth.scoping import DOCUMENTS_OWNER_ONLY
        assert r.json()["detail"] == DOCUMENTS_OWNER_ONLY
        from app.onboarding import config as onboarding_config
        monkeypatch.setitem(onboarding_config.load(), "territory_viewers_see_documents", True)
        assert client.get(url, headers=headers(head)).status_code == 200
        stranger = make_associate(db_session)
        assert client.get(url, headers=headers(stranger)).status_code == 403
