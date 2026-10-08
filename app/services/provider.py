"""Where a page gets its rows from.

My own pages read the local database. The admin "view as" mode shows another
user's data as of their last sync, fetched from the web app. Both are the same
shape to the pages, so the Dashboard, Saved Jobs and Generated Resumes pages
do not know which one they are showing; they only ask `read_only`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from app.data.models import GeneratedResume, SavedJob
from app.data.store import ResumeFilter, Store
from app.data.types import from_iso
from app.sync.api_client import WebApi


@dataclass
class ResumeRow:
    id: str
    candidate_name: str
    role: str
    company: str
    jd_url: str
    apply_url: str | None
    chat_url: str | None
    status: str
    created_at: datetime
    status_changed_at: datetime | None = None
    applied_at: datetime | None = None
    shortlisted_at: datetime | None = None
    rejected_at: datetime | None = None
    #: True synced, False waiting, None not applicable (another user's data).
    synced: bool | None = None
    sync_error: str | None = None
    jd_text: str | None = None
    resume_json: str | None = None

    @property
    def title(self) -> str:
        return f"{self.company} - {self.role}"


@dataclass
class JobRow:
    id: str
    source_site: str
    url: str
    role: str
    company: str
    attention_reason: str | None
    created_at: datetime
    #: Found by another profile and taken from the shared list.
    shared: bool = False


def resume_row(resume: GeneratedResume, *, light: bool = False) -> ResumeRow:
    """`light` rows come from a query that did not load the job description or the JSON."""
    return ResumeRow(
        id=resume.id,
        candidate_name=resume.candidate_name,
        role=resume.role,
        company=resume.company,
        jd_url=resume.jd_url,
        apply_url=resume.apply_url,
        chat_url=resume.chat_url,
        status=resume.status,
        created_at=resume.created_at,
        status_changed_at=resume.status_changed_at,
        applied_at=resume.applied_at,
        shortlisted_at=resume.shortlisted_at,
        rejected_at=resume.rejected_at,
        synced=not resume.dirty,
        sync_error=resume.sync_error,
        jd_text=None if light else resume.jd_text,
        resume_json=None if light else resume.resume_json,
    )


def job_row(job: SavedJob) -> JobRow:
    return JobRow(
        id=job.id,
        source_site=job.source_site,
        url=job.url,
        role=job.role or "",
        company=job.company or "",
        attention_reason=job.attention_reason,
        created_at=job.created_at,
        shared=bool(job.shared),
    )


class DataProvider(Protocol):
    read_only: bool

    def resumes(self, filters: ResumeFilter | None = None) -> list[ResumeRow]: ...

    def saved_jobs(
        self, *, search: str = "", newest_first: bool = False, attention_only: bool = False
    ) -> list[JobRow]: ...

    async def resume_detail(self, resume_id: str) -> ResumeRow | None: ...


class LocalProvider:
    read_only = False

    def __init__(self, store: Store) -> None:
        self.store = store

    def resumes(self, filters: ResumeFilter | None = None) -> list[ResumeRow]:
        # Lists never need the heavy fields; `resume_detail` fetches them for one record.
        return [resume_row(item, light=True) for item in self.store.list_resumes(filters, light=True)]

    def saved_jobs(self, *, search: str = "", newest_first: bool = False, attention_only: bool = False) -> list[JobRow]:
        return [
            job_row(item)
            for item in self.store.list_saved_jobs(
                search=search, newest_first=newest_first, attention_only=attention_only, light=True
            )
        ]

    async def resume_detail(self, resume_id: str) -> ResumeRow | None:
        resume = self.store.get_resume(resume_id)
        return resume_row(resume) if resume is not None else None


def _time(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return from_iso(value)
    except ValueError:
        return None


def remote_resume_row(data: dict[str, Any]) -> ResumeRow:
    import json

    resume_json = data.get("resumeJson")
    return ResumeRow(
        id=str(data.get("id") or ""),
        candidate_name=str(data.get("candidateName") or ""),
        role=str(data.get("role") or ""),
        company=str(data.get("company") or ""),
        jd_url=str(data.get("jdUrl") or ""),
        apply_url=data.get("applyUrl") or None,
        chat_url=data.get("chatUrl") or None,
        status=str(data.get("status") or "generated"),
        created_at=_time(data.get("createdAt")) or datetime.fromtimestamp(0).astimezone(),
        status_changed_at=_time(data.get("statusChangedAt")),
        applied_at=_time(data.get("appliedAt")),
        shortlisted_at=_time(data.get("shortlistedAt")),
        rejected_at=_time(data.get("rejectedAt")),
        synced=None,
        jd_text=data.get("jdText") if isinstance(data.get("jdText"), str) else None,
        resume_json=(resume_json if isinstance(resume_json, str) else json.dumps(resume_json) if resume_json else None),
    )


@dataclass
class RemoteProvider:
    """Another user's desktop data, read-only, as of their last sync."""

    api: WebApi
    user_id: str
    full_name: str
    email: str
    last_synced_at: datetime | None = None
    rows: list[ResumeRow] = field(default_factory=list)
    jobs: list[JobRow] = field(default_factory=list)
    truncated: bool = False
    read_only: bool = True

    @classmethod
    async def load(cls, api: WebApi, user_id: str) -> RemoteProvider:
        data = await api.admin_user_desktop(user_id)
        user = data.get("user") or {}
        sync = data.get("sync") or {}
        moments = [value for value in (_time(sync.get("lastPushAt")), _time(sync.get("lastImportAt"))) if value]
        jobs = [
            JobRow(
                id=str(item.get("id") or ""),
                source_site=str(item.get("sourceSite") or "other"),
                url=str(item.get("url") or ""),
                role=str(item.get("role") or ""),
                company=str(item.get("company") or ""),
                attention_reason=item.get("attentionReason") or None,
                created_at=_time(item.get("createdAt")) or datetime.fromtimestamp(0).astimezone(),
            )
            for item in data.get("savedJobs") or []
        ]
        return cls(
            api=api,
            user_id=user_id,
            full_name=str(user.get("fullName") or ""),
            email=str(user.get("email") or ""),
            last_synced_at=max(moments) if moments else None,
            rows=[remote_resume_row(item) for item in data.get("generatedResumes") or []],
            jobs=jobs,
            truncated=bool(data.get("truncated")),
        )

    def resumes(self, filters: ResumeFilter | None = None) -> list[ResumeRow]:
        f = filters or ResumeFilter()
        term = f.search.strip().lower()
        result = []
        for row in self.rows:
            if term and term not in row.company.lower() and term not in row.role.lower():
                continue
            if f.statuses and row.status not in f.statuses:
                continue
            moment = getattr(row, f.date_field)
            if f.date_from is not None and (moment is None or moment < f.date_from):
                continue
            if f.date_to is not None and (moment is None or moment >= f.date_to):
                continue
            result.append(row)
        result.sort(key=lambda row: row.created_at, reverse=f.newest_first)
        return result

    def saved_jobs(self, *, search: str = "", newest_first: bool = False, attention_only: bool = False) -> list[JobRow]:
        term = search.strip().lower()
        result = [
            job
            for job in self.jobs
            if (not term or term in job.company.lower() or term in job.role.lower())
            and (not attention_only or job.attention_reason)
        ]
        result.sort(key=lambda job: job.created_at, reverse=newest_first)
        return result

    async def resume_detail(self, resume_id: str) -> ResumeRow | None:
        for row in self.rows:
            if row.id == resume_id and row.resume_json and row.jd_text is not None:
                return row
        data = await self.api.admin_user_resume(self.user_id, resume_id)
        full = remote_resume_row(data)
        for index, row in enumerate(self.rows):
            if row.id == resume_id:
                self.rows[index] = full
        return full
