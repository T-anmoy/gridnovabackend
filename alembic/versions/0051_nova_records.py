"""BE-9: Nova Records — the staff assistant's conversations.

  * nova_conversations — one per staff conversation (random hex id, owner, title).
  * nova_turns — one question and its answer: status, the API response as returned, the
    tool calls and refs behind it, the verifier's result, model, tokens, latency, and
    the employee's feedback.

Rows older than config/nova_records.yaml retention.days are deleted by the retention
job. Downgrade drops both tables (conversation history is lost — a development/staging
downgrade).

Revision ID: 0051_nova_records
Revises: 0050_project_archive
Create Date: 2026-10-17
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0051_nova_records"
down_revision = "0050_project_archive"
branch_labels = None
depends_on = None


def _dt():
    return sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "nova_conversations",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("employee_id", sa.String(), sa.ForeignKey("employees.id"), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("title", sa.String(length=120), nullable=False),
        sa.Column("turn_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", _dt(), nullable=False),
        sa.Column("updated_at", _dt(), nullable=False),
    )
    op.create_index("ix_nova_conversations_employee_updated", "nova_conversations", ["employee_id", "updated_at"])

    op.create_table(
        "nova_turns",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("conversation_id", sa.String(length=32), sa.ForeignKey("nova_conversations.id"), nullable=False),
        sa.Column("employee_id", sa.String(), sa.ForeignKey("employees.id"), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("context", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("answer", sa.Text(), nullable=False),
        sa.Column("response", sa.JSON(), nullable=False),
        sa.Column("intent", sa.String(length=64), nullable=True),
        sa.Column("planner", sa.String(length=16), nullable=True),
        sa.Column("composer", sa.String(length=16), nullable=True),
        sa.Column("tool_calls", sa.JSON(), nullable=False),
        sa.Column("refs", sa.JSON(), nullable=False),
        sa.Column("verifier", sa.JSON(), nullable=True),
        sa.Column("model", sa.String(length=120), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("llm_calls", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("latency_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("run_id", sa.String(length=32), nullable=True),
        sa.Column("feedback_helpful", sa.Boolean(), nullable=True),
        sa.Column("feedback_note", sa.Text(), nullable=True),
        sa.Column("feedback_at", _dt(), nullable=True),
        sa.Column("created_at", _dt(), nullable=False),
    )
    op.create_index("ix_nova_turns_conversation_created", "nova_turns", ["conversation_id", "created_at"])
    op.create_index("ix_nova_turns_employee_created", "nova_turns", ["employee_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_nova_turns_employee_created", table_name="nova_turns")
    op.drop_index("ix_nova_turns_conversation_created", table_name="nova_turns")
    op.drop_table("nova_turns")
    op.drop_index("ix_nova_conversations_employee_updated", table_name="nova_conversations")
    op.drop_table("nova_conversations")
