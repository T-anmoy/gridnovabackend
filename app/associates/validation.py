"""Field validators for Associate / Preferred Associate registration.

Pure functions, no I/O. Each `normalize_*` returns the canonical form or raises
ValueError with a message safe to show the applicant — a message never echoes
the value back, because some of these values (the Aadhaar number above all) must
not travel anywhere they could be logged.
"""
from __future__ import annotations

import re
from datetime import date


class FieldError(ValueError):
    """A refused value. `code` is stable and language-free (BE-19): the website shows
    its own English, Hindi or Bengali text for it; the English message is a fallback."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code

# ── Aadhaar: Verhoeff checksum ────────────────────────────────────────────────
# The standard Verhoeff tables: _D is the multiplication table of the dihedral
# group D5, _P the position-dependent permutation, _INV the inverse table. UIDAI
# uses this checksum for the 12th digit of an Aadhaar number.

_D = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 2, 3, 4, 0, 6, 7, 8, 9, 5),
    (2, 3, 4, 0, 1, 7, 8, 9, 5, 6),
    (3, 4, 0, 1, 2, 8, 9, 5, 6, 7),
    (4, 0, 1, 2, 3, 9, 5, 6, 7, 8),
    (5, 9, 8, 7, 6, 0, 4, 3, 2, 1),
    (6, 5, 9, 8, 7, 1, 0, 4, 3, 2),
    (7, 6, 5, 9, 8, 2, 1, 0, 4, 3),
    (8, 7, 6, 5, 9, 3, 2, 1, 0, 4),
    (9, 8, 7, 6, 5, 4, 3, 2, 1, 0),
)
_P = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 5, 7, 6, 2, 8, 3, 0, 9, 4),
    (5, 8, 0, 3, 7, 9, 6, 1, 4, 2),
    (8, 9, 1, 6, 0, 4, 3, 5, 2, 7),
    (9, 4, 5, 3, 1, 2, 6, 8, 7, 0),
    (4, 2, 8, 6, 5, 7, 3, 9, 0, 1),
    (2, 7, 9, 3, 8, 0, 6, 4, 1, 5),
    (7, 0, 4, 6, 9, 1, 3, 2, 5, 8),
)
_INV = (0, 4, 3, 2, 1, 5, 6, 7, 8, 9)


def verhoeff_valid(digits: str) -> bool:
    if not digits or not digits.isdigit():
        return False
    c = 0
    for i, ch in enumerate(reversed(digits)):
        c = _D[c][_P[i % 8][int(ch)]]
    return c == 0


def verhoeff_check_digit(digits: str) -> str:
    """The digit that makes `digits + it` Verhoeff-valid."""
    c = 0
    for i, ch in enumerate(reversed(digits)):
        c = _D[c][_P[(i + 1) % 8][int(ch)]]
    return str(_INV[c])


def aadhaar_last4(value: str) -> str:
    """Validate an Aadhaar number and return ONLY its last four digits — the caller
    never gets the full number back, so it cannot be stored by accident. Spaces and
    hyphens (as printed on the card) are accepted."""
    digits = re.sub(r"[\s-]", "", value or "")
    if not re.fullmatch(r"[2-9]\d{11}", digits):
        raise FieldError("aadhaar_format", "Aadhaar number must be 12 digits and cannot start with 0 or 1.")
    if not verhoeff_valid(digits):
        raise FieldError("aadhaar_checksum", "Aadhaar number is not valid (checksum failed). Please check it and try again.")
    return digits[-4:]


# ── PAN ───────────────────────────────────────────────────────────────────────
# AAAAA9999A. The 4th character is the holder's entity type (Income Tax Department):
# P individual, C company, H HUF, F firm/LLP, A AOP, T trust, B BOI, L local
# authority, J artificial juridical person, G government. Every KYC field this PAN
# sits alongside (name as on Aadhaar, date of birth, photo) describes one person, so
# only an individual's PAN ('P') is accepted.
PAN_ENTITY_TYPES = frozenset("PCHFATBLJG")
PAN_INDIVIDUAL = "P"
_PAN_RE = re.compile(r"[A-Z]{5}[0-9]{4}[A-Z]")


def normalize_pan(value: str) -> str:
    pan = (value or "").strip().upper()
    if not _PAN_RE.fullmatch(pan):
        raise FieldError("pan_format", "PAN must be 10 characters: 5 letters, 4 digits, 1 letter (e.g. ABCPE1234F).")
    if pan[3] not in PAN_ENTITY_TYPES:
        raise FieldError("pan_holder_type", "PAN is not valid: its 4th character is not a recognised holder type.")
    if pan[3] != PAN_INDIVIDUAL:
        raise FieldError("pan_not_individual", "The PAN must be an individual's PAN (4th character 'P').")
    return pan


# ── Bank ──────────────────────────────────────────────────────────────────────
# IFSC: 4-letter bank code, a literal 0, then a 6-character branch code (RBI format).
_IFSC_RE = re.compile(r"[A-Z]{4}0[A-Z0-9]{6}")
# Indian bank account numbers vary by bank; 9 to 18 digits covers the range in use.
_ACCOUNT_RE = re.compile(r"\d{9,18}")


def normalize_ifsc(value: str) -> str:
    ifsc = (value or "").strip().upper()
    if not _IFSC_RE.fullmatch(ifsc):
        raise FieldError("ifsc_format", "IFSC must be 11 characters: 4 letters, then 0, then 6 letters or digits.")
    return ifsc


def normalize_bank_account(value: str) -> str:
    account = re.sub(r"\s", "", value or "")
    if not _ACCOUNT_RE.fullmatch(account):
        raise FieldError("bank_account_format", "Bank account number must be 9 to 18 digits.")
    return account


# ── GSTIN (format only) ───────────────────────────────────────────────────────
# 2-digit state code + the holder's 10-character PAN + entity number + 'Z' + check
# character. Only the shape is checked; the check character is not recomputed.
_GSTIN_RE = re.compile(r"[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]")


def normalize_gstin(value: str) -> str:
    gstin = (value or "").strip().upper()
    if not _GSTIN_RE.fullmatch(gstin):
        raise FieldError("gstin_format", "GSTIN must be 15 characters in the standard format (e.g. 19ABCPE1234F1Z5).")
    return gstin


# ── Contact ───────────────────────────────────────────────────────────────────
_MOBILE_RE = re.compile(r"(?:\+?91|0)?([6-9]\d{9})")
_PIN_RE = re.compile(r"[1-9]\d{5}")
# Same shape check app/api/routes/public_intake.py uses (email-validator is not installed).
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def normalize_mobile(value: str) -> str:
    """A 10-digit Indian mobile number, optionally written with +91 / 91 / 0."""
    compact = re.sub(r"[\s()-]", "", value or "")
    m = _MOBILE_RE.fullmatch(compact)
    if not m:
        raise FieldError("mobile_format", "Mobile number must be a 10-digit Indian mobile number starting with 6-9.")
    return m.group(1)


def normalize_pin(value: str) -> str:
    pin = (value or "").strip()
    if not _PIN_RE.fullmatch(pin):
        raise FieldError("pin_format", "PIN code must be 6 digits and cannot start with 0.")
    return pin


def normalize_email(value: str) -> str:
    email = (value or "").strip()
    if len(email) > 254 or not _EMAIL_RE.fullmatch(email):
        raise FieldError("email_format", "Must be a valid email address.")
    return email.lower()


# ── Identity ──────────────────────────────────────────────────────────────────
MINIMUM_AGE_YEARS = 18


def age_on(born: date, today: date) -> int:
    return today.year - born.year - ((today.month, today.day) < (born.month, born.day))


def check_adult(born: date, today: date) -> date:
    if born > today:
        raise FieldError("dob_future", "Date of birth cannot be in the future.")
    if age_on(born, today) < MINIMUM_AGE_YEARS:
        raise FieldError("underage", f"Applicants must be at least {MINIMUM_AGE_YEARS} years old.")
    if age_on(born, today) > 120:
        raise FieldError("dob_implausible", "Date of birth is not plausible.")
    return born


def normalize_name(value: str) -> str:
    """Collapse internal whitespace; the stored name is what the applicant typed."""
    name = " ".join((value or "").split())
    if not name:
        raise FieldError("name_required", "Full legal name is required.")
    if len(name) > 200:
        raise FieldError("name_too_long", "Full legal name must be at most 200 characters.")
    return name


def name_key(name: str) -> str:
    """Case- and spacing-insensitive form of a name, for duplicate detection."""
    return " ".join(name.upper().split())
