"""
Hindi and Bengali normalisation for the deterministic reply checks.

WHY THIS EXISTS
app/agents/guardrails.py reads amounts, scale words and day names as English. A
Hindi or Bengali sentence ("आठ लाख", "৮ লাখ টাকা", "सोमवार") sails past those
patterns, so the checks either miss an invented figure or fail to see that a
customer's own "३ लाख" justifies Nova saying "3 lakh". Everything here rewrites the
Indic forms into the English forms the checks already understand.

Only Devanagari and Bengali text is touched. A string with no character from either
block is returned unchanged, so every English check behaves exactly as before.

Python's `\\d` already matches Devanagari and Bengali digits and float() parses them,
but `\\b` and `\\w` do not treat Indic vowel signs as word characters, so nothing in
this module relies on word boundaries inside Indic words.
"""
from __future__ import annotations

import re
import unicodedata

_INDIC = re.compile("[ऀ-৿]")
DEVANAGARI = re.compile("[ऀ-ॿ]")
BENGALI = re.compile("[ঀ-৿]")

_DIGITS = str.maketrans("०१२३४५६७८९০১২৩৪৫৬৭৮৯", "0123456789" * 2)


def _nfc(text: str) -> str:
    # "ज़" can arrive precomposed (U+095B) or as ज + nukta; NFC gives one spelling.
    return unicodedata.normalize("NFC", text)


def _keys(mapping: dict) -> dict:
    return {_nfc(k): v for k, v in mapping.items()}


# Longest first so "करोड़" wins over "करोड" and "बृहस्पतिवार" over nothing shorter.
SCALE_WORDS = _keys({
    "हज़ार": "thousand", "हजार": "thousand", "लाख": "lakh", "करोड़": "crore", "करोड": "crore",
    "হাজার": "thousand", "লাখ": "lakh", "লক্ষ": "lakh", "কোটি": "crore",
})

CURRENCY_WORDS = tuple(_nfc(w) for w in (
    "रुपये", "रुपए", "रुपया", "रुपयों", "रुपयो", "रु.", "টাকা", "টাকার", "টাকায়",
))

# Small number words, enough for "आठ लाख" / "তিন লাখ" to carry a value. A word not in
# this table in front of a scale word is reported as unparsed by scaled_without_value.
NUMBER_WORDS = _keys({
    "एक": 1, "दो": 2, "तीन": 3, "चार": 4, "पांच": 5, "पाँच": 5, "छह": 6, "छः": 6, "सात": 7,
    "आठ": 8, "नौ": 9, "दस": 10, "बीस": 20, "पच्चीस": 25, "पचास": 50, "डेढ़": 1.5, "ढाई": 2.5,
    "এক": 1, "দুই": 2, "তিন": 3, "চার": 4, "পাঁচ": 5, "ছয়": 6, "সাত": 7, "আট": 8, "নয়": 9,
    "দশ": 10, "বিশ": 20, "পঁচিশ": 25, "পঞ্চাশ": 50, "দেড়": 1.5, "আড়াই": 2.5,
})

DAY_NAMES = _keys({
    "सोमवार": "monday", "मंगलवार": "tuesday", "बुधवार": "wednesday", "गुरुवार": "thursday",
    "बृहस्पतिवार": "thursday", "शुक्रवार": "friday", "शनिवार": "saturday", "रविवार": "sunday",
    "इतवार": "sunday",
    "সোমবার": "monday", "মঙ্গলবার": "tuesday", "বুধবার": "wednesday", "বৃহস্পতিবার": "thursday",
    "শুক্রবার": "friday", "শনিবার": "saturday", "রবিবার": "sunday",
})

MONTH_NAMES = _keys({
    "जनवरी": "jan", "फ़रवरी": "feb", "फरवरी": "feb", "मार्च": "mar", "अप्रैल": "apr", "मई": "may",
    "जून": "jun", "जुलाई": "jul", "अगस्त": "aug", "सितंबर": "sep", "सितम्बर": "sep",
    "अक्टूबर": "oct", "अक्तूबर": "oct", "नवंबर": "nov", "नवम्बर": "nov", "दिसंबर": "dec",
    "दिसम्बर": "dec",
    "জানুয়ারি": "jan", "ফেব্রুয়ারি": "feb", "মার্চ": "mar", "এপ্রিল": "apr", "মে": "may",
    "জুন": "jun", "জুলাই": "jul", "আগস্ট": "aug", "সেপ্টেম্বর": "sep", "অক্টোবর": "oct",
    "নভেম্বর": "nov", "ডিসেম্বর": "dec",
})


def _alternation(words) -> str:
    return "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))


_SCALE_ALT = _alternation(SCALE_WORDS)
_RE_WORD_THEN_SCALE = re.compile(r"(" + _alternation(NUMBER_WORDS) + r")\s*(" + _SCALE_ALT + r")")
_RE_SCALE = re.compile(_SCALE_ALT)
_RE_DIGIT_SCALE = re.compile(r"\d[\d,]*(?:\.\d+)?\s*(?:" + _SCALE_ALT + r")")
# "8 lakh रुपये" / "5,000 টাকা" → "₹8 lakh" / "₹5,000", so the currency pattern sees it.
_RE_AMOUNT_THEN_CURRENCY = re.compile(
    r"(\d[\d,]*(?:\.\d+)?(?:\s*(?:thousand|lakh|crore))?)\s*(?:" + _alternation(CURRENCY_WORDS) + r")")
_RE_DAY = re.compile(_alternation(DAY_NAMES))
# Not followed by another Indic letter: "মে" (May) must not fire inside "মেগাওয়াট".
_RE_MONTH = re.compile(r"(?:" + _alternation(MONTH_NAMES) + r")(?![ऀ-৿])")


def has_indic(text: str) -> bool:
    return bool(_INDIC.search(text or ""))


def ascii_digits(text: str) -> str:
    return (text or "").translate(_DIGITS)


def normalise(text: str) -> str:
    """Rewrite Hindi/Bengali digits, number words, scale words, currency words, day
    and month names into their English forms. Non-Indic text comes back unchanged."""
    if not has_indic(text):
        return text
    out = ascii_digits(_nfc(text))
    out = _RE_WORD_THEN_SCALE.sub(lambda m: f"{NUMBER_WORDS[m.group(1)]:g} {m.group(2)}", out)
    out = _RE_SCALE.sub(lambda m: f" {SCALE_WORDS[m.group(0)]} ", out)
    out = _RE_AMOUNT_THEN_CURRENCY.sub(lambda m: f" ₹{m.group(1)} ", out)
    out = _RE_DAY.sub(lambda m: f" {DAY_NAMES[m.group(0)]} ", out)
    out = _RE_MONTH.sub(lambda m: f" {MONTH_NAMES[m.group(0)]} ", out)
    return out


def scaled_without_value(text: str) -> int:
    """How many Hindi/Bengali scale words carry no readable number in front of them
    ("कई लाख", "सात-आठ लाख" with an unknown word). An amount the checks cannot read
    is an amount they cannot vouch for."""
    if not has_indic(text):
        return 0
    prepared = ascii_digits(_nfc(text))
    total = len(_RE_SCALE.findall(prepared))
    readable = len(_RE_DIGIT_SCALE.findall(prepared)) + len(_RE_WORD_THEN_SCALE.findall(prepared))
    return max(0, total - readable)


def script_ratio(text: str, locale: str) -> float:
    """Share of the letters in `text` that are in the locale's own script."""
    pattern = DEVANAGARI if locale == "hi" else BENGALI
    letters = [c for c in text or "" if c.isalpha() or unicodedata.category(c).startswith("M")]
    if not letters:
        return 0.0
    return sum(1 for c in letters if pattern.match(c)) / len(letters)


def indic_ratio(text: str) -> float:
    """Share of the letters in `text` that are Devanagari or Bengali."""
    letters = [c for c in text or "" if c.isalpha() or unicodedata.category(c).startswith("M")]
    if not letters:
        return 0.0
    return sum(1 for c in letters if _INDIC.match(c)) / len(letters)
