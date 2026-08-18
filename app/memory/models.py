import enum
from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import (
    String, Float, Boolean, Text, JSON, ForeignKey,
    Enum as SAEnum, DateTime, Integer, UniqueConstraint, CheckConstraint,
    Index, text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.memory.db import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── Enums ──────────────────────────────────────────────────────────────────────

class EmployeeRole(str, enum.Enum):
    admin = "admin"
    sales = "sales"
    management = "management"
    engineer = "engineer"


class LeadScore(str, enum.Enum):
    high = "high"
    medium = "medium"
    low = "low"


class PipelineStage(str, enum.Enum):
    new = "new"
    qualifying = "qualifying"
    qualified = "qualified"
    proposal_draft = "proposal_draft"
    proposal_sent = "proposal_sent"
    negotiation = "negotiation"
    won = "won"
    lost = "lost"


class ProposalStatus(str, enum.Enum):
    draft = "draft"
    pending_pricing_validation = "pending_pricing_validation"
    pending_approval = "pending_approval"
    revision_requested = "revision_requested"
    approved = "approved"
    sent = "sent"
    accepted = "accepted"  # customer accepted online (distinct from internal approval)


class TimelineEventType(str, enum.Enum):
    lead_created = "lead_created"
    qualification_completed = "qualification_completed"
    meeting_scheduled = "meeting_scheduled"
    proposal_draft_created = "proposal_draft_created"
    proposal_approved = "proposal_approved"
    manager_signoff_completed = "manager_signoff_completed"
    proposal_sent = "proposal_sent"
    revision_requested = "revision_requested"
    pricing_adjusted = "pricing_adjusted"
    customer_accepted = "customer_accepted"
    proposal_v2 = "proposal_v2"
    negotiation = "negotiation"
    won = "won"
    lost = "lost"
    escalated = "escalated"


class MeetingStatus(str, enum.Enum):
    scheduled = "scheduled"
    cancelled = "cancelled"
    completed = "completed"


class LeadSource(str, enum.Enum):
    website_chat = "website_chat"
    whatsapp = "whatsapp"
    instagram = "instagram"
    cold_call = "cold_call"
    referral = "referral"
    other = "other"


class Channel(str, enum.Enum):
    website = "website"
    whatsapp = "whatsapp"
    email = "email"
    dashboard = "dashboard"


class MessageRole(str, enum.Enum):
    customer = "customer"
    agent = "agent"
    employee = "employee"


# ── Models ────────────────────────────────────────────────────────────────────

class Employee(Base):
    __tablename__ = "employees"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    email: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    role: Mapped[EmployeeRole] = mapped_column(SAEnum(EmployeeRole), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    # Google Calendar connection (per-employee, opt-in). The refresh token is stored
    # encrypted at rest (Fernet) — never in plain text. See app/security/encryption.py.
    google_calendar_connected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    google_refresh_token: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    calendar_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    leads: Mapped[list["Lead"]] = relationship(back_populates="assigned_employee")
    approved_proposals: Mapped[list["Proposal"]] = relationship(back_populates="approved_by")
    meetings: Mapped[list["Meeting"]] = relationship(back_populates="employee")


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Nullable: a conversation can start with no identity at all (a placeholder
    # customer), with the name filled in once extracted from the chat.
    name: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    company: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    email: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    phone: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    gst: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    industry: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    projects: Mapped[list["Project"]] = relationship(back_populates="customer")
    leads: Mapped[list["Lead"]] = relationship(back_populates="customer")
    messages: Mapped[list["ConversationMessage"]] = relationship(back_populates="customer")


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), nullable=False)
    project_type: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    capacity_kw: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    location: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    timeline: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    budget: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    roof_type: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    monthly_electricity_bill: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    customer: Mapped["Customer"] = relationship(back_populates="projects")
    leads: Mapped[list["Lead"]] = relationship(back_populates="project")


class Lead(Base):
    __tablename__ = "leads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), nullable=False)
    project_id: Mapped[Optional[int]] = mapped_column(ForeignKey("projects.id"), nullable=True)
    # Which outreach channel this lead came from — for conversion-by-source analytics.
    source: Mapped[LeadSource] = mapped_column(
        SAEnum(LeadSource), nullable=False,
        default=LeadSource.website_chat, server_default=LeadSource.website_chat.value,
    )
    collected_fields: Mapped[dict] = mapped_column(JSON, default=dict)
    lead_score: Mapped[Optional[LeadScore]] = mapped_column(SAEnum(LeadScore), nullable=True)
    pipeline_stage: Mapped[PipelineStage] = mapped_column(
        SAEnum(PipelineStage), default=PipelineStage.new, nullable=False
    )
    assigned_employee_id: Mapped[Optional[str]] = mapped_column(ForeignKey("employees.id"), nullable=True)
    follow_up_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    decision_maker: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # Meeting slots most recently offered to the customer in conversation, so a later
    # confirmation maps to a real, calendar-verified slot instead of an invented time.
    pending_meeting_slots: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    # Stale-lead follow-up: when the customer last messaged, and when we last fired a
    # staleness notification (so the same threshold isn't re-notified repeatedly).
    last_customer_message_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    stale_notified_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    customer: Mapped["Customer"] = relationship(back_populates="leads")
    project: Mapped[Optional["Project"]] = relationship(back_populates="leads")
    assigned_employee: Mapped[Optional["Employee"]] = relationship(back_populates="leads")
    proposals: Mapped[list["Proposal"]] = relationship(back_populates="lead")
    timeline_events: Mapped[list["TimelineEvent"]] = relationship(back_populates="lead")
    messages: Mapped[list["ConversationMessage"]] = relationship(back_populates="lead")
    meetings: Mapped[list["Meeting"]] = relationship(back_populates="lead")


class Proposal(Base):
    __tablename__ = "proposals"
    __table_args__ = (
        # DB-level safety net for the approval gate: a proposal can never be
        # 'approved' without a recorded approver, regardless of application code.
        CheckConstraint(
            "status != 'approved' OR approved_by_employee_id IS NOT NULL",
            name="ck_proposal_approved_requires_approver",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[ProposalStatus] = mapped_column(
        SAEnum(ProposalStatus), default=ProposalStatus.draft, nullable=False
    )
    content: Mapped[dict] = mapped_column(JSON, nullable=False)
    pdf_url: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    total_value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    date_created: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    date_sent: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # Customer-facing online acceptance (a lightweight accept-link, not e-signature).
    accept_token: Mapped[Optional[str]] = mapped_column(String, unique=True, nullable=True)
    accepted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    approved_by_employee_id: Mapped[Optional[str]] = mapped_column(ForeignKey("employees.id"), nullable=True)
    requires_manager_signoff: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    used_fallback_template: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    revision_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    lead: Mapped["Lead"] = relationship(back_populates="proposals")
    approved_by: Mapped[Optional["Employee"]] = relationship(back_populates="approved_proposals")


class TimelineEvent(Base):
    __tablename__ = "timeline_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id"), nullable=False)
    event_type: Mapped[TimelineEventType] = mapped_column(SAEnum(TimelineEventType), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    lead: Mapped["Lead"] = relationship(back_populates="timeline_events")


class ConversationMessage(Base):
    __tablename__ = "conversation_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lead_id: Mapped[Optional[int]] = mapped_column(ForeignKey("leads.id"), nullable=True)
    customer_id: Mapped[Optional[int]] = mapped_column(ForeignKey("customers.id"), nullable=True)
    channel: Mapped[Channel] = mapped_column(SAEnum(Channel), nullable=False)
    role: Mapped[MessageRole] = mapped_column(SAEnum(MessageRole), nullable=False)
    agent_name: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    lead: Mapped[Optional["Lead"]] = relationship(back_populates="messages")
    customer: Mapped[Optional["Customer"]] = relationship(back_populates="messages")


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entity_type: Mapped[str] = mapped_column(String, nullable=False)
    entity_id: Mapped[str] = mapped_column(String, nullable=False)
    action: Mapped[str] = mapped_column(String, nullable=False)
    performed_by: Mapped[str] = mapped_column(String, nullable=False)
    before_value: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    after_value: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Meeting(Base):
    __tablename__ = "meetings"
    __table_args__ = (
        # Hard backstop against double-booking: the free/busy re-verification in
        # app/scheduling/booking.py is a check-then-act race under real concurrency
        # (confirmed live — two simultaneous bookings for the same employee/slot can
        # both pass the check before either commits). Scoped to status = 'scheduled'
        # so a cancelled meeting doesn't block ever re-booking that slot.
        Index(
            "uq_meetings_employee_slot_active",
            "employee_id", "scheduled_at",
            unique=True,
            sqlite_where=text("status = 'scheduled'"),
            postgresql_where=text("status = 'scheduled'"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id"), nullable=False)
    employee_id: Mapped[str] = mapped_column(ForeignKey("employees.id"), nullable=False)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    google_event_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    meet_link: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    status: Mapped[MeetingStatus] = mapped_column(
        SAEnum(MeetingStatus), default=MeetingStatus.scheduled, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    lead: Mapped["Lead"] = relationship(back_populates="meetings")
    employee: Mapped["Employee"] = relationship(back_populates="meetings")
