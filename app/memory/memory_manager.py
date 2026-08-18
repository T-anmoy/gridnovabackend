"""
Single gateway to the database. No agent or route reads/writes models directly.
Every mutating method writes an AuditLog entry as part of the same operation.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import select, func, distinct
from sqlalchemy.orm import Session, joinedload

from app.memory.models import (
    AuditLog,
    Channel,
    ConversationMessage,
    Customer,
    Employee,
    EmployeeRole,
    Lead,
    LeadScore,
    LeadSource,
    Meeting,
    MeetingStatus,
    MessageRole,
    PipelineStage,
    Project,
    Proposal,
    ProposalStatus,
    TimelineEvent,
    TimelineEventType,
)


# Placeholder customers (a chat that starts with no identity) get a unique, non-real
# email in this reserved domain so they never collide with, or match against, a real one.
PLACEHOLDER_EMAIL_DOMAIN = "pending.gridnova.internal"


def is_placeholder_email(email: Optional[str]) -> bool:
    return bool(email) and email.endswith("@" + PLACEHOLDER_EMAIL_DOMAIN)


def _as_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """SQLite can return naive datetimes — treat those as UTC for arithmetic."""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _serialize(obj: Any) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "__dict__"):
        return {k: v for k, v in vars(obj).items() if not k.startswith("_")}
    return str(obj)


class MemoryManager:
    def __init__(self, db: Session):
        self._db = db

    # ── Audit ──────────────────────────────────────────────────────────────────

    def _audit(
        self,
        entity_type: str,
        entity_id: str,
        action: str,
        performed_by: str,
        before: Any = None,
        after: Any = None,
    ) -> None:
        entry = AuditLog(
            entity_type=entity_type,
            entity_id=entity_id,
            action=action,
            performed_by=performed_by,
            before_value=_serialize(before),
            after_value=_serialize(after),
        )
        self._db.add(entry)

    # ── Customer ───────────────────────────────────────────────────────────────

    def get_or_create_customer(
        self,
        email: str,
        name: str,
        performed_by: str = "system",
        **kwargs: Any,
    ) -> Customer:
        customer = self._db.execute(
            select(Customer).where(Customer.email == email)
        ).scalar_one_or_none()

        if customer:
            return customer

        customer = Customer(email=email, name=name, **kwargs)
        self._db.add(customer)
        self._db.flush()
        self._audit("customer", str(customer.id), "created", performed_by, after={"email": email, "name": name})
        return customer

    def create_placeholder_customer(self, performed_by: str = "website") -> Customer:
        """Create an anonymous customer with a unique placeholder email.

        Used when a conversation starts with no identity. Once Nova extracts
        real contact details mid-conversation, call update_customer_details()
        to fill them in — same customer_id, no duplication.
        """
        placeholder_email = f"visitor_{uuid.uuid4().hex}@{PLACEHOLDER_EMAIL_DOMAIN}"
        customer = Customer(email=placeholder_email, name=None)
        self._db.add(customer)
        self._db.flush()
        self._audit(
            "customer", str(customer.id), "created_placeholder", performed_by,
            after={"email": placeholder_email, "placeholder": True},
        )
        return customer

    def update_customer(self, customer_id: int, performed_by: str, **fields: Any) -> Customer:
        customer = self._db.get(Customer, customer_id)
        before = {k: getattr(customer, k) for k in fields}
        for k, v in fields.items():
            setattr(customer, k, v)
        self._db.flush()
        self._audit("customer", str(customer_id), "updated", performed_by, before=before, after=fields)
        return customer

    def update_customer_details(
        self,
        *,
        customer_id: int,
        name: Optional[str],
        email: Optional[str],
        performed_by: str,
    ) -> Optional[Customer]:
        """Fill in a (typically placeholder) customer's real name/email in place, once
        the conversation reveals them — same customer_id, same lead, same history.

        If the newly-extracted email already belongs to a DIFFERENT real customer
        (someone messaging again from a new session), that's not this same person's
        record to overwrite — merge this customer's leads/history into the existing one
        instead, logged distinctly. Returns the surviving Customer (the existing one on
        a merge), so callers can adopt the effective customer_id.
        """
        customer = self._db.get(Customer, customer_id)
        if not customer:
            return None

        # Collision → merge, never overwrite someone else's email.
        if email and not is_placeholder_email(email) and email != customer.email:
            existing = self._db.execute(
                select(Customer).where(Customer.email == email, Customer.id != customer_id)
            ).scalar_one_or_none()
            if existing:
                self._merge_customers(source=customer, target=existing, performed_by=performed_by)
                # A late name can still enrich the surviving record if it lacked one.
                if name and not existing.name:
                    existing.name = name
                    self._db.flush()
                return existing

        before = {"name": customer.name, "email": customer.email}
        if name:
            customer.name = name
        if email:
            customer.email = email
        self._db.flush()
        self._audit(
            "customer", str(customer_id), "details_updated", performed_by,
            before=before, after={"name": customer.name, "email": customer.email},
        )
        return customer

    def _merge_customers(self, *, source: Customer, target: Customer, performed_by: str) -> None:
        """Repoint the source customer's leads and messages onto the target customer.
        Logged distinctly on both sides so the merge is visibly auditable."""
        source_id, target_id = source.id, target.id

        leads = list(self._db.execute(select(Lead).where(Lead.customer_id == source_id)).scalars())
        for lead in leads:
            lead.customer_id = target_id
        messages = list(
            self._db.execute(select(ConversationMessage).where(ConversationMessage.customer_id == source_id)).scalars()
        )
        for msg in messages:
            msg.customer_id = target_id
        self._db.flush()

        self._audit(
            "customer", str(source_id), "merged_into", performed_by,
            before={"customer_id": source_id, "email": source.email},
            after={"merged_into_customer_id": target_id, "email": target.email},
        )
        self._audit(
            "customer", str(target_id), "absorbed_customer", performed_by,
            after={
                "absorbed_customer_id": source_id,
                "leads_moved": [lead.id for lead in leads],
                "messages_moved": len(messages),
            },
        )

    # ── Project ────────────────────────────────────────────────────────────────

    def create_project(self, customer_id: int, performed_by: str = "system", **kwargs: Any) -> Project:
        project = Project(customer_id=customer_id, **kwargs)
        self._db.add(project)
        self._db.flush()
        self._audit("project", str(project.id), "created", performed_by, after={"customer_id": customer_id, **kwargs})
        return project

    def update_project(self, project_id: int, performed_by: str, **fields: Any) -> Project:
        project = self._db.get(Project, project_id)
        before = {k: getattr(project, k) for k in fields}
        for k, v in fields.items():
            setattr(project, k, v)
        self._db.flush()
        self._audit("project", str(project_id), "updated", performed_by, before=before, after=fields)
        return project

    # ── Lead ───────────────────────────────────────────────────────────────────

    def create_lead(
        self,
        customer_id: int,
        performed_by: str = "system",
        project_id: Optional[int] = None,
        source: LeadSource = LeadSource.website_chat,
    ) -> Lead:
        lead = Lead(
            customer_id=customer_id,
            project_id=project_id,
            pipeline_stage=PipelineStage.new,
            source=source,
            collected_fields={},
        )
        self._db.add(lead)
        self._db.flush()
        self._audit(
            "lead", str(lead.id), "created", performed_by,
            after={"customer_id": customer_id, "source": source.value},
        )
        self.add_timeline_event(
            lead_id=lead.id,
            event_type=TimelineEventType.lead_created,
            created_by=performed_by,
        )
        return lead

    def update_lead_fields(
        self,
        lead_id: int,
        new_fields: dict,
        performed_by: str = "system",
        stage: Optional[PipelineStage] = None,
        score: Optional[LeadScore] = None,
    ) -> Lead:
        lead = self._db.get(Lead, lead_id)
        before = {"collected_fields": dict(lead.collected_fields or {}), "pipeline_stage": lead.pipeline_stage}
        merged = {**(lead.collected_fields or {}), **new_fields}
        lead.collected_fields = merged
        if stage is not None:
            lead.pipeline_stage = stage
        if score is not None:
            lead.lead_score = score
        lead.updated_at = datetime.now(timezone.utc)
        self._db.flush()
        after = {"collected_fields": merged, "pipeline_stage": lead.pipeline_stage}
        self._audit("lead", str(lead_id), "fields_updated", performed_by, before=before, after=after)
        return lead

    def get_lead(self, lead_id: int) -> Optional[Lead]:
        return self._db.get(Lead, lead_id)

    def list_leads(
        self,
        assigned_employee_id: Optional[str] = None,
        source: Optional[LeadSource] = None,
    ) -> list[Lead]:
        q = select(Lead)
        if assigned_employee_id:
            q = q.where(Lead.assigned_employee_id == assigned_employee_id)
        if source is not None:
            q = q.where(Lead.source == source)
        return list(self._db.execute(q).scalars().all())

    # ── Stale-lead follow-up ─────────────────────────────────────────────────────

    def record_customer_message(self, lead_id: int, when: Optional[datetime] = None) -> Optional[Lead]:
        """Stamp when the customer last messaged (drives stale-lead detection).
        High-frequency conversational state — not audited, like pending_meeting_slots."""
        lead = self._db.get(Lead, lead_id)
        if not lead:
            return None
        lead.last_customer_message_at = when or datetime.now(timezone.utc)
        self._db.flush()
        return lead

    def list_stale_candidate_leads(self, stages: tuple[PipelineStage, ...]) -> list[Lead]:
        """Leads still in early (not-yet-qualified/closed) stages that have at least one
        inbound customer message — the population the stale-lead job scans."""
        q = (
            select(Lead)
            .where(Lead.pipeline_stage.in_(stages))
            .where(Lead.last_customer_message_at.isnot(None))
        )
        return list(self._db.execute(q).scalars().all())

    def mark_stale_notified(self, lead_id: int, when: datetime, threshold: str, performed_by: str = "system") -> Optional[Lead]:
        """Record that a staleness notification fired for this lead (dedup marker)."""
        lead = self._db.get(Lead, lead_id)
        if not lead:
            return None
        lead.stale_notified_at = when
        self._db.flush()
        self._audit("lead", str(lead_id), "stale_notified", performed_by, after={"threshold": threshold, "at": when.isoformat()})
        return lead

    # ── Proposal ───────────────────────────────────────────────────────────────

    def create_proposal_draft(
        self,
        lead_id: int,
        content: dict,
        total_value: Optional[float],
        requires_manager_signoff: bool,
        performed_by: str,
        used_fallback_template: bool = False,
    ) -> Proposal:
        max_version = self._db.execute(
            select(func.max(Proposal.version)).where(Proposal.lead_id == lead_id)
        ).scalar_one_or_none() or 0
        version = max_version + 1

        proposal = Proposal(
            lead_id=lead_id,
            version=version,
            status=ProposalStatus.draft,
            content=content,
            total_value=total_value,
            requires_manager_signoff=requires_manager_signoff,
            used_fallback_template=used_fallback_template,
        )
        self._db.add(proposal)
        self._db.flush()

        event_type = TimelineEventType.proposal_v2 if version > 1 else TimelineEventType.proposal_draft_created
        self.add_timeline_event(
            lead_id=lead_id,
            event_type=event_type,
            description=f"Proposal v{version} drafted",
            created_by=performed_by,
        )
        self._audit(
            "proposal", str(proposal.id), "draft_created", performed_by,
            after={
                "version": version,
                "total_value": total_value,
                "requires_manager_signoff": requires_manager_signoff,
                "used_fallback_template": used_fallback_template,
            },
        )
        return proposal

    def transition_proposal_status(
        self,
        proposal_id: int,
        new_status: ProposalStatus,
        performed_by: str,
        approved_by_employee_id: Optional[str] = None,
        revision_reason: Optional[str] = None,
        pdf_url: Optional[str] = None,
        date_sent: Optional[datetime] = None,
    ) -> Proposal:
        proposal = self._db.get(Proposal, proposal_id)
        before = {"status": proposal.status}
        proposal.status = new_status
        if new_status == ProposalStatus.approved and proposal.approved_at is None:
            proposal.approved_at = datetime.now(timezone.utc)
        if approved_by_employee_id:
            proposal.approved_by_employee_id = approved_by_employee_id
        if revision_reason is not None:
            proposal.revision_reason = revision_reason
        if pdf_url:
            proposal.pdf_url = pdf_url
        if date_sent:
            proposal.date_sent = date_sent
        self._db.flush()
        self._audit(
            "proposal", str(proposal_id), f"status_changed_to_{new_status}",
            performed_by, before=before, after={"status": new_status},
        )
        return proposal

    def get_proposal(self, proposal_id: int) -> Optional[Proposal]:
        return self._db.get(Proposal, proposal_id)

    def generate_accept_token(self, proposal_id: int, performed_by: str) -> Optional[str]:
        """Generate and store a customer accept-link token if one doesn't exist yet.
        Returns the token (existing or new). Idempotent."""
        import secrets
        proposal = self._db.get(Proposal, proposal_id)
        if not proposal:
            return None
        if not proposal.accept_token:
            proposal.accept_token = secrets.token_urlsafe(32)
            self._db.flush()
            self._audit("proposal", str(proposal_id), "accept_token_generated", performed_by)
        return proposal.accept_token

    def get_proposal_by_accept_token(self, token: str) -> Optional[Proposal]:
        if not token:
            return None
        return self._db.execute(
            select(Proposal).where(Proposal.accept_token == token)
        ).scalar_one_or_none()

    def mark_customer_accepted(self, proposal_id: int, when: Optional[datetime] = None) -> Proposal:
        """Record a CUSTOMER online acceptance — a distinct concept from internal staff
        approval (which is the two-stage approval gate). Audited as 'customer_accepted'."""
        proposal = self._db.get(Proposal, proposal_id)
        when = when or datetime.now(timezone.utc)
        before = {"status": proposal.status, "accepted_at": proposal.accepted_at.isoformat() if proposal.accepted_at else None}
        proposal.accepted_at = when
        proposal.status = ProposalStatus.accepted
        self._db.flush()
        self._audit(
            "proposal", str(proposal_id), "customer_accepted", "customer",
            before=before, after={"status": ProposalStatus.accepted.value, "accepted_at": when.isoformat()},
        )
        return proposal

    def adjust_proposal_pricing(
        self,
        proposal_id: int,
        new_value: float,
        requires_manager_signoff: bool,
        reason: str,
        performed_by: str,
    ) -> Proposal:
        """Set a proposal's total_value and the RECOMPUTED manager-signoff flag together.

        The flag is passed in already recomputed by the caller (from the Business Rules
        Engine) — this method never leaves total_value and requires_manager_signoff out
        of sync. The reason is recorded in the audit trail.
        """
        proposal = self._db.get(Proposal, proposal_id)
        before = {
            "total_value": proposal.total_value,
            "requires_manager_signoff": proposal.requires_manager_signoff,
        }
        proposal.total_value = new_value
        proposal.requires_manager_signoff = requires_manager_signoff
        self._db.flush()
        self._audit(
            "proposal", str(proposal_id), "pricing_adjusted", performed_by,
            before=before,
            after={
                "total_value": new_value,
                "requires_manager_signoff": requires_manager_signoff,
                "reason": reason,
            },
        )
        return proposal

    def list_proposals(
        self,
        status: Optional[ProposalStatus] = None,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
        lead_id: Optional[int] = None,
        assigned_employee_id: Optional[str] = None,
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[list[Proposal], int]:
        """
        Paginated proposals with lead + customer eagerly loaded for a summary join.
        `assigned_employee_id` scopes the result to a single owner's leads (Sales
        own-view) at the query level — never rely on the caller to filter after.
        Returns (page_of_proposals, total_matching_count).
        """
        base = select(Proposal).join(Lead, Proposal.lead_id == Lead.id)
        if status is not None:
            base = base.where(Proposal.status == status)
        if lead_id is not None:
            base = base.where(Proposal.lead_id == lead_id)
        if assigned_employee_id is not None:
            base = base.where(Lead.assigned_employee_id == assigned_employee_id)
        if date_from is not None:
            base = base.where(Proposal.date_created >= date_from)
        if date_to is not None:
            base = base.where(Proposal.date_created <= date_to)

        total = self._db.execute(
            select(func.count()).select_from(base.subquery())
        ).scalar_one()

        page = max(page, 1)
        page_size = max(1, min(page_size, 200))
        rows = self._db.execute(
            base.options(joinedload(Proposal.lead).joinedload(Lead.customer))
            .order_by(Proposal.date_created.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).unique().scalars().all()
        return list(rows), total

    def get_proposal_detail(self, proposal_id: int) -> Optional[dict]:
        """Full proposal detail plus every version drafted for the same lead."""
        proposal = self._db.get(Proposal, proposal_id)
        if not proposal:
            return None
        lead = self._db.get(Lead, proposal.lead_id)
        customer = self._db.get(Customer, lead.customer_id) if lead else None
        versions = list(
            self._db.execute(
                select(Proposal)
                .where(Proposal.lead_id == proposal.lead_id)
                .order_by(Proposal.version)
            ).scalars()
        )
        return {
            "proposal": proposal,
            "lead": lead,
            "customer": customer,
            "versions": versions,
        }

    # ── Timeline ───────────────────────────────────────────────────────────────

    def add_timeline_event(
        self,
        lead_id: int,
        event_type: TimelineEventType,
        created_by: str,
        description: Optional[str] = None,
    ) -> TimelineEvent:
        event = TimelineEvent(
            lead_id=lead_id,
            event_type=event_type,
            description=description,
            created_by=created_by,
        )
        self._db.add(event)
        self._db.flush()
        return event

    # ── Conversation ───────────────────────────────────────────────────────────

    def log_message(
        self,
        channel: Channel,
        role: MessageRole,
        content: str,
        lead_id: Optional[int] = None,
        customer_id: Optional[int] = None,
        agent_name: Optional[str] = None,
    ) -> ConversationMessage:
        msg = ConversationMessage(
            lead_id=lead_id,
            customer_id=customer_id,
            channel=channel,
            role=role,
            agent_name=agent_name,
            content=content,
        )
        self._db.add(msg)
        self._db.flush()
        return msg

    # ── Context assembly ───────────────────────────────────────────────────────

    def get_lead_context(self, lead_id: int) -> dict:
        lead = self._db.get(Lead, lead_id)
        if not lead:
            return {}
        customer = self._db.get(Customer, lead.customer_id)
        proposals = list(
            self._db.execute(select(Proposal).where(Proposal.lead_id == lead_id)).scalars()
        )
        timeline = list(
            self._db.execute(
                select(TimelineEvent).where(TimelineEvent.lead_id == lead_id)
                .order_by(TimelineEvent.created_at)
            ).scalars()
        )
        messages = list(
            self._db.execute(
                select(ConversationMessage).where(ConversationMessage.lead_id == lead_id)
                .order_by(ConversationMessage.created_at)
            ).scalars()
        )
        return {
            "lead": lead,
            "customer": customer,
            "proposals": proposals,
            "timeline": timeline,
            "messages": messages,
        }

    # ── Employee ───────────────────────────────────────────────────────────────

    def get_employee(self, employee_id: str) -> Optional[Employee]:
        return self._db.get(Employee, employee_id)

    def get_or_create_employee(self, employee_id: str, name: str, email: str, role: str) -> Employee:
        from app.memory.models import EmployeeRole
        emp = self._db.get(Employee, employee_id)
        if emp:
            return emp
        emp = Employee(
            id=employee_id,
            name=name,
            email=email,
            role=EmployeeRole(role),
        )
        self._db.add(emp)
        self._db.flush()
        self._audit("employee", employee_id, "created", "system", after={"name": name, "role": role})
        return emp

    def update_employee_google_credentials(
        self,
        employee_id: str,
        encrypted_refresh_token: Optional[str],
        calendar_id: str,
        performed_by: str,
    ) -> Optional[Employee]:
        """Store the (already-encrypted) Google refresh token and mark the calendar
        connected. The token value is NEVER written to the audit log — only the
        connection state changes are recorded.
        """
        emp = self._db.get(Employee, employee_id)
        if not emp:
            return None
        emp.google_refresh_token = encrypted_refresh_token
        emp.google_calendar_connected = encrypted_refresh_token is not None
        emp.calendar_id = calendar_id
        self._db.flush()
        self._audit(
            "employee", employee_id, "google_calendar_connection_changed", performed_by,
            after={"connected": emp.google_calendar_connected, "calendar_id": calendar_id},
        )
        return emp

    def list_calendar_connected_employees(
        self, roles: Optional[list[EmployeeRole]] = None
    ) -> list[Employee]:
        """Employees who have opted their Google Calendar in — optionally filtered by role."""
        q = select(Employee).where(Employee.google_calendar_connected.is_(True))
        if roles:
            q = q.where(Employee.role.in_(roles))
        return list(self._db.execute(q).scalars().all())

    # ── Lead scheduling state ────────────────────────────────────────────────────

    def set_pending_meeting_slots(self, lead_id: int, slots: Optional[dict]) -> Optional[Lead]:
        """Persist (or clear, with None) the slots most recently offered to a customer."""
        lead = self._db.get(Lead, lead_id)
        if not lead:
            return None
        lead.pending_meeting_slots = slots
        self._db.flush()
        return lead

    # ── Meeting ──────────────────────────────────────────────────────────────────

    def create_meeting(
        self,
        lead_id: int,
        employee_id: str,
        scheduled_at: datetime,
        duration_minutes: int,
        performed_by: str,
        google_event_id: Optional[str] = None,
        meet_link: Optional[str] = None,
    ) -> Meeting:
        meeting = Meeting(
            lead_id=lead_id,
            employee_id=employee_id,
            scheduled_at=scheduled_at,
            duration_minutes=duration_minutes,
            google_event_id=google_event_id,
            meet_link=meet_link,
            status=MeetingStatus.scheduled,
        )
        self._db.add(meeting)
        self._db.flush()
        self._audit(
            "meeting", str(meeting.id), "created", performed_by,
            after={
                "lead_id": lead_id,
                "employee_id": employee_id,
                "scheduled_at": scheduled_at.isoformat(),
                "duration_minutes": duration_minutes,
            },
        )
        return meeting

    def get_meeting(self, meeting_id: int) -> Optional[Meeting]:
        return self._db.get(Meeting, meeting_id)

    def list_meetings_for_lead(self, lead_id: int) -> list[Meeting]:
        return list(
            self._db.execute(
                select(Meeting).where(Meeting.lead_id == lead_id)
                .order_by(Meeting.scheduled_at)
            ).scalars()
        )

    # ── Audit retrieval ────────────────────────────────────────────────────────

    def list_audit_logs(self, entity_type: Optional[str] = None, entity_id: Optional[str] = None) -> list[AuditLog]:
        q = select(AuditLog).order_by(AuditLog.created_at.desc())
        if entity_type:
            q = q.where(AuditLog.entity_type == entity_type)
        if entity_id:
            q = q.where(AuditLog.entity_id == entity_id)
        return list(self._db.execute(q).scalars().all())

    def paginate_audit_logs(
        self,
        entity_type: Optional[str] = None,
        employee_id: Optional[str] = None,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[list[AuditLog], int]:
        """Filtered, paginated audit log for the dashboard. Returns (page, total)."""
        base = select(AuditLog)
        if entity_type:
            base = base.where(AuditLog.entity_type == entity_type)
        if employee_id:
            base = base.where(AuditLog.performed_by == employee_id)
        if date_from is not None:
            base = base.where(AuditLog.created_at >= date_from)
        if date_to is not None:
            base = base.where(AuditLog.created_at <= date_to)

        total = self._db.execute(
            select(func.count()).select_from(base.subquery())
        ).scalar_one()

        page = max(page, 1)
        page_size = max(1, min(page_size, 200))
        rows = self._db.execute(
            base.order_by(AuditLog.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).scalars().all()
        return list(rows), total

    # ── Employees ──────────────────────────────────────────────────────────────

    def list_employees(self) -> list[Employee]:
        return list(
            self._db.execute(select(Employee).order_by(Employee.created_at)).scalars()
        )

    def update_employee_role(
        self, employee_id: str, role: "EmployeeRole", performed_by: str
    ) -> Optional[Employee]:
        """Update the locally mirrored role. Clerk remains the source of truth —
        the route syncs Clerk's public_metadata separately. Returns None if the
        employee has never been mirrored locally yet."""
        emp = self._db.get(Employee, employee_id)
        if not emp:
            return None
        before = {"role": emp.role}
        emp.role = role
        self._db.flush()
        self._audit(
            "employee", employee_id, "role_changed", performed_by,
            before=before, after={"role": role},
        )
        return emp

    # ── Analytics ──────────────────────────────────────────────────────────────

    def compute_analytics(
        self,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
        employee_id: Optional[str] = None,
    ) -> dict:
        """
        Aggregate pipeline + AI-usage metrics over a window. When `employee_id`
        is set (Sales own-metrics view) every metric is scoped to that owner's
        leads at the query level.
        """
        from app.config import settings

        def _lead_scope(q):
            if date_from is not None:
                q = q.where(Lead.created_at >= date_from)
            if date_to is not None:
                q = q.where(Lead.created_at <= date_to)
            if employee_id is not None:
                q = q.where(Lead.assigned_employee_id == employee_id)
            return q

        stage = PipelineStage
        qualified_stages = (
            stage.qualified, stage.proposal_draft, stage.proposal_sent,
            stage.negotiation, stage.won,
        )
        sent_stages = (stage.proposal_sent, stage.negotiation, stage.won)

        def _count(*conditions) -> int:
            q = _lead_scope(select(func.count(Lead.id)))
            for c in conditions:
                q = q.where(c)
            return int(self._db.execute(q).scalar_one() or 0)

        new_leads = _count()
        qualified = _count(Lead.pipeline_stage.in_(qualified_stages))
        proposal_sent = _count(Lead.pipeline_stage.in_(sent_stages))
        won = _count(Lead.pipeline_stage == stage.won)

        # Leads that produced at least one proposal (conversion).
        conv_q = _lead_scope(
            select(func.count(distinct(Lead.id))).join(Proposal, Proposal.lead_id == Lead.id)
        )
        leads_with_proposal = int(self._db.execute(conv_q).scalar_one() or 0)

        # Turnaround: hours from proposal draft creation to send, scoped by lead.
        turn_q = (
            select(Proposal.date_created, Proposal.date_sent)
            .join(Lead, Proposal.lead_id == Lead.id)
            .where(Proposal.date_sent.isnot(None))
        )
        if date_from is not None:
            turn_q = turn_q.where(Lead.created_at >= date_from)
        if date_to is not None:
            turn_q = turn_q.where(Lead.created_at <= date_to)
        if employee_id is not None:
            turn_q = turn_q.where(Lead.assigned_employee_id == employee_id)
        deltas = [
            (sent - created).total_seconds() / 3600.0
            for created, sent in self._db.execute(turn_q).all()
            if created is not None and sent is not None
        ]
        avg_turnaround = round(sum(deltas) / len(deltas), 2) if deltas else None

        # AI usage: each agent reply ≈ one AI call.
        ai_q = (
            select(func.count(ConversationMessage.id))
            .join(Lead, ConversationMessage.lead_id == Lead.id)
            .where(ConversationMessage.role == MessageRole.agent)
        )
        if date_from is not None:
            ai_q = ai_q.where(ConversationMessage.created_at >= date_from)
        if date_to is not None:
            ai_q = ai_q.where(ConversationMessage.created_at <= date_to)
        if employee_id is not None:
            ai_q = ai_q.where(Lead.assigned_employee_id == employee_id)
        ai_call_count = int(self._db.execute(ai_q).scalar_one() or 0)

        # Escalations: leads with at least one escalated timeline event.
        esc_q = (
            select(func.count(distinct(TimelineEvent.lead_id)))
            .join(Lead, TimelineEvent.lead_id == Lead.id)
            .where(TimelineEvent.event_type == TimelineEventType.escalated)
        )
        if date_from is not None:
            esc_q = esc_q.where(Lead.created_at >= date_from)
        if date_to is not None:
            esc_q = esc_q.where(Lead.created_at <= date_to)
        if employee_id is not None:
            esc_q = esc_q.where(Lead.assigned_employee_id == employee_id)
        escalated_leads = int(self._db.execute(esc_q).scalar_one() or 0)

        def _rate(num: int, den: int) -> float:
            return round(num / den, 4) if den else 0.0

        return {
            "new_leads_count": new_leads,
            "qualification_completion_rate": _rate(qualified, new_leads),
            "lead_to_proposal_conversion_rate": _rate(leads_with_proposal, new_leads),
            "avg_proposal_turnaround_hours": avg_turnaround,
            "funnel": {
                "new": new_leads,
                "qualified": qualified,
                "proposal_sent": proposal_sent,
                "won": won,
            },
            "ai_call_count": ai_call_count,
            "ai_estimated_cost": round(ai_call_count * settings.ai_estimated_cost_per_call, 4),
            "escalation_rate": _rate(escalated_leads, new_leads),
        }

    # ── Reporting analytics ──────────────────────────────────────────────────────

    def analytics_conversion_by_source(self) -> list[dict]:
        """Conversion rate (leads → won) grouped by Lead.source."""
        total_by_source = dict(
            self._db.execute(select(Lead.source, func.count(Lead.id)).group_by(Lead.source)).all()
        )
        won_by_source = dict(
            self._db.execute(
                select(Lead.source, func.count(Lead.id))
                .where(Lead.pipeline_stage == PipelineStage.won)
                .group_by(Lead.source)
            ).all()
        )
        rows = []
        for source, total in sorted(total_by_source.items(), key=lambda kv: kv[0].value):
            won = won_by_source.get(source, 0)
            rows.append({
                "source": source.value,
                "total_leads": total,
                "won_leads": won,
                "conversion_rate": round(won / total, 4) if total else 0.0,
            })
        return rows

    def analytics_deal_size_by_tier(self, cfg: dict) -> list[dict]:
        """Average and total proposal value grouped by capacity tier (tiers read from
        config/proposal_content.yaml — never hardcoded here)."""
        tiers = (cfg.get("pricing") or {}).get("tiers") or []

        def _label(t: dict) -> str:
            hi = t.get("capacity_max_kw")
            lo = int(t.get("capacity_min_kw", 0) or 0)
            return f"{t.get('project_type')} {lo}-{'+' if hi is None else int(hi)}kW"

        buckets: dict[str, dict] = {_label(t): {"total_value": 0.0, "count": 0} for t in tiers}
        buckets["unclassified"] = {"total_value": 0.0, "count": 0}

        rows = self._db.execute(select(Proposal, Lead).join(Lead, Proposal.lead_id == Lead.id)).all()
        for proposal, lead in rows:
            if proposal.total_value is None:
                continue
            collected = lead.collected_fields or {}
            project_type = collected.get("project_type")
            try:
                capacity = float(collected.get("capacity_kw"))
            except (TypeError, ValueError):
                capacity = None
            matched = None
            if capacity is not None:
                for t in tiers:
                    if t.get("project_type") != project_type:
                        continue
                    lo = float(t.get("capacity_min_kw", 0) or 0)
                    hi = t.get("capacity_max_kw")
                    hi = float("inf") if hi is None else float(hi)
                    if lo <= capacity < hi:
                        matched = _label(t)
                        break
            key = matched or "unclassified"
            buckets[key]["total_value"] += proposal.total_value
            buckets[key]["count"] += 1

        return [
            {
                "tier": label,
                "proposal_count": v["count"],
                "total_value": round(v["total_value"], 2),
                "average_value": round(v["total_value"] / v["count"], 2) if v["count"] else None,
            }
            for label, v in buckets.items()
        ]

    def analytics_rep_performance(self) -> list[dict]:
        """Per-employee lead/proposal counts + average time-to-close (won proposal's
        approved_at minus the lead's created_at)."""
        result = []
        for emp in self.list_employees():
            leads = list(self._db.execute(select(Lead).where(Lead.assigned_employee_id == emp.id)).scalars())
            lead_by_id = {l.id: l for l in leads}
            won_lead_ids = {l.id for l in leads if l.pipeline_stage == PipelineStage.won}
            proposals = (
                list(self._db.execute(select(Proposal).where(Proposal.lead_id.in_(list(lead_by_id)))).scalars())
                if lead_by_id else []
            )
            won_props = [p for p in proposals if p.lead_id in won_lead_ids]
            deltas = []
            for p in won_props:
                if p.approved_at is not None:
                    lead = lead_by_id[p.lead_id]
                    deltas.append(
                        (_as_utc(p.approved_at) - _as_utc(lead.created_at)).total_seconds() / 3600.0
                    )
            result.append({
                "employee_id": emp.id,
                "name": emp.name,
                "leads_handled": len(leads),
                "proposals_drafted": len(proposals),
                "proposals_won": len(won_props),
                "avg_time_to_close_hours": round(sum(deltas) / len(deltas), 2) if deltas else None,
            })
        return result

    def analytics_pipeline_trend(self, weeks: int = 12, now: Optional[datetime] = None) -> list[dict]:
        """Leads created and proposals won (approved), grouped by ISO week, last N weeks."""
        now = now or datetime.now(timezone.utc)
        today = now.date()
        this_monday = today - timedelta(days=today.weekday())
        week_starts = [this_monday - timedelta(weeks=i) for i in range(weeks - 1, -1, -1)]
        window_start = datetime.combine(week_starts[0], datetime.min.time(), tzinfo=timezone.utc)

        leads = list(self._db.execute(select(Lead).where(Lead.created_at >= window_start)).scalars())
        won_props = list(
            self._db.execute(
                select(Proposal).where(Proposal.approved_at.isnot(None), Proposal.approved_at >= window_start)
            ).scalars()
        )

        def _week_of(dt: datetime):
            d = _as_utc(dt).date()
            return d - timedelta(days=d.weekday())

        leads_by_week: dict = {}
        for l in leads:
            leads_by_week[_week_of(l.created_at)] = leads_by_week.get(_week_of(l.created_at), 0) + 1
        won_by_week: dict = {}
        for p in won_props:
            won_by_week[_week_of(p.approved_at)] = won_by_week.get(_week_of(p.approved_at), 0) + 1

        return [
            {
                "week_start": ws.isoformat(),
                "leads_created": leads_by_week.get(ws, 0),
                "proposals_won": won_by_week.get(ws, 0),
            }
            for ws in week_starts
        ]

    # ── Dashboard companion aggregates ───────────────────────────────────────────

    def list_recent_activity(
        self, limit: int = 15, exclude_actions: Optional[tuple[str, ...]] = None
    ) -> list[AuditLog]:
        """Most recent audit entries, newest first. `exclude_actions` drops entries
        whose action is in the set (used to hide pricing entries from engineers)."""
        q = select(AuditLog).order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        if exclude_actions:
            q = q.where(AuditLog.action.notin_(exclude_actions))
        return list(self._db.execute(q.limit(limit)).scalars())

    def list_proposals_for_export(
        self,
        status: Optional[ProposalStatus] = None,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
        lead_id: Optional[int] = None,
    ) -> list[Proposal]:
        """All proposals matching the same filters as list_proposals, unpaginated,
        with lead + customer eagerly loaded — for CSV export."""
        q = select(Proposal).join(Lead, Proposal.lead_id == Lead.id)
        if status is not None:
            q = q.where(Proposal.status == status)
        if lead_id is not None:
            q = q.where(Proposal.lead_id == lead_id)
        if date_from is not None:
            q = q.where(Proposal.date_created >= date_from)
        if date_to is not None:
            q = q.where(Proposal.date_created <= date_to)
        q = q.options(joinedload(Proposal.lead).joinedload(Lead.customer)).order_by(
            Proposal.date_created.desc()
        )
        return list(self._db.execute(q).unique().scalars())

    def compute_overview_stats(self, now: Optional[datetime] = None) -> dict:
        """The Overview page's four headline counts, each with a `trend` value = the
        same count for the prior day, so the frontend can render a delta/direction."""
        now = now or datetime.now(timezone.utc)
        today_start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
        yesterday_start = today_start - timedelta(days=1)
        day_ago = now - timedelta(days=1)

        pending_states = (
            ProposalStatus.draft,
            ProposalStatus.pending_pricing_validation,
            ProposalStatus.pending_approval,
        )

        def _count_leads_created(start: datetime, end: Optional[datetime] = None) -> int:
            q = select(func.count(Lead.id)).where(Lead.created_at >= start)
            if end is not None:
                q = q.where(Lead.created_at < end)
            return int(self._db.execute(q).scalar_one() or 0)

        def _count_followups_due(as_of: datetime) -> int:
            return int(self._db.execute(
                select(func.count(Lead.id))
                .where(Lead.follow_up_date.isnot(None), Lead.follow_up_date <= as_of)
            ).scalar_one() or 0)

        def _count_pending(signoff: bool, created_before: Optional[datetime] = None) -> int:
            q = select(func.count(Proposal.id)).where(
                Proposal.status.in_(pending_states),
                Proposal.requires_manager_signoff.is_(signoff),
            )
            if created_before is not None:
                q = q.where(Proposal.date_created < created_before)
            return int(self._db.execute(q).scalar_one() or 0)

        return {
            "new_leads_today": _count_leads_created(today_start),
            "follow_ups_due": _count_followups_due(now),
            "pending_approval": _count_pending(False),
            "pending_manager_signoff": _count_pending(True),
            "trends": {
                # New leads: the prior full day. State counts: the same metric as it
                # stood ~24h ago (entities that already existed by the day boundary).
                "new_leads_today": _count_leads_created(yesterday_start, today_start),
                "follow_ups_due": _count_followups_due(day_ago),
                "pending_approval": _count_pending(False, created_before=today_start),
                "pending_manager_signoff": _count_pending(True, created_before=today_start),
            },
        }
