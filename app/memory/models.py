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
    proposal_inputs_submitted = "proposal_inputs_submitted"
    proposal_inputs_reviewed = "proposal_inputs_reviewed"


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


# ── ProposalInputs enums ─────────────────────────────────────────────────────────

class CustomerType(str, enum.Enum):
    individual = "individual"
    organisation = "organisation"


class ProjectFamily(str, enum.Enum):
    rooftop_solar = "rooftop_solar"
    ground_mounted = "ground_mounted"
    government_institutional = "government_institutional"
    street_lighting = "street_lighting"
    water_pump = "water_pump"
    om_service = "om_service"
    consulting = "consulting"
    # TODO: add emerging (BESS/EV/microgrid) once Raju Paul confirms


class GovernmentScheme(str, enum.Enum):
    pm_surya_ghar = "pm_surya_ghar"
    pm_kusum_a = "pm_kusum_a"
    pm_kusum_b = "pm_kusum_b"
    pm_kusum_c = "pm_kusum_c"
    wbreda = "wbreda"
    other_government = "other_government"
    none = "none"


class ConnectionType(str, enum.Enum):
    on_grid = "on_grid"
    off_grid = "off_grid"
    hybrid = "hybrid"


class ConnectionPhase(str, enum.Enum):
    single_phase = "single_phase"
    three_phase = "three_phase"


class SiteType(str, enum.Enum):
    rooftop_residential = "rooftop_residential"
    rooftop_commercial = "rooftop_commercial"
    rooftop_institutional = "rooftop_institutional"
    ground_mounted = "ground_mounted"
    carport = "carport"
    other = "other"


class ShadingCondition(str, enum.Enum):
    none = "none"
    partial_morning = "partial_morning"
    partial_evening = "partial_evening"
    heavy = "heavy"


class EquipmentTier(str, enum.Enum):
    standard = "standard"
    premium = "premium"
    as_recommended = "as_recommended"
    # TODO: Raju Paul to define Good/Better/Best tier names formally


class InputStatus(str, enum.Enum):
    submitted = "submitted"                        # just received from form
    under_review = "under_review"                  # sales team is looking at it
    approved_for_proposal = "approved_for_proposal"  # ready for Proposal Agent
    rejected = "rejected"                           # not viable, with reason


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
    memories: Mapped[list["CustomerMemory"]] = relationship(back_populates="customer")
    profile: Mapped[Optional["CustomerProfile"]] = relationship(
        back_populates="customer", uselist=False
    )


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
    proposal_inputs: Mapped[list["ProposalInputs"]] = relationship(back_populates="lead")
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


class ProposalInputs(Base):
    """Raw customer-provided site assessment data, submitted via the /proposal/new
    form. Deliberately separate from Proposal (the AI-generated output) — these
    have different lifecycles: customer provides inputs -> staff reviews ->
    AI generates proposal -> staff approves -> customer receives.
    """
    __tablename__ = "proposal_inputs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id"), nullable=False)
    status: Mapped[InputStatus] = mapped_column(
        SAEnum(InputStatus), default=InputStatus.submitted, nullable=False
    )
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    reviewed_by_id: Mapped[Optional[str]] = mapped_column(ForeignKey("employees.id"), nullable=True)
    rejection_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # ── Customer & Project ───────────────────────────────────────────────────
    customer_type: Mapped[CustomerType] = mapped_column(SAEnum(CustomerType), nullable=False)
    organisation_name: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    gst_number: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    project_family: Mapped[ProjectFamily] = mapped_column(SAEnum(ProjectFamily), nullable=False)
    scheme_linked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    scheme_name: Mapped[Optional[GovernmentScheme]] = mapped_column(
        SAEnum(GovernmentScheme), nullable=True, default=GovernmentScheme.none
    )

    # ── Site Location ─────────────────────────────────────────────────────────
    state: Mapped[str] = mapped_column(String, nullable=False)
    district: Mapped[str] = mapped_column(String, nullable=False)
    pin_code: Mapped[str] = mapped_column(String, nullable=False)
    full_address: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Free text for now — TODO: enum once Raju Paul confirms the full DISCOM list.
    discom: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    consumer_number: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    site_type: Mapped[SiteType] = mapped_column(SAEnum(SiteType), nullable=False)
    roof_area_sqft: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    floors: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    shading_condition: Mapped[Optional[ShadingCondition]] = mapped_column(SAEnum(ShadingCondition), nullable=True)
    structural_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # ── Load & Output ─────────────────────────────────────────────────────────
    avg_monthly_bill_inr: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    avg_monthly_units_kwh: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    peak_load_kw: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    connection_phase: Mapped[Optional[ConnectionPhase]] = mapped_column(SAEnum(ConnectionPhase), nullable=True)
    desired_output_kw: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    load_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # ── System Configuration ─────────────────────────────────────────────────
    connection_type: Mapped[ConnectionType] = mapped_column(SAEnum(ConnectionType), nullable=False)
    net_metering_required: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    dcr_panels_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    ev_charging: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    battery_storage: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    equipment_tier: Mapped[Optional[EquipmentTier]] = mapped_column(SAEnum(EquipmentTier), nullable=True)
    additional_requirements: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # ── Documents ─────────────────────────────────────────────────────────────
    # List of {type, filename, storage_key} — storage_key is a Cloudflare R2 object
    # key, null until upload is wired.
    # TODO: wire actual file upload when R2 credentials are configured.
    document_refs: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)

    # ── Internal notes ────────────────────────────────────────────────────────
    internal_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    lead: Mapped["Lead"] = relationship(back_populates="proposal_inputs")
    reviewed_by: Mapped[Optional["Employee"]] = relationship()


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


# ── Customer memory (episodic + self-reflection) ───────────────────────────────

class MemoryKind(str, enum.Enum):
    """What sort of thing a remembered item is.

    `episode` is what happened; the rest are what the agent concluded from it.
    Keeping them apart matters at recall time — a question about facts should not
    be answered out of the agent's own self-critique.
    """
    episode = "episode"          # something that happened in the conversation
    preference = "preference"    # a durable customer preference or constraint
    objection = "objection"      # a concern, hesitation or blocker they raised
    commitment = "commitment"    # something either side said they would do
    outcome = "outcome"          # what humans did with the agent's work (approved,
                                 # revised, repriced, sent, accepted) — the Proposal
                                 # Agent's feedback signal, and unlike the rest it is
                                 # recorded deterministically from a real event
    insight = "insight"          # the agent's reflection on how the lead is going
    self_critique = "self_critique"  # the agent's reflection on its OWN handling


class MemorySource(str, enum.Enum):
    reflection = "reflection"    # written by the reflection engine
    system = "system"            # written by deterministic code (milestones)
    employee = "employee"        # written by a human in the dashboard


class CustomerMemory(Base):
    """One remembered item about one customer.

    This is the episodic store proper: durable, retrievable, and independent of
    the Sales Agent's short conversation window. Rows are append-only — a memory
    is never rewritten, so the record of what the agent believed and when stays
    intact.
    """
    __tablename__ = "customer_memories"
    __table_args__ = (
        Index("ix_customer_memories_customer", "customer_id", "created_at"),
        Index("ix_customer_memories_lead", "lead_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), nullable=False)
    lead_id: Mapped[Optional[int]] = mapped_column(ForeignKey("leads.id"), nullable=True)
    kind: Mapped[MemoryKind] = mapped_column(SAEnum(MemoryKind), nullable=False)
    source: Mapped[MemorySource] = mapped_column(
        SAEnum(MemorySource), nullable=False, default=MemorySource.reflection
    )
    # One-line statement of the memory — this is what recall shows and cites.
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    # Optional fuller text, including the customer's own words where they matter.
    detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Lowercased keywords, used by the keyword retrieval path.
    tags: Mapped[list] = mapped_column(JSON, default=list)
    # 1 (trivia) to 5 (decisive). Ranks recall when many memories match.
    salience: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    # When the thing happened, which is not always when it was recorded — a
    # reflection run on Friday can record an episode from Tuesday.
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    created_by: Mapped[str] = mapped_column(String, nullable=False, default="sales_agent")
    # Populated only when semantic search is enabled; the keyword path ignores it.
    embedding: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    embedding_model: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    customer: Mapped["Customer"] = relationship(back_populates="memories")


class CustomerProfile(Base):
    """The agent's current, rewritten-in-place view of one customer.

    Where CustomerMemory is append-only history, this is the living summary: what
    we believe now, what we still don't know, and what the agent thinks it should
    do differently next time. Exactly one row per customer.
    """
    __tablename__ = "customer_profiles"

    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id"), primary_key=True
    )
    # Rolling narrative — who this customer is and what they are trying to do.
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Stable characteristics inferred over time (decision style, priorities…).
    traits: Mapped[list] = mapped_column(JSON, default=list)
    # What the agent still hasn't learned — drives what it asks next.
    open_questions: Mapped[list] = mapped_column(JSON, default=list)
    # SELF-REFLECTION: what the agent judges it handled poorly, and what it should
    # do differently. Deliberately separate from everything above, which is about
    # the customer rather than about the agent.
    agent_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    next_best_action: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_reflected_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    reflection_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # How far the conversation had got at the last reflection, so a re-run only
    # considers what is genuinely new.
    last_reflected_message_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    customer: Mapped["Customer"] = relationship(back_populates="profile")
