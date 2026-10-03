"""The local database: one SQLite file per signed-in account.

Everything the app produces lives here, and the app works from this file
alone. `saved_jobs` and `generated_resumes` are synced to the web app;
`user_assets` (your prompt and your original resume) never leave this PC.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.data.types import UtcIso, new_id, utcnow

STATUSES = ("generated", "applied", "shortlisted", "rejected")
SUBMITTED_STATUSES = ("applied", "shortlisted", "rejected")
SOURCE_SITES = ("jobright", "hiring_cafe", "other")
SKIP_REASONS = (
    "linkedin",
    "form_only_no_jd",
    "user_cancelled",
    "needs_user_action",
    "extraction_failed",
    "chatgpt_failed",
    "invalid_json",
)


class Base(DeclarativeBase):
    pass


class Meta(Base):
    """`schema_version`, `account_id`, `app_version`: read by the web importer."""

    __tablename__ = "meta"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)


class SavedJob(Base):
    __tablename__ = "saved_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source_site: Mapped[str] = mapped_column(String(20), nullable=False, default="other")
    url: Mapped[str] = mapped_column(Text, nullable=False)
    #: The URL reduced to what identifies the job; what "already saved" compares.
    url_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    role: Mapped[str | None] = mapped_column(Text)
    company: Mapped[str | None] = mapped_column(Text)
    #: jobright's own description, kept because the real job site is sometimes a bare form.
    jd_text: Mapped[str | None] = mapped_column(Text)
    extraction_confidence: Mapped[float | None] = mapped_column(Float)
    #: Why the job was set aside ("page needs sign-in"); empty when it is ready to run.
    attention_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UtcIso, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UtcIso, nullable=False, default=utcnow)
    #: Soft delete, so a removal reaches the server too.
    deleted_at: Mapped[datetime | None] = mapped_column(UtcIso)
    synced_at: Mapped[datetime | None] = mapped_column(UtcIso)
    dirty: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    #: Why the server refused the row, in plain words; empty when accepted.
    sync_error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("saved_jobs_created_idx", "created_at"),)


class GeneratedResume(Base):
    __tablename__ = "generated_resumes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    candidate_name: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    company: Mapped[str] = mapped_column(Text, nullable=False)
    #: The page the description was read from.
    jd_url: Mapped[str] = mapped_column(Text, nullable=False)
    #: The real application page, when it differs.
    apply_url: Mapped[str | None] = mapped_column(Text)
    #: The URL the job was saved under (the jobright or hiring.cafe link).
    source_url: Mapped[str | None] = mapped_column(Text)
    url_key: Mapped[str | None] = mapped_column(Text)
    jd_text: Mapped[str] = mapped_column(Text, nullable=False)
    chat_url: Mapped[str | None] = mapped_column(Text)
    chat_id: Mapped[str | None] = mapped_column(Text)
    #: The validated resume, exactly as the web app returned it.
    resume_json: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="generated")
    created_at: Mapped[datetime] = mapped_column(UtcIso, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UtcIso, nullable=False, default=utcnow)
    status_changed_at: Mapped[datetime | None] = mapped_column(UtcIso)
    # Milestones: set the first time the status reaches that stage, never cleared.
    applied_at: Mapped[datetime | None] = mapped_column(UtcIso)
    shortlisted_at: Mapped[datetime | None] = mapped_column(UtcIso)
    rejected_at: Mapped[datetime | None] = mapped_column(UtcIso)
    #: Deleted on the web: hidden here, but the row is kept.
    deleted_at: Mapped[datetime | None] = mapped_column(UtcIso)
    synced_at: Mapped[datetime | None] = mapped_column(UtcIso)
    dirty: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    server_id: Mapped[str | None] = mapped_column(String(36))
    #: Why the server refused the row, in plain words; empty when accepted.
    sync_error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint("status in ('generated','applied','shortlisted','rejected')", name="generated_resumes_status"),
        Index("generated_resumes_created_idx", "created_at"),
        Index("generated_resumes_url_key_idx", "url_key"),
    )


class StatusEvent(Base):
    __tablename__ = "status_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    generated_resume_id: Mapped[str] = mapped_column(ForeignKey("generated_resumes.id"), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(20))
    to_status: Mapped[str] = mapped_column(String(20), nullable=False)
    at: Mapped[datetime] = mapped_column(UtcIso, nullable=False, default=utcnow)


class SkippedJob(Base):
    """An audit log, so no job ever disappears without a trace."""

    __tablename__ = "skipped_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str | None] = mapped_column(Text)
    company: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(String(40), nullable=False)
    detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UtcIso, nullable=False, default=utcnow)


class UserAssets(Base):
    """Your prompt and your original resume. LOCAL ONLY: never synced."""

    __tablename__ = "user_assets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    prompt_text: Mapped[str | None] = mapped_column(Text)
    prompt_filename: Mapped[str | None] = mapped_column(Text)
    original_resume_text: Mapped[str | None] = mapped_column(Text)
    original_resume_filename: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime | None] = mapped_column(UtcIso)

    __table_args__ = (CheckConstraint("id = 1", name="user_assets_single_row"),)


class SyncState(Base):
    __tablename__ = "sync_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    last_push_at: Mapped[datetime | None] = mapped_column(UtcIso)
    last_pull_at: Mapped[datetime | None] = mapped_column(UtcIso)
    last_error: Mapped[str | None] = mapped_column(Text)
    server_cursor: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (CheckConstraint("id = 1", name="sync_state_single_row"),)
