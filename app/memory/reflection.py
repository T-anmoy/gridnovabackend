"""
Reflection engine — turns a conversation into durable, retrievable memory.

WHY THIS EXISTS
The Sales Agent only ever sees the last handful of messages. Anything older falls
out of its window and, unless it happened to land in one of the twelve structured
qualification fields, is gone. Reflection is the step that catches the rest: the
reason behind a budget, the worry about roof shading, the fact that the spouse
signs off — and writes it somewhere it can be found again months later.

TWO KINDS OF OUTPUT, KEPT APART
- Memory about the CUSTOMER — episodes, preferences, objections, commitments.
- Memory about the AGENT ITSELF — what it judges it handled badly and would do
  differently. That lands in CustomerProfile.agent_notes and, when specific enough
  to stand alone, as a self_critique memory. Recall is careful to label these as
  the assistant's own opinion rather than something the customer said.

WHEN IT RUNS
At milestones, not every turn: qualification completes, a meeting is booked, a
lead goes quiet, or a long conversation crosses a message interval. That is one to
three LLM calls over a lead's whole life, and each one sees a meaningful chunk of
conversation instead of a single line.

THE PROPOSAL AGENT'S HALF
Further down, `record_proposal_outcome` does the same job for the Proposal Agent,
but deterministically. It never needs a model, because every judgement on a draft is
already a structured human action: approved, sent back with a written reason,
repriced from X to Y, accepted. See the section header there for why that half is
kept off CustomerProfile.

FAILURE POSTURE
Reflection is an enhancement, never a dependency. Every entry point swallows its
own exceptions: a lead is never damaged, and a customer reply is never delayed or
lost because remembering failed.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

from app.config import settings
from app.llm.client import _keywords, llm_client
from app.memory.models import MemoryKind, MemorySource, MessageRole

if TYPE_CHECKING:
    from app.memory.memory_manager import MemoryManager

logger = logging.getLogger(__name__)

# Milestones that justify spending an LLM call on reflection.
TRIGGER_QUALIFIED = "qualification_completed"
TRIGGER_MEETING_BOOKED = "meeting_booked"
TRIGGER_LEAD_QUIET = "lead_quiet"
TRIGGER_INTERVAL = "message_interval"
TRIGGER_MANUAL = "manual"

# How much conversation a single reflection will look at. Generous — this runs
# rarely, and the whole point is to see more than the agent's live window does.
_MAX_TRANSCRIPT_MESSAGES = 60

_VALID_KINDS = {
    "episode": MemoryKind.episode,
    "preference": MemoryKind.preference,
    "objection": MemoryKind.objection,
    "commitment": MemoryKind.commitment,
    "insight": MemoryKind.insight,
    "self_critique": MemoryKind.self_critique,
}


def _normalise(text: str) -> str:
    """Loose key for near-duplicate detection across reflection runs."""
    return " ".join((text or "").lower().split())[:180]


def _build_transcript(messages: list, after_message_id: Optional[int]) -> tuple[list[dict], Optional[int]]:
    """Turn stored messages into a chat transcript, skipping anything already
    reflected on. Returns the transcript and the id of the last message in it, so
    the next run knows where to resume."""
    fresh = [m for m in messages if after_message_id is None or m.id > after_message_id]
    if not fresh:
        return [], after_message_id

    window = fresh[-_MAX_TRANSCRIPT_MESSAGES:]
    transcript = []
    for m in window:
        if m.role == MessageRole.customer:
            transcript.append({"role": "user", "content": m.content})
        elif m.role == MessageRole.agent:
            transcript.append({"role": "assistant", "content": m.content})
    return transcript, window[-1].id


def reflect_on_lead(
    memory: "MemoryManager",
    lead_id: int,
    trigger: str = TRIGGER_MANUAL,
    performed_by: str = "sales_agent",
) -> dict:
    """Reflect on one lead's conversation and persist what comes back.

    Returns a summary dict for logging and tests. Never raises — a failure here
    leaves the lead exactly as it was.
    """
    result = {"trigger": trigger, "lead_id": lead_id, "memories_written": 0, "skipped": None}
    try:
        ctx = memory.get_lead_context(lead_id)
        lead = ctx.get("lead")
        if not lead:
            result["skipped"] = "lead not found"
            return result

        customer_id = lead.customer_id
        profile = memory.get_customer_profile(customer_id)
        transcript, last_id = _build_transcript(
            ctx.get("messages", []),
            profile.last_reflected_message_id if profile else None,
        )
        if not transcript:
            result["skipped"] = "no new messages since last reflection"
            return result

        collected = dict(lead.collected_fields or {})
        reflection = llm_client.reflect_on_conversation(
            transcript=transcript,
            collected_fields=collected,
            previous_summary=profile.summary if profile else None,
            lead_id=lead_id,
        )
        if not reflection:
            result["skipped"] = "reflection returned nothing"
            return result

        written = _persist(
            memory=memory,
            reflection=reflection,
            customer_id=customer_id,
            lead_id=lead_id,
            trigger=trigger,
            performed_by=performed_by,
            last_message_id=last_id,
        )
        result["memories_written"] = written
        logger.info(
            "Reflection complete",
            extra={"lead_id": lead_id, "customer_id": customer_id,
                   "trigger": trigger, "memories_written": written},
        )
        return result
    except Exception as exc:
        logger.exception("Reflection failed for lead %s (%s)", lead_id, exc)
        result["skipped"] = f"error: {exc}"
        return result


def _persist(
    memory: "MemoryManager",
    reflection: dict,
    customer_id: int,
    lead_id: int,
    trigger: str,
    performed_by: str,
    last_message_id: Optional[int],
) -> int:
    """Write the reflection out: memories first, then the rewritten profile."""
    existing = {_normalise(m.summary) for m in memory.list_customer_memories(customer_id)}
    written = 0

    for item in reflection.get("memories") or []:
        if not isinstance(item, dict):
            continue
        summary = (item.get("summary") or "").strip()
        if not summary:
            continue
        key = _normalise(summary)
        if key in existing:
            continue  # already remembered — reflection runs overlap by design
        existing.add(key)

        kind = _VALID_KINDS.get(str(item.get("kind", "")).lower(), MemoryKind.episode)
        detail = (item.get("detail") or "").strip() or None
        tags = item.get("tags")
        if not isinstance(tags, list) or not tags:
            tags = _keywords(f"{summary} {detail or ''}")

        memory.add_customer_memory(
            customer_id=customer_id,
            kind=kind,
            summary=summary,
            detail=detail,
            tags=tags,
            salience=item.get("salience", 3),
            lead_id=lead_id,
            source=MemorySource.reflection,
            created_by=performed_by,
            **_embedding_for(f"{summary} {detail or ''}"),
        )
        written += 1

    # Self-reflection: keep it as its own retrievable memory when it says something
    # concrete, so "what did we get wrong with this customer" is answerable — but
    # never let a filler answer become a permanent record.
    notes = (reflection.get("agent_notes") or "").strip()
    if notes and _normalise(notes) not in existing and not _is_filler(notes):
        memory.add_customer_memory(
            customer_id=customer_id,
            kind=MemoryKind.self_critique,
            summary=notes[:200],
            detail=notes,
            tags=_keywords(notes),
            salience=3,
            lead_id=lead_id,
            source=MemorySource.reflection,
            created_by=performed_by,
            **_embedding_for(notes),
        )
        written += 1

    memory.upsert_customer_profile(
        customer_id=customer_id,
        summary=reflection.get("summary"),
        traits=reflection.get("traits"),
        open_questions=reflection.get("open_questions"),
        agent_notes=notes or None,
        next_best_action=reflection.get("next_best_action"),
        last_reflected_message_id=last_message_id,
        performed_by=performed_by,
    )
    return written


_FILLER = {
    "nothing to note", "none", "n/a", "no notes", "nothing", "no issues",
    "nothing to report", "no self-critique available",
}


def _is_filler(text: str) -> bool:
    """True for a self-critique that says nothing, which must not become a permanent
    record. The [MOCK] marker is stripped first — mock mode's placeholder critique is
    the most common filler of all, and it would otherwise be stored on every lead."""
    stripped = _normalise(text).rstrip(".")
    if stripped.startswith("[mock]"):
        stripped = stripped[len("[mock]"):].strip()
    if stripped in _FILLER:
        return True
    return any(stripped.startswith(f) for f in _FILLER)


def _embedding_for(text: str) -> dict:
    """Embedding kwargs for a new memory, or empty when semantic search is off.

    Kept as one helper so the flag is checked in exactly one place: with it off,
    nothing here ever calls the embeddings API.
    """
    if not settings.memory_semantic_search:
        return {}
    vector = llm_client.embed(text)
    if not vector:
        return {}
    return {"embedding": vector, "embedding_model": settings.memory_embedding_model}


# ── Proposal outcomes (the Proposal Agent's feedback loop) ─────────────────────
#
# The Sales Agent has to judge its own conversation, which is why its reflection
# needs a model. The Proposal Agent does not: every judgement on its work is a real
# human action with structure behind it — approved, sent back with a written reason,
# repriced from X to Y, accepted. So this half of the loop is DETERMINISTIC. Nothing
# below calls an LLM, nothing below can hallucinate a lesson, and it all works with
# no API key.
#
# Proposal lessons live on customer_memories only. They deliberately do NOT write to
# CustomerProfile.agent_notes — that field is the Sales Agent's self-critique, and
# two agents rewriting one field would silently clobber each other.

OUTCOME_DRAFTED = "drafted"
OUTCOME_APPROVED = "approved"
OUTCOME_SIGNED_OFF = "manager_signed_off"
OUTCOME_REVISION_REQUESTED = "revision_requested"
OUTCOME_PRICING_ADJUSTED = "pricing_adjusted"
OUTCOME_SENT = "sent"
OUTCOME_ACCEPTED = "accepted"

# Every proposal memory carries this tag, so recall can ask for "what happened to my
# drafts" without walking everything ever remembered about the customer.
PROPOSAL_TAG = "proposal"

# How much each outcome matters when ranking. An acceptance is the reference point
# for every future price; an approval is routine.
_OUTCOME_SALIENCE = {
    OUTCOME_ACCEPTED: 5,
    OUTCOME_REVISION_REQUESTED: 5,
    OUTCOME_PRICING_ADJUSTED: 5,
    OUTCOME_SENT: 3,
    OUTCOME_SIGNED_OFF: 3,
    OUTCOME_APPROVED: 2,
    OUTCOME_DRAFTED: 2,
}


def _money(value: Optional[float]) -> str:
    return f"INR {value:,.0f}" if value else "no value set"


def record_proposal_outcome(
    memory: "MemoryManager",
    proposal_id: int,
    event: str,
    performed_by: str,
    reason: Optional[str] = None,
    previous_value: Optional[float] = None,
) -> list:
    """Write down what just happened to a proposal, and the lesson if there is one.

    Returns the memories written. Never raises — an outcome that fails to record
    must not roll back the approval or the send that triggered it.
    """
    written = []
    try:
        proposal = memory.get_proposal(proposal_id)
        if not proposal:
            return written
        lead = memory.get_lead(proposal.lead_id)
        if not lead:
            return written

        customer_id = lead.customer_id
        version = proposal.version
        value = proposal.total_value
        tags = [PROPOSAL_TAG, event, f"v{version}"]

        summary = _outcome_summary(event, version, value, reason, previous_value)
        written.append(memory.add_customer_memory(
            customer_id=customer_id,
            kind=MemoryKind.outcome,
            summary=summary,
            detail=reason,
            tags=tags,
            salience=_OUTCOME_SALIENCE.get(event, 3),
            lead_id=lead.id,
            source=MemorySource.system,
            created_by=performed_by,
        ))

        # A rejection or a reprice is the agent being told it got something wrong.
        # That is worth keeping separately from the event itself, because it is what
        # the next draft should actually read.
        lesson = _lesson_from(event, version, value, reason, previous_value)
        if lesson:
            written.append(memory.add_customer_memory(
                customer_id=customer_id,
                kind=MemoryKind.self_critique,
                summary=lesson,
                detail=reason,
                tags=tags + ["lesson"],
                salience=5,
                lead_id=lead.id,
                source=MemorySource.system,
                created_by="proposal_agent",
            ))

        logger.info(
            "Proposal outcome recorded",
            extra={"proposal_id": proposal_id, "event": event,
                   "customer_id": customer_id, "memories_written": len(written)},
        )
        return written
    except Exception:
        logger.exception("Failed to record proposal outcome %s for %s", event, proposal_id)
        return written


def _outcome_summary(
    event: str,
    version: int,
    value: Optional[float],
    reason: Optional[str],
    previous_value: Optional[float],
) -> str:
    if event == OUTCOME_PRICING_ADJUSTED:
        delta = ""
        if previous_value:
            change = (value or 0) - previous_value
            pct = (change / previous_value) * 100 if previous_value else 0
            direction = "down" if change < 0 else "up"
            delta = f" ({direction} {abs(pct):.0f}% from {_money(previous_value)})"
        return f"Proposal v{version} repriced to {_money(value)}{delta}"
    if event == OUTCOME_REVISION_REQUESTED:
        return f"Proposal v{version} ({_money(value)}) was sent back for revision"
    if event == OUTCOME_ACCEPTED:
        return f"Customer accepted proposal v{version} at {_money(value)}"
    if event == OUTCOME_SENT:
        return f"Proposal v{version} ({_money(value)}) was sent to the customer"
    if event == OUTCOME_SIGNED_OFF:
        return f"Proposal v{version} ({_money(value)}) cleared manager sign-off"
    if event == OUTCOME_APPROVED:
        return f"Proposal v{version} ({_money(value)}) was approved"
    return f"Proposal v{version} ({_money(value)}) — {event}"


def _lesson_from(
    event: str,
    version: int,
    value: Optional[float],
    reason: Optional[str],
    previous_value: Optional[float],
) -> Optional[str]:
    """The takeaway for the next draft, or None when the outcome taught nothing.

    An approval teaches nothing — the draft was fine. A rejection or a reprice
    teaches something specific, and the human already wrote down what.
    """
    if event == OUTCOME_REVISION_REQUESTED and reason:
        return f"v{version} at {_money(value)} was rejected internally: {reason}"
    if event == OUTCOME_PRICING_ADJUSTED and previous_value:
        change = (value or 0) - previous_value
        if change == 0:
            return None
        direction = "too high" if change < 0 else "too low"
        note = f"v{version} was drafted at {_money(previous_value)} and moved to "
        note += f"{_money(value)} — the tiered rate came out {direction} here"
        return f"{note}: {reason}" if reason else note
    return None


# ── Milestone triggers ─────────────────────────────────────────────────────────

def should_reflect_on_interval(memory: "MemoryManager", lead_id: int) -> bool:
    """True when a long conversation has gone far enough since the last reflection
    to be worth another one. Stops a lead that never qualifies from never being
    remembered at all."""
    every = settings.memory_reflect_every_n_messages
    if every <= 0:
        return False
    ctx = memory.get_lead_context(lead_id)
    lead = ctx.get("lead")
    if not lead:
        return False
    profile = memory.get_customer_profile(lead.customer_id)
    last_id = profile.last_reflected_message_id if profile else None
    new_customer_messages = sum(
        1 for m in ctx.get("messages", [])
        if m.role == MessageRole.customer and (last_id is None or m.id > last_id)
    )
    return new_customer_messages >= every


def _reflect_with_own_session(lead_id: int, trigger: str, performed_by: str) -> None:
    """Run a reflection on a fresh session — used by the background thread, which
    must never touch the caller's session."""
    from app.memory.db import SessionLocal
    from app.memory.memory_manager import MemoryManager

    db = SessionLocal()
    try:
        reflect_on_lead(MemoryManager(db), lead_id, trigger=trigger, performed_by=performed_by)
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Background reflection failed for lead %s", lead_id)
    finally:
        db.close()


def start_reflection(lead_id: int, trigger: str, performed_by: str = "sales_agent") -> None:
    """Fire a reflection without making the customer wait for it.

    This is the seam tests replace with a synchronous call — the same arrangement
    the PDF generator uses, and for the same reason: the test harness shares one
    SQLite connection across sessions, which is not safe to use from two threads.
    Runs on the background runner (app/jobs/runner.py), not a bare daemon thread.
    """
    from app.jobs import runner

    runner.submit(lambda: _reflect_with_own_session(lead_id, trigger, performed_by), f"reflection lead {lead_id}")
