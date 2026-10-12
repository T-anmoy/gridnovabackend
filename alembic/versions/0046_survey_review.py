"""S1 Steps 5–8 (BE-2): the survey review queue's overdue alert.

  * proposal_inputs.survey_review_alerted_for — the moment a survey started waiting for
    the Engineer's review that the overdue alert (POST
    /api/v1/dashboard/internal/check-survey-reviews) has already reported. The job
    claims a survey by setting it with a conditional UPDATE, so an overlapping run
    never alerts the same wait twice; a survey that re-enters the queue later waits
    from a new moment and is alerted again.

Everything else Steps 5–8 use was created by 0044_site_survey.

Downgrade drops the column (the job would then alert every overdue survey once more).

Revision ID: 0046_survey_review
Revises: 0045_be1_security
Create Date: 2026-10-12
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0046_survey_review"
down_revision = "0045_be1_security"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("proposal_inputs") as batch:
        batch.add_column(sa.Column("survey_review_alerted_for", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("proposal_inputs") as batch:
        batch.drop_column("survey_review_alerted_for")
