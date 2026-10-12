"""Render a PDF with a Latin, a Hindi and a Bengali name and check the fonts it embeds.

    python scripts/smoke_fonts.py

Shipped in the image and run against it in CI (the image has no pytest): exit 0 when the
PDF embeds the Latin, Devanagari and Bengali families of app/pdf/fonts.py, 1 otherwise,
naming what is missing. A missing font is exactly the failure this catches — the PDF still
renders, with empty boxes where the names should be.

tests/test_pdf_fonts.py runs the same check wherever the fonts are installed.
"""
from __future__ import annotations

import re
import sys
import zlib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.pdf.fonts import BENGALI_FAMILY, DEVANAGARI_FAMILY, FONT_STACK, LATIN_FAMILY  # noqa: E402

# Sample names, one per script (not real people).
SAMPLES = {
    LATIN_FAMILY: "Anita Sharma",
    DEVANAGARI_FAMILY: "अनीता शर्मा",
    BENGALI_FAMILY: "অনিতা শর্মা",
}

_BASE_FONT = re.compile(rb"/BaseFont\s*/(?:[A-Z]{6}\+)?([A-Za-z0-9_-]+)")
_STREAM = re.compile(rb"stream\r?\n(.*?)\r?\nendstream", re.S)


def sample_html() -> str:
    rows = "".join(f"<p>{name}</p>" for name in SAMPLES.values())
    return (f'<!DOCTYPE html><html><head><meta charset="utf-8"><style>body {{ font-family: {FONT_STACK}; }}'
            f"</style></head><body>{rows}</body></html>")


def _readable_parts(pdf: bytes):
    """The PDF itself and every Flate-compressed stream in it, inflated: WeasyPrint packs
    the font dictionaries into compressed object streams."""
    yield pdf
    for match in _STREAM.finditer(pdf):
        try:
            yield zlib.decompress(match.group(1))
        except zlib.error:
            continue


def embedded_fonts(pdf: bytes) -> set[str]:
    """PostScript names of the fonts a PDF embeds, without the subset prefix
    (e.g. "NotoSansDevanagari-Regular")."""
    return {m.group(1).decode("ascii") for part in _readable_parts(pdf) for m in _BASE_FONT.finditer(part)}


def missing_families(pdf: bytes) -> list[str]:
    """The required families the PDF does not embed. A family matches an embedded name
    that starts with the family name once spaces and hyphens are ignored ("Noto Sans
    Bengali" → "NotoSansBengali-Regular", or "Noto-Sans-Bengali" as WeasyPrint 70 writes it)."""
    names = [n.replace("-", "") for n in embedded_fonts(pdf)]
    return [family for family in SAMPLES
            if not any(n.startswith(family.replace(" ", "")) for n in names)]


def render_sample() -> bytes:
    from app.pdf.fetcher import render_pdf

    return render_pdf(sample_html())


def main() -> int:
    pdf = render_sample()
    missing = missing_families(pdf)
    print(f"smoke_fonts: embedded {', '.join(sorted(embedded_fonts(pdf))) or 'nothing'}")
    if missing:
        print(f"smoke_fonts: FAILED — not embedded: {', '.join(missing)}. Hindi/Bengali names would print as "
              "boxes; check the Dockerfile's font packages.", file=sys.stderr)
        return 1
    print("smoke_fonts: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
