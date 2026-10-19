"""BE-5 (Phase 6A): handover pack, warranty register and service requests.

  * handover_packs — one per project: the three manual checklist items, the customer's
    link (SHA-256 of the token, revocable) and the rendered handover PDF.
  * project_warranties / warranty_items — the warranty register, written when
    `handover_complete` is recorded.
  * service_requests / service_request_events — warranty / O&M requests; the events table
    is append-only (a trigger refuses UPDATE and DELETE).

Downgrade drops the five tables, the trigger function and the two enum types (service
request history is lost — a development/staging downgrade).

Revision ID: 0049_handover_warranty
Revises: 0048_commissions
Create Date: 2026-10-13
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.memory.models import SERVICE_EVENTS_PG_FUNCTION, SERVICE_EVENTS_PG_TRIGGER, SERVICE_EVENTS_SQLITE_TRIGGERS

revision = "0049_handover_warranty"
down_revision = "0048_commissions"
branch_labels = None
depends_on = None

_ENUMS = {
    "servicerequestcategory": ("no_generation", "inverter_error", "physical_damage", "cleaning_om", "other"),
    "servicerequeststatus": ("open", "in_progress", "resolved", "rejected"),
}


def _enum(name: str, pg: bool):
    values = _ENUMS[name]
    if pg:
        return sa.Enum(*values, name=name).with_variant(postgresql.ENUM(*values, name=name, create_type=False),
                                                         "postgresql")
    return sa.Enum(*values, name=name)


def _dt():
    return sa.DateTime(timezone=True)


def upgrade() -> None:
    bind = op.get_bind()
    pg = bind.dialect.name == "postgresql"
    if pg:
        for name, values in _ENUMS.items():
            postgresql.ENUM(*values, name=name).create(bind, checkfirst=True)

    op.create_table(
        "handover_packs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id"), nullable=False, unique=True),
        sa.Column("onboarding_id", sa.Integer(), sa.ForeignKey("customer_onboardings.id"), nullable=False, unique=True),
        sa.Column("created_at", _dt(), nullable=False),
        sa.Column("orientation_done_at", _dt(), nullable=True),
        sa.Column("orientation_done_by", sa.String(), nullable=True),
        sa.Column("orientation_note", sa.Text(), nullable=True),
        sa.Column("ack_method", sa.String(length=16), nullable=True),
        sa.Column("acknowledged_at", _dt(), nullable=True),
        sa.Column("acknowledged_by", sa.String(), nullable=True),
        sa.Column("ack_customer_name", sa.String(length=200), nullable=True),
        sa.Column("ack_signature_ref", sa.JSON(), nullable=True),
        sa.Column("ack_note", sa.Text(), nullable=True),
        sa.Column("om_contact_given_at", _dt(), nullable=True),
        sa.Column("om_contact_given_by", sa.String(), nullable=True),
        sa.Column("om_contact_note", sa.Text(), nullable=True),
        sa.Column("link_token_hash", sa.String(length=64), nullable=True, unique=True),
        sa.Column("link_issued_at", _dt(), nullable=True),
        sa.Column("link_issued_by", sa.String(), nullable=True),
        sa.Column("link_expires_at", _dt(), nullable=True),
        sa.Column("link_revoked_at", _dt(), nullable=True),
        sa.Column("link_revoked_by", sa.String(), nullable=True),
        sa.Column("pdf_key", sa.String(), nullable=True),
        sa.Column("pdf_generated_at", _dt(), nullable=True),
        sa.Column("render_error", sa.Text(), nullable=True),
        sa.Column("held_reason", sa.String(), nullable=True),
        sa.Column("email_status", sa.String(length=16), nullable=True),
        sa.Column("email_skipped_reason", sa.String(), nullable=True),
        sa.Column("emailed_at", _dt(), nullable=True),
    )
    op.create_table(
        "project_warranties",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id"), nullable=False, unique=True),
        sa.Column("onboarding_id", sa.Integer(), sa.ForeignKey("customer_onboardings.id"), nullable=False, unique=True),
        sa.Column("handover_date", sa.Date(), nullable=False),
        sa.Column("system_warranty_years", sa.Integer(), nullable=False),
        sa.Column("system_warranty_end", sa.Date(), nullable=False),
        sa.Column("panel_performance_life_years", sa.Integer(), nullable=True),
        sa.Column("om_free_months", sa.Integer(), nullable=False),
        sa.Column("om_free_until", sa.Date(), nullable=False),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column("created_at", _dt(), nullable=False),
    )
    op.create_table(
        "warranty_items",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("warranty_id", sa.Integer(), sa.ForeignKey("project_warranties.id"), nullable=False),
        sa.Column("unit_id", sa.Integer(), sa.ForeignKey("installed_units.id"), nullable=False),
        sa.Column("category", sa.String(length=16), nullable=False),
        sa.Column("warranty_start", sa.Date(), nullable=True),
        sa.Column("warranty_end", sa.Date(), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.UniqueConstraint("warranty_id", "unit_id", name="uq_warranty_items_unit"),
    )
    op.create_index("ix_warranty_items_warranty_id", "warranty_items", ["warranty_id"])
    op.create_table(
        "service_requests",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("onboarding_id", sa.Integer(), sa.ForeignKey("customer_onboardings.id"), nullable=False),
        sa.Column("category", _enum("servicerequestcategory", pg), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("status", _enum("servicerequeststatus", pg), nullable=False),
        sa.Column("raised_via", sa.String(length=16), nullable=False),
        sa.Column("raised_by", sa.String(), nullable=False),
        sa.Column("contact_phone", sa.String(length=32), nullable=True),
        sa.Column("within_warranty", sa.Boolean(), nullable=True),
        sa.Column("warranty_end", sa.Date(), nullable=True),
        sa.Column("assignee_id", sa.String(), sa.ForeignKey("employees.id"), nullable=True),
        sa.Column("photos", sa.JSON(), nullable=False),
        sa.Column("sla_due_at", _dt(), nullable=True),
        sa.Column("overdue_alerted_at", _dt(), nullable=True),
        sa.Column("resolution_note", sa.Text(), nullable=True),
        sa.Column("created_at", _dt(), nullable=False),
        sa.Column("updated_at", _dt(), nullable=True),
        sa.Column("closed_at", _dt(), nullable=True),
    )
    op.create_index("ix_service_requests_project_id", "service_requests", ["project_id"])
    op.create_index("ix_service_requests_onboarding_id", "service_requests", ["onboarding_id"])
    op.create_index("ix_service_requests_assignee_id", "service_requests", ["assignee_id"])
    op.create_index("ix_service_requests_status", "service_requests", ["status"])
    op.create_table(
        "service_request_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("request_id", sa.Integer(), sa.ForeignKey("service_requests.id"), nullable=False),
        sa.Column("kind", sa.String(length=24), nullable=False),
        sa.Column("actor", sa.String(), nullable=False),
        sa.Column("from_status", sa.String(length=16), nullable=True),
        sa.Column("to_status", sa.String(length=16), nullable=True),
        sa.Column("detail", sa.JSON(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("at", _dt(), nullable=False),
    )
    op.create_index("ix_service_request_events_request_id", "service_request_events", ["request_id"])
    if pg:
        op.execute(SERVICE_EVENTS_PG_FUNCTION)
        op.execute(SERVICE_EVENTS_PG_TRIGGER)
    else:
        for sql in SERVICE_EVENTS_SQLITE_TRIGGERS:
            op.execute(sql)


def downgrade() -> None:
    bind = op.get_bind()
    pg = bind.dialect.name == "postgresql"
    op.drop_index("ix_service_request_events_request_id", table_name="service_request_events")
    op.drop_table("service_request_events")   # its triggers go with it
    for name in ("ix_service_requests_status", "ix_service_requests_assignee_id",
                 "ix_service_requests_onboarding_id", "ix_service_requests_project_id"):
        op.drop_index(name, table_name="service_requests")
    op.drop_table("service_requests")
    op.drop_index("ix_warranty_items_warranty_id", table_name="warranty_items")
    op.drop_table("warranty_items")
    op.drop_table("project_warranties")
    op.drop_table("handover_packs")
    if pg:
        op.execute("DROP FUNCTION IF EXISTS service_request_events_append_only()")
        for name in _ENUMS:
            op.execute(f"DROP TYPE IF EXISTS {name}")
