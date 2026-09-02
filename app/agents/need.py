"""
Need gates — deciding, in code, whether a model call is actually warranted.

WHY
The Sales Agent used to make three model calls on every turn regardless of what the
customer said. One of them was routinely wasted: the meeting-signal call ran on every
message and its answer was discarded moments later whenever no slots were pending and
the conversation was nowhere near offering one. On a ten-turn conversation that is
ten calls bought and eight thrown away.

THE SHAPE OF EVERY GATE HERE
Each function answers one question — "is there anything here for the model to do?" —
using only deterministic signals. They are all biased the same way: **when in doubt,
call the model.** A wasted call costs a fraction of a cent; a missed meeting request
costs a customer, and a missed field means asking someone twice for something they
already told us. The gates only skip when skipping is provably safe.

None of these change what the agent decides. They change whether a question worth
asking gets asked at all.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

# Words that mean the customer might be talking about meeting, in any phrasing loose
# enough to be worth spending a call to interpret properly.
_SCHEDULING_TERMS = {
    "meet", "meeting", "call", "calls", "phone", "ring", "visit", "site",
    "schedule", "scheduling", "book", "booking", "appointment", "slot", "slots",
    "available", "availability", "free", "time", "times", "when", "date",
    "talk", "speak", "discuss", "chat", "zoom", "teams", "calendar", "diary",
    "today", "tomorrow", "morning", "afternoon", "evening", "week", "weekend",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "mon", "tue", "tues", "wed", "thu", "thur", "thurs", "fri", "sat", "sun",
    "am", "pm",
}

# Messages that carry nothing to extract. Only the unambiguous ones: a customer
# saying "ok thanks" has told us nothing, and asking a model to confirm that is
# money spent to learn nothing.
_ACKNOWLEDGEMENTS = {
    "ok", "okay", "k", "kk", "yes", "yeah", "yep", "yup", "no", "nope", "nah",
    "sure", "thanks", "thank", "thankyou", "ta", "cheers", "great", "good",
    "cool", "nice", "perfect", "lovely", "fine", "alright", "right", "got",
    "it", "understood", "noted", "sounds", "hi", "hello", "hey", "bye",
    "please", "you", "hmm", "hm", "haha", "lol", "and",
}

_WORD_RE = re.compile(r"[a-z0-9@'&.+-]+")
_MAX_ACK_WORDS = 5


def _words(text: str) -> list:
    return _WORD_RE.findall((text or "").lower())


def needs_field_extraction(message: str, collected: Optional[dict] = None) -> bool:
    """Is there plausibly anything in this message to extract?

    Conservative on purpose: this returns False only for a short message made
    entirely of acknowledgements, with no digits and no email. Everything else is
    worth a look, because a field missed here is a question the customer gets asked
    twice.
    """
    text = (message or "").strip()
    if not text:
        return False

    words = _words(text)
    if not words or len(words) > _MAX_ACK_WORDS:
        return True
    if any(char.isdigit() for char in text) or "@" in text:
        return True
    return not all(word in _ACKNOWLEDGEMENTS for word in words)


def needs_meeting_signal(
    message: str,
    conv_stage: str,
    pending_slots: Optional[dict] = None,
) -> bool:
    """Is a meeting-signal call worth making on this turn?

    Yes in three cases, and the first two matter most:

    1. Slots are already pending. Anything the customer says next might be picking
       one — including a bare "yes", which carries no scheduling word at all. This
       is exactly why the check cannot be keyword-only.
    2. The stage is meeting_offer, so the agent is about to offer times regardless.
    3. The message mentions anything to do with meeting, calling or visiting.

    Otherwise there is no path in the scheduling code that could act on the answer,
    so the call is skipped and treated as no intent.
    """
    if isinstance(pending_slots, dict) and pending_slots.get("slots"):
        return True
    if conv_stage == "meeting_offer":
        return True
    return bool(_SCHEDULING_TERMS.intersection(_words(message)))


def skipped(operation: str, reason: str, lead_id: Optional[int] = None) -> None:
    """Record that a model call was deliberately not made.

    Counted on the run so the saving is visible in the governance report rather than
    being an invisible optimisation nobody can verify or challenge.
    """
    from app.agents import harness

    harness.note_llm_skip()
    logger.debug(
        "Model call skipped",
        extra={"operation": operation, "reason": reason, "lead_id": lead_id},
    )
