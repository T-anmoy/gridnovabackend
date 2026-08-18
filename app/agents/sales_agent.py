"""
Sales Agent — qualifies inbound leads via conversational field extraction.

ARCHITECTURAL INVARIANT: This module must never import from agents.proposal_agent.
All cross-agent data flows through the Orchestrator and MemoryManager only.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

from app.llm.client import llm_client
from app.memory.models import Channel, LeadScore, MessageRole, PipelineStage, TimelineEventType
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

        # Retrieve existing context
        ctx = memory.get_lead_context(lead_id)
        lead = ctx["lead"]
        existing_fields = dict(lead.collected_fields or {})
        history = _build_history(ctx["messages"])

        # Field extraction — LLM does data work only, never makes decisions
        # Prompt-injection safety: the system prompt instructs the model to ignore
        # any instructions embedded in customer_message, treating it as data only.
        extracted = llm_client.extract_fields(
            user_message=message,
            conversation_history=history,
            lead_id=lead_id,
        )

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
            reply = scheduling["reply_text"]
        else:
            available_human = scheduling.get("offered_slots_human") or None
            reply = llm_client.generate_reply(
                user_message=message,
                stage=conv_stage,
                collected_fields=merged,
                missing_fields=missing,
                conversation_history=history,
                available_slots=available_human,
                lead_id=lead_id,
            )

        # Log agent reply
        memory.log_message(
            channel=channel,
            role=MessageRole.agent,
            content=reply,
            lead_id=lead_id,
            customer_id=customer_id,
            agent_name="Nova",
        )

        return {
            "reply_text": reply,
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

        signal = llm_client.extract_meeting_signal(
            user_message=message,
            conversation_history=history,
            offered_slots_iso=offered_iso,
            lead_id=lead_id,
        )
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
