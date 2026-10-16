"""
Deterministic checks on a Hindi or Bengali translation of a guarded English reply.

WHY THIS EXISTS
Hindi and Bengali replies are generated in English, run through every guardrail in
app/agents/guardrails.py, and only then translated by a separate model call. That
call is told to add nothing and remove nothing, but "the prompt said so" is not a
control here any more than it is for the reply itself. This module is the control:
nothing in a translation reaches a customer unless these pure functions agree it
says what the checked English said.

THE RULE
A translation may change the language and nothing else. The money values,
percentages, other numbers, times, dates, emails, phone numbers and URLs in it must
be exactly the ones in the English. It must actually be in the target language, and
of a plausible length. And it must not carry a warranty, guarantee, subsidy-certainty,
"you are eligible", approved/sent or DCR statement the English did not already make.

On any failure the caller sends the guarded English reply instead — never an
unchecked translation — and records TRANSLATION_REJECTED.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.agents import guardrails, indic

TRANSLATION_REJECTED = "translation_rejected"
TRANSLATED_LOCALES = ("hi", "bn")

# Engineering bounds, not business values. Brand names, "kW" and email addresses stay
# in Latin script, so a good translation is rarely 100% target script.
MIN_TARGET_SCRIPT_RATIO = 0.6
MIN_LENGTH_RATIO = 0.5
MAX_LENGTH_RATIO = 2.5


@dataclass
class TranslationCheck:
    reasons: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.reasons

    def violation(self, translation: str) -> dict:
        return {"code": TRANSLATION_REJECTED,
                "detail": "; ".join(self.reasons),
                "rejected_translation": (translation or "")[:500]}


# ── What a text states ─────────────────────────────────────────────────────────

# ASCII only: Bengali writes case endings straight onto a word ("info@gridnova.in-এ"),
# and a Unicode \w would read the ending as part of the address.
_RE_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
_URL_CHARS = r"[A-Za-z0-9._~:/?#@!$&'*+,;=%-]"
_RE_URL = re.compile(r"(?:https?://|www\.)" + _URL_CHARS + r"+"
                     r"|(?<![A-Za-z0-9@.-])[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.(?:in|com|org|net|co)(?![A-Za-z0-9])"
                     r"(?:/" + _URL_CHARS + r"*)?", re.I)
_TRAILING = ".,;:!?-"
_RE_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
_RE_CLOCK = re.compile(r"(?<![\d:])(\d{1,2}):(\d{2})(?!\d)")
_RE_CLOCK_WITH_PERIOD = re.compile(r"(?<![\d:])\d{1,2}:\d{2}\s*(?:am|pm)\b", re.I)
_RE_HOUR = re.compile(r"(?<![\d:])(\d{1,2})\s*(?:am|pm|बजे|টায়|টা)(?![a-z])", re.I)
_DAYS = {"mon": "monday", "tue": "tuesday", "tues": "tuesday", "wed": "wednesday", "weds": "wednesday",
         "thu": "thursday", "thur": "thursday", "thurs": "thursday", "fri": "friday", "sat": "saturday",
         "sun": "sunday"}
_RE_DAY = re.compile(r"\b(" + "|".join(sorted(set(_DAYS) | set(_DAYS.values()), key=len, reverse=True)) + r")\b", re.I)
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_RE_DATE = re.compile(r"(?<![\d.])(\d{1,2})\s+(" + "|".join(_MONTHS) + r")[a-z]*\.?", re.I)


def _strip_links(text: str) -> str:
    return _RE_URL.sub(" ", _RE_EMAIL.sub(" ", text))


def statements(text: str) -> dict:
    """Every checkable fact in `text`, Hindi/Bengali forms read as English."""
    text = text or ""
    norm = indic.normalise(text)
    emails = {m.group(0).lower().rstrip(_TRAILING) for m in _RE_EMAIL.finditer(norm)}
    urls = {m.group(0).lower().rstrip(_TRAILING) for m in _RE_URL.finditer(_RE_EMAIL.sub(" ", norm))}
    plain = _strip_links(norm)
    _, phones, _ = guardrails._contact_details(plain)
    times = {f"{int(h)}:{m}" for h, m in _RE_CLOCK.findall(plain)}
    times |= {("hour", int(h)) for h in _RE_HOUR.findall(plain)}
    times |= {_DAYS.get(d.lower(), d.lower()) for d in _RE_DAY.findall(plain)}
    dates = {(int(d), mon.lower()[:3]) for d, mon in _RE_DATE.findall(plain)}
    numbers = {float(n.replace(",", "")) for n in _RE_NUMBER.findall(plain) if n.replace(",", "").strip(".")}
    return {
        "money": guardrails._numbers_in(plain),
        "numbers": numbers,
        "times": times,
        "dates": dates,
        "emails": emails,
        "phones": phones,
        "urls": urls,
    }


# ── Second net: statements Hindi/Bengali can make that English patterns cannot see ──
#
# Each entry: the Hindi/Bengali (or Latin) words that make the statement, and the
# English pattern that shows the guarded English already made it. A translation may
# carry one of these only when the English did.

def _alt(*words) -> str:
    return "|".join(re.escape(indic._nfc(w)) for w in words)


_SUBSIDY = _alt("सब्सिडी", "अनुदान", "ভর্তুকি", "সাবসিডি", "subsidy", "CFA")
_CERTAIN = _alt("ज़रूर", "जरूर", "निश्चित", "पक्का", "पक्की", "गारंटी", "अवश्य", "तय है",
                "অবশ্যই", "নিশ্চিত", "নিশ্চিতভাবে", "পাক্কা", "গ্যারান্টি", "নিশ্চয়ই")

SECOND_NET = [
    ("warranty",
     re.compile(_alt("वारंटी", "वॉरंटी", "वारन्टी", "ওয়ারেন্টি", "ওয়ারান্টি", "ওয়্যারেন্টি", "warranty"), re.I),
     re.compile(r"\bwarrant", re.I)),
    ("guarantee",
     re.compile(_alt("गारंटी", "गारण्टी", "गैरंटी", "গ্যারান্টি", "গ্যারেন্টি", "guarantee"), re.I),
     re.compile(r"\bguarantee", re.I)),
    ("subsidy_certainty",
     re.compile(r"(?:" + _SUBSIDY + r")[^.!?।\n]*(?:" + _CERTAIN + r")|(?:" + _CERTAIN + r")[^.!?।\n]*(?:"
                + _SUBSIDY + r")", re.I),
     re.compile(r"\bsubsid[^.!?\n]*\b(?:definitely|certainly|surely|guarantee|assured|confirmed)\b"
                r"|\b(?:definitely|certainly|surely|guarantee|assured|confirmed)\b[^.!?\n]*\bsubsid", re.I)),
    ("eligibility",
     re.compile(r"(?:आप|आपका|आपकी|आपको|আপনি|আপনার)[^.!?।\n]{0,25}(?:" + _alt(
         "पात्र", "योग्य", "हकदार", "हक़दार", "एलिजिबल", "যোগ্য", "পাত্র", "অধিকারী", "এলিজিবল") + r")", re.I),
     re.compile(r"\byou(?:'re|'ll| are| will| would)?\s+(?:\w+\s+){0,2}(?:eligible|entitled|qualif)", re.I)),
    ("approved_or_sent",
     re.compile(_alt("मंज़ूर", "मंजूर", "स्वीकृत", "अनुमोदित", "भेज दिया", "भेज दी", "भेज दिए", "भेजा जा चुका",
                     "भेजा गया", "भेजी गई", "भेज दिया गया", "डिस्पैच", "रवाना",
                     "অনুমোদিত", "অনুমোদন হয়ে", "মঞ্জুর", "পাঠানো হয়েছে", "পাঠিয়ে দিয়েছি", "পাঠিয়েছি",
                     "পাঠিয়ে দেওয়া হয়েছে", "ডিসপ্যাচ", "রওনা"), re.I),
     re.compile(r"\b(?:approved|sent|dispatched|issued|signed off|on its way)\b", re.I)),
    ("dcr",
     re.compile(_alt("डीसीआर", "ডিসিআর", "DCR"), re.I),
     re.compile(r"\bDCR\b", re.I)),
]


def _second_net(english: str, translation: str) -> list[str]:
    reasons = []
    for name, in_translation, in_english in SECOND_NET:
        if in_translation.search(indic._nfc(translation)) and not in_english.search(english):
            reasons.append(f"translation made a {name.replace('_', ' ')} statement the English did not")
    return reasons


def _equipment_and_leaks(english: str, translation: str) -> list[str]:
    reasons = []
    for make in guardrails._KNOWN_EQUIPMENT_MAKERS_CI:
        if guardrails._mentions(make, translation) and not guardrails._mentions(make, english):
            reasons.append(f"translation named equipment make {make!r} the English did not")
    if guardrails.check_leaks(translation) and not guardrails.check_leaks(english):
        reasons.append("translation echoed an internal marker")
    return reasons


# ── Entry point ────────────────────────────────────────────────────────────────

def verify_translation(english: str, translation: str, locale: str, *, mock: bool = False) -> TranslationCheck:
    """Compare a translation with the guarded English it came from.

    `mock` is set only for the mock-mode client, whose "translation" is the English
    text itself: the script check is skipped for it, every other check still runs.
    """
    result = TranslationCheck()
    english, translation = english or "", (translation or "").strip()
    if not translation:
        result.reasons.append("translation was empty")
        return result

    want, got = statements(english), statements(translation)
    for kind in ("money", "numbers", "times", "dates", "emails", "phones", "urls"):
        if want[kind] != got[kind]:
            missing = sorted(want[kind] - got[kind], key=str)
            added = sorted(got[kind] - want[kind], key=str)
            result.reasons.append(f"{kind} differ (missing {missing}, added {added})")
    for clock in _RE_CLOCK_WITH_PERIOD.findall(english):
        if clock.lower() not in translation.lower():
            result.reasons.append(f"meeting time {clock!r} not kept exactly")
    unread = indic.scaled_without_value(translation)
    if unread:
        result.reasons.append(f"{unread} amount(s) written in words the checks cannot read")

    if not mock:
        ratio = indic.script_ratio(_strip_links(translation), locale)
        if ratio < MIN_TARGET_SCRIPT_RATIO:
            result.reasons.append(f"only {ratio:.0%} of the letters are in the target script")
    length_ratio = len(translation) / max(len(english), 1)
    if not MIN_LENGTH_RATIO <= length_ratio <= MAX_LENGTH_RATIO:
        result.reasons.append(f"length ratio {length_ratio:.2f} is outside {MIN_LENGTH_RATIO}–{MAX_LENGTH_RATIO}")

    result.reasons += _second_net(english, translation)
    result.reasons += _equipment_and_leaks(english, translation)
    return result
