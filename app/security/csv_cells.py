"""CSV-injection-safe cells, for every CSV this API writes.

A spreadsheet evaluates a cell that starts with = + - @ (or a tab / carriage return) as
a formula. Names, references and notes in these exports come from public forms and the
website chat, so such a text cell is prefixed with an apostrophe and shown as text.
A plain number that merely starts with "-" (a ledger reversal, say) is left as a number.
"""
from __future__ import annotations

import re

_NUMBER = re.compile(r"^-?\d+(\.\d+)?$")
_DANGEROUS = ("=", "+", "-", "@", "\t", "\r")


def safe_cell(value) -> str:
    if value is None:
        return ""
    text = str(value)
    if text.startswith(_DANGEROUS) and not _NUMBER.match(text):
        return "'" + text
    return text
