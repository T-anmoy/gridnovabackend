"""Response models for the staff lead / memory endpoints (BE-6).

These describe what the routes in app/api/routes/dashboard.py already return — they
exist so the OpenAPI schema (and the frontend built from it) is no longer `{}`. A model
here must never add, drop or rename a field the route returns; docs/api/staff_contracts.md
is written from them.
"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel

from app.memory.models import (
    LeadScore, LeadSource, MeetingStatus, MessageRole, PipelineStage, ProjectFamily,
    ProposalStatus, TimelineEventType,
)


# ── Leads ─────────────────────────────────────────────────────────────────────

class LeadListItem(BaseModel):
    id: int
    customer_id: int
    pipeline_stage: PipelineStage
    lead_score: Optional[LeadScore] = None
    source: LeadSource
    created_at: str
    assigned_employee_id: Optional[str] = None
    project_reference_id: Optional[str] = None


class LeadListResponse(BaseModel):
    items: list[LeadListItem]
    page: int
    page_size: int
    total: int


class LeadCustomerBrief(BaseModel):
    id: int
    name: Optional[str] = None
    email: str


class LeadProjectSummary(BaseModel):
    id: int
    reference_id: str
    project_type: Optional[str] = None
    project_family: Optional[ProjectFamily] = None
    state: Optional[str] = None
    location: Optional[str] = None
    capacity_kw: Optional[float] = None
    roof_type: Optional[str] = None
    monthly_electricity_bill: Optional[float] = None
    budget: Optional[float] = None
    timeline: Optional[str] = None
    created_at: Optional[str] = None


class LeadProposalBrief(BaseModel):
    id: int
    version: int
    status: ProposalStatus
    total_value: Optional[float] = None


class LeadTimelineEntry(BaseModel):
    event_type: TimelineEventType
    description: Optional[str] = None
    created_at: str


class LeadMessageEntry(BaseModel):
    role: MessageRole
    content: str
    created_at: str


class LeadDetailResponse(BaseModel):
    """Admin / Management / Sales / (any role with lead access except Engineer)."""
    lead_id: int
    customer: Optional[LeadCustomerBrief] = None
    pipeline_stage: PipelineStage
    lead_score: Optional[LeadScore] = None
    source: LeadSource
    assigned_employee_id: Optional[str] = None
    project: Optional[LeadProjectSummary] = None
    collected_fields: Optional[dict[str, Any]] = None
    proposals: list[LeadProposalBrief]
    timeline: list[LeadTimelineEntry]
    messages: list[LeadMessageEntry]


class EngineerLeadDetailResponse(BaseModel):
    """Engineers get technical fields only: no customer, pricing, messages or timeline."""
    lead_id: int
    pipeline_stage: PipelineStage
    collected_fields: dict[str, Any]


class LeadFieldsUpdateResponse(BaseModel):
    lead_id: int
    collected_fields: Optional[dict[str, Any]] = None
    pipeline_stage: PipelineStage
    lead_score: Optional[LeadScore] = None
    is_qualified: bool
    missing_fields: list[str]


class LeadStageUpdateResponse(BaseModel):
    lead_id: int
    previous_stage: PipelineStage
    pipeline_stage: PipelineStage
    reason: Optional[str] = None


class LeadAssignResponse(BaseModel):
    lead_id: int
    previous_assigned_employee_id: Optional[str] = None
    assigned_employee_id: str


# ── Schedule meeting ──────────────────────────────────────────────────────────

class ScheduleMeetingResponse(BaseModel):
    meeting_id: int
    lead_id: int
    employee_id: str
    scheduled_at: str
    duration_minutes: int
    meet_link: Optional[str] = None
    status: MeetingStatus


# ── Customer memory ───────────────────────────────────────────────────────────

class MemoryCustomerSummary(BaseModel):
    """A customer as the Memory Agent lists a candidate."""
    customer_id: int
    name: Optional[str] = None
    email: str
    company: Optional[str] = None


class MemoryCitation(BaseModel):
    id: int
    kind: str
    summary: str
    occurred_at: Optional[str] = None


class MemoryAskResponse(BaseModel):
    answer: Optional[str] = None
    resolved: bool
    customer_id: Optional[int] = None
    candidates: list[MemoryCustomerSummary]
    citations: list[MemoryCitation]
    retrieval_mode: Optional[str] = None


class CustomerMemoryProfile(BaseModel):
    summary: Optional[str] = None
    traits: list[Any]
    open_questions: list[Any]
    agent_notes: Optional[str] = None
    next_best_action: Optional[str] = None
    last_reflected_at: Optional[str] = None
    reflection_count: int


class CustomerMemoryItem(BaseModel):
    id: int
    kind: str
    source: str
    summary: str
    detail: Optional[str] = None
    tags: list[Any]
    salience: int
    lead_id: Optional[int] = None
    occurred_at: Optional[str] = None
    created_by: str


class CustomerMemoryResponse(BaseModel):
    customer: MemoryCustomerSummary
    profile: Optional[CustomerMemoryProfile] = None
    memory_count: int
    memories: list[CustomerMemoryItem]


class ReflectResponse(BaseModel):
    """Always 200 once access is granted — reflection never raises; a no-op or a
    failure is reported in `skipped` (null when it ran)."""
    trigger: str
    lead_id: int
    memories_written: int
    skipped: Optional[str] = None
