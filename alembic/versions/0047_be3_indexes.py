"""BE-3 P2: indexes for what the API actually filters, joins and sorts on.

PostgreSQL does not index a foreign key by itself, so every lookup of a customer's leads,
a lead's proposals / timeline / messages / meetings, a Sales employee's leads, and the
audit trail of one entity was a sequential scan. Each index below backs a real query
(file references in the BE-3 report):

  leads(customer_id)                          customer → leads (merge, dossier, audit visibility)
  leads(assigned_employee_id)                 every Sales-scoped list (app/auth/scoping.py)
  leads(pipeline_stage, created_at)           stage-filtered lists ordered newest first; stale-lead scan
  proposals(lead_id, status)                  a lead's proposals / its accepted one
  proposals(date_created)                     proposal lists and exports by date
  audit_logs(entity_type, entity_id)          one entity's trail; last-activity per lead
  audit_logs(created_at)                      the recent-activity feed
  timeline_events(lead_id, created_at)        a lead's timeline; last-activity per lead
  conversation_messages(lead_id, created_at)  a conversation in order
  meetings(lead_id)                           a lead's meetings
  meetings(scheduled_at)                      calendar / availability windows

On PostgreSQL each index is built with CREATE INDEX CONCURRENTLY (outside a transaction),
so the tables stay writable while the deploy's migration job runs. IF NOT EXISTS makes a
re-run safe. If a concurrent build is interrupted it leaves an INVALID index behind:
drop it (DROP INDEX CONCURRENTLY <name>) and run the job again.

Downgrade drops the indexes (the same way); no data changes either way.

Revision ID: 0047_be3_indexes
Revises: 0046_survey_review
Create Date: 2026-10-12
"""
from __future__ import annotations

from alembic import op

revision = "0047_be3_indexes"
down_revision = "0046_survey_review"
branch_labels = None
depends_on = None

INDEXES = (
    ("ix_leads_customer_id", "leads", ["customer_id"]),
    ("ix_leads_assigned_employee_id", "leads", ["assigned_employee_id"]),
    ("ix_leads_pipeline_stage_created_at", "leads", ["pipeline_stage", "created_at"]),
    ("ix_proposals_lead_id_status", "proposals", ["lead_id", "status"]),
    ("ix_proposals_date_created", "proposals", ["date_created"]),
    ("ix_audit_logs_entity_type_entity_id", "audit_logs", ["entity_type", "entity_id"]),
    ("ix_audit_logs_created_at", "audit_logs", ["created_at"]),
    ("ix_timeline_events_lead_id_created_at", "timeline_events", ["lead_id", "created_at"]),
    ("ix_conversation_messages_lead_id_created_at", "conversation_messages", ["lead_id", "created_at"]),
    ("ix_meetings_lead_id", "meetings", ["lead_id"]),
    ("ix_meetings_scheduled_at", "meetings", ["scheduled_at"]),
)


def _postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    if _postgres():
        with op.get_context().autocommit_block():
            for name, table, columns in INDEXES:
                op.create_index(name, table, columns, postgresql_concurrently=True, if_not_exists=True)
    else:
        for name, table, columns in INDEXES:
            op.create_index(name, table, columns, if_not_exists=True)


def downgrade() -> None:
    if _postgres():
        with op.get_context().autocommit_block():
            for name, table, _ in reversed(INDEXES):
                op.drop_index(name, table_name=table, postgresql_concurrently=True, if_exists=True)
    else:
        for name, table, _ in reversed(INDEXES):
            op.drop_index(name, table_name=table, if_exists=True)
