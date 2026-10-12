"""The font stack every generated PDF uses (proposals, survey reports, ID cards, welcome
letters, agreements, receipts).

Customer and associate names arrive in Devanagari and Bengali as well as Latin script.
A bare `Arial, sans-serif` resolves, in the slim Debian image, to a font with neither
script, and those names render as empty boxes. The Dockerfile installs fonts-liberation
(Liberation Sans, metric-compatible with Arial) and fonts-noto-core (Noto Sans and its
Devanagari / Bengali families); Pango falls back glyph by glyph along this list, so a
mixed-script line takes each character from the first family that has it.

scripts/smoke_fonts.py (run against the built image in CI) renders a Hindi and a Bengali
name and checks these families are what the PDF actually embeds.
"""
from __future__ import annotations

FONT_STACK = ('"Liberation Sans", Arial, "Noto Sans", "Noto Sans Devanagari", "Noto Sans Bengali", '
              'sans-serif')

# Families a PDF with Latin, Hindi and Bengali text must embed — what the smoke check
# and tests/test_pdf_fonts.py look for.
LATIN_FAMILY = "Liberation Sans"
DEVANAGARI_FAMILY = "Noto Sans Devanagari"
BENGALI_FAMILY = "Noto Sans Bengali"
