"""
Sales Agent — qualifies inbound leads via conversational field extraction.

ARCHITECTURAL INVARIANT: This module must never import from agents.proposal_agent.
All cross-agent data flows through the Orchestrator and MemoryManager only.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

from app.agents import guardrails, need
from app.governance import policy, registry
from app.llm.client import llm_client
from app.llm.company_facts import COMPANY_FACTS
from app.memory.models import (
    Channel, LeadScore, MemoryKind, MessageRole, PipelineStage, TimelineEventType,
)
from app.rules.business_rules import REQUIRED_FIELDS, check_missing_fields, evaluate_lead

if TYPE_CHECKING:
    from app.memory.memory_manager import MemoryManager

logger = logging.getLogger(__name__)

QUALIFICATION_FIELDS = [
    "name", "email", "phone", "company", "project_type", "segment", "location",
    "capacity_kw", "timeline", "budget", "monthly_electricity_bill", "decision_maker",
]

# Situational fields describe the project itself (not who the person is). We learn
# these BEFORE asking for contact details — that ordering is the whole point of the
# staged redesign.
SITUATIONAL_FIELDS = ["project_type", "location", "capacity_kw", "timeline"]

# How many remembered items to put in front of the reply generator. Small on purpose:
# the point is to shape a reply, not to hand the model a dossier to recite.
_MEMORY_LINES_IN_PROMPT = 5


def determine_conversation_stage(collected: dict) -> str:
    """Deterministically pick the conversation stage from what's collected so far.

    The stage is a pure function of collected_fields — the LLM never decides it; it
    only shapes the wording within a stage.
    """
    has_situational = any(collected.get(f) for f in SITUATIONAL_FIELDS)
    has_contact = bool(collected.get("name") and collected.get("email"))
    missing = check_missing_fields(collected)

    if not missing:
        return "meeting_offer"
    if not has_situational:
        return "opening"
    if not has_contact:
        return "contact_bridge"
    return "understanding"  # situational + contact both partial, still filling gaps


def _score_from_fields(collected: dict) -> LeadScore:
    scored = ["capacity_kw", "budget", "timeline", "monthly_electricity_bill"]
    present = sum(1 for f in scored if collected.get(f))
    if present == len(scored):
        return LeadScore.high
    if present >= 2:
        return LeadScore.medium
    return LeadScore.low


def _build_history(messages: list) -> list[dict]:
    history = []
    for m in messages[-12:]:
        if m.role == MessageRole.customer:
            history.append({"role": "user", "content": m.content})
        elif m.role in (MessageRole.agent,):
            history.append({"role": "assistant", "content": m.content})
    return history


def _justifiable_text(
    message: str,
    history: list[dict],
    collected: dict,
    available_slots: Optional[list[str]],
) -> str:
    """Everything a reply is entitled to assert, as one blob for the guardrails.

    Only the CUSTOMER's own words count, never Nova's earlier replies — otherwise a
    figure the model invented once would justify itself forever afterwards. The one
    exception is COMPANY_FACTS: those figures (e.g. "4,800+ street lamps") are
    comma-grouped numbers a verified fact, not a conversational claim, and
    app/agents/guardrails.py's money check cannot otherwise tell one apart from an
    invented price — so they are always justified, not conditioned on anything the
    customer said.
    """
    parts = [message, COMPANY_FACTS]
    parts += [turn.get("content", "") for turn in history if turn.get("role") == "user"]
    parts += [f"{k} {v}" for k, v in (collected or {}).items() if v is not None]
    parts += list(available_slots or [])
    return "\n".join(str(p) for p in parts)


class SalesAgent:
    """
    Qualifies a customer lead through conversational field collection.
    Every call:
      1. Logs the inbound customer message
      2. Extracts fields via LLM (with fallback)
      3. Merges into lead.collected_fields
      4. Generates a reply
      5. Logs the agent reply
      6. Returns the reply text and updated state
    """

    def handle_message(
        self,
        memory: "MemoryManager",
        customer_id: int,
        lead_id: int,
        message: str,
        channel: Channel = Channel.website,
    ) -> dict:
        # Governance runs in the harness now (app/agents/harness.py), which calls
        # stand_down below when it refuses. Sub-actions taken part-way through a turn
        # — booking a meeting — are still authorised individually further down.

        # Log the inbound customer message
        memory.log_message(
            channel=channel,
            role=MessageRole.customer,
            content=message,
            lead_id=lead_id,
            customer_id=customer_id,
        )
        # Stamp customer activity so stale-lead detection resets on every reply.
        memory.record_customer_message(lead_id)

        # Detection only — the message is answered normally either way. Refusing to
        # reply would punish a real customer for phrasing something oddly, and the
        # output guardrails below are what actually contain an attempt that lands.
        injection = guardrails.detect_injection(message)
        if injection:
            logger.warning(
                "Possible prompt-injection attempt in customer message",
                extra={"lead_id": lead_id, "customer_id": customer_id, "matched": injection},
            )
            from app.notifications.notifier import notifier

            notifier.notify("prompt_injection_suspected", {
                "lead_id": lead_id, "customer_id": customer_id, "matched": injection,
            })

        # Retrieve existing context
        ctx = memory.get_lead_context(lead_id)
        lead = ctx["lead"]
        existing_fields = dict(lead.collected_fields or {})
        # Captured before any update so a milestone fires on the TRANSITION into a
        # stage, not on every turn spent sitting in it.
        previous_stage = lead.pipeline_stage
        history = _build_history(ctx["messages"])

        # Field extraction — LLM does data work only, never makes decisions
        # Prompt-injection safety: the system prompt instructs the model to ignore
        # any instructions embedded in customer_message, treating it as data only.
        if need.needs_field_extraction(message, existing_fields):
            extracted = llm_client.extract_fields(
                user_message=message,
                conversation_history=history,
                lead_id=lead_id,
            )
        else:
            # "ok thanks" carries nothing to extract; asking a model to confirm that
            # is money spent to learn nothing.
            need.skipped("extract_fields", "message is a bare acknowledgement", lead_id)
            extracted = {}

        # Merge extracted fields (never overwrite with None/empty)
        new_fields = {k: v for k, v in extracted.items() if k in QUALIFICATION_FIELDS and v}

        # Score update
        merged = {**existing_fields, **new_fields}
        score = _score_from_fields(merged)
        missing = check_missing_fields(merged)
        eval_result = evaluate_lead(merged)

        # Determine new stage
        stage = None
        if eval_result.is_ready_for_proposal:
            stage = PipelineStage.qualified
        elif merged:
            stage = PipelineStage.qualifying

        memory.update_lead_fields(
            lead_id=lead_id,
            new_fields=new_fields,
            performed_by="sales_agent",
            stage=stage,
            score=score,
        )

        # Fill in the customer record in place once we learn who they are. If the
        # extracted email turns out to belong to a different existing customer, this
        # merges into that record (and returns it), so we adopt its id going forward.
        if new_fields.get("name") or new_fields.get("email"):
            surviving = memory.update_customer_details(
                customer_id=customer_id,
                name=new_fields.get("name"),
                email=new_fields.get("email"),
                performed_by="sales_agent",
            )
            if surviving is not None:
                customer_id = surviving.id

        if stage == PipelineStage.qualified:
            memory.add_timeline_event(
                lead_id=lead_id,
                event_type=TimelineEventType.qualification_completed,
                description="All required fields collected via Sales Agent",
                created_by="sales_agent",
            )

        # Deterministic conversation stage — decides HOW Nova talks, never the LLM.
        conv_stage = determine_conversation_stage(merged)

        # What we remember about this person from earlier conversations, including
        # the agent's own note on what it handled badly last time. Loaded AFTER the
        # identity step above, because a mid-chat email match can move us onto a
        # different customer record — and their memory, not the placeholder's, is
        # the one that matters.
        memory_context = self._recall_context(memory, customer_id, message)

        # Meeting scheduling — detect intent / confirmation, offer real slots (also
        # proactively once fully qualified), and auto-book once the customer confirms a
        # slot that was actually offered.
        scheduling = self._handle_scheduling(
            memory=memory,
            lead_id=lead_id,
            customer_id=customer_id,
            message=message,
            history=history,
            conv_stage=conv_stage,
        )

        # Reply selection:
        # - A completed booking is a factual confirmation → deterministic, safe text.
        # - Everything else is a warm, stage-aware LLM reply. Real available slots (if
        #   any) are handed to the generator so it phrases them without inventing times.
        if scheduling.get("reply_text"):
            # Code-generated from a real calendar booking. There is no model output
            # here to guard, and the time it names is one the calendar just accepted.
            reply = scheduling["reply_text"]
        else:
            available_human = scheduling.get("offered_slots_human") or None
            generated = llm_client.generate_reply(
                user_message=message,
                stage=conv_stage,
                collected_fields=merged,
                missing_fields=missing,
                conversation_history=history,
                available_slots=available_human,
                memory_context=memory_context,
                lead_id=lead_id,
            )

            # Everything the reply is allowed to assert: what the customer actually
            # said, what is on file, and slots a real free/busy query returned. A
            # figure or a time outside this set was invented by the model.
            checked = guardrails.enforce(
                generated,
                allowed_text=_justifiable_text(message, history, merged, available_human),
                memory_context=memory_context,
                user_message=message,
                fallback=llm_client._fallback_reply(conv_stage, missing, available_human),
            )
            reply = checked.reply
            if checked.violations:
                memory.record_guardrail_block(
                    lead_id=lead_id,
                    violations=checked.violations,
                    original_reply=generated,
                    sent_reply=reply,
                    blocked=checked.blocked,
                )
                if checked.blocked:
                    from app.notifications.notifier import notifier

                    notifier.notify("agent_reply_blocked", {
                        "lead_id": lead_id,
                        "codes": checked.codes,
                        "stage": conv_stage,
                    })

        # Log agent reply
        memory.log_message(
            channel=channel,
            role=MessageRole.agent,
            content=reply,
            lead_id=lead_id,
            customer_id=customer_id,
            agent_name="Nova",
        )

        # Milestone reflection. The agent only DECIDES that a milestone was reached —
        # the orchestrator node fires it after committing, because a reflection
        # running before the commit would read a conversation missing this very turn
        # (and, on a single-writer backend, contend with the open write transaction).
        reflection_trigger = self._reflection_trigger(
            memory=memory,
            lead_id=lead_id,
            previous_stage=previous_stage,
            new_stage=stage,
            meeting_booked=scheduling.get("meeting_booked"),
        )

        return {
            "reply_text": reply,
            "reflection_trigger": reflection_trigger,
            "collected_fields": merged,
            "missing_fields": missing,
            "lead_score": score.value,
            "is_qualified": eval_result.is_ready_for_proposal,
            "lead_id": lead_id,
            "customer_id": customer_id,
            "meeting_intent": scheduling.get("intent", False),
            "offered_slots": scheduling.get("offered_slots", []),
            "meeting_booked": scheduling.get("meeting_booked"),
        }

    @staticmethod
    def stand_down(
        memory: "MemoryManager",
        decision,
        customer_id: int,
        lead_id: int,
        message: str,
        channel: Channel,
    ) -> dict:
        """What the agent does when governance says it may not act.

        The customer gets a plain handoff rather than silence or an error, the team
        is notified so someone actually picks it up, and the lead is left untouched:
        no extraction, no scoring, no stage change. An agent that has been stood down
        should not still be quietly writing to the record.
        """
        memory.log_message(
            channel=channel, role=MessageRole.customer, content=message,
            lead_id=lead_id, customer_id=customer_id,
        )
        memory.record_customer_message(lead_id)

        reply = (
            "Thanks for that — let me bring someone from our team in to pick this up "
            "with you properly. They'll be in touch shortly."
        )
        memory.log_message(
            channel=channel, role=MessageRole.agent, content=reply,
            lead_id=lead_id, customer_id=customer_id, agent_name="Nova",
        )
        from app.notifications.notifier import notifier

        notifier.notify("agent_stood_down", {
            "lead_id": lead_id, "customer_id": customer_id,
            "code": decision.code, "reason": decision.reason,
        })

        lead = memory.get_lead(lead_id)
        collected = dict(lead.collected_fields or {}) if lead else {}
        return {
            "reply_text": reply,
            "collected_fields": collected,
            "missing_fields": check_missing_fields(collected),
            "lead_score": lead.lead_score.value if lead and lead.lead_score else None,
            "is_qualified": False,
            "lead_id": lead_id,
            "customer_id": customer_id,
            "meeting_intent": False,
            "offered_slots": [],
            "meeting_booked": None,
            "governance": {"allowed": False, "code": decision.code,
                           "reason": decision.reason},
        }

    @staticmethod
    def _recall_context(
        memory: "MemoryManager", customer_id: int, message: str
    ) -> Optional[str]:
        """What the agent remembers about this customer, shaped for the reply prompt.

        This is the closing half of the reflection loop: reflection writes memory
        down at a milestone, and this reads it back on every later turn so the agent
        actually behaves differently instead of just having a record.

        Retrieval is ranked against what the customer just said, so a question about
        shading surfaces the shading objection rather than a generic briefing. Recall
        is a plain tool, imported here in the method body — the Sales Agent still
        never calls another agent.

        Returns None for a customer with nothing remembered, which is the common
        case on a first conversation and costs nothing.
        """
        from app.memory import recall

        try:
            profile = memory.get_customer_profile(customer_id)
            memories = memory.list_customer_memories(customer_id)
            if not profile and not memories:
                return None

            lines = []
            if profile:
                if profile.summary:
                    lines.append(f"Who they are: {profile.summary}")
                if profile.traits:
                    lines.append(f"How they operate: {', '.join(str(t) for t in profile.traits)}")
                if profile.open_questions:
                    lines.append(
                        "Still unknown: " + "; ".join(str(q) for q in profile.open_questions)
                    )
                if profile.next_best_action:
                    lines.append(f"Suggested next step: {profile.next_best_action}")
                if profile.agent_notes:
                    # The self-critique, stated as a directive — a note filed away and
                    # never read back would leave the agent repeating the same mistake.
                    lines.append(f"What YOU should do differently: {profile.agent_notes}")

            top = recall.rank_memories(
                memories, message, top_k=_MEMORY_LINES_IN_PROMPT
            )
            profile_notes = (profile.agent_notes or "") if profile else ""
            for m in top:
                if m.kind == MemoryKind.self_critique and m.summary in profile_notes:
                    # Already surfaced above as a directive. Only skip the ones the
                    # profile actually carries — the Proposal Agent writes lessons as
                    # self_critique memories and never touches the profile, so a blanket
                    # skip would hide "the customer rejected our price" from the very
                    # conversation where it matters most.
                    continue
                when = m.occurred_at.strftime("%d %b %Y") if m.occurred_at else "undated"
                lines.append(f"- ({m.kind.value}, {when}) {m.summary}")

            return "\n".join(lines) if lines else None
        except Exception:
            logger.exception("Memory recall failed for customer %s — replying without it", customer_id)
            return None

    @staticmethod
    def _reflection_trigger(
        memory: "MemoryManager",
        lead_id: int,
        previous_stage,
        new_stage,
        meeting_booked: Optional[dict],
    ) -> Optional[str]:
        """Which milestone (if any) this turn just crossed.

        Ordered by how much a reflection is worth at each: a booked meeting is the
        richest moment to look back on, qualification next, and the message-interval
        check last — that one exists only so a long conversation that never qualifies
        still gets remembered.
        """
        from app.memory import reflection

        if meeting_booked:
            return reflection.TRIGGER_MEETING_BOOKED
        if new_stage == PipelineStage.qualified and previous_stage != PipelineStage.qualified:
            return reflection.TRIGGER_QUALIFIED
        try:
            if reflection.should_reflect_on_interval(memory, lead_id):
                return reflection.TRIGGER_INTERVAL
        except Exception:
            logger.exception("Reflection interval check failed for lead %s", lead_id)
        return None

    def _handle_scheduling(
        self,
        memory: "MemoryManager",
        lead_id: int,
        customer_id: int,
        message: str,
        history: list[dict],
        conv_stage: str,
    ) -> dict:
        """Detect meeting intent / confirmation and act on it.

        Scheduling modules are plain tools imported here inside the method body — the
        Sales Agent never calls another agent. Returns `intent`, `offered_slots` (real
        ISO times, also human-formatted in `offered_slots_human` for the reply
        generator), and `meeting_booked`. Only a completed booking carries a
        deterministic `reply_text` (a factual confirmation); slot offers are phrased by
        the warm reply generator. Any scheduling failure degrades gracefully to normal
        conversation (logged internally, never surfaced to the customer as a raw error).
        """
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from app.config import settings
        from app.scheduling import availability, booking
        from app.scheduling.google_calendar import GoogleCalendarNotConfiguredError

        result: dict = {
            "intent": False, "offered_slots": [], "offered_slots_human": [], "meeting_booked": None,
        }

        lead = memory.get_lead(lead_id)
        pending = lead.pending_meeting_slots if lead else None
        offered_iso = list(pending.get("slots", [])) if isinstance(pending, dict) else []

        # The expensive part of this method used to run on every single turn and have
        # its answer thrown away moments later. It is only worth asking when the
        # scheduling code below could actually act on the reply.
        if need.needs_meeting_signal(message, conv_stage, pending):
            signal = llm_client.extract_meeting_signal(
                user_message=message,
                conversation_history=history,
                offered_slots_iso=offered_iso,
                lead_id=lead_id,
            )
        else:
            need.skipped("extract_meeting_signal", "nothing pending and no meeting talk", lead_id)
            signal = {"intent": False, "confirmed_slot_iso": None}

        result["intent"] = signal.get("intent", False)
        confirmed_iso = signal.get("confirmed_slot_iso")

        # Offer slots when the customer asks, OR proactively once fully qualified
        # (the meeting_offer stage). No trigger → nothing to do here.
        should_offer = result["intent"] or conv_stage == "meeting_offer"
        if not should_offer and not confirmed_iso:
            return result

        duration = (
            pending.get("duration_minutes")
            if isinstance(pending, dict) and pending.get("duration_minutes")
            else settings.scheduling_default_duration_minutes
        )

        # 1) Customer confirmed a real, previously-offered slot → auto-book it.
        if confirmed_iso and confirmed_iso in offered_iso:
            # Booking puts a real event in a real employee's calendar, so it is
            # authorised on its own rather than riding on the reply's permission.
            if not policy.authorize(
                registry.SALES_AGENT.id, registry.BOOK_MEETING, memory=memory, lead_id=lead_id
            ).allowed:
                logger.warning("Booking not permitted for lead %s; offering slots only", lead_id)
                return result
            try:
                slot_dt = datetime.fromisoformat(confirmed_iso)
                employee_id = availability.is_slot_free(memory, slot_dt, duration)
                if employee_id:
                    meeting = booking.book_meeting(
                        memory=memory,
                        lead_id=lead_id,
                        employee_id=employee_id,
                        scheduled_at=slot_dt,
                        duration_minutes=duration,
                        performed_by="sales_agent",
                    )
                    memory.set_pending_meeting_slots(lead_id, None)
                    result["meeting_booked"] = {
                        "meeting_id": meeting.id,
                        "scheduled_at": meeting.scheduled_at.isoformat(),
                        "meet_link": meeting.meet_link,
                    }
                    result["reply_text"] = (
                        f"You're booked for {self._format_slot(slot_dt, settings)}. "
                        "A calendar invite with the Google Meet link is on its way to your inbox."
                    )
                    return result
                # Nobody free anymore — fall through to re-offer.
            except (booking.SlotUnavailableError, GoogleCalendarNotConfiguredError,
                    availability.NoConnectedCalendarsError) as exc:
                logger.warning("Auto-book failed, will re-offer: %s", exc)
            except Exception:
                logger.exception("Unexpected error auto-booking meeting for lead %s", lead_id)

        # 2) Offer real open slots — the warm reply generator phrases them.
        try:
            slots = availability.get_available_slots(memory, duration_minutes=duration)
        except availability.NoConnectedCalendarsError:
            logger.warning("Meeting requested for lead %s but no calendars are connected", lead_id)
            return result
        except GoogleCalendarNotConfiguredError:
            logger.warning("Meeting requested for lead %s but Google Calendar is not configured", lead_id)
            return result
        except Exception:
            logger.exception("Failed to fetch availability for lead %s", lead_id)
            return result

        if not slots:
            # Let the reply generator handle the "no slots yet" wording warmly
            # (meeting_offer stage with no available_slots).
            return result

        top = slots[:3]
        offered = [s.isoformat() for s in top]
        memory.set_pending_meeting_slots(
            lead_id,
            {"slots": offered, "duration_minutes": duration,
             "offered_at": datetime.now(ZoneInfo("UTC")).isoformat()},
        )
        result["offered_slots"] = offered
        result["offered_slots_human"] = [self._format_slot(s, settings) for s in top]
        return result

    @staticmethod
    def _format_slot(slot, settings) -> str:
        from zoneinfo import ZoneInfo

        local = slot.astimezone(ZoneInfo(settings.business_timezone))
        return local.strftime("%a %d %b, %H:%M IST")


sales_agent = SalesAgent()
