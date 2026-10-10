"""The one URL fetcher every WeasyPrint render uses.

Proposal PDFs, receipts, agreements, ID cards and welcome letters are rendered from
HTML that carries customer- and applicant-typed text. Every value is escaped before it
reaches the template, and this fetcher is the second wall: WeasyPrint's default fetcher
would follow any URL in the document (http, https, file, ftp), so a stray tag could
make the server read a local file or call an internal address and put the result into
a PDF that is then sent to the customer.

Only `data:` URIs are fetched. That is the one kind of resource the templates use
legitimately: the ID card embeds the associate's photo as a data URI (the QR code is
inline SVG, and no template links a stylesheet, font or image file). Everything else is
refused before any connection or file read, and logged.
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

ALLOWED_SCHEMES = ("data",)
FETCH_TIMEOUT_SECONDS = 5
_SCHEME_RE = re.compile(r"^\s*([a-zA-Z][a-zA-Z0-9+.\-]*):")


def is_allowed(url: str) -> bool:
    match = _SCHEME_RE.match(url or "")
    return bool(match) and match.group(1).lower() in ALLOWED_SCHEMES


def safe_url_fetcher():
    """A WeasyPrint URLFetcher that fetches data: URIs and refuses everything else."""
    from weasyprint import URLFetcher

    class _DataOnlyFetcher(URLFetcher):
        def fetch(self, url, headers=None):
            if not is_allowed(url):
                logger.warning("PDF render: refused to fetch a non-data resource (%s)",
                               (url or "")[:12] + "…")
                raise ValueError("Only data: URIs may be fetched while rendering a PDF.")
            return super().fetch(url, headers)

    return _DataOnlyFetcher(timeout=FETCH_TIMEOUT_SECONDS, allowed_protocols=ALLOWED_SCHEMES,
                            allow_redirects=False)


def render_pdf(html_doc: str) -> bytes:
    """HTML → PDF bytes with the data-only fetcher and no base URL (so a relative
    path resolves to nothing rather than to the server's working directory)."""
    from weasyprint import HTML

    return HTML(string=html_doc, base_url=None, url_fetcher=safe_url_fetcher()).write_pdf()
