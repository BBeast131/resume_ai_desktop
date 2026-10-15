"""Skipped jobs stay in Saved Jobs, marked with why they were skipped.

Local only: the sync payloads are unchanged.

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("saved_jobs") as batch:
        # Why a run skipped the job ("LinkedIn job", "ChatGPT did not answer"); empty = not skipped.
        batch.add_column(sa.Column("skip_reason", sa.Text))
        batch.add_column(sa.Column("skipped_at", sa.String(32)))


def downgrade() -> None:
    with op.batch_alter_table("saved_jobs") as batch:
        batch.drop_column("skipped_at")
        batch.drop_column("skip_reason")
