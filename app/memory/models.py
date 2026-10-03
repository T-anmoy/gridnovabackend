import enum
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional
from datetime import date
from sqlalchemy import (
    String, Float, Boolean, Text, JSON, ForeignKey,
    Enum as SAEnum, DateTime, Date, Integer, UniqueConstraint, CheckConstraint,
    Index, text, DDL, event, Numeric,
)
from sqlalchemy.orm import Mapped, declared_attr, mapped_column, relationship, validates
from sqlalchemy.types import TypeDecorator
from app.memory.db import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    """DateTime(timezone=True) that survives SQLite's dialect dropping tzinfo.

    Postgres stores and returns real timezone-aware timestamps, so this is a no-op
    there. SQLite has no native timestamp type — it stores the value as a plain
    string and hands back a naive datetime on every read, silently discarding the
    UTC marker every write in this codebase already attaches (_now() and every
    other timestamp in this module always uses datetime.now(timezone.utc)). Left
    unfixed, that naive value flows straight into API responses via .isoformat(),
    producing a string with no timezone designator — which a browser parses as
    local time, not UTC, corrupting every displayed timestamp by the viewer's UTC
    offset (e.g. exactly 5:30h for IST).
    """
    impl = DateTime(timezone=True)
    cache_ok = True

    def process_result_value(self, value, dialect):
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value


# ── Enums ──────────────────────────────────────────────────────────────────────

class EmployeeRole(str, enum.Enum):
    admin = "admin"
    sales = "sales"
    management = "management"
    engineer = "engineer"
    # operations (formerly `subcontractor`, renamed in 0026_operations_accounts_roles)
    # and accounts: no access to leads, proposals or pricing. Operations runs project
    # execution (Phase 4A: can_view/verify/record_execution); Accounts runs payments.
    # Each grant is explicit in app/auth/dependencies.py and pinned by
    # tests/test_operations_accounts_roles.py.
    operations = "operations"
    accounts = "accounts"


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
    proposal_auto_drafted_from_inputs = "proposal_auto_drafted_from_inputs"
    stage_changed = "stage_changed"
    lead_assigned = "lead_assigned"
    fields_filled_from_inputs = "fields_filled_from_inputs"
    onboarding_submitted = "onboarding_submitted"
    onboarding_verified = "onboarding_verified"
    attribution_flagged = "attribution_flagged"
    project_stage_recorded = "project_stage_recorded"
    indicative_estimate_created = "indicative_estimate_created"
    execution_stage_submitted = "execution_stage_submitted"
    execution_stage_rejected = "execution_stage_rejected"


class MeetingStatus(str, enum.Enum):
    scheduled = "scheduled"
    cancelled = "cancelled"
    completed = "completed"


class LeadSource(str, enum.Enum):
    website_chat = "website_chat"
    website_estimate_form = "website_estimate_form"
    website_booking = "website_booking"
    website_contact = "website_contact"
    # Entered by a Grid Nova associate / Preferred Associate on the customer's behalf
    # (app/api/routes/associate_onboarding.py).
    associate = "associate"
    preferred_associate = "preferred_associate"
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


class DiscomConsumerCategory(str, enum.Enum):
    """The DISCOM's own tariff-category classification — distinct from
    CustomerType above (which is about who the customer entity is: an
    individual or an organisation), not about how the electricity connection
    itself is billed."""
    domestic = "domestic"
    industrial = "industrial"
    # TODO: expand once Raju Paul confirms the full DISCOM tariff-category
    # list (commercial, agricultural, etc.) — domestic/industrial are the two
    # named in the current site-assessment gap analysis.


class ConsumerPriorityClass(str, enum.Enum):
    normal = "normal"
    vip = "vip"


# The fixed vocabulary of required installation-photo categories, sourced from
# a real professional rooftop-solar site-survey checklist. Keys into
# ProposalInputs.installation_photo_refs — a dict, not the open-ended list
# document_refs already uses, because these are specific required categories,
# not an arbitrary pile of uploads.
INSTALLATION_PHOTO_SLOTS = (
    "panel_installation_area",
    "acdb_dcdb_placement",
    "earthing_location",
    "inverter_installation_area",
    "wiring_path",
    "panel_to_inverter_distance",
    "inverter_to_lt_panel_distance",
)


class InputStatus(str, enum.Enum):
    submitted = "submitted"                        # just received from form
    under_review = "under_review"                  # sales team is looking at it
    approved_for_proposal = "approved_for_proposal"  # ready for Proposal Agent
    rejected = "rejected"                           # not viable, with reason


class EquipmentCategory(str, enum.Enum):
    module = "module"
    inverter = "inverter"
    structure = "structure"
    bos_component = "bos_component"  # balance-of-system: cables, connectors, earthing, etc.


# ── Models ────────────────────────────────────────────────────────────────────

class Employee(Base):
    __tablename__ = "employees"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    email: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    role: Mapped[EmployeeRole] = mapped_column(SAEnum(EmployeeRole), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)

    # Deactivation is a flag, never a delete — every historical FK (leads, proposals,
    # meetings, audit log) keeps pointing at a real row. A deactivated employee is
    # blocked at authentication (see app/auth/dependencies.py) rather than removed.
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

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
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now, onupdate=_now)

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
    # GNE-{STATE}-{TYPE}-{YEAR}-{SEQUENCE}, e.g. GNE-WB-RES-2026-00125 — generated
    # once at creation (see app/memory/reference_id.py + MemoryManager.create_project)
    # and never recomputed afterward, so it must be immutable once assigned.
    reference_id: Mapped[str] = mapped_column(String, unique=True, index=True, nullable=False)
    project_type: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # Free-text state name (e.g. "West Bengal") — feeds the reference_id's state
    # code at creation time; not an enum because no state enum exists anywhere
    # in this codebase yet (see ProposalInputs.state, the same shape).
    state: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    project_family: Mapped[Optional[ProjectFamily]] = mapped_column(
        SAEnum(ProjectFamily), nullable=True
    )
    capacity_kw: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    location: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    timeline: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    budget: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    roof_type: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    monthly_electricity_bill: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now, onupdate=_now)

    customer: Mapped["Customer"] = relationship(back_populates="projects")
    leads: Mapped[list["Lead"]] = relationship(back_populates="project")


class ProjectSequenceCounter(Base):
    """One row per (state code, type code, year) bucket — the atomic counter
    app/memory/reference_id.py increments to assign the next reference_id
    sequence number. See that module for why this is a real DB-level atomic
    increment (INSERT ... ON CONFLICT ... RETURNING), not a naive count+1.
    """
    __tablename__ = "project_sequence_counters"

    counter_key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class Equipment(Base):
    """Equipment/SKU master — empty, ready for real data. Deactivate old or
    discontinued items via is_active; never hard-delete, same discipline as
    Employee (a Proposal or historical record may reference a SKU that's since
    been discontinued, and that reference must stay meaningful)."""
    __tablename__ = "equipment"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    category: Mapped[EquipmentCategory] = mapped_column(SAEnum(EquipmentCategory), nullable=False)
    brand: Mapped[str] = mapped_column(String, nullable=False)
    model_name: Mapped[str] = mapped_column(String, nullable=False)
    # Free text for now — wattage, capacity, etc. No structured spec schema
    # exists yet across categories this different (a module's spec and a
    # cable's spec share nothing), so this isn't a placeholder to "fix" later
    # so much as the honest shape for a first version.
    specification: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_dcr_compliant: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Cost to Grid Nova — TBD from Raju Paul, hence nullable.
    procurement_price_inr: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Development-only dummy rows (scripts/seed_dummy_data.py): shown as such in the
    # dashboard, and never named by Nova or put in front of a customer.
    is_placeholder: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)


class Lead(Base):
    __tablename__ = "leads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), nullable=False)
    # Every Lead has a real Project — MemoryManager.create_lead() creates an empty
    # one automatically when the caller doesn't supply project_id. Backfilled for
    # pre-existing rows in migration 0015_project_required.
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False)
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
    follow_up_date: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    decision_maker: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # Meeting slots most recently offered to the customer in conversation, so a later
    # confirmation maps to a real, calendar-verified slot instead of an invented time.
    pending_meeting_slots: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    # Stale-lead follow-up: when the customer last messaged, and when we last fired a
    # staleness notification (so the same threshold isn't re-notified repeatedly).
    last_customer_message_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    stale_notified_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now, onupdate=_now)

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
    # The PDF's object key in the private bucket — what a signed download link is
    # made from. pdf_url is kept for compatibility; for rows written before this
    # column existed the key is derived from it (app/storage/r2.py key_from_url).
    pdf_storage_key: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    total_value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    date_created: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)
    date_sent: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    approved_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    # Customer-facing online acceptance (a lightweight accept-link, not e-signature).
    accept_token: Mapped[Optional[str]] = mapped_column(String, unique=True, nullable=True)
    accepted_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    approved_by_employee_id: Mapped[Optional[str]] = mapped_column(ForeignKey("employees.id"), nullable=True)
    requires_manager_signoff: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    used_fallback_template: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    revision_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Who drafted this version: the employee id that triggered it, or
    # "associate:GNA-…" when an associate requested it. For associate-sourced leads the
    # standard approver must be someone else (dashboard.approve_proposal).
    drafted_by: Mapped[Optional[str]] = mapped_column(String, nullable=True)

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
    submitted_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
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

    # ── Roof detail (site-assessment gap analysis) ───────────────────────────
    roof_ownership_confirmed: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    roofs_available_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    roofs_considered_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    future_roof_expansion_planned: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    roof_age_years: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # ── Load & Output ─────────────────────────────────────────────────────────
    avg_monthly_bill_inr: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    avg_monthly_units_kwh: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # 6 months of real consumption history — list of {month: "YYYY-MM",
    # units_kwh, bill_inr}, the same list-of-dicts pattern document_refs
    # already uses below, since this is small, bounded (6 entries), and never
    # queried independently of the row it belongs to. avg_monthly_bill_inr/
    # avg_monthly_units_kwh above are kept as-is for backward compatibility —
    # a single average a caller may still submit directly, distinct from this
    # richer per-month history.
    consumption_history: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    peak_load_kw: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # The DISCOM's own officially sanctioned load — distinct from peak_load_kw
    # (the customer's own observed/estimated peak), which already existed.
    sanctioned_load_kw: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    connection_phase: Mapped[Optional[ConnectionPhase]] = mapped_column(SAEnum(ConnectionPhase), nullable=True)
    desired_output_kw: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    load_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # ── DISCOM consumer classification ───────────────────────────────────────
    discom_consumer_category: Mapped[Optional[DiscomConsumerCategory]] = mapped_column(
        SAEnum(DiscomConsumerCategory), nullable=True
    )
    consumer_priority_class: Mapped[Optional[ConsumerPriorityClass]] = mapped_column(
        SAEnum(ConsumerPriorityClass), nullable=True, default=ConsumerPriorityClass.normal
    )

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

    # Structured installation photos, one of INSTALLATION_PHOTO_SLOTS above per
    # key -> {filename, storage_key} (same per-item shape as document_refs,
    # but keyed rather than a loose list, since a real site-survey checklist
    # calls for these specific categories, not an open-ended pile). Same R2
    # wiring TODO as document_refs above.
    installation_photo_refs: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    # ── Signatures ────────────────────────────────────────────────────────────
    # {filename, storage_key, signed_at} — same shape/TODO as document_refs;
    # a signature is stored the same way any other captured image is.
    staff_signature_ref: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    customer_signature_ref: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

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
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)

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
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)

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
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)


class AgentGovernanceOverride(Base):
    """DB-backed kill-switch override for one agent, keyed by the registry's
    agent id. DISABLED_AGENTS (an env var) is the deploy-time default; a row here
    takes precedence over it, so an Admin can toggle an agent off in the running
    process without a redeploy. Absence of a row means "no override" — governance
    falls back to the env var, exactly as it always has.
    """
    __tablename__ = "agent_governance_overrides"

    agent_id: Mapped[str] = mapped_column(String, primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=_now, onupdate=_now
    )
    updated_by: Mapped[str] = mapped_column(String, nullable=False)


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
    scheduled_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    google_event_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    meet_link: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    status: Mapped[MeetingStatus] = mapped_column(
        SAEnum(MeetingStatus), default=MeetingStatus.scheduled, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)

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
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)
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
    last_reflected_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    reflection_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # How far the conversation had got at the last reflection, so a re-run only
    # considers what is genuinely new.
    last_reflected_message_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now, onupdate=_now)

    customer: Mapped["Customer"] = relationship(back_populates="profile")


# ── Territory master (State → District → Block) ─────────────────────────────────
# Keyed by Local Government Directory (LGD) codes; loaded from data/territories/ by
# scripts/seed_territories.py. Never deleted — switched off with is_active, since
# applications, associates and seats keep pointing at them.

class TerritoryState(Base):
    __tablename__ = "territory_states"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lgd_code: Mapped[int] = mapped_column(Integer, unique=True, nullable=False)
    # ISO 3166-2:IN code without the "IN-" prefix — the {STATE} in Associate IDs.
    iso_code: Mapped[str] = mapped_column(String(2), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)


class TerritoryDistrict(Base):
    __tablename__ = "territory_districts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    state_id: Mapped[int] = mapped_column(ForeignKey("territory_states.id"), nullable=False, index=True)
    lgd_code: Mapped[int] = mapped_column(Integer, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)

    state: Mapped["TerritoryState"] = relationship()


class TerritoryBlock(Base):
    __tablename__ = "territory_blocks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    district_id: Mapped[int] = mapped_column(ForeignKey("territory_districts.id"), nullable=False, index=True)
    lgd_code: Mapped[int] = mapped_column(Integer, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)

    district: Mapped["TerritoryDistrict"] = relationship()


# ── Associates ───────────────────────────────────────────────────────────────────
# Associates are a separate kind of user from employees: they never get an Employee
# row, and they authenticate through app/auth/dependencies.py get_current_associate
# on their own Clerk claims. See the Phase 2A report for the claim structure.

class AssociateType(str, enum.Enum):
    associate = "associate"
    preferred_associate = "preferred_associate"


class AssociateCapability(str, enum.Enum):
    sales = "sales"
    execution = "execution"


class TerritoryLevel(str, enum.Enum):
    state_head = "state_head"
    district_head = "district_head"
    block = "block"


class ApplicationStatus(str, enum.Enum):
    submitted = "submitted"
    under_review = "under_review"
    needs_info = "needs_info"
    approved = "approved"
    rejected = "rejected"


class AssociateStatus(str, enum.Enum):
    active = "active"
    suspended = "suspended"
    deactivated = "deactivated"


class ClerkInviteStatus(str, enum.Enum):
    sent = "sent"          # Clerk created the invitation
    skipped = "skipped"    # no CLERK_SECRET_KEY (local dev)
    failed = "failed"      # Clerk refused or was unreachable — retry from the dashboard


# Documents every registration must upload. Keys into the application's `documents`.
APPLICATION_DOCUMENTS = ("masked_aadhaar", "pan_card", "photo", "cancelled_cheque")


class _ApplicationKyc:
    """Columns shared by both registration tables. The full Aadhaar number is never
    stored — only its last four digits, after a Verhoeff check. The bank account
    number is stored Fernet-encrypted (app/security/encryption.py) with its last four
    digits alongside for display."""

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    status: Mapped[ApplicationStatus] = mapped_column(
        SAEnum(ApplicationStatus), nullable=False, default=ApplicationStatus.submitted,
    )
    full_name: Mapped[str] = mapped_column(String, nullable=False)
    date_of_birth: Mapped[date] = mapped_column(Date, nullable=False)
    address_line: Mapped[str] = mapped_column(Text, nullable=False)
    state_id: Mapped[int] = mapped_column(ForeignKey("territory_states.id"), nullable=False)
    district_id: Mapped[int] = mapped_column(ForeignKey("territory_districts.id"), nullable=False)
    # Null only where the district has no blocks (e.g. Kolkata).
    block_id: Mapped[Optional[int]] = mapped_column(ForeignKey("territory_blocks.id"), nullable=True)
    pin_code: Mapped[str] = mapped_column(String(6), nullable=False)
    mobile: Mapped[str] = mapped_column(String(10), nullable=False)
    email: Mapped[str] = mapped_column(String, nullable=False)
    aadhaar_last4: Mapped[str] = mapped_column(String(4), nullable=False)
    pan: Mapped[str] = mapped_column(String(10), nullable=False)
    bank_account_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    bank_account_last4: Mapped[str] = mapped_column(String(4), nullable=False)
    bank_ifsc: Mapped[str] = mapped_column(String(11), nullable=False)
    kyc_consent_version: Mapped[str] = mapped_column(String, nullable=False)
    kyc_consent_accepted_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    # {doc: {storage_key, filename, content_type, size}} for each APPLICATION_DOCUMENTS.
    documents: Mapped[dict] = mapped_column(JSON, nullable=False)

    submitted_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now, nullable=False)
    status_changed_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now, nullable=False)
    needs_info_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    rejection_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Stands in for OTP: an Admin phones the applicant and records that they did.
    verification_call_completed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    verification_call_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    decided_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)

    @declared_attr
    def decided_by_employee_id(cls) -> Mapped[Optional[str]]:
        return mapped_column(ForeignKey("employees.id"), nullable=True)

    @declared_attr.directive
    def __table_args__(cls):
        t = cls.__tablename__
        return (
            # DB-level backstops for the approval gate, like ck_proposal_approved_requires_approver.
            CheckConstraint("status != 'rejected' OR rejection_reason IS NOT NULL",
                            name=f"ck_{t}_rejected_has_reason"),
            CheckConstraint(
                "status != 'approved' OR (verification_call_completed = true "
                "AND verification_call_note IS NOT NULL AND decided_by_employee_id IS NOT NULL)",
                name=f"ck_{t}_approved_after_call",
            ),
            Index(f"ix_{t}_status", "status"),
        )


class AssociateApplication(_ApplicationKyc, Base):
    __tablename__ = "associate_applications"

    capability: Mapped[AssociateCapability] = mapped_column(SAEnum(AssociateCapability), nullable=False)


class PreferredAssociateApplication(_ApplicationKyc, Base):
    __tablename__ = "preferred_associate_applications"

    requested_level: Mapped[TerritoryLevel] = mapped_column(SAEnum(TerritoryLevel), nullable=False)
    requested_state_id: Mapped[int] = mapped_column(ForeignKey("territory_states.id"), nullable=False)
    requested_district_id: Mapped[Optional[int]] = mapped_column(ForeignKey("territory_districts.id"), nullable=True)
    requested_block_id: Mapped[Optional[int]] = mapped_column(ForeignKey("territory_blocks.id"), nullable=True)
    # Free text: Grid Nova has not fixed a list of business types.
    business_type: Mapped[str] = mapped_column(String, nullable=False)
    gstin: Mapped[Optional[str]] = mapped_column(String(15), nullable=True)
    office_address: Mapped[str] = mapped_column(Text, nullable=False)
    prior_experience: Mapped[str] = mapped_column(Text, nullable=False)
    team_size: Mapped[int] = mapped_column(Integer, nullable=False)
    # Exactly two: [{name, mobile}, {name, mobile}].
    references: Mapped[list] = mapped_column(JSON, nullable=False)


class AssociateIdentityClaim(Base):
    """One row per identity attribute an application has claimed, across BOTH
    registration tables. The unique (kind, value_hash) index is what refuses a
    duplicate registration — in the database, so two simultaneous submissions
    cannot both get through. value_hash is an HMAC (app/associates/service.py), so
    this table reveals nothing about the values themselves. Claims are never
    released: a rejected applicant cannot re-register under the same identity."""
    __tablename__ = "associate_identity_claims"
    __table_args__ = (UniqueConstraint("kind", "value_hash", name="uq_associate_identity_claim"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # pan | mobile | bank_account | aadhaar_dob_name
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    value_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    application_type: Mapped[AssociateType] = mapped_column(SAEnum(AssociateType), nullable=False)
    application_id: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)


class AssociateIdCounter(Base):
    """One row per (state, two-digit year) bucket — the atomic counter behind
    GNA-{STATE}-{YY}-{SEQ}, incremented with the same single-statement upsert as
    project_sequence_counters (app/memory/reference_id.py)."""
    __tablename__ = "associate_id_counters"

    counter_key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class Associate(Base):
    """An approved Associate or Preferred Associate. Created only by approving an
    application; never deleted — deactivation is a status."""
    __tablename__ = "associates"
    __table_args__ = (
        CheckConstraint(
            "(associate_type = 'associate' AND associate_application_id IS NOT NULL "
            "AND preferred_application_id IS NULL AND capability IS NOT NULL AND level IS NULL) OR "
            "(associate_type = 'preferred_associate' AND preferred_application_id IS NOT NULL "
            "AND associate_application_id IS NULL AND level IS NOT NULL)",
            name="ck_associates_type_shape",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # GNA-{STATE}-{YY}-{SEQ}. Immutable once issued: the ORM refuses a change and a
    # database trigger (below) refuses one made by any other route.
    associate_id: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    associate_type: Mapped[AssociateType] = mapped_column(SAEnum(AssociateType), nullable=False)
    status: Mapped[AssociateStatus] = mapped_column(
        SAEnum(AssociateStatus), nullable=False, default=AssociateStatus.active,
    )
    associate_application_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("associate_applications.id"), unique=True, nullable=True,
    )
    preferred_application_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("preferred_associate_applications.id"), unique=True, nullable=True,
    )
    full_name: Mapped[str] = mapped_column(String, nullable=False)
    email: Mapped[str] = mapped_column(String, nullable=False)
    mobile: Mapped[str] = mapped_column(String(10), nullable=False)
    # The state whose code is in associate_id.
    state_id: Mapped[int] = mapped_column(ForeignKey("territory_states.id"), nullable=False)
    capability: Mapped[Optional[AssociateCapability]] = mapped_column(SAEnum(AssociateCapability), nullable=True)
    level: Mapped[Optional[TerritoryLevel]] = mapped_column(SAEnum(TerritoryLevel), nullable=True)
    territory_state_id: Mapped[Optional[int]] = mapped_column(ForeignKey("territory_states.id"), nullable=True)
    territory_district_id: Mapped[Optional[int]] = mapped_column(ForeignKey("territory_districts.id"), nullable=True)
    territory_block_id: Mapped[Optional[int]] = mapped_column(ForeignKey("territory_blocks.id"), nullable=True)
    photo_storage_key: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    photo_content_type: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    # The Clerk user this associate signs in as — bound on first sign-in, then fixed.
    clerk_user_id: Mapped[Optional[str]] = mapped_column(String, unique=True, nullable=True)
    clerk_invitation_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    clerk_invite_status: Mapped[Optional[ClerkInviteStatus]] = mapped_column(SAEnum(ClerkInviteStatus), nullable=True)

    # Private-bucket object keys; null until generated (a failure is retried from the dashboard).
    id_card_storage_key: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    welcome_letter_storage_key: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    documents_generated_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    documents_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    approved_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now, nullable=False)
    approved_by_employee_id: Mapped[str] = mapped_column(ForeignKey("employees.id"), nullable=False)
    status_changed_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now, nullable=False)
    status_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)

    @validates("associate_id")
    def _associate_id_is_immutable(self, key, value):
        current = self.__dict__.get("associate_id")
        if current is not None and value != current:
            raise ValueError("associate_id is immutable once issued")
        return value


# The database-level half of associate_id immutability: an UPDATE that changes it
# fails whatever issued it (raw SQL, a bulk update, another service). Attached to
# the table so create_all builds it too, and created by 0028_associates for
# migrated databases.
ASSOCIATE_ID_IMMUTABLE_PG_FUNCTION = """
CREATE OR REPLACE FUNCTION associates_id_immutable() RETURNS trigger AS $$
BEGIN
    IF NEW.associate_id IS DISTINCT FROM OLD.associate_id THEN
        RAISE EXCEPTION 'associate_id is immutable once issued';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""
ASSOCIATE_ID_IMMUTABLE_PG_TRIGGER = (
    "CREATE TRIGGER trg_associates_id_immutable BEFORE UPDATE ON associates "
    "FOR EACH ROW EXECUTE FUNCTION associates_id_immutable()"
)
ASSOCIATE_ID_IMMUTABLE_SQLITE_TRIGGER = (
    "CREATE TRIGGER trg_associates_id_immutable BEFORE UPDATE OF associate_id ON associates "
    "WHEN NEW.associate_id IS NOT OLD.associate_id "
    "BEGIN SELECT RAISE(ABORT, 'associate_id is immutable once issued'); END"
)
event.listen(Associate.__table__, "after_create",
             DDL(ASSOCIATE_ID_IMMUTABLE_PG_FUNCTION).execute_if(dialect="postgresql"))
event.listen(Associate.__table__, "after_create",
             DDL(ASSOCIATE_ID_IMMUTABLE_PG_TRIGGER).execute_if(dialect="postgresql"))
event.listen(Associate.__table__, "after_create",
             DDL(ASSOCIATE_ID_IMMUTABLE_SQLITE_TRIGGER).execute_if(dialect="sqlite"))


# Seats held: at most 2 State Heads per state, at most 1 District Head per district.
# Enforced by the database, not only by application code: a state head seat carries
# seat_no 1 or 2 and a district head seat seat_no 1 (the CHECK), and (territory,
# seat_no) is unique among seats not yet released (the partial unique indexes) — so
# a third State Head, or a second District Head, cannot be inserted however the
# write arrives, including two approvals racing each other. Block seats have no
# limit in this phase (none was specified). Releasing a seat sets released_at; the
# row stays as history.
MAX_STATE_HEADS_PER_STATE = 2
MAX_DISTRICT_HEADS_PER_DISTRICT = 1

_SEAT_HELD = "released_at IS NULL"


class TerritorySeat(Base):
    __tablename__ = "territory_seats"
    __table_args__ = (
        CheckConstraint(
            "(level = 'state_head' AND district_id IS NULL AND block_id IS NULL "
            f"AND seat_no BETWEEN 1 AND {MAX_STATE_HEADS_PER_STATE}) OR "
            "(level = 'district_head' AND district_id IS NOT NULL AND block_id IS NULL "
            f"AND seat_no BETWEEN 1 AND {MAX_DISTRICT_HEADS_PER_DISTRICT}) OR "
            "(level = 'block' AND district_id IS NOT NULL AND block_id IS NOT NULL AND seat_no IS NULL)",
            name="ck_territory_seats_shape",
        ),
        Index("uq_territory_seats_state_head", "state_id", "seat_no", unique=True,
              sqlite_where=text(f"level = 'state_head' AND {_SEAT_HELD}"),
              postgresql_where=text(f"level = 'state_head' AND {_SEAT_HELD}")),
        Index("uq_territory_seats_district_head", "district_id", "seat_no", unique=True,
              sqlite_where=text(f"level = 'district_head' AND {_SEAT_HELD}"),
              postgresql_where=text(f"level = 'district_head' AND {_SEAT_HELD}")),
        # One held seat per associate.
        Index("uq_territory_seats_holder", "holder_id", unique=True,
              sqlite_where=text(_SEAT_HELD), postgresql_where=text(_SEAT_HELD)),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    level: Mapped[TerritoryLevel] = mapped_column(SAEnum(TerritoryLevel), nullable=False)
    state_id: Mapped[int] = mapped_column(ForeignKey("territory_states.id"), nullable=False)
    district_id: Mapped[Optional[int]] = mapped_column(ForeignKey("territory_districts.id"), nullable=True)
    block_id: Mapped[Optional[int]] = mapped_column(ForeignKey("territory_blocks.id"), nullable=True)
    seat_no: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    holder_id: Mapped[int] = mapped_column(ForeignKey("associates.id"), nullable=False)
    claimed_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now, nullable=False)
    released_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    released_by: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    release_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


# ── Associate-led customer onboarding ────────────────────────────────────────────
# One row per customer an associate onboards. It ties together what the rest of the
# system already has — Customer, Lead (source associate/preferred_associate), Project
# and the site assessment (ProposalInputs) — and adds what is specific to associate
# sourcing: the owning associate, the site's block and GPS, the Customer Information
# Notice acknowledgement, the staff verification call and the attribution lock.

class OnboardingStatus(str, enum.Enum):
    draft = "draft"            # being filled in by the associate
    submitted = "submitted"    # declared complete by the associate; awaiting the staff verification call
    verified = "verified"      # staff verification call done — attribution lock running
    closed = "closed"          # an Admin resolved an attribution conflict against it (kept as history)


class CustomerTimeline(str, enum.Enum):
    """When the customer says they want the system — as the associate recorded it."""
    within_1_month = "within_1_month"
    within_3_months = "within_3_months"
    within_6_months = "within_6_months"
    later = "later"
    unsure = "unsure"


class ScoreBand(str, enum.Enum):
    """Lead scoring v2 band (app/onboarding/scoring.py)."""
    hot = "hot"
    warm = "warm"
    cold = "cold"


class AttributionFlagStatus(str, enum.Enum):
    open = "open"
    resolved = "resolved"


class AttributionResolution(str, enum.Enum):
    keep_existing = "keep_existing"   # the earlier onboarding keeps the customer; the later one is closed
    award_new = "award_new"           # the later onboarding takes the customer; the earlier one is closed


class CustomerOnboarding(Base):
    __tablename__ = "customer_onboardings"
    __table_args__ = (
        Index("ix_customer_onboardings_owner", "owner_associate_id"),
        Index("ix_customer_onboardings_mobile", "customer_mobile"),
        Index("ix_customer_onboardings_consumer", "consumer_number_key"),
        Index("ix_customer_onboardings_territory", "state_id", "district_id", "block_id"),
        CheckConstraint(
            "status != 'verified' OR (verification_call_completed = true AND verification_call_note IS NOT NULL "
            "AND verified_by_employee_id IS NOT NULL AND attribution_locked_until IS NOT NULL)",
            name="ck_customer_onboardings_verified_after_call",
        ),
        CheckConstraint(
            "(site_latitude IS NULL AND site_longitude IS NULL) OR "
            "(site_latitude BETWEEN -90 AND 90 AND site_longitude BETWEEN -180 AND 180)",
            name="ck_customer_onboardings_gps",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id"), unique=True, nullable=False)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), nullable=False)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False)
    proposal_inputs_id: Mapped[int] = mapped_column(ForeignKey("proposal_inputs.id"), unique=True, nullable=False)
    owner_associate_id: Mapped[int] = mapped_column(ForeignKey("associates.id"), nullable=False)
    status: Mapped[OnboardingStatus] = mapped_column(
        SAEnum(OnboardingStatus), nullable=False, default=OnboardingStatus.draft,
    )

    # Where the site is — from the territory master (the free-text state/district on
    # the site assessment is kept in step with these names).
    state_id: Mapped[int] = mapped_column(ForeignKey("territory_states.id"), nullable=False)
    district_id: Mapped[int] = mapped_column(ForeignKey("territory_districts.id"), nullable=False)
    block_id: Mapped[Optional[int]] = mapped_column(ForeignKey("territory_blocks.id"), nullable=True)
    site_latitude: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    site_longitude: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Duplicate detection across associates (normalised copies of what is on the
    # Customer / site assessment).
    customer_mobile: Mapped[str] = mapped_column(String(10), nullable=False)
    consumer_number_key: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    connection_in_customer_name: Mapped[bool] = mapped_column(Boolean, nullable=False)
    property_in_customer_name: Mapped[bool] = mapped_column(Boolean, nullable=False)
    customer_timeline: Mapped[Optional[CustomerTimeline]] = mapped_column(SAEnum(CustomerTimeline), nullable=True)

    # Customer Information Notice — acknowledged by the customer, recorded by the associate.
    notice_version: Mapped[str] = mapped_column(String, nullable=False)
    notice_acknowledged_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    notice_recorded_by: Mapped[str] = mapped_column(String, nullable=False)

    submitted_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    # Staff verification call — required before verification, and before any future
    # commission eligibility (Phase 5).
    verification_call_completed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    verification_call_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    verified_by_employee_id: Mapped[Optional[str]] = mapped_column(ForeignKey("employees.id"), nullable=True)
    verified_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    attribution_locked_until: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    closed_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Lead scoring v2 — the score, its band, and every factor that produced it
    # (app/onboarding/scoring.py). response_due_at is when the staff verification call
    # is due; escalation_level counts how far up the chain an overdue customer has gone.
    score: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    score_band: Mapped[Optional[ScoreBand]] = mapped_column(SAEnum(ScoreBand), nullable=True)
    score_factors: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    scored_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    response_due_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    escalation_level: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    last_escalated_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)

    # The last attempt to prepare the formal proposal failed (app/onboarding/formal.py) —
    # kept until a draft succeeds, so the failure stays visible (formal_proposal_blocked).
    formal_draft_failed_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    formal_draft_failure: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_by: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now, onupdate=_now, nullable=False)


class OnboardingAttributionFlag(Base):
    """A later onboarding that matches an earlier one from a DIFFERENT associate (same
    electricity consumer number or customer mobile). Never resolved automatically:
    an Admin decides, and neither onboarding can be verified while it is open."""
    __tablename__ = "onboarding_attribution_flags"
    __table_args__ = (
        UniqueConstraint("onboarding_id", "existing_onboarding_id", name="uq_onboarding_attribution_pair"),
        Index("ix_onboarding_attribution_flags_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    onboarding_id: Mapped[int] = mapped_column(ForeignKey("customer_onboardings.id"), nullable=False)
    existing_onboarding_id: Mapped[int] = mapped_column(ForeignKey("customer_onboardings.id"), nullable=False)
    matched_on: Mapped[list] = mapped_column(JSON, nullable=False)   # ["consumer_number", "mobile"]
    existing_was_locked: Mapped[bool] = mapped_column(Boolean, nullable=False)
    status: Mapped[AttributionFlagStatus] = mapped_column(
        SAEnum(AttributionFlagStatus), nullable=False, default=AttributionFlagStatus.open,
    )
    resolution: Mapped[Optional[AttributionResolution]] = mapped_column(SAEnum(AttributionResolution), nullable=True)
    resolution_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    resolved_by_employee_id: Mapped[Optional[str]] = mapped_column(ForeignKey("employees.id"), nullable=True)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now, nullable=False)


class ProjectStage(str, enum.Enum):
    """Sales-side progress of an associate-sourced project (app/onboarding/progress.py)."""
    registered = "registered"
    verified = "verified"
    site_assessed = "site_assessed"
    proposal_requested = "proposal_requested"
    proposal_prepared = "proposal_prepared"   # staff drafted the formal proposal
    proposal_issued = "proposal_issued"
    accepted = "accepted"
    agreement_signed = "agreement_signed"
    portal_agreement_approved = "portal_agreement_approved"   # PM Surya Ghar-linked projects only
    # Execution (app/execution/) — which apply to a project comes from
    # config/execution_payments.yaml execution.stages.applicability.
    feasibility_submitted = "feasibility_submitted"   # DISCOM feasibility/application submitted
    material_dispatched = "material_dispatched"
    installed = "installed"
    inspection_net_meter = "inspection_net_meter"
    commissioned = "commissioned"
    subsidy_claimed = "subsidy_claimed"
    subsidy_received = "subsidy_received"
    handover_complete = "handover_complete"


class ProjectStageEvent(Base):
    """One recorded progress stage: who moved it, when, and the evidence behind it.
    Append-only — each stage is recorded once per project and never edited or deleted."""
    __tablename__ = "project_stage_events"
    __table_args__ = (UniqueConstraint("project_id", "stage", name="uq_project_stage_once"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    stage: Mapped[ProjectStage] = mapped_column(SAEnum(ProjectStage), nullable=False)
    recorded_by: Mapped[str] = mapped_column(String, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now, nullable=False)
    evidence: Mapped[dict] = mapped_column(JSON, nullable=False)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class CustomerAgreement(Base):
    """The customer agreement for an associate-sourced project: the PDF generated for
    printing on stamp paper, then the signed scan with the stamp-paper number and date.
    Never deleted; once signed it can no longer be regenerated."""
    __tablename__ = "customer_agreements"
    __table_args__ = (
        CheckConstraint(
            "(signed_scan_key IS NULL AND stamp_paper_number IS NULL AND stamp_paper_date IS NULL "
            "AND signed_uploaded_at IS NULL) OR (signed_scan_key IS NOT NULL AND stamp_paper_number IS NOT NULL "
            "AND stamp_paper_date IS NOT NULL AND signed_uploaded_at IS NOT NULL)",
            name="ck_customer_agreements_signed_together",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    onboarding_id: Mapped[int] = mapped_column(ForeignKey("customer_onboardings.id"), unique=True, nullable=False)
    proposal_id: Mapped[int] = mapped_column(ForeignKey("proposals.id"), nullable=False)
    agreement_storage_key: Mapped[str] = mapped_column(String, nullable=False)
    generated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now, nullable=False)
    generated_by: Mapped[str] = mapped_column(String, nullable=False)
    stamp_paper_number: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    stamp_paper_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    signed_scan_key: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    signed_scan_filename: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    signed_scan_content_type: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    signed_uploaded_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    signed_uploaded_by: Mapped[Optional[str]] = mapped_column(String, nullable=True)


class IndicativeEstimate(Base):
    """An approximate, unsigned estimate an associate shows the customer during
    onboarding — computed by the same engine as the public Solar Calculator
    (app/pricing/instant_estimate.py) and kept exactly as shown. Not a proposal: no
    approval, no send-guard, never feeds the formal proposal or the agreement."""
    __tablename__ = "indicative_estimates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    onboarding_id: Mapped[int] = mapped_column(ForeignKey("customer_onboardings.id"), nullable=False, index=True)
    inputs: Mapped[dict] = mapped_column(JSON, nullable=False)
    result: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_by: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now, nullable=False)


class PlaceholderResolution(Base):
    """An admin marking a `manual` placeholder-registry entry (config/placeholder_registry.yaml)
    as resolved, with a note. Append-only: one row per entry, never edited or deleted.
    Probed entries are never recorded here — their value itself says whether they are
    resolved."""
    __tablename__ = "placeholder_resolutions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entry_id: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    resolved_by: Mapped[str] = mapped_column(String, nullable=False)
    resolved_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now, nullable=False)
    note: Mapped[str] = mapped_column(Text, nullable=False)


# ── Execution (Phase 4A) ─────────────────────────────────────────────────────────

class ExecutionMode(str, enum.Enum):
    associate = "associate"    # the owning associate submits stages; Operations verifies
    grid_nova = "grid_nova"    # Grid Nova's own team records stages directly


class ProjectExecution(Base):
    """One per associate-sourced project, created when `agreement_signed` is recorded
    (app/execution/service.py). Execution files uploaded so far (the latest per photo
    slot / document type) are kept here; a stage submission snapshots the ones it
    relies on into its own evidence."""
    __tablename__ = "project_executions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), unique=True, nullable=False)
    onboarding_id: Mapped[int] = mapped_column(ForeignKey("customer_onboardings.id"), unique=True, nullable=False)
    execution_mode: Mapped[ExecutionMode] = mapped_column(SAEnum(ExecutionMode), nullable=False)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now, nullable=False)
    # {"photos": {slot: file}, "documents": {document_type: file}}
    files: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    # The installed-equipment serial register, once Operations has verified it.
    register_verified_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    register_verified_by: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # The stage a "stuck" alert was last emailed for — so each stall is emailed once.
    stuck_alerted_stage: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # PM Surya Ghar projects with a non-DCR module: emailed once (dcr_alerted_at), and
    # subsidy_claimed is refused until an Admin records why it may proceed.
    dcr_alerted_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    dcr_override_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    dcr_override_by: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    dcr_override_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)


class StageSubmissionStatus(str, enum.Enum):
    pending = "pending"
    verified = "verified"
    rejected = "rejected"


class StageSubmission(Base):
    """An associate's claim that an execution stage is done, with its evidence.
    Operations (or Admin) verifies it — which records the ProjectStageEvent — or
    rejects it with a note; a rejected stage is resubmitted as a new row. At most one
    pending submission per project and stage. Never deleted."""
    __tablename__ = "stage_submissions"
    __table_args__ = (
        Index("uq_stage_submissions_one_pending", "project_id", "stage", unique=True,
              sqlite_where=text("status = 'pending'"), postgresql_where=text("status = 'pending'")),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    onboarding_id: Mapped[int] = mapped_column(ForeignKey("customer_onboardings.id"), nullable=False, index=True)
    stage: Mapped[ProjectStage] = mapped_column(SAEnum(ProjectStage), nullable=False)
    submitted_by: Mapped[str] = mapped_column(String, nullable=False)
    submitted_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now, nullable=False)
    evidence: Mapped[dict] = mapped_column(JSON, nullable=False)
    status: Mapped[StageSubmissionStatus] = mapped_column(
        SAEnum(StageSubmissionStatus), nullable=False, default=StageSubmissionStatus.pending)
    reviewed_by: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    review_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class PhotoFingerprint(Base):
    """The SHA-256 of every onboarding and execution photo, so the same image used
    again on another project is flagged (duplicate_photo). Append-only."""
    __tablename__ = "photo_fingerprints"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String, nullable=False)   # onboarding_photo | execution_photo
    slot: Mapped[str] = mapped_column(String, nullable=False)
    storage_key: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now, nullable=False)


class InstalledUnitCategory(str, enum.Enum):
    module = "module"
    inverter = "inverter"
    other = "other"


class InstalledUnitStatus(str, enum.Enum):
    entered = "entered"
    verified = "verified"   # Operations verified the project's register
    voided = "voided"       # entered by mistake; kept, never deleted


class InstalledUnit(Base):
    """One installed module/inverter/other unit, recorded by serial number
    (app/execution/register.py). serial_normalized is unique across the whole system
    — the database refuses a serial already installed anywhere. A voided unit keeps
    what was entered in serial_number but gives up its serial_normalized (NULL), so a
    typo cannot block the real serial elsewhere."""
    __tablename__ = "installed_units"
    __table_args__ = (
        CheckConstraint("category = 'other' OR equipment_id IS NOT NULL",
                        name="ck_installed_units_equipment_required"),
        CheckConstraint("status = 'voided' OR serial_normalized IS NOT NULL",
                        name="ck_installed_units_serial_held"),
        CheckConstraint("warranty_start IS NULL OR warranty_end IS NULL OR warranty_end > warranty_start",
                        name="ck_installed_units_warranty_order"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    onboarding_id: Mapped[int] = mapped_column(ForeignKey("customer_onboardings.id"), nullable=False, index=True)
    category: Mapped[InstalledUnitCategory] = mapped_column(SAEnum(InstalledUnitCategory), nullable=False)
    equipment_id: Mapped[Optional[int]] = mapped_column(ForeignKey("equipment.id"), nullable=True)
    serial_number: Mapped[str] = mapped_column(String(80), nullable=False)
    serial_normalized: Mapped[Optional[str]] = mapped_column(String(80), unique=True, nullable=True)
    # A voided unit gives up serial_normalized (so the uniqueness stays literal) but keeps
    # it here, so the same serial turning up on another project can be flagged.
    voided_serial_normalized: Mapped[Optional[str]] = mapped_column(String(80), nullable=True, index=True)
    rating_wp: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2), nullable=True)
    rating_kw: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 3), nullable=True)
    is_dcr: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    warranty_start: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    warranty_end: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    status: Mapped[InstalledUnitStatus] = mapped_column(SAEnum(InstalledUnitStatus), nullable=False,
                                                        default=InstalledUnitStatus.entered)
    created_by: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now, nullable=False)
    updated_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    verified_by: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    verified_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    void_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


# ── Payments (Phase 4A) — app/payments/ ─────────────────────────────────────────
# Exact decimals only (Numeric(12, 2)); amounts leave the API as decimal strings.

class PaymentMode(str, enum.Enum):
    """No cash mode, by design."""
    cheque = "cheque"
    demand_draft = "demand_draft"
    neft = "neft"
    rtgs = "rtgs"
    imps = "imps"
    upi = "upi"


INSTRUMENT_MODES = (PaymentMode.cheque, PaymentMode.demand_draft)


class PaymentStatus(str, enum.Enum):
    recorded = "recorded"         # entered; not yet confirmed
    deposited = "deposited"       # cheque/DD handed to the bank (optional step)
    verified = "verified"         # funds confirmed in Grid Nova's bank — there is no separate "cleared"
    dishonoured = "dishonoured"   # bounced/returned (before or after verification)
    cancelled = "cancelled"       # entered by mistake


class LedgerEntryKind(str, enum.Enum):
    credit = "credit"       # a payment verified
    reversal = "reversal"   # a verified payment dishonoured


class PaymentMilestone(Base):
    """One instalment of a project's payment schedule, made from the payments
    schedule template (config/execution_payments.yaml) against the ACCEPTED
    proposal's total. The last milestone carries any rounding remainder, so the
    schedule always sums to the total exactly."""
    __tablename__ = "payment_milestones"
    __table_args__ = (UniqueConstraint("project_id", "number", name="uq_payment_milestones_number"),
                      CheckConstraint("amount_due >= 0", name="ck_payment_milestones_amount"))

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # project_id is the key; onboarding_id is set for associate-sourced projects (the only
    # kind with payments today) and stays NULL for a project sold another way.
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    onboarding_id: Mapped[Optional[int]] = mapped_column(ForeignKey("customer_onboardings.id"), nullable=True,
                                                         index=True)
    proposal_id: Mapped[int] = mapped_column(ForeignKey("proposals.id"), nullable=False)
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    label: Mapped[str] = mapped_column(String, nullable=False)
    percent: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    amount_due: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    trigger: Mapped[str] = mapped_column(String, nullable=False)   # a ProjectStage value
    created_by: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now, nullable=False)
    updated_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    # Overdue alert already emailed (once per milestone).
    overdue_alerted_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)


class Payment(Base):
    """A customer payment toward a project. Never deleted: a mistake is cancelled, a
    bounce is dishonoured. The same instrument cannot be recorded twice
    (mode + normalised reference + normalised bank), cancelled entries excepted."""
    __tablename__ = "payments"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_payments_amount_positive"),
        # One live instrument per (mode, reference, bank). A cancelled or dishonoured
        # payment releases its reference: a bounced cheque may be presented again
        # (recorded as a new payment with previous_payment_id pointing at the bounce).
        Index("uq_payments_instrument", "mode", "reference_normalized", "bank_name_normalized", unique=True,
              sqlite_where=text("status NOT IN ('cancelled', 'dishonoured')"),
              postgresql_where=text("status NOT IN ('cancelled', 'dishonoured')")),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    onboarding_id: Mapped[Optional[int]] = mapped_column(ForeignKey("customer_onboardings.id"), nullable=True,
                                                         index=True)
    milestone_id: Mapped[Optional[int]] = mapped_column(ForeignKey("payment_milestones.id"), nullable=True)
    # Set when this payment re-presents an instrument whose earlier payment was dishonoured.
    previous_payment_id: Mapped[Optional[int]] = mapped_column(ForeignKey("payments.id"), nullable=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    mode: Mapped[PaymentMode] = mapped_column(SAEnum(PaymentMode), nullable=False)
    reference: Mapped[str] = mapped_column(String(100), nullable=False)
    reference_normalized: Mapped[str] = mapped_column(String(100), nullable=False)
    bank_name: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    bank_name_normalized: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    instrument_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    payer_name: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    proof_key: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    proof_filename: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    proof_content_type: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    status: Mapped[PaymentStatus] = mapped_column(SAEnum(PaymentStatus), nullable=False,
                                                  default=PaymentStatus.recorded)
    recorded_by: Mapped[str] = mapped_column(String, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now, nullable=False)
    deposited_by: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    deposited_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    verified_by: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    verified_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    dishonoured_by: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    dishonoured_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    dishonour_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    cancelled_by: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    cancelled_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    cancel_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # "Awaiting verification" alert already emailed (once per payment).
    alerted_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)


class PaymentLedgerEntry(Base):
    """Append-only (a database trigger refuses UPDATE and DELETE). A credit when a
    payment is verified; a reversal — referencing that credit — when a verified
    payment is dishonoured. Project balances are computed from here, and this is
    the table commissions (Phase 5) trace back to. At most one credit and one
    reversal per payment, enforced by the database."""
    __tablename__ = "payment_ledger_entries"
    __table_args__ = (
        UniqueConstraint("payment_id", "kind", name="uq_payment_ledger_one_per_kind"),
        CheckConstraint("(kind = 'credit' AND amount > 0 AND reverses_entry_id IS NULL) OR "
                        "(kind = 'reversal' AND amount < 0 AND reverses_entry_id IS NOT NULL)",
                        name="ck_payment_ledger_shape"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    payment_id: Mapped[int] = mapped_column(ForeignKey("payments.id"), nullable=False, index=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    kind: Mapped[LedgerEntryKind] = mapped_column(SAEnum(LedgerEntryKind), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    reverses_entry_id: Mapped[Optional[int]] = mapped_column(ForeignKey("payment_ledger_entries.id"), unique=True,
                                                             nullable=True)
    actor: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now, nullable=False)


LEDGER_APPEND_ONLY_PG_FUNCTION = """
CREATE OR REPLACE FUNCTION payment_ledger_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'payment_ledger_entries is append-only';
END;
$$ LANGUAGE plpgsql
"""
LEDGER_APPEND_ONLY_PG_TRIGGER = (
    "CREATE TRIGGER payment_ledger_append_only BEFORE UPDATE OR DELETE ON payment_ledger_entries "
    "FOR EACH ROW EXECUTE FUNCTION payment_ledger_append_only()"
)
LEDGER_APPEND_ONLY_SQLITE_TRIGGERS = (
    "CREATE TRIGGER payment_ledger_no_update BEFORE UPDATE ON payment_ledger_entries "
    "BEGIN SELECT RAISE(ABORT, 'payment_ledger_entries is append-only'); END",
    "CREATE TRIGGER payment_ledger_no_delete BEFORE DELETE ON payment_ledger_entries "
    "BEGIN SELECT RAISE(ABORT, 'payment_ledger_entries is append-only'); END",
)
event.listen(PaymentLedgerEntry.__table__, "after_create",
             DDL(LEDGER_APPEND_ONLY_PG_FUNCTION).execute_if(dialect="postgresql"))
event.listen(PaymentLedgerEntry.__table__, "after_create",
             DDL(LEDGER_APPEND_ONLY_PG_TRIGGER).execute_if(dialect="postgresql"))
for _sql in LEDGER_APPEND_ONLY_SQLITE_TRIGGERS:
    event.listen(PaymentLedgerEntry.__table__, "after_create", DDL(_sql).execute_if(dialect="sqlite"))


class ReceiptSequence(Base):
    """Per-year receipt counter, advanced atomically (INSERT … ON CONFLICT … RETURNING)."""
    __tablename__ = "receipt_sequences"

    year: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    value: Mapped[int] = mapped_column(Integer, nullable=False)


class ReceiptEmailStatus(str, enum.Enum):
    pending = "pending"     # generated or being generated; not emailed yet
    sent = "sent"
    skipped = "skipped"     # nobody to send to (email_skipped_reason)
    held = "held"           # would contain placeholder content (held_reason)
    failed = "failed"       # render or delivery failed — retry with resend


class PaymentReceipt(Base):
    """The receipt for one verified payment — numbered when the payment is verified,
    rendered and emailed after that commit (app/payments/receipts.py). Never before
    verification; never deleted."""
    __tablename__ = "payment_receipts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    payment_id: Mapped[int] = mapped_column(ForeignKey("payments.id"), unique=True, nullable=False)
    receipt_number: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    public_token: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    pdf_key: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    generated_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    render_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    email_status: Mapped[ReceiptEmailStatus] = mapped_column(SAEnum(ReceiptEmailStatus), nullable=False,
                                                             default=ReceiptEmailStatus.pending)
    email_skipped_reason: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    held_reason: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    emailed_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now, nullable=False)
