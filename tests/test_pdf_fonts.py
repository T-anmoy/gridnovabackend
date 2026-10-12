"""D1 (BE-3): generated PDFs carry fonts with Devanagari and Bengali glyphs.

The rendering test needs WeasyPrint's native libraries AND the fonts the Dockerfile
installs (fonts-liberation, fonts-noto-core); CI's test jobs install both. Elsewhere
(a developer laptop without those fonts) it is skipped with the reason. The source check
runs everywhere."""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from app.pdf import fonts
from scripts import smoke_fonts

APP = Path(__file__).resolve().parents[1] / "app"


def _installed_families() -> set[str]:
    if shutil.which("fc-list") is None:
        return set()
    out = subprocess.run(["fc-list", ":", "family"], capture_output=True, text=True, timeout=30).stdout
    return {name.strip() for line in out.splitlines() for name in line.split(",")}


def test_every_pdf_template_uses_the_shared_font_stack():
    """No template may fall back to a bare `Arial, sans-serif` again: the only
    font-family values in app/ are the shared stack (and monospace for the ID number)."""
    offenders = []
    for path in APP.rglob("*.py"):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for value in re.findall(r"font-family:\s*([^;]+);", line):
                if value.strip() not in ("{FONT_STACK}", "{font_stack}", "monospace"):
                    offenders.append(f"{path.relative_to(APP.parent)}:{n}: {value.strip()}")
    assert not offenders, offenders


def test_the_stack_names_the_required_families():
    for family in (fonts.LATIN_FAMILY, fonts.DEVANAGARI_FAMILY, fonts.BENGALI_FAMILY):
        assert f'"{family}"' in fonts.FONT_STACK


def test_embedded_font_names_are_read_without_the_subset_prefix():
    pdf = b"<< /Type /Font /BaseFont /ABCDEF+NotoSansBengali-Regular >> /BaseFont /LiberationSans"
    assert smoke_fonts.embedded_fonts(pdf) == {"NotoSansBengali-Regular", "LiberationSans"}
    assert smoke_fonts.missing_families(pdf) == [fonts.DEVANAGARI_FAMILY]
    # WeasyPrint 70 writes the family with hyphens, and inside compressed object streams.
    import zlib
    packed = b"1 0 obj\n<</Filter /FlateDecode>>\nstream\n" + zlib.compress(
        b"<< /BaseFont /QWERTY+Noto-Sans-Devanagari >> << /BaseFont /Liberation-Sans >>") + b"\nendstream"
    assert smoke_fonts.missing_families(packed) == [fonts.BENGALI_FAMILY]


def test_hindi_and_bengali_names_render_with_real_glyph_fonts():
    try:
        import weasyprint  # noqa: F401
    except OSError:
        pytest.skip("WeasyPrint's native libraries are not available")
    missing = [f for f in smoke_fonts.SAMPLES if f not in _installed_families()]
    if missing:
        pytest.skip(f"fonts not installed here: {', '.join(missing)} "
                    "(the Docker image and CI install fonts-liberation and fonts-noto-core)")
    pdf = smoke_fonts.render_sample()
    assert smoke_fonts.missing_families(pdf) == [], smoke_fonts.embedded_fonts(pdf)
