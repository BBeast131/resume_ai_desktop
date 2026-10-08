"""An in-memory stand-in for the web app's sync endpoints.

It applies the same rules as the real server (idempotent by id, newest status
wins, tombstones), so the desktop sync engine can be tested end to end
without HTTP.
"""

from __future__ import annotations

import json
from typing import Any

from app.sync.api_client import ApiError, OfflineError


class SharedPool:
    """The web app's shared job list, shared by every FakeServer (account) given it."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.push_calls: list[list[dict[str, Any]]] = []
        self.pull_calls: list[dict[str, Any]] = []
        self.missing = False  # an older web app without the endpoint

    def push(self, jobs: list[dict[str, Any]]) -> dict[str, Any]:
        if self.missing:
            raise ApiError("Not found", "NOT_FOUND", 404)
        assert len(jobs) <= 50
        self.push_calls.append(jobs)
        known = {row["urlKey"] for row in self.rows}
        added = 0
        for job in jobs:
            if job["urlKey"] in known:
                continue
            known.add(job["urlKey"])
            self.rows.append({**job, "_seq": len(self.rows) + 1})
            added += 1
        return {"received": len(jobs), "added": added, "rejected": 0}

    def pull(self, after: int, since: str | None, limit: int) -> dict[str, Any]:
        if self.missing:
            raise ApiError("Not found", "NOT_FOUND", 404)
        self.pull_calls.append({"after": after, "since": since, "limit": limit})
        rows = [row for row in self.rows if row["_seq"] > after and (since is None or row["foundAt"] >= since)]
        page = rows[:limit]
        return {
            "jobs": [{k: v for k, v in row.items() if not k.startswith("_")} for row in page],
            "cursor": page[-1]["_seq"] if page else after,
            "more": len(rows) > limit,
        }


class FakeServer:
    def __init__(self, pool: SharedPool | None = None) -> None:
        self.pool = pool or SharedPool()
        self.resumes: dict[str, dict[str, Any]] = {}
        self.saved_jobs: dict[str, dict[str, Any]] = {}
        self.tombstones: dict[str, int] = {}
        self.clock = 0
        self.offline = False
        self.fail_with: ApiError | None = None
        self.push_calls: list[dict[str, Any]] = []
        self.pull_calls: list[dict[str, Any]] = []
        #: ids the server will reject, with the reason.
        self.reject: dict[str, str] = {}
        #: Fail AFTER storing the batch, as when the answer is lost on the way back.
        self.drop_next_response = False

    def _tick(self) -> int:
        self.clock += 1
        return self.clock

    def _check(self) -> None:
        if self.offline:
            raise OfflineError()
        if self.fail_with is not None:
            raise self.fail_with

    async def sync_push(self, body: dict[str, Any]) -> dict[str, Any]:
        self._check()
        self.push_calls.append(body)
        assert len(body["generatedResumes"]) <= 50 and len(body["savedJobs"]) <= 50
        assert len(json.dumps(body).encode()) < 4_000_000

        resume_results = []
        for row in body["generatedResumes"]:
            row_id = row["id"]
            if row_id in self.reject:
                resume_results.append(
                    {"id": row_id, "result": "rejected", "code": "invalid_row", "reason": self.reject[row_id]}
                )
                continue
            if row_id in self.tombstones:
                resume_results.append(
                    {"id": row_id, "result": "rejected", "code": "deleted_on_web", "reason": "deleted"}
                )
                continue
            existing = self.resumes.get(row_id)
            if existing is None:
                self.resumes[row_id] = {**row, "serverId": f"srv-{row_id[:8]}", "_seq": self._tick()}
                resume_results.append({"id": row_id, "result": "created", "serverId": f"srv-{row_id[:8]}"})
                continue
            merged = {**existing, **{k: v for k, v in row.items() if k not in ("status", "statusChangedAt")}}
            if (row.get("statusChangedAt") or "") >= (existing.get("statusChangedAt") or ""):
                merged["status"], merged["statusChangedAt"] = row["status"], row.get("statusChangedAt")
            for key in ("appliedAt", "shortlistedAt", "rejectedAt"):
                values = [v for v in (existing.get(key), row.get(key)) if v]
                merged[key] = min(values) if values else None
            comparable = lambda item: {k: v for k, v in item.items() if not k.startswith("_")}  # noqa: E731
            if comparable(merged) == comparable(existing):
                resume_results.append({"id": row_id, "result": "unchanged", "serverId": existing["serverId"]})
            else:
                merged["_seq"] = self._tick()
                self.resumes[row_id] = merged
                resume_results.append({"id": row_id, "result": "updated", "serverId": existing["serverId"]})

        job_results = []
        for row in body["savedJobs"]:
            previous = self.saved_jobs.get(row["id"])
            self.saved_jobs[row["id"]] = row
            job_results.append(
                {
                    "id": row["id"],
                    "result": "created" if previous is None else ("unchanged" if previous == row else "updated"),
                }
            )

        if self.drop_next_response:
            self.drop_next_response = False
            raise OfflineError()
        return {"generatedResumes": resume_results, "savedJobs": job_results, "serverTime": "now"}

    async def sync_pull(self, since: str | None, *, slim: bool, limit: int = 100) -> dict[str, Any]:
        self._check()
        self.pull_calls.append({"since": since, "slim": slim})
        after_rows, after_tombs = (int(part) for part in (since or "0:0").split(":"))
        rows = sorted((row for row in self.resumes.values() if row["_seq"] > after_rows), key=lambda row: row["_seq"])
        page = rows[:limit]
        tombs = sorted((item for item in self.tombstones.items() if item[1] > after_tombs), key=lambda item: item[1])
        payload = []
        for row in page:
            item = {k: v for k, v in row.items() if not k.startswith("_")}
            if slim:
                item.pop("jdText", None)
                item.pop("resumeJson", None)
            payload.append(item)
        return {
            "generatedResumes": payload,
            "deleted": [{"id": row_id, "deletedAt": "2026-10-02T00:00:00.000Z"} for row_id, _ in tombs],
            "cursor": f"{page[-1]['_seq'] if page else after_rows}:{tombs[-1][1] if tombs else after_tombs}",
            "hasMore": len(rows) > limit,
            "serverTime": "now",
        }

    async def public_jobs_push(self, jobs: list[dict[str, Any]]) -> dict[str, Any]:
        self._check()
        return self.pool.push(jobs)

    async def public_jobs_pull(self, *, after: int, since: str | None = None, limit: int = 50) -> dict[str, Any]:
        self._check()
        return self.pool.pull(after, since, limit)

    # -- what a person does on the web page ---------------------------------------

    def web_set_status(self, row_id: str, status: str, at: str) -> None:
        row = self.resumes[row_id]
        row["status"], row["statusChangedAt"] = status, at
        if status in ("applied", "shortlisted", "rejected") and not row.get("appliedAt"):
            row["appliedAt"] = at
        if status == "shortlisted" and not row.get("shortlistedAt"):
            row["shortlistedAt"] = at
        if status == "rejected" and not row.get("rejectedAt"):
            row["rejectedAt"] = at
        row["_seq"] = self._tick()

    def web_delete(self, row_id: str) -> None:
        self.resumes.pop(row_id, None)
        self.tombstones[row_id] = self._tick()
