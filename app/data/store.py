"""Everything the app does with the local database, in one place.

The UI and the automation never write SQL; they call these methods. Each
method is one transaction, so a crash or a Stop can never leave half a record.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import func, or_, select
from sqlalchemy.orm import defer

from app.data.db import Database
from app.data.models import (
    STATUSES,
    SUBMITTED_STATUSES,
    GeneratedResume,
    SavedJob,
    SkippedJob,
    StatusEvent,
    SyncState,
    UserAssets,
)
from app.data.types import from_iso, utcnow

SaveOutcome = Literal["saved", "restored", "duplicate_saved", "duplicate_generated"]
DateField = Literal["created_at", "applied_at", "shortlisted_at", "rejected_at"]


@dataclass(frozen=True)
class SaveResult:
    outcome: SaveOutcome
    job: SavedJob | None

    @property
    def saved(self) -> bool:
        return self.outcome in ("saved", "restored")


@dataclass(frozen=True)
class ResumeFilter:
    """What the Generated Resumes table shows. The dashboard builds these for its drill-downs."""

    search: str = ""
    #: One status, or several (the Applied card means applied + shortlisted + rejected).
    statuses: tuple[str, ...] = ()
    newest_first: bool = True
    date_from: datetime | None = None
    date_to: datetime | None = None
    date_field: DateField = "created_at"
    include_deleted: bool = False


@dataclass(frozen=True)
class MergeResult:
    changed: int
    #: Records the server knows and this file does not, seen in a status-only pull.
    missing: tuple[str, ...] = ()


@dataclass(frozen=True)
class PushSnapshot:
    """A dirty row as it was when it was sent, so a later edit is not marked clean."""

    id: str
    updated_at: datetime


def apply_status(resume: GeneratedResume, status: str, at: datetime) -> bool:
    """Move a resume to `status` and stamp the milestones it implies.

    Milestones are set the first time a stage is reached and never cleared.
    Shortlisted and rejected both imply the resume was applied.
    Returns False when the status did not change.
    """
    if status not in STATUSES:
        raise ValueError(f"Unknown status: {status}")
    if resume.status == status:
        return False

    resume.status = status
    resume.status_changed_at = at
    if status in SUBMITTED_STATUSES and resume.applied_at is None:
        resume.applied_at = at
    if status == "shortlisted" and resume.shortlisted_at is None:
        resume.shortlisted_at = at
    if status == "rejected" and resume.rejected_at is None:
        resume.rejected_at = at
    resume.updated_at = at
    resume.dirty = True
    return True


def _earliest(a: datetime | None, b: datetime | None) -> datetime | None:
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)


def _parse(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return from_iso(value)
    except ValueError:
        return None


class Store:
    def __init__(self, database: Database) -> None:
        self.database = database

    # ------------------------------------------------------------------
    # Saved jobs
    # ------------------------------------------------------------------

    def add_saved_job(
        self,
        *,
        url: str,
        url_key: str,
        source_site: str,
        role: str | None,
        company: str | None,
        jd_text: str | None = None,
        confidence: float | None = None,
        now: datetime | None = None,
    ) -> SaveResult:
        """Save a job unless it is already saved or already generated."""
        at = now or utcnow()
        with self.database.session() as session:
            generated = session.scalar(select(GeneratedResume.id).where(GeneratedResume.url_key == url_key).limit(1))
            if generated:
                return SaveResult("duplicate_generated", None)

            existing = session.scalar(select(SavedJob).where(SavedJob.url_key == url_key))
            if existing is not None and existing.deleted_at is None:
                return SaveResult("duplicate_saved", existing)

            if existing is not None:
                # Removed earlier and saved again: the same row comes back to the end of the queue.
                existing.deleted_at = None
                existing.url = url
                existing.source_site = source_site
                existing.role = role
                existing.company = company
                existing.jd_text = jd_text
                existing.extraction_confidence = confidence
                existing.attention_reason = None
                existing.created_at = at
                existing.updated_at = at
                existing.dirty = True
                return SaveResult("restored", existing)

            job = SavedJob(
                url=url,
                url_key=url_key,
                source_site=source_site,
                role=role,
                company=company,
                jd_text=jd_text,
                extraction_confidence=confidence,
                created_at=at,
                updated_at=at,
                dirty=True,
            )
            session.add(job)
            return SaveResult("saved", job)

    def list_saved_jobs(
        self, *, search: str = "", newest_first: bool = False, attention_only: bool = False
    ) -> list[SavedJob]:
        with self.database.session() as session:
            query = select(SavedJob).where(SavedJob.deleted_at.is_(None))
            term = search.strip()
            if term:
                like = f"%{term}%"
                query = query.where(or_(SavedJob.company.ilike(like), SavedJob.role.ilike(like)))
            if attention_only:
                query = query.where(SavedJob.attention_reason.is_not(None))
            order = SavedJob.created_at.desc() if newest_first else SavedJob.created_at.asc()
            return list(session.scalars(query.order_by(order, SavedJob.id)))

    def queue(self, *, include_attention: bool) -> list[SavedJob]:
        """The jobs a run will process, oldest first."""
        jobs = self.list_saved_jobs()
        return jobs if include_attention else [job for job in jobs if not job.attention_reason]

    def saved_job_count(self) -> int:
        with self.database.session() as session:
            return int(
                session.scalar(select(func.count()).select_from(SavedJob).where(SavedJob.deleted_at.is_(None))) or 0
            )

    def attention_count(self) -> int:
        with self.database.session() as session:
            return int(
                session.scalar(
                    select(func.count())
                    .select_from(SavedJob)
                    .where(SavedJob.deleted_at.is_(None), SavedJob.attention_reason.is_not(None))
                )
                or 0
            )

    def get_saved_job(self, job_id: str) -> SavedJob | None:
        with self.database.session() as session:
            return session.get(SavedJob, job_id)

    def remove_saved_jobs(self, job_ids: Iterable[str], now: datetime | None = None) -> int:
        at = now or utcnow()
        removed = 0
        with self.database.session() as session:
            for job_id in job_ids:
                job = session.get(SavedJob, job_id)
                if job is not None and job.deleted_at is None:
                    job.deleted_at = at
                    job.updated_at = at
                    job.dirty = True
                    removed += 1
        return removed

    def set_attention(self, job_id: str, reason: str | None, now: datetime | None = None) -> None:
        """Set a job aside ("page needs sign-in"), or clear the flag with `None`."""
        with self.database.session() as session:
            job = session.get(SavedJob, job_id)
            if job is None or (job.attention_reason or None) == (reason or None):
                return
            job.attention_reason = (reason or "")[:200] or None
            job.updated_at = now or utcnow()
            job.dirty = True

    # ------------------------------------------------------------------
    # Skipped jobs (audit log)
    # ------------------------------------------------------------------

    def log_skip(
        self,
        *,
        url: str,
        role: str | None,
        company: str | None,
        reason: str,
        detail: str | None = None,
        now: datetime | None = None,
    ) -> None:
        with self.database.session() as session:
            session.add(
                SkippedJob(
                    url=url,
                    role=role,
                    company=company,
                    reason=reason,
                    detail=(detail or "")[:500] or None,
                    created_at=now or utcnow(),
                )
            )

    def skip_saved_job(
        self,
        job_id: str,
        *,
        reason: str,
        detail: str | None = None,
        keep_saved: bool = False,
        now: datetime | None = None,
    ) -> None:
        """Close a job without a resume, in one transaction.

        The job leaves the saved list and is written to the skipped log.
        `keep_saved` is for a job that needs the user (a blocked page): it
        stays in Saved Jobs, flagged with the reason.
        """
        at = now or utcnow()
        with self.database.session() as session:
            job = session.get(SavedJob, job_id)
            if job is None:
                return
            session.add(
                SkippedJob(
                    url=job.url,
                    role=job.role,
                    company=job.company,
                    reason=reason,
                    detail=(detail or "")[:500] or None,
                    created_at=at,
                )
            )
            if keep_saved:
                job.attention_reason = (detail or reason)[:200]
            else:
                job.deleted_at = at
            job.updated_at = at
            job.dirty = True

    def list_skips(self, limit: int = 200) -> list[SkippedJob]:
        with self.database.session() as session:
            return list(session.scalars(select(SkippedJob).order_by(SkippedJob.created_at.desc()).limit(limit)))

    # ------------------------------------------------------------------
    # Generated resumes
    # ------------------------------------------------------------------

    def save_generated(
        self,
        *,
        saved_job_id: str | None,
        candidate_name: str,
        role: str,
        company: str,
        jd_url: str,
        apply_url: str | None,
        source_url: str | None,
        url_key: str | None,
        jd_text: str,
        chat_url: str | None,
        chat_id: str | None,
        resume_json: str,
        now: datetime | None = None,
    ) -> GeneratedResume:
        """Store a validated resume and take its job off the saved list, atomically."""
        json.loads(resume_json)  # never store text that is not JSON
        at = now or utcnow()
        with self.database.session() as session:
            resume = GeneratedResume(
                candidate_name=candidate_name,
                role=role,
                company=company,
                jd_url=jd_url,
                apply_url=apply_url,
                source_url=source_url,
                url_key=url_key,
                jd_text=jd_text,
                chat_url=chat_url,
                chat_id=chat_id,
                resume_json=resume_json,
                status="generated",
                created_at=at,
                updated_at=at,
                dirty=True,
            )
            session.add(resume)
            if saved_job_id:
                job = session.get(SavedJob, saved_job_id)
                if job is not None and job.deleted_at is None:
                    job.deleted_at = at
                    job.attention_reason = None
                    job.updated_at = at
                    job.dirty = True
            return resume

    def list_resumes(self, filters: ResumeFilter | None = None, *, light: bool = False) -> list[GeneratedResume]:
        """The resumes matching `filters`. With `light`, `jd_text` and `resume_json` are not loaded
        (and must not be read from the returned rows)."""
        f = filters or ResumeFilter()
        with self.database.session() as session:
            query = select(GeneratedResume)
            if not f.include_deleted:
                query = query.where(GeneratedResume.deleted_at.is_(None))
            term = f.search.strip()
            if term:
                like = f"%{term}%"
                query = query.where(or_(GeneratedResume.company.ilike(like), GeneratedResume.role.ilike(like)))
            if f.statuses:
                query = query.where(GeneratedResume.status.in_(f.statuses))
            column = getattr(GeneratedResume, f.date_field)
            if f.date_from is not None:
                query = query.where(column >= f.date_from)
            if f.date_to is not None:
                query = query.where(column < f.date_to)
            order = GeneratedResume.created_at.desc() if f.newest_first else GeneratedResume.created_at.asc()
            query = query.order_by(order, GeneratedResume.id)
            if light:
                # Tables and the dashboard do not need the job description or the resume JSON:
                # with thousands of records those are tens of megabytes.
                query = query.options(defer(GeneratedResume.jd_text), defer(GeneratedResume.resume_json))
            return list(session.scalars(query))

    def get_resume(self, resume_id: str) -> GeneratedResume | None:
        with self.database.session() as session:
            return session.get(GeneratedResume, resume_id)

    def resume_count(self) -> int:
        with self.database.session() as session:
            return int(
                session.scalar(
                    select(func.count()).select_from(GeneratedResume).where(GeneratedResume.deleted_at.is_(None))
                )
                or 0
            )

    def set_status(self, resume_id: str, status: str, now: datetime | None = None) -> GeneratedResume | None:
        at = now or utcnow()
        with self.database.session() as session:
            resume = session.get(GeneratedResume, resume_id)
            if resume is None:
                return None
            previous = resume.status
            if apply_status(resume, status, at):
                session.add(StatusEvent(generated_resume_id=resume.id, from_status=previous, to_status=status, at=at))
            return resume

    def status_events(self, resume_id: str) -> list[StatusEvent]:
        with self.database.session() as session:
            return list(
                session.scalars(
                    select(StatusEvent).where(StatusEvent.generated_resume_id == resume_id).order_by(StatusEvent.at)
                )
            )

    # ------------------------------------------------------------------
    # Prompt and original resume (local only)
    # ------------------------------------------------------------------

    def assets(self) -> UserAssets:
        with self.database.session() as session:
            assets = session.get(UserAssets, 1)
            if assets is None:
                assets = UserAssets(id=1)
                session.add(assets)
            return assets

    def set_prompt(self, text: str, filename: str) -> None:
        with self.database.session() as session:
            assets = session.get(UserAssets, 1) or UserAssets(id=1)
            assets.prompt_text = text
            assets.prompt_filename = filename
            assets.updated_at = utcnow()
            session.add(assets)

    def set_original_resume(self, text: str | None, filename: str | None) -> None:
        with self.database.session() as session:
            assets = session.get(UserAssets, 1) or UserAssets(id=1)
            assets.original_resume_text = text
            assets.original_resume_filename = filename
            assets.updated_at = utcnow()
            session.add(assets)

    # ------------------------------------------------------------------
    # Sync
    # ------------------------------------------------------------------

    def dirty_resumes(self, limit: int) -> list[GeneratedResume]:
        with self.database.session() as session:
            return list(
                session.scalars(
                    select(GeneratedResume)
                    .where(GeneratedResume.dirty.is_(True), GeneratedResume.deleted_at.is_(None))
                    .order_by(GeneratedResume.updated_at)
                    .limit(limit)
                )
            )

    def dirty_saved_jobs(self, limit: int) -> list[SavedJob]:
        with self.database.session() as session:
            return list(
                session.scalars(
                    select(SavedJob).where(SavedJob.dirty.is_(True)).order_by(SavedJob.updated_at).limit(limit)
                )
            )

    def pending_count(self) -> int:
        """Rows still waiting to reach the web app (rejected rows are not waiting)."""
        with self.database.session() as session:
            resumes = session.scalar(
                select(func.count())
                .select_from(GeneratedResume)
                .where(
                    GeneratedResume.dirty.is_(True),
                    GeneratedResume.deleted_at.is_(None),
                    GeneratedResume.sync_error.is_(None),
                )
            )
            jobs = session.scalar(select(func.count()).select_from(SavedJob).where(SavedJob.dirty.is_(True)))
            return int(resumes or 0) + int(jobs or 0)

    def rejected_resumes(self) -> list[GeneratedResume]:
        with self.database.session() as session:
            return list(
                session.scalars(
                    select(GeneratedResume)
                    .where(GeneratedResume.sync_error.is_not(None), GeneratedResume.deleted_at.is_(None))
                    .options(defer(GeneratedResume.jd_text), defer(GeneratedResume.resume_json))
                )
            )

    def rejected_saved_jobs(self) -> list[SavedJob]:
        with self.database.session() as session:
            return list(session.scalars(select(SavedJob).where(SavedJob.sync_error.is_not(None))))

    def rejected_count(self) -> int:
        with self.database.session() as session:
            resumes = session.scalar(
                select(func.count())
                .select_from(GeneratedResume)
                .where(GeneratedResume.sync_error.is_not(None), GeneratedResume.deleted_at.is_(None))
            )
            jobs = session.scalar(select(func.count()).select_from(SavedJob).where(SavedJob.sync_error.is_not(None)))
            return int(resumes or 0) + int(jobs or 0)

    def apply_push_results(
        self,
        *,
        resume_results: Sequence[dict[str, Any]],
        job_results: Sequence[dict[str, Any]],
        sent_resumes: Sequence[PushSnapshot],
        sent_jobs: Sequence[PushSnapshot],
        now: datetime | None = None,
    ) -> tuple[int, int]:
        """Record what the server said about a batch.

        Only accepted rows are marked clean, and only if they have not been
        edited since they were sent. Returns (accepted, rejected).
        """
        at = now or utcnow()
        sent_resume_at = {item.id: item.updated_at for item in sent_resumes}
        sent_job_at = {item.id: item.updated_at for item in sent_jobs}
        accepted = rejected = 0

        with self.database.session() as session:
            for result in resume_results:
                row_id = result.get("id")
                if not isinstance(row_id, str) or row_id not in sent_resume_at:
                    continue
                resume = session.get(GeneratedResume, row_id)
                if resume is None:
                    continue
                outcome = result.get("result")
                if outcome in ("created", "updated", "unchanged"):
                    accepted += 1
                    resume.synced_at = at
                    resume.sync_error = None
                    server_id = result.get("serverId")
                    if isinstance(server_id, str):
                        resume.server_id = server_id
                    if resume.updated_at == sent_resume_at[row_id]:
                        resume.dirty = False
                elif result.get("code") == "deleted_on_web":
                    # Not an error: the user deleted it there. Hide it here and stop sending it.
                    resume.deleted_at = resume.deleted_at or at
                    resume.dirty = False
                    resume.sync_error = None
                elif result.get("code") == "database_error":
                    # The server's problem, not the row's: leave it dirty and try again later.
                    continue
                else:
                    rejected += 1
                    resume.sync_error = str(result.get("reason") or result.get("code") or "Rejected by the web app")[
                        :500
                    ]
                    # Stays dirty: it is sent again once the user (or an app update) has fixed it,
                    # but it does not count as "waiting" and never blocks the other rows.

            for result in job_results:
                row_id = result.get("id")
                if not isinstance(row_id, str) or row_id not in sent_job_at:
                    continue
                job = session.get(SavedJob, row_id)
                if job is None:
                    continue
                if result.get("result") in ("created", "updated", "unchanged"):
                    accepted += 1
                    job.synced_at = at
                    job.sync_error = None
                    if job.updated_at == sent_job_at[row_id]:
                        job.dirty = False
                elif result.get("code") != "database_error":
                    rejected += 1
                    # A saved job the server will never take (e.g. a non-http URL) is only a
                    # backup copy: stop retrying it, but keep the reason for the Sync panel.
                    job.sync_error = str(result.get("reason") or result.get("code") or "Rejected by the web app")[:500]
                    job.dirty = False

        return accepted, rejected

    def merge_remote(
        self,
        remote_resumes: Sequence[dict[str, Any]],
        deleted: Sequence[dict[str, Any]],
        now: datetime | None = None,
    ) -> MergeResult:
        """Bring web-side changes into the local file.

        The newest `status_changed_at` wins. Milestones are merged (the
        earliest of each) and never cleared. A record this PC has never seen
        (a new PC, a restored account) is created when the server sent it in
        full.
        """
        at = now or utcnow()
        changed = 0
        missing: list[str] = []

        with self.database.session() as session:
            for remote in remote_resumes:
                row_id = remote.get("id")
                if not isinstance(row_id, str):
                    continue
                local = session.get(GeneratedResume, row_id)
                remote_status = str(remote.get("status") or "generated")
                if remote_status not in STATUSES:
                    remote_status = "generated"
                remote_changed = _parse(remote.get("statusChangedAt"))

                if local is None:
                    resume_json = remote.get("resumeJson")
                    jd_text = remote.get("jdText")
                    if not resume_json or not jd_text:
                        missing.append(row_id)  # a status-only pull cannot create a record
                        continue
                    created = _parse(remote.get("createdAt")) or at
                    session.add(
                        GeneratedResume(
                            id=row_id,
                            candidate_name=str(remote.get("candidateName") or ""),
                            role=str(remote.get("role") or ""),
                            company=str(remote.get("company") or ""),
                            jd_url=str(remote.get("jdUrl") or ""),
                            apply_url=remote.get("applyUrl") or None,
                            source_url=None,
                            url_key=None,
                            jd_text=str(jd_text),
                            chat_url=remote.get("chatUrl") or None,
                            chat_id=None,
                            resume_json=resume_json if isinstance(resume_json, str) else json.dumps(resume_json),
                            status=remote_status,
                            created_at=created,
                            updated_at=at,
                            status_changed_at=remote_changed,
                            applied_at=_parse(remote.get("appliedAt")),
                            shortlisted_at=_parse(remote.get("shortlistedAt")),
                            rejected_at=_parse(remote.get("rejectedAt")),
                            synced_at=at,
                            dirty=False,
                            server_id=remote.get("serverId") or None,
                        )
                    )
                    changed += 1
                    continue

                before = (
                    local.status,
                    local.status_changed_at,
                    local.applied_at,
                    local.shortlisted_at,
                    local.rejected_at,
                )

                remote_wins = remote_changed is not None and (
                    local.status_changed_at is None or remote_changed > local.status_changed_at
                )
                if remote_wins and local.status != remote_status:
                    session.add(
                        StatusEvent(
                            generated_resume_id=local.id,
                            from_status=local.status,
                            to_status=remote_status,
                            at=remote_changed or at,
                        )
                    )
                if remote_wins:
                    local.status = remote_status
                    local.status_changed_at = remote_changed

                local.applied_at = _earliest(local.applied_at, _parse(remote.get("appliedAt")))
                local.shortlisted_at = _earliest(local.shortlisted_at, _parse(remote.get("shortlistedAt")))
                local.rejected_at = _earliest(local.rejected_at, _parse(remote.get("rejectedAt")))
                server_id = remote.get("serverId")
                if isinstance(server_id, str):
                    local.server_id = server_id

                after = (
                    local.status,
                    local.status_changed_at,
                    local.applied_at,
                    local.shortlisted_at,
                    local.rejected_at,
                )
                if after != before:
                    # `updated_at` is deliberately left alone: a merge is not a local edit,
                    # so it must not make the row look newer than what was pushed.
                    changed += 1

            for item in deleted:
                row_id = item.get("id")
                if not isinstance(row_id, str):
                    continue
                local = session.get(GeneratedResume, row_id)
                if local is not None and local.deleted_at is None:
                    local.deleted_at = _parse(item.get("deletedAt")) or at
                    local.dirty = False
                    changed += 1

        return MergeResult(changed, tuple(missing))

    def sync_state(self) -> SyncState:
        with self.database.session() as session:
            state = session.get(SyncState, 1)
            if state is None:
                state = SyncState(id=1)
                session.add(state)
            return state

    def update_sync_state(self, **values: Any) -> None:
        with self.database.session() as session:
            state = session.get(SyncState, 1) or SyncState(id=1)
            for key, value in values.items():
                setattr(state, key, value)
            session.add(state)
