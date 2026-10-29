"""
Grounding — deterministic checks that an answer states only what its evidence says.

WHY THIS EXISTS
The staff "ask memory" answer is told to use only the retrieved records and to cite
them. That is an instruction to a model, and this codebase does not treat
instructions as controls (see app/agents/guardrails.py). These pure functions are
the control: every bracketed citation must exist in the context block, and every
checkable fact the answer states — an amount, a size, a percentage, a date, an
email, a phone number, a Grid Nova reference id — must appear in the evidence.
Both sides go through the same normalisation, so "₹4.5 lakh" in an answer matches
"INR 450,000" in a record.

Nothing here calls an LLM, and nothing here is specific to customer memory: any
generated answer with a context block can be checked the same way.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from app.agents import guardrails, indic

# ── Violation codes ────────────────────────────────────────────────────────────

UNGROUNDED_ANSWER = "ungrounded_answer"     # the audit action when a fallback was sent
UNKNOWN_CITATION = "unknown_citation"
NO_CITATION = "no_citation"
UNSUPPORTED_FACT = "unsupported_fact"

# The sentence an answer gives when the records do not hold the answer. An answer
# that says so and states no fact needs no citation.
NOT_ON_RECORD = "That is not on record."
_RE_NOT_ON_RECORD = re.compile(r"\b(?:not|nothing(?: is)?) on record\b", re.I)

# A memory that only records what the customer typed ("[M12] (episode, …): Customer said: …",
# plus its detail line) proves they typed it, not that it is true. It may be cited, but its
# figures are not evidence: "budget is 50 lakh" from such a line would pass as a fact otherwise.
_RE_CUSTOMER_WORDS = re.compile(r"^[ \t]*\[M\d+\] \([^)]*\)[^:\n]*: Customer said:.*(?:\n[ \t]+detail:.*)?", re.M)


def verified_text(context_block: str) -> str:
    """The context block without customer-written memory lines: what a stated fact must match."""
    return _RE_CUSTOMER_WORDS.sub("", context_block or "")


@dataclass
class GroundingResult:
    violations: list = field(default_factory=list)
    citations: set = field(default_factory=set)
    facts: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.violations

    def describe(self) -> list[str]:
        return [v["detail"] for v in self.violations]


# ── Citations ──────────────────────────────────────────────────────────────────
# Markers as app/memory/recall.py builds them: [C1] identity, [L1] lead, [P1]
# profile, [M] / [M12] memories, [Q1] proposal, [T1] meeting.

_RE_CITATION = re.compile(r"\[([A-Z]\d*)\]")


def extract_citations(text: str) -> set[str]:
    return set(_RE_CITATION.findall(text or ""))


# ── Facts ──────────────────────────────────────────────────────────────────────

_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), start=1)}
_MON = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
_RE_ISO_DATE = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")
_RE_DMY_SLASH = re.compile(r"\b(\d{1,2})[/.](\d{1,2})[/.](\d{4})\b")
_RE_D_MON_Y = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+" + _MON + r",?\s+(\d{4})\b", re.I)
_RE_MON_D_Y = re.compile(r"\b" + _MON + r"\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b", re.I)
_RE_D_MON = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+" + _MON + r"(?!\s*,?\s*\d{4})", re.I)
_RE_KW = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(?:kwp?|kilowatts?)\b", re.I)
_RE_REFERENCE = re.compile(r"\bGN[ER]-[A-Z0-9]+(?:-[A-Z0-9]+)*\b", re.I)
_RE_ANY_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _iso(y: int, m: int, d: int) -> Optional[str]:
    try:
        return date(y, m, d).isoformat()
    except ValueError:
        return None


def _dates(text: str) -> set:
    found = set()
    for y, m, d in _RE_ISO_DATE.findall(text):
        found.add(_iso(int(y), int(m), int(d)))
    for d, m, y in _RE_DMY_SLASH.findall(text):   # Indian order: day first
        found.add(_iso(int(y), int(m), int(d)))
    for d, mon, y in _RE_D_MON_Y.findall(text):
        found.add(_iso(int(y), _MONTHS[mon.lower()[:3]], int(d)))
    for mon, d, y in _RE_MON_D_Y.findall(text):
        found.add(_iso(int(y), _MONTHS[mon.lower()[:3]], int(d)))
    found.discard(None)
    return found


def _day_months(text: str) -> set:
    """Dates written without a year ("14 Oct") — matched by day and month only."""
    return {(int(d), _MONTHS[mon.lower()[:3]]) for d, mon in _RE_D_MON.findall(text)}


def _value(raw: str) -> float:
    return float(raw.replace(",", ""))


def extract_facts(text: str) -> dict:
    """Every checkable fact a text states, normalised.

    money    — amounts with a currency marker or scale word, and 4+-digit / grouped
               figures (Indian grouping, lakh/crore — app/agents/guardrails.py's rules)
    kw       — system sizes
    percent  — percentages
    dates    — ISO strings; day_month for dates written without a year
    emails, phones (last 10 digits), references (GNE-…, GNR-…)
    """
    text = indic.normalise(_RE_CITATION.sub(" ", text or ""))
    references = {r.upper() for r in _RE_REFERENCE.findall(text)}
    # Reference ids and contact details are checked as themselves, never as amounts
    # or phone numbers: GNR-2026-000042 is not a phone, "+91 98765 43210" is not ₹98,765.
    text = _RE_REFERENCE.sub(" ", text)
    emails, phones, _ = guardrails._contact_details(text)
    scrubbed = guardrails._RE_PHONE.sub(" ", guardrails._RE_EMAIL.sub(" ", text))
    money, percent = set(), set()
    for value in guardrails._numbers_in(scrubbed):
        (percent.add(value[1]) if isinstance(value, tuple) else money.add(value))
    return {
        "money": money,
        "kw": {_value(n) for n in _RE_KW.findall(scrubbed)},
        "percent": percent,
        "dates": _dates(text),
        "day_month": _day_months(text),
        "emails": emails,
        "phones": phones,
        "references": references,
    }


_RE_KW_FIELD = re.compile(r"\w*kw\w*\s*[:=]\s*(\d[\d,]*(?:\.\d+)?)", re.I)
_RE_PERCENT_FIELD = re.compile(r"\w*(?:percent|pct)\w*\s*[:=]\s*(\d[\d,]*(?:\.\d+)?)", re.I)


def _evidence_numbers(text: str) -> dict:
    """What the evidence can support, per kind. Money is the permissive side: any
    number, raw or scaled, so "budget: 450000" supports "₹4.5 lakh" and
    "monthly_electricity_bill: 800" supports "₹800". A size or a percentage needs the
    same kind on record ("5 kW", "capacity_kw: 5", "30%") — "LEAD 7" never makes
    "7 kW" true."""
    text = indic.normalise(text or "")
    facts = extract_facts(text)
    money = {_value(n) for n in _RE_ANY_NUMBER.findall(text)} | facts["money"]
    return {
        "money": money,
        "kw": facts["kw"] | {_value(n) for n in _RE_KW_FIELD.findall(text)},
        "percent": facts["percent"] | {_value(n) for n in _RE_PERCENT_FIELD.findall(text)},
    }


# ── Check ──────────────────────────────────────────────────────────────────────

def check_grounding(answer: str, context_block: str, evidence: str = "") -> GroundingResult:
    """Check `answer` against the context block it was generated from.

    `evidence` is anything else the answer may legitimately repeat (the customer
    label it was given). The question itself is NOT evidence: repeating the
    employee's own guess back would otherwise pass as a fact on record.
    """
    answer = answer or ""
    corpus = f"{verified_text(context_block)}\n{evidence or ''}"
    result = GroundingResult(citations=extract_citations(answer), facts=extract_facts(answer))

    known = extract_citations(context_block)
    for marker in sorted(result.citations - known):
        result.violations.append({"code": UNKNOWN_CITATION,
                                  "detail": f"cites [{marker}], which is not in the records"})

    facts = result.facts
    stated_any = any(facts[k] for k in facts)
    if not result.citations and not (_RE_NOT_ON_RECORD.search(answer) and not stated_any):
        result.violations.append({"code": NO_CITATION,
                                  "detail": "cites no record and is not the not-on-record answer"})

    for item in _unsupported(facts, corpus):
        result.violations.append({"code": UNSUPPORTED_FACT, "detail": f"states {item}, which no record holds"})
    return result


def _unsupported(facts: dict, corpus: str) -> list[str]:
    """Each stated fact the corpus does not hold, as "<kind> <value>"."""
    have = extract_facts(corpus)
    numbers = _evidence_numbers(corpus)
    unsupported = []
    for kind in ("money", "kw", "percent"):
        unsupported += [f"{kind} {v:g}" for v in sorted(facts[kind]) if v not in numbers[kind]]
    unsupported += [f"date {d}" for d in sorted(facts["dates"] - have["dates"])]
    have_day_months = have["day_month"] | {(int(d[8:]), int(d[5:7])) for d in have["dates"]}
    unsupported += [f"date {d} {m}" for d, m in sorted(facts["day_month"] - have_day_months)]
    for kind in ("emails", "phones", "references"):
        unsupported += [f"{kind[:-1]} {v}" for v in sorted(facts[kind] - have[kind])]
    return unsupported


def records_found_answer(context_block: str, customer_label: str) -> str:
    """The deterministic answer when a generated one cannot be verified: the
    retrieved records themselves, labelled as such (the same shape as the mock
    answer, without the mock marker)."""
    if not (context_block or "").strip():
        return f"Nothing is on record for {customer_label} that matches that question."
    return (f"Records found for {customer_label} (the written answer could not be verified "
            f"against them, so here they are in relevance order):\n\n{context_block}")


# ── Nova Records (BE-9) ────────────────────────────────────────────────────────
# A staff answer composed from read-only tool results. Same fact checks as above, plus
# what only a records answer can get wrong: citing a record no tool returned, stating a
# total, count or average that no tool computed (the model must never do arithmetic),
# and stating a kind of value the caller's role may not see.

MODEL_ARITHMETIC = "model_arithmetic"
HIDDEN_FIELD = "hidden_field"
CLAIMED_ACTION = "claimed_action"
RECORDS_UNVERIFIED = "records_answer_unverified"   # the audit action when a fallback was sent

# A citation is whatever sits in brackets and looks like a reference (it has a digit or
# a hyphen): [GNE-WB-RES-2026-00125], [ARC-17], [L45], [C12], [M3], [SR-4], [COMPANY].
_RE_RECORD_CITATION = re.compile(r"\[([A-Za-z0-9][A-Za-z0-9/_.-]{0,99})\]")
_RE_RECORD_REF = re.compile(r"(?<![\w-])(?:GN[ERA]|ARC|SR|PKG|EQ|PRICE)-[A-Z0-9]+(?:[-_][A-Z0-9]+)*\b", re.I)
_RE_AGGREGATE_WORDS = re.compile(
    r"\b(?:total(?:s|ling|ing)?|sum|in all|altogether|combined|average|avg|mean|overall|"
    r"how many|number of|count(?:s|ed)?)\b", re.I)
_RE_COUNTED = re.compile(r"\b(\d[\d,]*)\s+(?:\w+\s+){0,2}?(?:projects?|leads?|customers?|installations?|systems?|"
                         r"requests?|associates?|results?|records?|matches|sites?)\b", re.I)
_RE_SENTENCE = re.compile(r"[^.!?\n]+(?:[.!?]+|$)")
_RE_MONEY_MARK = re.compile(r"(?:₹|\brs\.?\s*\d|\binr\b|\blakhs?\b|\bcrores?\b|\brupees?\b)", re.I)
_SCALES = (1e3, 1e5, 1e7)
# Nova Records changes nothing, so an answer that says it did something is wrong however
# well its facts check out ("I have approved the refund", "the request has been closed").
_ACTION_VERBS = (r"approved|sent|emailed|messaged|updated|changed|marked|assigned|reassigned|booked|scheduled|"
                 r"deleted|removed|cancelled|canceled|refunded|recorded|closed|resolved|created|added|notified|paid")
_RE_CLAIMED_ACTION = re.compile(
    rf"\b(?:i(?:'ve| have)?|nova(?: records)? has)\s+(?:now\s+|just\s+|already\s+)?(?:{_ACTION_VERBS})\b|"
    rf"\b(?:has|have)\s+been\s+(?:{_ACTION_VERBS})\s+(?:by me|for you|as (?:you )?requested|now)\b|"
    rf"\bi(?:'ll| will)\s+(?:now\s+)?(?:approve|send|email|update|change|mark|assign|book|delete|cancel|refund|"
    rf"record|close|resolve|notify|pay)\b", re.I)


def record_citations(text: str) -> set[str]:
    return {m for m in _RE_RECORD_CITATION.findall(text or "") if any(ch.isdigit() for ch in m) or "-" in m
            or m.isupper() and len(m) > 3}


def _scrub_for_numbers(text: str) -> str:
    text = _RE_RECORD_CITATION.sub(" ", text)
    text = _RE_RECORD_REF.sub(" ", text)
    for pattern in (_RE_ISO_DATE, _RE_DMY_SLASH, _RE_D_MON_Y, _RE_MON_D_Y, _RE_D_MON):
        text = pattern.sub(" ", text)
    return guardrails._RE_PHONE.sub(" ", guardrails._RE_EMAIL.sub(" ", text))


def _numbers_ok(sentence: str, allowed: set) -> list[float]:
    """Raw numbers in `sentence` that are neither allowed themselves nor the leading
    figure of an allowed scaled amount ("₹4.5 lakh" for 450000)."""
    scaled = {v for v in guardrails._numbers_in(sentence) if not isinstance(v, tuple)}
    bad = []
    for raw in _RE_ANY_NUMBER.findall(sentence):
        v = _value(raw)
        if v in allowed or any(m in allowed and abs(m - v * k) < 0.5 for m in scaled for k in _SCALES):
            continue
        bad.append(v)
    return bad


def check_records_answer(answer: str, evidence: str, *, known_refs: set, computed: set, counts: set,
                         searched_text: str = "", sees_money: bool = True, sees_contact: bool = True,
                         allow_no_citation: bool = False) -> GroundingResult:
    """Check a Nova Records answer.

    evidence   the tool results the answer may state facts from (customer-written free
               text excluded by the caller, so an instruction inside a note cannot make
               its own numbers "true")
    known_refs every ref a tool returned (case-insensitive)
    computed   every total / average / count a tool computed in SQL (as floats)
    counts     every count a tool returned (search totals, row counts, group sizes)
    searched_text  the filters as applied — numbers the employee asked about (a year,
               a size) may be repeated in a total's sentence
    """
    answer = answer or ""
    result = GroundingResult(citations=record_citations(answer), facts=extract_facts(answer))
    known = {r.upper() for r in known_refs}
    for marker in sorted(result.citations):
        if marker.upper() not in known:
            result.violations.append({"code": UNKNOWN_CITATION,
                                      "detail": f"cites [{marker}], which no tool returned"})
    for ref in sorted({r.upper() for r in _RE_RECORD_REF.findall(answer)} - known):
        result.violations.append({"code": UNSUPPORTED_FACT, "detail": f"names {ref}, which no tool returned"})
    facts = result.facts
    stated_any = any(facts[k] for k in facts)
    if not result.citations and not allow_no_citation and not (_RE_NOT_ON_RECORD.search(answer) and not stated_any):
        result.violations.append({"code": NO_CITATION, "detail": "cites no record and is not the not-on-record answer"})
    for item in _unsupported(facts, f"{evidence}\n{' '.join(known_refs)}"):
        result.violations.append({"code": UNSUPPORTED_FACT, "detail": f"states {item}, which no record holds"})

    searched_numbers = {_value(n) for n in _RE_ANY_NUMBER.findall(searched_text or "")}
    allowed_totals = set(computed) | set(counts) | searched_numbers
    scrubbed = _scrub_for_numbers(indic.normalise(answer))
    for sentence in _RE_SENTENCE.findall(scrubbed):
        if _RE_AGGREGATE_WORDS.search(sentence):
            for v in _numbers_ok(sentence, allowed_totals):
                result.violations.append({"code": MODEL_ARITHMETIC,
                                          "detail": f"states {v:g} as a total, count or average no tool computed"})
    for raw in _RE_COUNTED.findall(scrubbed):
        v = _value(raw)
        if v not in counts and v not in computed and v not in searched_numbers:
            result.violations.append({"code": MODEL_ARITHMETIC, "detail": f"counts {v:g}, which no tool counted"})

    if _RE_CLAIMED_ACTION.search(answer):
        result.violations.append({"code": CLAIMED_ACTION,
                                  "detail": "says an action was or will be taken; Nova Records only reads records"})
    if not sees_money and _RE_MONEY_MARK.search(answer):
        result.violations.append({"code": HIDDEN_FIELD, "detail": "states an amount of money; the role sees none"})
    if not sees_contact and (facts["emails"] or facts["phones"]):
        result.violations.append({"code": HIDDEN_FIELD, "detail": "states contact details; the role sees none"})
    # One violation per distinct detail.
    seen, unique = set(), []
    for v in result.violations:
        if v["detail"] not in seen:
            seen.add(v["detail"])
            unique.append(v)
    result.violations = unique
    return result
