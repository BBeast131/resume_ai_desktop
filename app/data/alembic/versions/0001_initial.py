"""Initial schema (schema_version 1).

Revision ID: 0001
Revises:
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

TS = sa.String(32)


def upgrade() -> None:
    op.create_table(
        "meta",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("value", sa.Text, nullable=False),
    )
    op.create_table(
        "saved_jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("source_site", sa.String(20), nullable=False, server_default="other"),
        sa.Column("url", sa.Text, nullable=False),
        sa.Column("url_key", sa.Text, nullable=False, unique=True),
        sa.Column("role", sa.Text),
        sa.Column("company", sa.Text),
        sa.Column("jd_text", sa.Text),
        sa.Column("extraction_confidence", sa.Float),
        sa.Column("attention_reason", sa.Text),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.Column("deleted_at", TS),
        sa.Column("synced_at", TS),
        sa.Column("dirty", sa.Boolean, nullable=False, server_default=sa.text("1")),
        sa.Column("sync_error", sa.Text),
    )
    op.create_index("saved_jobs_created_idx", "saved_jobs", ["created_at"])
    op.create_table(
        "generated_resumes",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("candidate_name", sa.Text, nullable=False),
        sa.Column("role", sa.Text, nullable=False),
        sa.Column("company", sa.Text, nullable=False),
        sa.Column("jd_url", sa.Text, nullable=False),
        sa.Column("apply_url", sa.Text),
        sa.Column("source_url", sa.Text),
        sa.Column("url_key", sa.Text),
        sa.Column("jd_text", sa.Text, nullable=False),
        sa.Column("chat_url", sa.Text),
        sa.Column("chat_id", sa.Text),
        sa.Column("resume_json", sa.Text, nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="generated"),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.Column("status_changed_at", TS),
        sa.Column("applied_at", TS),
        sa.Column("shortlisted_at", TS),
        sa.Column("rejected_at", TS),
        sa.Column("deleted_at", TS),
        sa.Column("synced_at", TS),
        sa.Column("dirty", sa.Boolean, nullable=False, server_default=sa.text("1")),
        sa.Column("server_id", sa.String(36)),
        sa.Column("sync_error", sa.Text),
        sa.CheckConstraint(
            "status in ('generated','applied','shortlisted','rejected')", name="generated_resumes_status"
        ),
    )
    op.create_index("generated_resumes_created_idx", "generated_resumes", ["created_at"])
    op.create_index("generated_resumes_url_key_idx", "generated_resumes", ["url_key"])
    op.create_table(
        "status_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("generated_resume_id", sa.String(36), sa.ForeignKey("generated_resumes.id"), nullable=False),
        sa.Column("from_status", sa.String(20)),
        sa.Column("to_status", sa.String(20), nullable=False),
        sa.Column("at", TS, nullable=False),
    )
    op.create_table(
        "skipped_jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("url", sa.Text, nullable=False),
        sa.Column("role", sa.Text),
        sa.Column("company", sa.Text),
        sa.Column("reason", sa.String(40), nullable=False),
        sa.Column("detail", sa.Text),
        sa.Column("created_at", TS, nullable=False),
    )
    op.create_table(
        "user_assets",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("prompt_text", sa.Text),
        sa.Column("prompt_filename", sa.Text),
        sa.Column("original_resume_text", sa.Text),
        sa.Column("original_resume_filename", sa.Text),
        sa.Column("updated_at", TS),
        sa.CheckConstraint("id = 1", name="user_assets_single_row"),
    )
    op.create_table(
        "sync_state",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("last_push_at", TS),
        sa.Column("last_pull_at", TS),
        sa.Column("last_error", sa.Text),
        sa.Column("server_cursor", sa.Text),
        sa.CheckConstraint("id = 1", name="sync_state_single_row"),
    )


def downgrade() -> None:
    for table in (
        "sync_state",
        "user_assets",
        "skipped_jobs",
        "status_events",
        "generated_resumes",
        "saved_jobs",
        "meta",
    ):
        op.drop_table(table)
