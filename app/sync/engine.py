"""Sync: local SQLite ⇄ the web app.

Push sends every dirty row (generated resumes and saved jobs, removals
included) in batches. Pull brings back what changed on the web: in practice,
a status changed on the Generated Resumes page, or a record deleted there.

It never blocks the UI: it is plain asyncio and the database calls are short.
It works after days offline: dirty rows simply wait, and the pull cursor picks
up where it stopped.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Literal

from app.config import SCHEMA_VERSION
from app.data.models import GeneratedResume, SavedJob
from app.data.store import PushSnapshot, Store
from app.data.types import to_iso, utcnow
from app.sync.api_client import ApiError, OfflineError, WebApi

log = logging.getLogger(__name__)

SyncPhase = Literal["idle", "syncing", "offline", "error"]

BATCH_MAX_ROWS = 50
#: Vercel refuses request bodies over about 4.5 MB. Stay well below.
BATCH_MAX_BYTES = 2_500_000
#: Seconds to wait before trying again after a failure: 1, 2, 5, 15 minutes, then every 15.
BACKOFF_SECONDS = (60, 120, 300, 900)
PULL_PAGE_SLIM = 100
PULL_PAGE_FULL = 25


@dataclass(frozen=True)
class SyncStatus:
    phase: SyncPhase = "idle"
    pending: int = 0
    rejected: int = 0
    last_push_at: datetime | None = None
    last_pull_at: datetime | None = None
    last_error: str | None = None

    @property
    def last_synced_at(self) -> datetime | None:
        moments = [value for value in (self.last_push_at, self.last_pull_at) if value is not None]
        return max(moments) if moments else None


def resume_payload(resume: GeneratedResume) -> dict[str, Any]:
    def stamp(value: datetime | None) -> str | None:
        return to_iso(value) if value is not None else None

    return {
        "id": resume.id,
        "candidateName": resume.candidate_name,
        "role": resume.role,
        "company": resume.company,
        "jdUrl": resume.jd_url,
        "applyUrl": resume.apply_url,
        "jdText": resume.jd_text,
        "chatUrl": resume.chat_url,
        "resumeJson": resume.resume_json,
        "status": resume.status,
        "createdAt": to_iso(resume.created_at),
        "statusChangedAt": stamp(resume.status_changed_at),
        "appliedAt": stamp(resume.applied_at),
        "shortlistedAt": stamp(resume.shortlisted_at),
        "rejectedAt": stamp(resume.rejected_at),
    }


def saved_job_payload(job: SavedJob) -> dict[str, Any]:
    return {
        "id": job.id,
        "sourceSite": job.source_site,
        "url": job.url,
        "role": job.role[:160] if job.role else None,
        "company": job.company[:160] if job.company else None,
        "attentionReason": job.attention_reason[:200] if job.attention_reason else None,
        "createdAt": to_iso(job.created_at),
        "deletedAt": to_iso(job.deleted_at) if job.deleted_at is not None else None,
    }


def plan_batches(
    rows: list[dict[str, Any]], max_rows: int = BATCH_MAX_ROWS, max_bytes: int = BATCH_MAX_BYTES
) -> list[list[dict[str, Any]]]:
    """Split rows into batches that respect both the row cap and the size cap."""
    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    size = 0
    for row in rows:
        weight = len(json.dumps(row, ensure_ascii=False).encode("utf-8")) + 1
        if current and (len(current) >= max_rows or size + weight > max_bytes):
            batches.append(current)
            current, size = [], 0
        current.append(row)
        size += weight
    if current:
        batches.append(current)
    return batches


class SyncEngine:
    def __init__(self, store: Store, api: WebApi, on_change: Callable[[SyncStatus], None] | None = None) -> None:
        self.store = store
        self.api = api
        self.on_change = on_change
        #: Called when a pull changed local rows, so open pages can refresh.
        self.on_data_changed: Callable[[], None] | None = None
        self._lock = asyncio.Lock()
        self._failures = 0
        self._again = False
        #: The server's error code for the last failed sync (`UNAUTHENTICATED` means "sign in again").
        self.last_error_code: str | None = None
        self.status = self._read_status("idle")

    # -- status ------------------------------------------------------------------

    def _read_status(self, phase: SyncPhase, error: str | None = None) -> SyncStatus:
        state = self.store.sync_state()
        return SyncStatus(
            phase=phase,
            pending=self.store.pending_count(),
            rejected=self.store.rejected_count(),
            last_push_at=state.last_push_at,
            last_pull_at=state.last_pull_at,
            last_error=error if error is not None else (state.last_error if phase in ("offline", "error") else None),
        )

    def _publish(self, status: SyncStatus) -> None:
        self.status = status
        if self.on_change is not None:
            self.on_change(status)

    def refresh_status(self) -> SyncStatus:
        """Re-count pending rows (after a local change) without talking to the server."""
        phase: SyncPhase = "syncing" if self._lock.locked() else self.status.phase
        status = self._read_status(phase)
        if phase in ("offline", "error"):
            status = replace(status, last_error=self.status.last_error)
        self._publish(status)
        return status

    @property
    def retry_delay(self) -> int | None:
        """Seconds until the next automatic retry, or None when the last sync succeeded."""
        if self._failures == 0:
            return None
        return BACKOFF_SECONDS[min(self._failures, len(BACKOFF_SECONDS)) - 1]

    # -- the sync itself -----------------------------------------------------------

    async def sync(self) -> SyncStatus:
        """Push, then pull. Safe to call at any time; overlapping calls are folded into one more run."""
        if self._lock.locked():
            self._again = True
            return self.status

        async with self._lock:
            while True:
                self._again = False
                self._publish(self._read_status("syncing"))
                try:
                    await self._push()
                    changed = await self._pull()
                except OfflineError as error:
                    self._failures += 1
                    self.last_error_code = error.code
                    self.store.update_sync_state(last_error=error.message)
                    self._publish(self._read_status("offline", error.message))
                    return self.status
                except Exception as error:  # noqa: BLE001 - a sync must never be left "Syncing…"
                    if not isinstance(error, ApiError):
                        log.error("sync.crashed %s", type(error).__name__)
                        error = ApiError("Sync stopped unexpectedly. It will be tried again.", "INTERNAL")
                    self._failures += 1
                    self.last_error_code = error.code
                    log.warning("sync.failed code=%s status=%s", error.code, error.status)
                    self.store.update_sync_state(last_error=error.message)
                    self._publish(self._read_status("error", error.message))
                    return self.status

                self._failures = 0
                self.last_error_code = None
                self.store.update_sync_state(last_error=None)
                if changed and self.on_data_changed is not None:
                    self.on_data_changed()
                if not self._again:
                    break

            self._publish(self._read_status("idle"))
            return self.status

    async def _push(self) -> None:
        # Loop until nothing is left, so a backlog from days offline drains in one sync.
        sent_ids: set[str] = set()
        while True:
            resumes = [row for row in self.store.dirty_resumes(BATCH_MAX_ROWS * 4) if row.id not in sent_ids]
            jobs = [row for row in self.store.dirty_saved_jobs(BATCH_MAX_ROWS * 4) if row.id not in sent_ids]
            if not resumes and not jobs:
                return

            resume_snapshots = {row.id: PushSnapshot(row.id, row.updated_at) for row in resumes}
            job_snapshots = {row.id: PushSnapshot(row.id, row.updated_at) for row in jobs}

            for batch in plan_batches([resume_payload(row) for row in resumes]):
                await self._push_batch(batch, [], resume_snapshots, job_snapshots)
            for batch in plan_batches([saved_job_payload(row) for row in jobs]):
                await self._push_batch([], batch, resume_snapshots, job_snapshots)

            # A row the server rejected stays dirty; do not send it twice in one sync.
            sent_ids.update(resume_snapshots)
            sent_ids.update(job_snapshots)

    async def _push_batch(
        self,
        resumes: list[dict[str, Any]],
        jobs: list[dict[str, Any]],
        resume_snapshots: dict[str, PushSnapshot],
        job_snapshots: dict[str, PushSnapshot],
    ) -> None:
        body = {
            "schemaVersion": SCHEMA_VERSION,
            "accountId": self.store.database.account_id,
            "origin": "desktop",
            "generatedResumes": resumes,
            "savedJobs": jobs,
        }
        result = await self.api.sync_push(body)
        accepted, rejected = self.store.apply_push_results(
            resume_results=list(result.get("generatedResumes") or []),
            job_results=list(result.get("savedJobs") or []),
            sent_resumes=[resume_snapshots[row["id"]] for row in resumes],
            sent_jobs=[job_snapshots[row["id"]] for row in jobs],
        )
        self.store.update_sync_state(last_push_at=utcnow())
        log.info("sync.pushed resumes=%d jobs=%d accepted=%d rejected=%d", len(resumes), len(jobs), accepted, rejected)

    async def _pull(self) -> int:
        changed = 0
        state = self.store.sync_state()
        cursor = state.server_cursor
        # The first pull on a PC asks for full records, so a new PC gets its history back.
        # After that only statuses can have changed on the web, so the pull is slim.
        slim = cursor is not None
        # Full records are large (a job description and a resume each): fewer per page, so a
        # page always fits in one response.
        limit = PULL_PAGE_SLIM if slim else PULL_PAGE_FULL
        for _page in range(400):
            result = await self.api.sync_pull(cursor, slim=slim, limit=limit)
            merged = self.store.merge_remote(
                list(result.get("generatedResumes") or []), list(result.get("deleted") or [])
            )
            if merged.missing and slim:
                # A record made on another PC: fetch this same page again, in full.
                result = await self.api.sync_pull(cursor, slim=False, limit=limit)
                merged = self.store.merge_remote(
                    list(result.get("generatedResumes") or []), list(result.get("deleted") or [])
                )
            changed += merged.changed
            cursor = str(result.get("cursor") or "") or cursor
            self.store.update_sync_state(server_cursor=cursor, last_pull_at=utcnow())
            if not result.get("hasMore"):
                break
        return changed
