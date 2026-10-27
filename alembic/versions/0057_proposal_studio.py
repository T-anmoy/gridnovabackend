"""BE-18: Proposal Studio — customers, proposals, versions, events and the library, and the
Proposal Desk role.

  * employeerole gains 'proposal_desk'; leadsource gains 'proposal_studio' (PostgreSQL: ADD
    VALUE; SQLite stores them as text).
  * studio_customers, studio_proposals (code SP-000123, owner, type, status, lead_id once
    converted), studio_proposal_versions (the document, reference, PDF hash, verification
    code, hashed access token, approval, acceptance), studio_events (timeline and analytics),
    studio_library_items (notes, scope items, BOQ presets, templates).
  * The Studio draws its GN/DD/MM/YYYY/NN references from proposal_reference_sequences, the
    same counter as lead proposals (0055), so a reference is never issued twice;
    uq_studio_versions_reference is the wall on this side.

Downgrade drops the five tables. The two enum values cannot be dropped from the PostgreSQL
types without rebuilding them and are left in place, as every earlier enum-extending revision
does; a downgrade refuses while anyone holds the Proposal Desk role or a lead came from the
Studio.

Revision ID: 0057_proposal_studio
Revises: 0056_access_control
Create Date: 2026-10-27
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0057_proposal_studio"
down_revision = "0056_access_control"
branch_labels = None
depends_on = None

_STATUSES = ("draft", "pending_approval", "ready", "issued", "viewed", "accepted", "declined", "expired",
             "superseded", "converted", "lost")


def _dt():
    return sa.DateTime(timezone=True)


def _status():
    return sa.Enum(*_STATUSES, name="studiostatus", native_enum=False, length=24)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute("ALTER TYPE employeerole ADD VALUE IF NOT EXISTS 'proposal_desk'")
            op.execute("ALTER TYPE leadsource ADD VALUE IF NOT EXISTS 'proposal_studio'")

    op.create_table(
        "studio_customers",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("customer_type", sa.String(length=16), nullable=False),
        sa.Column("title", sa.String(length=8), nullable=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("attention", sa.String(length=200), nullable=True),
        sa.Column("mobile", sa.String(length=10), nullable=False),
        sa.Column("email", sa.String(length=254), nullable=True),
        sa.Column("address_lines", sa.JSON(), nullable=False),
        sa.Column("gstin", sa.String(length=15), nullable=True),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column("created_at", _dt(), nullable=False),
        sa.Column("updated_at", _dt(), nullable=True),
    )
    op.create_index("ix_studio_customers_mobile", "studio_customers", ["mobile"])
    op.create_index("ix_studio_customers_gstin", "studio_customers", ["gstin"])
    op.create_index("ix_studio_customers_name", "studio_customers", ["name"])

    op.create_table(
        "studio_proposals",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("code", sa.String(length=16), nullable=True, unique=True),
        sa.Column("customer_id", sa.Integer(), sa.ForeignKey("studio_customers.id"), nullable=False),
        sa.Column("owner_id", sa.String(), sa.ForeignKey("employees.id"), nullable=False),
        sa.Column("project_type", sa.String(length=32), nullable=False),
        sa.Column("status", _status(), nullable=False),
        sa.Column("current_version", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=240), nullable=False),
        sa.Column("lead_id", sa.Integer(), sa.ForeignKey("leads.id"), nullable=True, unique=True),
        sa.Column("lost_reason", sa.Text(), nullable=True),
        sa.Column("created_at", _dt(), nullable=False),
        sa.Column("updated_at", _dt(), nullable=False),
    )
    op.create_index("ix_studio_proposals_status_updated", "studio_proposals", ["status", "updated_at"])
    op.create_index("ix_studio_proposals_owner", "studio_proposals", ["owner_id"])
    op.create_index("ix_studio_proposals_customer", "studio_proposals", ["customer_id"])

    op.create_table(
        "studio_proposal_versions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("proposal_id", sa.Integer(), sa.ForeignKey("studio_proposals.id"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", _status(), nullable=False),
        sa.Column("document", sa.JSON(), nullable=False),
        sa.Column("total", sa.Numeric(14, 2), nullable=True),
        sa.Column("figures", sa.JSON(), nullable=True),
        sa.Column("document_reference", sa.String(length=40), nullable=True),
        sa.Column("document_date", sa.Date(), nullable=True),
        sa.Column("verification_code", sa.String(length=16), nullable=False, unique=True),
        sa.Column("token_hash", sa.String(length=64), nullable=True, unique=True),
        sa.Column("token_encrypted", sa.Text(), nullable=True),
        sa.Column("pdf_storage_key", sa.String(), nullable=True),
        sa.Column("pdf_sha256", sa.String(length=64), nullable=True),
        sa.Column("pdf_filename", sa.String(length=200), nullable=True),
        sa.Column("submitted_by", sa.String(), nullable=True),
        sa.Column("submitted_at", _dt(), nullable=True),
        sa.Column("approved_by", sa.String(), nullable=True),
        sa.Column("approved_at", _dt(), nullable=True),
        sa.Column("approval_revision", sa.Integer(), nullable=True),
        sa.Column("approval_comment", sa.Text(), nullable=True),
        sa.Column("issued_by", sa.String(), nullable=True),
        sa.Column("issued_at", _dt(), nullable=True),
        sa.Column("valid_until", sa.Date(), nullable=True),
        sa.Column("supersedes_version", sa.Integer(), nullable=True),
        sa.Column("change_summary", sa.JSON(), nullable=True),
        sa.Column("locked", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("acknowledged", sa.JSON(), nullable=True),
        sa.Column("spell_ignore", sa.JSON(), nullable=True),
        sa.Column("first_viewed_at", _dt(), nullable=True),
        sa.Column("view_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("accepted_at", _dt(), nullable=True),
        sa.Column("accepted_via", sa.String(length=16), nullable=True),
        sa.Column("accepted_name", sa.String(length=200), nullable=True),
        sa.Column("accepted_ip", sa.String(length=64), nullable=True),
        sa.Column("accepted_user_agent", sa.String(length=400), nullable=True),
        sa.Column("accepted_pdf_sha256", sa.String(length=64), nullable=True),
        sa.Column("signed_copy_key", sa.String(), nullable=True),
        sa.Column("signed_copy_sha256", sa.String(length=64), nullable=True),
        sa.Column("declined_at", _dt(), nullable=True),
        sa.Column("decline_reason", sa.Text(), nullable=True),
        sa.Column("reminders_sent", sa.JSON(), nullable=True),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column("created_at", _dt(), nullable=False),
        sa.Column("updated_at", _dt(), nullable=False),
        sa.UniqueConstraint("proposal_id", "version", name="uq_studio_versions_proposal_version"),
    )
    op.create_index("uq_studio_versions_reference", "studio_proposal_versions", ["document_reference"], unique=True)
    op.create_index("ix_studio_versions_status_valid", "studio_proposal_versions", ["status", "valid_until"])

    op.create_table(
        "studio_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("proposal_id", sa.Integer(), sa.ForeignKey("studio_proposals.id"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=True),
        sa.Column("type", sa.String(length=40), nullable=False),
        sa.Column("actor_type", sa.String(length=16), nullable=False),
        sa.Column("actor", sa.String(), nullable=True),
        sa.Column("detail", sa.JSON(), nullable=True),
        sa.Column("ip", sa.String(length=64), nullable=True),
        sa.Column("user_agent", sa.String(length=400), nullable=True),
        sa.Column("at", _dt(), nullable=False),
    )
    op.create_index("ix_studio_events_proposal_at", "studio_events", ["proposal_id", "at"])
    op.create_index("ix_studio_events_type_at", "studio_events", ["type", "at"])

    op.create_table(
        "studio_library_items",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("kind", sa.String(length=24), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("project_type", sa.String(length=32), nullable=True),
        sa.Column("body", sa.JSON(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column("created_at", _dt(), nullable=False),
        sa.Column("updated_at", _dt(), nullable=True),
    )
    op.create_index("ix_studio_library_kind_active", "studio_library_items", ["kind", "active"])


def downgrade() -> None:
    bind = op.get_bind()
    held = bind.execute(sa.text(
        "SELECT COUNT(*) FROM employees WHERE CAST(role AS TEXT) = 'proposal_desk'")).scalar()
    held += bind.execute(sa.text("SELECT COUNT(*) FROM employee_roles WHERE role = 'proposal_desk'")).scalar()
    if held:
        raise RuntimeError(f"{held} role grant(s) of proposal_desk exist; remove them first.")
    lead_columns = {c["name"] for c in sa.inspect(bind).get_columns("leads")}
    converted = bind.execute(sa.text(
        "SELECT COUNT(*) FROM leads WHERE CAST(source AS TEXT) = 'proposal_studio'")).scalar() \
        if "source" in lead_columns else 0
    if converted:
        raise RuntimeError(f"{converted} lead(s) came from the Proposal Studio; the downgrade would orphan them.")
    op.drop_index("ix_studio_library_kind_active", table_name="studio_library_items")
    op.drop_table("studio_library_items")
    op.drop_index("ix_studio_events_type_at", table_name="studio_events")
    op.drop_index("ix_studio_events_proposal_at", table_name="studio_events")
    op.drop_table("studio_events")
    op.drop_index("ix_studio_versions_status_valid", table_name="studio_proposal_versions")
    op.drop_index("uq_studio_versions_reference", table_name="studio_proposal_versions")
    op.drop_table("studio_proposal_versions")
    op.drop_index("ix_studio_proposals_customer", table_name="studio_proposals")
    op.drop_index("ix_studio_proposals_owner", table_name="studio_proposals")
    op.drop_index("ix_studio_proposals_status_updated", table_name="studio_proposals")
    op.drop_table("studio_proposals")
    op.drop_index("ix_studio_customers_name", table_name="studio_customers")
    op.drop_index("ix_studio_customers_gstin", table_name="studio_customers")
    op.drop_index("ix_studio_customers_mobile", table_name="studio_customers")
    op.drop_table("studio_customers")
