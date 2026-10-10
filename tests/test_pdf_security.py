"""BE-1 S1 — PDF rendering never turns typed text into markup, and never fetches
anything but data: URIs.

The proposal template used to be filled with str.format() and no escaping, and
WeasyPrint rendered it with its default fetcher: a website visitor's "name" could make
the server embed a local file or call an internal URL into a PDF that staff then sent
back to them. Every renderer now escapes its values, and every render goes through
app/pdf/fetcher.py.
"""
import base64

import pytest

from app.pdf import fetcher, generator

HOSTILE_NAME = ('Asha <link rel="attachment" href="file:///etc/hosts">'
                '<img src="http://127.0.0.1:9/probe.png"> & Co')


def _weasyprint():
    try:
        import weasyprint  # noqa: F401
    except Exception:  # native libraries missing on this machine
        pytest.skip("WeasyPrint's native libraries are not available")
    return weasyprint


def _proposal(**overrides):
    data = {"proposal_id": 7, "lead_id": 3, "version": 1, "customer_name": HOSTILE_NAME,
            "project_overview": "Line one\n<script>x</script>", "total_value": None,
            "sections": [{"id": "pricing", "body": '<img src="https://169.254.169.254/latest">'}]}
    data.update(overrides)
    return data


class TestEscaping:
    def test_customer_text_is_escaped_in_the_proposal_html(self):
        html = generator._build_html(_proposal(), {"company": {"name": "Grid <Nova>"}})
        assert "<link" not in html and "<img" not in html and "<script>" not in html
        assert "&lt;link rel=&quot;attachment&quot;" in html
        assert "Asha &lt;link" in html and "&amp; Co" in html
        assert "Grid &lt;Nova&gt;" in html
        # Line breaks survive through the pre-line sections.
        assert "Line one\n&lt;script&gt;" in html

    def test_legacy_scope_and_terms_are_escaped(self):
        html = generator._build_html(
            _proposal(sections=None, scope_of_work="<b>scope</b>", terms_and_conditions="<i>t</i>"), {})
        assert "&lt;b&gt;scope&lt;/b&gt;" in html and "&lt;i&gt;t&lt;/i&gt;" in html

    def test_a_non_hex_letterhead_colour_is_not_used(self):
        html = generator._build_html(_proposal(), {"letterhead": {"accent_color": "red;} body{background:url(x)",
                                                                  "primary_color": "#123abc"}})
        assert "background:url(x)" not in html
        assert "#f7a800" in html and "#123abc" in html


class TestFetcherPolicy:
    @pytest.mark.parametrize("url", ["data:text/plain;base64,aGk=", "DATA:image/png;base64,AA=="])
    def test_data_uris_are_allowed(self, url):
        assert fetcher.is_allowed(url)

    @pytest.mark.parametrize("url", ["file:///etc/hosts", "http://127.0.0.1:9/x", "https://169.254.169.254/",
                                     "ftp://x/y", "/etc/hosts", "relative.png", "", " javascript:alert(1)"])
    def test_everything_else_is_refused(self, url):
        assert not fetcher.is_allowed(url)

    def test_both_renderers_use_the_safe_fetcher(self, monkeypatch):
        seen = []
        monkeypatch.setattr(fetcher, "render_pdf", lambda html: seen.append(html) or b"%PDF-fake")
        from app.associates import documents
        assert generator.render_pdf(_proposal(), {}) == b"%PDF-fake"
        assert documents.render_pdf("<p>x</p>") == b"%PDF-fake"
        assert len(seen) == 2


class TestRenderedPdf:
    """Real WeasyPrint renders (skipped where its native libraries are missing)."""

    def test_hostile_name_embeds_nothing_and_calls_nothing(self, monkeypatch):
        weasyprint = _weasyprint()
        delegated = []
        real_fetch = weasyprint.URLFetcher.fetch

        def recording_fetch(self, url, headers=None):
            delegated.append(url)
            return real_fetch(self, url, headers)

        # The data-only fetcher delegates to URLFetcher.fetch only for allowed URLs;
        # any other URL reaching it would be a network or file access.
        monkeypatch.setattr(weasyprint.URLFetcher, "fetch", recording_fetch)
        pdf = generator.render_pdf(_proposal(), {})
        assert pdf.startswith(b"%PDF")
        assert b"EmbeddedFile" not in pdf
        assert all(url.lower().startswith("data:") for url in delegated), delegated

    def test_raw_hostile_markup_is_still_blocked_by_the_fetcher(self, monkeypatch):
        """Defence in depth: even unescaped markup cannot reach a file or the network."""
        weasyprint = _weasyprint()
        delegated = []
        real_fetch = weasyprint.URLFetcher.fetch
        monkeypatch.setattr(weasyprint.URLFetcher, "fetch",
                            lambda self, url, headers=None: delegated.append(url) or real_fetch(self, url, headers))
        pdf = fetcher.render_pdf('<html><head><link rel="attachment" href="file:///etc/hosts"></head>'
                                 '<body><img src="http://127.0.0.1:9/probe.png">x</body></html>')
        assert b"EmbeddedFile" not in pdf
        assert delegated == []

    def test_positive_control_a_data_attachment_is_detectable(self):
        """Proves the EmbeddedFile check above can see an attachment when one exists."""
        _weasyprint()
        payload = base64.b64encode(b"hello").decode()
        pdf = fetcher.render_pdf(f'<html><head><link rel="attachment" href="data:text/plain;base64,{payload}">'
                                 '</head><body>x</body></html>')
        assert b"EmbeddedFile" in pdf


def test_the_validity_sentence_comes_from_config(monkeypatch):
    """BE-1 C2: the PDF prints config/sales_policy.yaml proposal_validity_days, the
    same period the accept link enforces — not a hard-coded 30."""
    from app.rules import sales_policy
    monkeypatch.setattr(sales_policy, "proposal_validity_days", lambda: 45)
    html = generator._build_html(_proposal(), {})
    assert "valid for 45 days from the date of issue" in html


def test_no_domain_module_imports_a_route_for_the_validity_period():
    import pathlib
    for path in pathlib.Path("app").rglob("*.py"):
        if "/api/routes/" in str(path):
            continue
        assert "PROPOSAL_VALIDITY_DAYS" not in path.read_text(encoding="utf-8"), path
