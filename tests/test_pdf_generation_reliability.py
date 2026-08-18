"""
PDF background-thread reliability fix:
- a configuration failure (R2 not set up) fails fast — no pointless retry sleep
- a genuinely transient failure still retries, but bounded by timeout_seconds
- the background thread's own session close/rollback never raises unhandled
  (covered by the live 20-run full-suite stress test — this file covers the
  deterministic, fast-running unit-level behavior of generate_and_upload_pdf)
"""
import time
from unittest.mock import patch

from app.pdf.generator import StorageNotConfiguredError, generate_and_upload_pdf

_CONTENT_CONFIG = {
    "company": {"name": "Grid Nova Energy", "tagline": "", "address": "", "phone": "", "email": "", "website": "", "gstin": ""},
    "letterhead": {"logo_text": "Grid Nova", "accent_color": "#000", "primary_color": "#000"},
}

_PROPOSAL_DATA = {
    "proposal_id": 1, "version": 1, "requires_manager_signoff": False, "total_value": 100_000,
    "customer_name": "Test", "customer_email": "test@example.com", "customer_company": "",
    "project_type": "rooftop", "capacity_kw": 5, "location": "Pune", "sections": [],
}


class TestFastFailOnMissingConfig:
    def test_not_configured_fails_without_retry_sleep(self):
        # upload_to_r2 itself raises StorageNotConfiguredError synchronously when
        # STORAGE_* is unset (the actual, current dev/test state) — confirm the
        # retry loop recognizes it and does not sleep through back-off for it.
        start = time.monotonic()
        with patch("app.pdf.generator.render_pdf", return_value=b"%PDF-fake"):
            result = generate_and_upload_pdf(_PROPOSAL_DATA, _CONTENT_CONFIG, proposal_id=1)
        elapsed = time.monotonic() - start
        assert result is None
        assert elapsed < 1.0, f"expected a fast fail, took {elapsed:.2f}s"


class TestTimeoutBoundsTransientRetries:
    def test_transient_failure_stops_retrying_once_timeout_exceeded(self):
        call_count = {"n": 0}

        def _always_fails(pdf_bytes, proposal_id):
            call_count["n"] += 1
            raise RuntimeError("R2 temporarily unreachable")

        start = time.monotonic()
        with patch("app.pdf.generator.render_pdf", return_value=b"%PDF-fake"), \
             patch("app.pdf.generator.upload_to_r2", side_effect=_always_fails):
            result = generate_and_upload_pdf(
                _PROPOSAL_DATA, _CONTENT_CONFIG, proposal_id=1, timeout_seconds=0.5,
            )
        elapsed = time.monotonic() - start
        assert result is None
        # Without a timeout, attempts 1+2 alone sleep 2s + 4s = 6s of back-off.
        # Bounded to ~0.5s means it can only ever get through a first attempt or two.
        assert elapsed < 2.0, f"timeout did not bound retry time, took {elapsed:.2f}s"
        assert call_count["n"] >= 1

    def test_render_failure_returns_none_immediately(self):
        with patch("app.pdf.generator.render_pdf", side_effect=RuntimeError("template error")):
            result = generate_and_upload_pdf(_PROPOSAL_DATA, _CONTENT_CONFIG, proposal_id=1)
        assert result is None


class TestSuccessPath:
    def test_successful_upload_returns_url_on_first_attempt(self):
        with patch("app.pdf.generator.render_pdf", return_value=b"%PDF-fake"), \
             patch("app.pdf.generator.upload_to_r2", return_value="https://r2.example/fake.pdf"):
            result = generate_and_upload_pdf(_PROPOSAL_DATA, _CONTENT_CONFIG, proposal_id=1)
        assert result == "https://r2.example/fake.pdf"
