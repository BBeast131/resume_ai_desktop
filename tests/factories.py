"""Small builders shared by the data, sync and UI tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.data.db import Database
from app.data.store import Store

ACCOUNT = "11111111-1111-4111-8111-111111111111"

RESUME: dict[str, Any] = {
    "contact": {
        "fullName": "Anthony Fox",
        "headline": "Staff Software Engineer",
        "email": "anthony@example.com",
        "phone": "(512) 555-0134",
        "location": "Austin, TX",
    },
    "summary": ["Staff engineer building distributed systems."],
    "skills": [{"category": "Languages", "items": ["TypeScript", "Python"]}],
    "experience": [
        {
            "company": "Salesforce",
            "location": "San Francisco, CA",
            "role": "Staff Software Engineer",
            "startDate": "06/2024",
            "endDate": "07/2026",
            "bullets": ["Led the migration of a monolith to services.", "Ran architecture reviews."],
        }
    ],
}

JD = "We are hiring a Staff Engineer to build distributed systems. " * 5


def utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def make_store(tmp_path: Path, account: str = ACCOUNT) -> Store:
    database = Database(tmp_path / f"{account}.db", account)
    database.upgrade()
    return Store(database)


def add_job(store: Store, n: int = 1, *, site: str = "jobright", now: datetime | None = None, **overrides: Any):
    values: dict[str, Any] = {
        "url": f"https://jobright.ai/jobs/info/{n}",
        "url_key": f"jobright.ai/jobs/info/{n}",
        "source_site": site,
        "role": f"Engineer {n}",
        "company": f"Company {n}",
    }
    values.update(overrides)
    return store.add_saved_job(now=now, **values)


def add_resume(
    store: Store, n: int = 1, *, now: datetime | None = None, saved_job_id: str | None = None, **overrides: Any
):
    values: dict[str, Any] = {
        "candidate_name": "Anthony Fox",
        "role": f"Staff Engineer {n}",
        "company": f"Acme {n}",
        "jd_url": f"https://boards.greenhouse.io/acme/jobs/{n}",
        "apply_url": None,
        "source_url": f"https://jobright.ai/jobs/info/{n}",
        "url_key": f"jobright.ai/jobs/info/{n}",
        "jd_text": JD,
        "chat_url": "https://chatgpt.com/c/abcdef12-3456-7890-abcd-ef1234567890",
        "chat_id": "abcdef12-3456-7890-abcd-ef1234567890",
        "resume_json": json.dumps(RESUME),
    }
    values.update(overrides)
    return store.save_generated(saved_job_id=saved_job_id, now=now, **values)
