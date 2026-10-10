"""
Guardrails — deterministic checks on what the Sales Agent is about to say.

WHY THIS EXISTS
The reply prompt already tells Nova never to quote a price, never to invent a
meeting time, and never to claim something was approved or sent. Those are
instructions to a model, and this codebase already treats that distinction
seriously elsewhere: a confirmed meeting slot is validated against the offered list
in code precisely because "the prompt said not to" is not a control. This module
applies the same standard to the rest of the reply.

Nothing here calls an LLM. Every check is a pure function over the generated text
and what the conversation can actually justify.

THE CENTRAL RULE
The agent may repeat a number the customer gave it. It may not introduce one.
That single rule is what makes price checking precise instead of guesswork — a
customer saying "our bill is 3 lakh" and Nova saying "3 lakh a month is a lot to
work with" is fine, while Nova saying "we can do this for 8 lakh" is a commercial
commitment nobody authorised. The same shape applies to times: a slot that came out
of a real calendar query is quotable, an invented Tuesday is not.

WHAT HAPPENS ON A VIOLATION
A blocking violation replaces the whole reply with the deterministic, stage-aware
fallback the LLM client already uses when a call fails — warm, safe, and never
transactional. Redacting mid-sentence would leave a worse message than replacing it.
Non-blocking violations are recorded and the reply goes out unchanged.

Every violation is written to the audit log against the lead, so a model that starts
drifting is visible rather than silently corrected.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

from app.agents import nemo_guard

logger = logging.getLogger(__name__)

# Replies are chat-length by design. Past this, something has gone wrong with the
# generation rather than the customer needing more words.
MAX_REPLY_CHARS = 900

# ── Violation codes ────────────────────────────────────────────────────────────

NEW_MONEY = "new_money_figure"
UNOFFERED_TIME = "unoffered_meeting_time"
FALSE_CLAIM = "false_completion_claim"
INTERNAL_LEAK = "internal_prompt_leak"
EMPTY_REPLY = "empty_reply"
OVERLONG = "overlong_reply"
MULTIPLE_QUESTIONS = "multiple_questions"
UNAPPROVED_EQUIPMENT = "unapproved_equipment_claim"
WARRANTY_TERM = "unverified_warranty_term"
MODEL_NUMBER = "equipment_model_number"
DCR_CLAIM = "unverified_dcr_claim"
SUBSIDY_CLAIM = "subsidy_eligibility_claim"
PERSONAL_DATA = "unverified_personal_data"

# Blocking codes replace the reply. The rest are recorded and let through: a reply
# that stacks two questions is a quality problem, not a commercial risk, and
# silently swapping it for a fallback would make the conversation worse.
_BLOCKING = {NEW_MONEY, UNOFFERED_TIME, FALSE_CLAIM, INTERNAL_LEAK, EMPTY_REPLY,
             UNAPPROVED_EQUIPMENT, WARRANTY_TERM, SUBSIDY_CLAIM, MODEL_NUMBER, DCR_CLAIM,
             PERSONAL_DATA, nemo_guard.INTERNAL_DISCLOSURE}


@dataclass
class GuardrailResult:
    reply: str
    violations: list = field(default_factory=list)
    blocked: bool = False

    @property
    def codes(self) -> list:
        return [v["code"] for v in self.violations]


# ── Money ──────────────────────────────────────────────────────────────────────
#
# Amounts are normalised to their VALUE, not their spelling, for two reasons found
# by running real replies through this:
#
#   "we can do this for about eight lakh"  — an invented price written in words.
#   Matching digits alone missed it completely.
#
#   "3 lakh" once normalised to the digit string "3", which a customer saying
#   "3 bedrooms" would then have justified. Comparing magnitudes rather than
#   substrings closes that.

_MULTIPLIERS = {
    "hundred": 100, "thousand": 1_000, "k": 1_000,
    "lakh": 100_000, "lakhs": 100_000, "lac": 100_000, "lacs": 100_000,
    "crore": 10_000_000, "crores": 10_000_000,
}

_WORD_NUMBERS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30,
    "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80,
    "ninety": 90,
}

_NUM_WORDS = "|".join(sorted(_WORD_NUMBERS, key=len, reverse=True))
_MULT_WORDS = "|".join(sorted(_MULTIPLIERS, key=len, reverse=True))

# A currency marker with digits, optionally scaled: "INR 45,000", "Rs 1.2 crore".
_RE_CURRENCY = re.compile(
    r"(?:₹|\binr\b|\brs\.?)\s*([\d,]+(?:\.\d+)?)\s*(" + _MULT_WORDS + r")?\b", re.I)
# Digits with a scale word: "45 lakh", "1.2 crore", "50k".
_RE_DIGIT_SCALED = re.compile(r"\b([\d,]+(?:\.\d+)?)\s*(" + _MULT_WORDS + r")\b", re.I)
# Words with a scale word: "eight lakh", "twenty five thousand".
_RE_WORD_SCALED = re.compile(
    r"\b((?:" + _NUM_WORDS + r")(?:[-\s]+(?:" + _NUM_WORDS + r"))?)\s+("
    + _MULT_WORDS + r")\b", re.I)
# A bare figure big enough to be money: 4+ digits, or comma-grouped. A calendar
# year is excluded on purpose - "running by 2026" is a timeline, not a quote.
_RE_BARE = re.compile(
    r"\b(\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|(?!(?:19|20)\d{2}\b)\d{4,}(?:\.\d+)?)\b")
# Per-kW rates, the specific shape a solar quote takes.
_RE_RATE = re.compile(r"\b([\d,]+(?:\.\d+)?)\s*(?:per|/)\s*kw\b", re.I)
# A promised percentage commits the company just as surely as a rupee figure:
# "you'll save 40%" is an outcome Nova is not entitled to promise. Kept in its own
# namespace so a percentage never matches a rupee amount of the same digits.
_RE_PERCENT = re.compile(r"\b([\d,]+(?:\.\d+)?)\s*(?:%|per ?cent)", re.I)


def _to_number(text: str) -> float:
    cleaned = text.replace(",", "").strip().lower()
    try:
        return float(cleaned)
    except ValueError:
        pass
    total = 0.0
    for word in re.split(r"[-\s]+", cleaned):
        total += _WORD_NUMBERS.get(word, 0)
    return total


def _numbers_in(text: str) -> set:
    """Every money-shaped amount in the text, as a normalised value.

    "45,000", "45000" and "forty five thousand" all land on the same number, so a
    reply is judged on what it actually claims rather than how it spelled it.
    """
    text = text or ""
    found = set()

    for match in _RE_CURRENCY.finditer(text):
        scale = _MULTIPLIERS.get((match.group(2) or "").lower(), 1)
        found.add(_to_number(match.group(1)) * scale)
    for match in _RE_DIGIT_SCALED.finditer(text):
        found.add(_to_number(match.group(1)) * _MULTIPLIERS[match.group(2).lower()])
    for match in _RE_WORD_SCALED.finditer(text):
        found.add(_to_number(match.group(1)) * _MULTIPLIERS[match.group(2).lower()])
    for match in _RE_BARE.finditer(text):
        found.add(_to_number(match.group(1)))
    for match in _RE_RATE.finditer(text):
        found.add(_to_number(match.group(1)))
    for match in _RE_PERCENT.finditer(text):
        found.add(("pct", _to_number(match.group(1))))

    return {v for v in found if v != 0 and v != ("pct", 0)}


def check_money(reply: str, allowed_text: str) -> Optional[dict]:
    """Flag any amount in the reply that the conversation cannot justify."""
    introduced = _numbers_in(reply) - _numbers_in(allowed_text)
    if not introduced:
        return None
    return {
        "code": NEW_MONEY,
        "detail": f"reply introduced amounts not present in the conversation: {sorted(introduced, key=str)}",
    }


# ── Meeting times ──────────────────────────────────────────────────────────────

_TIME_PATTERNS = [
    re.compile(r"\b(\d{1,2}:\d{2})\b"),
    re.compile(r"\b(\d{1,2}\s*(?:am|pm))\b", re.I),
    re.compile(r"\b(mon|tue|tues|wed|weds|thu|thur|thurs|fri|sat|sun)(?:day|sday|nesday|rsday|urday)?\b", re.I),
]


def _times_in(text: str) -> set:
    found = set()
    for pattern in _TIME_PATTERNS:
        for match in pattern.finditer(text or ""):
            found.add(match.group(1).lower().replace(" ", ""))
    return found


def check_times(reply: str, allowed_text: str) -> Optional[dict]:
    """Flag a specific time or day the reply proposes that was never actually free.

    Allowed text carries the slots a real free/busy query returned, so a genuine
    offer passes and an invented "how about Tuesday?" does not.
    """
    introduced = _times_in(reply) - _times_in(allowed_text)
    if not introduced:
        return None
    return {
        "code": UNOFFERED_TIME,
        "detail": f"reply proposed times that were never offered: {sorted(introduced)}",
    }


# ── Claims the agent cannot make ───────────────────────────────────────────────

# Past-tense completion claims only. "I'll pass this to the team" is fine and must
# stay fine — it is the claim that something already happened that Nova cannot know.
_FALSE_CLAIM_PATTERNS = [
    re.compile(r"\b(?:has|have|is|was|been|already)\s+(?:been\s+)?"
               r"(?:approved|signed off|dispatched|issued)\b", re.I),
    re.compile(r"\b(?:i|we)(?:'ve| have|'ll have)\s+(?:sent|approved|issued|dispatched)\b", re.I),
    re.compile(r"\byour (?:proposal|quote|quotation) (?:has been|was|is)\s+"
               r"(?:approved|sent|ready|issued)\b", re.I),
    re.compile(r"\b(?:i|we)\s+guarantee\b|\bguaranteed\b", re.I),
    re.compile(r"\b(?:locked in|confirmed) (?:your|the) (?:price|rate|discount)\b", re.I),
]


def check_claims(reply: str) -> Optional[dict]:
    for pattern in _FALSE_CLAIM_PATTERNS:
        match = pattern.search(reply or "")
        if match:
            return {
                "code": FALSE_CLAIM,
                "detail": f"reply claimed a completed or guaranteed action: {match.group(0)!r}",
            }
    return None


# ── Equipment, warranty and subsidy claims ─────────────────────────────────────
# Three things Nova must never state on its own authority: which make of equipment
# Grid Nova installs (only active Equipment/SKU Master records say that), what a
# warranty covers or for how long (only COMPANY_FACTS could, and today it states no
# warranty terms at all), and that a particular customer will get a subsidy (only
# the scheme's own verification decides that). Explaining the published PM Surya
# Ghar slabs as general scheme information stays allowed.

def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+|\n+", text or "") if s.strip()]


def _mentions(term: str, text: str) -> bool:
    return bool(term) and re.search(r"(?<![\w-])" + re.escape(term) + r"(?![\w-])", text or "", re.I) is not None


# Recognition list only — names that, when they appear in a reply, are a claim about
# a specific make. It asserts nothing about any of them. Short or common-word names
# are matched case-sensitively.
_KNOWN_EQUIPMENT_MAKERS_CI = (
    "Adani", "Waaree", "Tata Power Solar", "Tata Power", "Vikram Solar", "Premier Energies", "RenewSys",
    "Goldi", "Rayzon", "Saatvik", "Emmvee", "Navitas", "Loom Solar", "Havells", "Microtek", "Exide",
    "Amara Raja", "Amaron", "Su-Kam", "Livguard", "Longi", "LONGi", "Jinko", "JinkoSolar", "Trina",
    "JA Solar", "Canadian Solar", "Risen", "Astronergy", "Huawei", "Sungrow", "Growatt", "Solis",
    "GoodWe", "Fronius", "Enphase", "SolarEdge", "Schneider", "Polycab", "Tesla", "Panasonic",
    "SunPower", "Q Cells", "Qcells", "Q-Cells", "Pylontech", "Luminous", "V-Guard", "UTL Solar",
)
_KNOWN_EQUIPMENT_MAKERS_CS = ("Tata", "LG", "REC", "SMA", "ABB", "BYD", "Delta", "UTL")

_PRODUCT = r"(?:solar\s+)?(?:panels?|modules?|inverters?|batter(?:y|ies))"
# "<Name> panels", "<Name> inverter" — a capitalised word right before a product.
_RE_NAMED_PRODUCT = re.compile(r"\b([A-Z][A-Za-z0-9&-]+)\s+" + _PRODUCT + r"\b")
# "panels from <Name>", "inverters made by <Name>".
_RE_PRODUCT_FROM = re.compile(_PRODUCT + r"\s+(?:from|by|made by|manufactured by)\s+([A-Z][A-Za-z0-9&-]+)\b", re.I)
# Words that commonly sit capitalised before "panels"/"inverter" without naming a make.
_NOT_A_MAKE = {
    "solar", "rooftop", "roof", "mono", "monocrystalline", "poly", "polycrystalline", "bifacial",
    "perc", "topcon", "hjt", "dcr", "non-dcr", "lithium", "li-ion", "lead-acid", "string", "hybrid",
    "grid", "on-grid", "off-grid", "grid-tied", "micro", "our", "the", "your", "these", "those", "their",
    "good", "quality", "high", "premium", "standard", "tier", "branded", "indian", "domestic",
    "imported", "new", "any", "all", "some", "which", "what", "most", "such", "both", "more", "extra",
    "additional", "efficient", "reliable", "certified", "approved", "bis", "mnre", "alm", "almm",
    "pm", "kusum", "surya", "ghar", "residential", "commercial", "industrial", "backup", "storage",
    "one", "two", "three", "four", "five", "ten", "many", "several", "a", "an", "for", "with", "and",
}
# Nova claiming it supplies/uses/recommends something — what turns naming a make into
# an assertion even when the customer raised the name first.
_RE_ENDORSEMENT = re.compile(
    r"\b(?:we|i)\s+(?:(?:can|will|would|do|usually|typically|mostly|generally|always)\s+)?"
    r"(?:use|install|supply|offer|provide|stock|recommend|fit|source|carry|work with|go with|deal in)\b"
    r"|\bwe'll\s+(?:use|install|supply|fit)\b|\bcomes?\s+with\b|^\s*yes\b"
    # "our panels", "our preferred inverter brand" — not "our team will recommend".
    r"|\bour\s+(?:\w+\s+){0,2}(?:panels?|modules?|inverters?|batter(?:y|ies)|brands?|suppliers?|equipment)\b",
    re.I,
)


def _named_makes(sentence: str) -> list[str]:
    names = [m for m in _KNOWN_EQUIPMENT_MAKERS_CI if _mentions(m, sentence)]
    names += [m for m in _KNOWN_EQUIPMENT_MAKERS_CS
              if re.search(r"(?<![\w-])" + re.escape(m) + r"(?![\w-])", sentence)]
    for match in _RE_NAMED_PRODUCT.finditer(sentence):
        word = match.group(1)
        at_sentence_start = sentence[:match.start()].strip() == ""
        if word.lower() not in _NOT_A_MAKE and not at_sentence_start:
            names.append(word)
    for match in _RE_PRODUCT_FROM.finditer(sentence):
        if match.group(1).lower() not in _NOT_A_MAKE:
            names.append(match.group(1))
    return names


def check_equipment(reply: str, approved_terms=(), customer_text: str = "",
                    facts_text: str = "") -> Optional[dict]:
    """Block naming a specific equipment make/model unless an active Equipment/SKU
    Master record or company facts carry it. A make the customer raised may be named
    back to them, but not endorsed ("we use…", "our…", "yes…")."""
    approved = [t for t in approved_terms if t]
    for sentence in _sentences(reply):
        for name in _named_makes(sentence):
            if any(_mentions(term, name) or _mentions(name, term) for term in approved):
                continue
            if _mentions(name, facts_text):
                continue
            if _mentions(name, customer_text) and not _RE_ENDORSEMENT.search(sentence):
                continue
            return {"code": UNAPPROVED_EQUIPMENT,
                    "detail": f"reply named equipment make {name!r} that no active SKU record approves"}
    return None


_DURATION_WORDS = ("one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|"
                   "twenty|twenty[- ]five|thirty")
_RE_DURATION = re.compile(r"\b(?:\d+(?:\.\d+)?|" + _DURATION_WORDS + r")\s*[- ]?\s*(?:years?|yrs?|months?)\b", re.I)
_RE_WARRANTY_WORD = re.compile(r"\bwarrant(?:y|ies|eed)\b|\bguarantee[sd]?\b", re.I)
_RE_DURATION_ON_PART = re.compile(
    _RE_DURATION.pattern + r"\s+(?:of\s+\w+\s+)?(?:on|for|covering)\s+(?:the\s+|your\s+|its\s+)?"
    r"(?:panels?|modules?|inverters?|batter(?:y|ies)|system|structure|workmanship|installation)\b", re.I,
)


_NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
                 "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20,
                 "twenty five": 25, "twenty-five": 25, "thirty": 30}
# A part of the system: Grid Nova's supplied warranty is the STANDARD warranty from
# handover — a term tied to one part (panels, inverter, workmanship…) is a different claim.
_RE_SPECIFIC_PART = re.compile(r"\b(?:panels?|modules?|inverters?|batter(?:y|ies)|cables?|structure|workmanship|"
                               r"installation|performance|output)\b", re.I)


def _duration_key(text: str) -> tuple:
    m = re.match(r"\s*(\d+(?:\.\d+)?|[a-z]+(?:[- ][a-z]+)?)\s*[- ]?\s*(year|yr|month)", text.lower())
    if not m:
        return (text.lower(),)
    raw = m.group(1).replace("-", " ")
    value = float(raw) if raw[0].isdigit() else _NUMBER_WORDS.get(raw, raw)
    return (value, "month" if m.group(2) == "month" else "year")


def check_warranty(reply: str, facts_text: str = "") -> Optional[dict]:
    """Block a warranty duration or term ("25-year warranty", "10 years on the
    inverter") unless company facts state that duration as a warranty — and then only
    as the standard warranty, not tied to one part of the system. "5-year", "5 years"
    and "five years" are the same duration. "Warranty terms are in your proposal" names
    no term and is fine."""
    stated = {_duration_key(m.group(0)) for s in _sentences(facts_text) if _RE_WARRANTY_WORD.search(s)
              for m in _RE_DURATION.finditer(s)}
    for sentence in _sentences(reply):
        durations = [m.group(0) for m in _RE_DURATION.finditer(sentence)]
        if not durations:
            continue
        if not (_RE_WARRANTY_WORD.search(sentence) or _RE_DURATION_ON_PART.search(sentence)):
            continue
        for duration in durations:
            if _duration_key(duration) not in stated or _RE_SPECIFIC_PART.search(sentence):
                return {"code": WARRANTY_TERM,
                        "detail": f"reply stated a warranty term company facts do not: {sentence.strip()!r}"}
    return None


# A model number: a token mixing letters and digits ("WS-540", "SG5.0RS", "M10") in a
# sentence about equipment. Units and ordinals ("5kW", "10 kWh", "4 sqmm", "1st") are not.
_RE_ALNUM_TOKEN = re.compile(r"(?<![\w-])(?=[\w.\-/]*\d)(?=[\w.\-/]*[A-Za-z])[A-Za-z0-9][\w.\-/]*[A-Za-z0-9](?![\w-])")
_RE_UNIT = re.compile(r"^\d+(?:\.\d+)?(?:kw|kwp|kwh|w|wp|v|a|mw|mwp|sqmm|mm|yrs?|years?|months?|st|nd|rd|th|"
                      r"x|kv|kva|hrs?|hours?|am|pm|lakh|cr)$", re.I)
_RE_EQUIPMENT_WORD = re.compile(r"\b(?:panels?|modules?|inverters?|batter(?:y|ies)|cables?|models?)\b", re.I)


def check_model_numbers(reply: str, makes=()) -> Optional[dict]:
    """Nova never states an equipment model number (Grid Nova's master data supplies
    none; what is installed is recorded from the nameplate later)."""
    for sentence in _sentences(reply):
        about_equipment = _RE_EQUIPMENT_WORD.search(sentence) or any(_mentions(m, sentence) for m in makes if m)
        if not about_equipment:
            continue
        for token in _RE_ALNUM_TOKEN.findall(sentence):
            if _RE_UNIT.match(token) or token.upper().startswith(("PM-", "COVID")):
                continue
            return {"code": MODEL_NUMBER, "detail": f"reply stated an equipment model number: {token!r}"}
    return None


_RE_DCR_CLAIM = re.compile(
    r"\bDCR[- ]?(?:compliant|certified|approved|eligible|listed|registered)\b"
    r"|\b(?:is|are)\s+(?:all\s+|fully\s+|also\s+)?DCR\b"
    r"|\bDCR\s+(?:panels?|modules?)\s+(?:from|by)\b", re.I)
_RE_DCR_HEDGE = re.compile(r"\b(?:not|whether|if|confirm|check|verify|need|needs|required|requires|must)\b", re.I)


def check_dcr_claims(reply: str) -> Optional[dict]:
    """Nova never asserts that a make or package is DCR compliant — DCR status per
    make/model is not in Grid Nova's master data. Saying PM Surya Ghar needs DCR
    modules (general scheme information) stays allowed."""
    for sentence in _sentences(reply):
        if _RE_DCR_CLAIM.search(sentence) and not _RE_DCR_HEDGE.search(sentence):
            return {"code": DCR_CLAIM, "detail": f"reply asserted DCR compliance: {sentence.strip()!r}"}
    return None


def price_list_makes() -> list[str]:
    """Approved makes from Grid Nova's master data (every package, every component,
    and the common electrical specification) — part of Nova's brand allow-list."""
    from app.pricing import price_list
    data = price_list.load()
    makes = {m for package in price_list.PACKAGES for spec in data["equipment"][package].values()
             for m in (spec.get("makes") or [])}
    makes |= {m for item in data["electrical_spec_up_to_5kw"].values() for m in (item.get("makes") or [])}
    return sorted(makes)


_RE_MONEY_OR_SUBSIDY = r"(?:₹|\brs\.?|\binr\b|subsid|\bcfa\b|\blakh|\b\d{1,3}(?:,\d{2,3})+\b|\b\d{4,}\b)"
_SUBSIDY_CLAIM_PATTERNS = [
    re.compile(r"\byou(?:'re| are)\s+(?:definitely |certainly |fully |already |surely )?"
               r"(?:eligible|entitled|qualified|approved)\b", re.I),
    re.compile(r"\byou(?:'ll| will| would)\s+(?:definitely |certainly |surely )?"
               r"(?:qualify|be eligible|be entitled|be approved)\b", re.I),
    re.compile(r"\byou qualify\b", re.I),
    re.compile(r"\byou(?:'ll| will| would)\s+(?:definitely |certainly |surely )?"
               r"(?:get|receive|be paid|be given|be credited)\b[^.!?\n]{0,60}" + _RE_MONEY_OR_SUBSIDY, re.I),
    re.compile(r"\byour\s+(?:subsidy|cfa)\s+(?:is|has been|will be)\s+"
               r"(?:approved|sanctioned|confirmed|guaranteed|assured)\b", re.I),
    re.compile(r"\b(?:guaranteed|assured|confirmed|sure[- ]shot)\s+subsid", re.I),
    re.compile(r"\bsubsidy\s+(?:is|will be)\s+(?:guaranteed|assured|confirmed|certain)\b", re.I),
    # Negative determinations are just as much a ruling on this customer's case.
    re.compile(r"\byou(?:'re| are)\s+(?:not\s+|in)(?:eligible|entitled|qualified)\b", re.I),
    re.compile(r"\byou\s+aren't\s+(?:eligible|entitled|qualified)\b", re.I),
    re.compile(r"\byou\s+(?:do not|don't|will not|won't|would not|wouldn't|cannot|can't|can not)\s+"
               r"(?:qualify|be eligible|be entitled|avail)\b", re.I),
    re.compile(r"\byou\s+(?:do not|don't|will not|won't|would not|wouldn't|cannot|can't|can not)\s+"
               r"(?:get|receive|claim)\s+(?:the\s+|any\s+|a\s+)?(?:subsidy|cfa)\b", re.I),
    re.compile(r"\byour\s+(?:home|house|roof|rooftop|system|property|site|connection|application)\s+"
               r"(?:is not|isn't|does not|doesn't|will not|won't)\s+(?:be\s+)?(?:eligible|qualify|covered)\b", re.I),
]
# "if you qualify", "whether you are eligible" — conditional, not a determination.
_RE_CONDITIONAL_LEAD_IN = re.compile(r"\b(?:if|whether|once|when|unless|until)\s*$", re.I)


def check_subsidy(reply: str) -> Optional[dict]:
    """Block definite eligibility / approval / payout claims about THIS customer —
    positive ("you are eligible") or negative ("you don't qualify"). General statements
    of the published scheme ("residential systems get ₹30,000/kW for the first 2kW",
    "commercial rooftops aren't covered"), conditional or hedged phrasing ("if you
    qualify", "you may not be eligible") and "our team will confirm your eligibility"
    stay allowed."""
    for pattern in _SUBSIDY_CLAIM_PATTERNS:
        for match in pattern.finditer(reply or ""):
            if _RE_CONDITIONAL_LEAD_IN.search(reply[:match.start()]):
                continue
            return {"code": SUBSIDY_CLAIM,
                    "detail": f"reply made a definite subsidy claim: {match.group(0)!r}"}
    return None


# ── Internal leakage ───────────────────────────────────────────────────────────

# Markers straight out of the system prompt and the memory block. If any of these
# reach a customer, the model has started reciting its instructions.
_LEAK_MARKERS = [
    "current stage:", "already known:", "still needed:", "available times:",
    "what you remember about this person", "stage guidance", "you are nova,",
    "system prompt", "collected_fields", "what you should do differently",
    "salience", "self_critique",
]


def check_leaks(reply: str, memory_context: Optional[str] = None) -> Optional[dict]:
    lowered = (reply or "").lower()
    for marker in _LEAK_MARKERS:
        if marker in lowered:
            return {"code": INTERNAL_LEAK, "detail": f"reply echoed an internal marker: {marker!r}"}

    # An employee identifier has no business in a customer-facing message.
    if re.search(r"\bemp-[a-z0-9-]+\b", lowered):
        return {"code": INTERNAL_LEAK, "detail": "reply contained an internal employee id"}

    # A stored memory line repeated verbatim means the model is reading the dossier
    # out loud rather than using it. Long lines only — a short one could coincide.
    for line in (memory_context or "").split("\n"):
        payload = _memory_payload(line)
        if len(payload) > 40 and payload.lower() in lowered:
            return {"code": INTERNAL_LEAK, "detail": "reply recited a stored memory verbatim"}
    return None


def _memory_payload(line: str) -> str:
    """The human-readable half of a memory line, without its bookkeeping.

    Recall formats memories as "- (objection, 30 Aug 2026) summary" and profile
    fields as "Who they are: summary". Comparing the whole line would never match,
    because a reply that recites a memory recites the summary, not the metadata.
    """
    text = line.strip().lstrip("-").strip()
    prefixed = re.match(r"^\([^)]*\)\s*(.+)$", text)
    if prefixed:
        text = prefixed.group(1)
    head, sep, tail = text.partition(":")
    if sep and len(head) <= 40 and tail.strip():
        text = tail
    return text.strip()



# ── Personal data ──────────────────────────────────────────────────────────────
#
# A reply may repeat an email address, phone number or PIN code only if the customer
# typed it in this conversation (or it is Grid Nova's own published contact detail,
# from COMPANY_FACTS). Anything else came from somewhere the customer has no claim to —
# another customer's record, a memory, the model's imagination — and is withheld.
# Street addresses have no reliable shape to match; the PIN code is the part of an
# address this check can see.

_RE_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_RE_PHONE = re.compile(r"(?<![\w])\+?\d[\d\s().-]{7,}\d(?![\w])")
_RE_PIN = re.compile(r"(?<![\d,.])\d{6}(?![\d,.])")


def _phone_key(text: str) -> Optional[str]:
    digits = re.sub(r"\D", "", text)
    return digits[-10:] if len(digits) >= 10 else None


def _contact_details(text: str) -> tuple[set, set, set]:
    text = text or ""
    emails = {m.group(0).lower().rstrip(".") for m in _RE_EMAIL.finditer(text)}
    phones = {k for k in (_phone_key(m.group(0)) for m in _RE_PHONE.finditer(text)) if k}
    pins = {m.group(0) for m in _RE_PIN.finditer(text)}
    return emails, phones, pins


def check_personal_data(reply: str, customer_text: str = "", facts_text: str = "") -> Optional[dict]:
    emails, phones, pins = _contact_details(reply)
    known_emails, known_phones, known_pins = _contact_details(f"{customer_text}\n{facts_text}")
    found = []
    if emails - known_emails:
        found.append("email")
    if phones - known_phones:
        found.append("phone")
    if pins - known_pins:
        found.append("pin_code")
    if found:
        return {"code": PERSONAL_DATA,
                "detail": f"reply contained contact details the customer did not give: {', '.join(found)}"}
    return None


# ── Shape ──────────────────────────────────────────────────────────────────────

def check_shape(reply: str) -> list:
    """Non-commercial quality checks: empty, runaway, or a stacked pile of questions."""
    violations = []
    text = (reply or "").strip()
    if not text:
        violations.append({"code": EMPTY_REPLY, "detail": "generator returned nothing"})
        return violations
    if len(text) > MAX_REPLY_CHARS:
        violations.append({
            "code": OVERLONG,
            "detail": f"reply was {len(text)} chars, trimmed to {MAX_REPLY_CHARS}",
        })
    if text.count("?") > 2:
        violations.append({
            "code": MULTIPLE_QUESTIONS,
            "detail": f"reply stacked {text.count('?')} questions",
        })
    return violations


def _trim(text: str) -> str:
    """Cut an overlong reply back to its last complete sentence, never mid-word."""
    if len(text) <= MAX_REPLY_CHARS:
        return text
    window = text[:MAX_REPLY_CHARS]
    cut = max(window.rfind("."), window.rfind("!"), window.rfind("?"))
    return window[: cut + 1].strip() if cut > 200 else window.rsplit(" ", 1)[0].strip() + "…"


# ── Inbound ────────────────────────────────────────────────────────────────────

_INJECTION_PATTERNS = [
    re.compile(r"\bignore (?:all )?(?:the )?(?:previous|prior|above|earlier) (?:instructions?|rules?|prompts?)\b", re.I),
    re.compile(r"\bdisregard (?:your|all|the) (?:instructions?|rules?|training|guidelines?)\b", re.I),
    re.compile(r"\byou are now\b|\bfrom now on,? you\b|\bact as (?:if|though|a)\b", re.I),
    re.compile(r"\b(?:reveal|show|print|repeat|output) (?:your |the )?(?:system )?(?:prompt|instructions?|rules?)\b", re.I),
    re.compile(r"\bpretend (?:you are|to be)\b", re.I),
    re.compile(r"\bdeveloper mode\b|\bjailbreak\b", re.I),
]


def detect_injection(message: str) -> Optional[str]:
    """Spot an attempt to talk the agent out of its instructions.

    Detection only — the message is never rejected and never edited. A real customer
    can phrase something oddly, and refusing to answer them would be a worse failure
    than the attempt itself. The output checks are what actually contain the damage;
    this exists so the attempt is visible to the team.
    """
    for pattern in _INJECTION_PATTERNS:
        match = pattern.search(message or "")
        if match:
            return match.group(0)
    return None


# ── Entry point ────────────────────────────────────────────────────────────────

def enforce(
    reply: str,
    *,
    allowed_text: str,
    memory_context: Optional[str] = None,
    user_message: str = "",
    fallback: str = "",
    approved_equipment=(),
    customer_text: str = "",
) -> GuardrailResult:
    """Run every check over a generated reply and return what is safe to send.

    `allowed_text` is everything the conversation can justify — the customer's own
    words, the fields on file, and any meeting slots a real calendar query returned.
    `fallback` is the deterministic reply to send instead if something blocks.
    `approved_equipment` is the brand/model names of active Equipment/SKU Master
    records; `customer_text` is everything the customer has said (a make they named
    may be named back, not endorsed).
    """
    from app.llm.company_facts import COMPANY_FACTS
    makes = price_list_makes()
    approved = list(approved_equipment) + makes
    violations = []
    for check in (
        lambda: check_money(reply, allowed_text),
        lambda: check_times(reply, allowed_text),
        lambda: check_claims(reply),
        lambda: check_leaks(reply, memory_context),
        lambda: check_equipment(reply, approved, customer_text or user_message, COMPANY_FACTS),
        lambda: check_warranty(reply, COMPANY_FACTS),
        lambda: check_model_numbers(reply, makes),
        lambda: check_dcr_claims(reply),
        lambda: check_subsidy(reply),
        lambda: check_personal_data(reply, customer_text or user_message, COMPANY_FACTS),
    ):
        found = check()
        if found:
            violations.append(found)

    # The semantic rail is layered on top of the checks above, never in place of
    # them: it catches internal information paraphrased past every pattern. It costs
    # a model call, so it is skipped when a deterministic check has already blocked
    # the reply — the verdict could not change the outcome, only the bill. It is
    # also a no-op unless NEMO_GUARDRAILS_ENABLED is on.
    if violations:
        from app.agents import need

        need.skipped("nemo_output_rail", "reply already blocked deterministically")
    else:
        disclosure = nemo_guard.check_output(reply, user_message)
        if disclosure:
            violations.append(disclosure)

    violations.extend(check_shape(reply))

    blocked = any(v["code"] in _BLOCKING for v in violations)
    if blocked:
        safe = fallback or (
            "Let me get one of our team to pick this up with you properly."
        )
        logger.warning(
            "Guardrail blocked a generated reply",
            extra={"codes": [v["code"] for v in violations],
                   "details": [v["detail"] for v in violations]},
        )
        return GuardrailResult(reply=safe, violations=violations, blocked=True)

    return GuardrailResult(reply=_trim(reply), violations=violations, blocked=False)
