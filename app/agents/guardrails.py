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

# Blocking codes replace the reply. The rest are recorded and let through: a reply
# that stacks two questions is a quality problem, not a commercial risk, and
# silently swapping it for a fallback would make the conversation worse.
_BLOCKING = {NEW_MONEY, UNOFFERED_TIME, FALSE_CLAIM, INTERNAL_LEAK, EMPTY_REPLY,
             nemo_guard.INTERNAL_DISCLOSURE}


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
) -> GuardrailResult:
    """Run every check over a generated reply and return what is safe to send.

    `allowed_text` is everything the conversation can justify — the customer's own
    words, the fields on file, and any meeting slots a real calendar query returned.
    `fallback` is the deterministic reply to send instead if something blocks.
    """
    violations = []
    for check in (
        lambda: check_money(reply, allowed_text),
        lambda: check_times(reply, allowed_text),
        lambda: check_claims(reply),
        lambda: check_leaks(reply, memory_context),
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
