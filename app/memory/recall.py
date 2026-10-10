"""
Recall — finding the right memories to answer a question about a customer.

TWO RETRIEVAL PATHS, ONE INTERFACE
Keyword is the default and needs nothing: it scores a memory on term overlap,
how important the memory was judged to be, and how recent it is. Semantic search
is the same pipeline with a cosine-similarity term blended in, and it only
activates when MEMORY_SEMANTIC_SEARCH is on AND the memories in question were
actually embedded when they were written. A memory saved before the flag was
turned on simply scores on keywords — the two paths coexist rather than one
replacing the other, so switching the flag never strands existing memory.

WHAT GETS RETRIEVED
Not only memories. A question like "what capacity did they want" is answered from
the structured fields on the lead, and "have we sent them anything" from the
proposal rows. Recall assembles all of it into one context block, with a citation
marker on every line, so the answer can point at where each claim came from.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

from app.config import settings
from app.llm.client import _keywords, llm_client
from app.memory.models import MemoryKind

if TYPE_CHECKING:
    from app.memory.memory_manager import MemoryManager

logger = logging.getLogger(__name__)

# A memory's score decays with age, halving roughly every 120 days, but never to
# zero — an 18-month-old objection is still the answer to a question about it.
_HALF_LIFE_DAYS = 120.0
_RECENCY_FLOOR = 0.35

# Kinds that are the assistant's own opinion rather than something the customer
# said. Recall keeps them, but marks them so an answer can attribute them properly.
_OPINION_KINDS = {MemoryKind.insight, MemoryKind.self_critique}


def _age_days(when: Optional[datetime], now: datetime) -> float:
    if when is None:
        return 0.0
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (now - when).total_seconds() / 86400.0)


def _recency_weight(when: Optional[datetime], now: datetime) -> float:
    decay = 0.5 ** (_age_days(when, now) / _HALF_LIFE_DAYS)
    return _RECENCY_FLOOR + (1.0 - _RECENCY_FLOOR) * decay


def _cosine(a: list, b: list) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _keyword_score(terms: list, memory) -> float:
    """Term overlap across summary, detail and tags. Tags and the summary count for
    more than the body, because they are what the memory is actually *about*."""
    if not terms:
        return 0.0
    summary = (memory.summary or "").lower()
    detail = (memory.detail or "").lower()
    tags = {str(t).lower() for t in (memory.tags or [])}

    score = 0.0
    for term in terms:
        if term in tags:
            score += 3.0
        if term in summary:
            score += 2.5
        if term in detail:
            score += 1.0
    return score / len(terms)


def rank_memories(
    memories: list,
    question: str,
    top_k: int,
    question_vector: Optional[list] = None,
    now: Optional[datetime] = None,
) -> list:
    """Score and order memories for one question.

    Returns the top_k memories. When the question has no usable search terms (an
    open request like "tell me about them"), everything is ranked on salience and
    recency alone, which is the right behaviour — the caller wants a briefing.
    """
    now = now or datetime.now(timezone.utc)
    terms = _keywords(question)
    scored = []

    for memory in memories:
        keyword = _keyword_score(terms, memory)
        semantic = 0.0
        if question_vector and memory.embedding:
            semantic = _cosine(question_vector, list(memory.embedding))

        # Salience and recency shape every ranking; relevance dominates when the
        # question actually carries search terms.
        base = (memory.salience or 3) / 5.0
        relevance = max(keyword, semantic * 2.0)
        score = (relevance * 2.0 + base) * _recency_weight(memory.occurred_at, now)
        scored.append((score, memory.id, memory))

    scored.sort(key=lambda row: (-row[0], -row[1]))
    return [memory for _, _, memory in scored[:top_k]]


def embed_question(question: str) -> Optional[list]:
    """Embed the question for the semantic path, or None to stay on keywords."""
    if not settings.memory_semantic_search:
        return None
    return llm_client.embed(question)


def resolve_customer(
    memory: "MemoryManager",
    question: str,
    customer_id: Optional[int] = None,
    visible=None,
) -> tuple:
    """Work out which customer a question is about.

    An explicit customer_id always wins. Otherwise the question is searched for a
    name, email or company. Returns (customer, candidates) — when candidates holds
    more than one, the caller must ask which one rather than guessing, because
    answering about the wrong customer is worse than not answering.

    `visible(customer) -> bool`, when given, drops every match the asker may not see
    BEFORE anything is returned — so an ambiguous name never lists, and never
    resolves to, another rep's customer.
    """
    if customer_id is not None:
        customer = memory.get_customer_dossier(customer_id).get("customer")
        return customer, ([customer] if customer else [])

    # Try the longest plausible fragments first: a full quoted name beats a
    # single first name, which beats a bare word that happens to match.
    fragments = _identity_fragments(question)
    for fragment in fragments:
        matches = memory.find_customers(fragment)
        if visible is not None:
            seen = [m for m in matches if visible(m)]
            if matches and not seen:
                # The name matched only customers the asker may not see. Stop here —
                # falling through to a vaguer fragment ("Priya" after "Priya Sharma")
                # would answer about somebody else. Same reply as no match at all.
                return None, []
            matches = seen
        if len(matches) == 1:
            return matches[0], matches
        if len(matches) > 1:
            return None, matches
    return None, []


# Words that start a question rather than name anybody. Without this list, "What"
# and "Tell" get treated as customer names on every other query.
_QUESTION_OPENERS = {
    "what", "when", "where", "who", "whose", "why", "how", "is", "are", "was",
    "were", "do", "does", "did", "can", "could", "should", "would", "will",
    "tell", "give", "show", "remind", "find", "look", "check", "get", "list",
    "any", "anything", "the", "a", "an", "i", "we", "our", "us", "me", "my",
    "he", "she", "they", "them", "there", "this", "that", "and", "but", "for",
    "about", "on", "in", "at", "to", "of", "with", "from", "brief", "summarise",
    "summarize", "catch", "remember", "recall", "know", "anyone", "someone",
    "customer", "client", "lead", "please", "hey", "hi",
}


def _strip_possessive(text: str) -> str:
    """Priya Sharma's -> Priya Sharma. A stored name never carries the possessive,
    so leaving it on turns every "what is X's budget" into a failed lookup."""
    for suffix in ("'s", "’s", "s'", "s’"):
        if text.endswith(suffix):
            return text[: -len(suffix)] + ("s" if suffix.startswith("s") else "")
    return text


def _identity_fragments(question: str) -> list:
    """Candidate customer identifiers pulled out of a question, most specific first.

    Order matters: an email is unambiguous, a full quoted or capitalised name is
    next, and bare single words come last — that ordering is what makes "Priya
    Sharma" win over the "Priya" that also appears inside it.
    """
    import re

    fragments = []
    # An email identifies a customer outright.
    fragments += re.findall(r"[\w.+-]+@[\w-]+\.[\w.-]+", question)
    # Anything the asker quoted, they meant literally.
    fragments += [f for f in re.findall(r"[\"']([^\"']{2,60})[\"']", question)]

    # Runs of capitalised words that aren't just question grammar.
    tokens = re.findall(r"[A-Za-z][A-Za-z&.'’-]*", question)
    runs, current = [], []
    for token in tokens:
        bare = _strip_possessive(token).lower()
        if token[0].isupper() and bare not in _QUESTION_OPENERS:
            current.append(_strip_possessive(token))
        elif current:
            runs.append(" ".join(current))
            current = []
    if current:
        runs.append(" ".join(current))

    fragments += sorted(runs, key=len, reverse=True)
    # Last resort: each word on its own, so a surname-only or first-name-only
    # question still finds someone.
    for run in runs:
        fragments += [w for w in run.split() if len(w) > 2]

    seen, out = set(), []
    for f in fragments:
        f = f.strip().strip(".,?!")
        if len(f) >= 2 and f.lower() not in seen:
            seen.add(f.lower())
            out.append(f)
    return out


def build_context_block(dossier: dict, memories: list) -> str:
    """Assemble the retrieved records into the text the answering model sees.

    Every line carries a marker so the answer can cite it, and opinion is labelled
    as opinion. Nothing is invented here — this is pure formatting of stored rows.
    """
    customer = dossier.get("customer")
    profile = dossier.get("profile")
    leads = dossier.get("leads") or []
    proposals = dossier.get("proposals") or []
    meetings = dossier.get("meetings") or []
    lines = []

    if customer:
        lines.append("[C1] IDENTITY")
        lines.append(f"  name: {customer.name or 'not recorded'}")
        lines.append(f"  email: {customer.email}")
        lines.append(f"  company: {customer.company or 'not recorded'}")
        lines.append(f"  phone: {customer.phone or 'not recorded'}")
        lines.append(f"  first seen: {_fmt(customer.created_at)}")

    for index, lead in enumerate(leads, start=1):
        fields = dict(lead.collected_fields or {})
        lines.append(
            f"[L{index}] LEAD {lead.id} — stage {lead.pipeline_stage.value}, "
            f"source {lead.source.value}, opened {_fmt(lead.created_at)}"
        )
        if fields:
            for key, value in fields.items():
                lines.append(f"  {key}: {value}")
        else:
            lines.append("  no qualification fields collected")

    if profile:
        lines.append("[P1] ASSISTANT'S PROFILE (the assistant's own summary, not the customer's words)")
        if profile.summary:
            lines.append(f"  summary: {profile.summary}")
        if profile.traits:
            lines.append(f"  traits: {', '.join(str(t) for t in profile.traits)}")
        if profile.open_questions:
            lines.append(f"  still unknown: {'; '.join(str(q) for q in profile.open_questions)}")
        if profile.next_best_action:
            lines.append(f"  suggested next step: {profile.next_best_action}")
        if profile.agent_notes:
            lines.append(f"  assistant's self-critique: {profile.agent_notes}")
        lines.append(f"  last reflected: {_fmt(profile.last_reflected_at)} "
                     f"({profile.reflection_count} reflections)")

    if memories:
        lines.append("[M] MEMORIES (most relevant first)")
        for memory in memories:
            opinion = " — ASSISTANT'S OPINION" if memory.kind in _OPINION_KINDS else ""
            lines.append(
                f"  [M{memory.id}] ({memory.kind.value}, salience {memory.salience}, "
                f"{_fmt(memory.occurred_at)}){opinion}: {memory.summary}"
            )
            if memory.detail and memory.detail.strip() != memory.summary.strip():
                lines.append(f"      detail: {memory.detail}")

    for index, proposal in enumerate(proposals, start=1):
        value = f"INR {proposal.total_value:,.0f}" if proposal.total_value else "no value set"
        lines.append(
            f"[Q{index}] PROPOSAL v{proposal.version} — {proposal.status.value}, {value}, "
            f"created {_fmt(proposal.date_created)}, sent {_fmt(proposal.date_sent)}"
        )

    for index, meeting in enumerate(meetings, start=1):
        lines.append(
            f"[T{index}] MEETING — {meeting.status.value} at {_fmt(meeting.scheduled_at)} "
            f"with {meeting.employee_id}"
        )

    return "\n".join(lines)


def _fmt(when: Optional[datetime]) -> str:
    if when is None:
        return "never"
    return when.strftime("%Y-%m-%d %H:%M")


def retrieve(
    memory: "MemoryManager",
    customer_id: int,
    question: str,
    top_k: Optional[int] = None,
) -> dict:
    """Pull everything relevant about one customer for one question.

    Returns the dossier, the ranked memories, the formatted context block, and
    which retrieval path actually ran — the caller surfaces that last one so an
    employee can tell keyword recall from semantic recall.
    """
    top_k = top_k or settings.memory_recall_top_k
    dossier = memory.get_customer_dossier(customer_id)
    if not dossier:
        return {"dossier": {}, "memories": [], "context_block": "", "mode": "none"}

    all_memories = dossier.get("memories") or []
    question_vector = embed_question(question)
    mode = "semantic" if question_vector else "keyword"
    if question_vector and not any(m.embedding for m in all_memories):
        # The flag is on but nothing here was embedded — say keyword, because that
        # is what actually decided the ranking.
        mode = "keyword"

    ranked = rank_memories(all_memories, question, top_k, question_vector=question_vector)
    return {
        "dossier": dossier,
        "memories": ranked,
        "context_block": build_context_block(dossier, ranked),
        "mode": mode,
    }
