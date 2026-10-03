"""Write the files the web app's tests use to prove it accepts what this app produces.

  python tools/make_contract_fixtures.py <web repo>/apps/web/tests/fixtures

desktop-sample.db       a real database made by this app's own code (for the Import DB test)
desktop-push-body.json  the exact body the sync engine sends to POST /api/desktop/sync
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.data.db import Database  # noqa: E402
from app.data.store import Store  # noqa: E402
from app.sync.engine import SyncEngine  # noqa: E402
from tests.factories import ACCOUNT, add_job, add_resume, utc  # noqa: E402
from tests.fake_server import FakeServer  # noqa: E402


def main() -> int:
    target = Path(sys.argv[1])
    target.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp())
    database = Database(work / "resume_ai.db", ACCOUNT)
    database.upgrade()
    store = Store(database)

    one = add_resume(store, 1, now=utc(2026, 10, 1, 9), company="Acme Robotics", role="Senior Platform Engineer")
    two = add_resume(
        store,
        2,
        now=utc(2026, 10, 1, 10),
        company="Globex",
        role="Staff Engineer",
        apply_url="https://globex.example/apply/2",
    )
    three = add_resume(
        store, 3, now=utc(2026, 10, 1, 11), company="Initech", role="Backend Engineer", chat_url=None, chat_id=None
    )
    store.set_status(two.id, "applied", now=utc(2026, 10, 2, 9))
    store.set_status(three.id, "applied", now=utc(2026, 10, 2, 10))
    store.set_status(three.id, "shortlisted", now=utc(2026, 10, 3, 10))
    add_job(store, 10, now=utc(2026, 10, 1, 8))
    removed = add_job(store, 11, site="hiring_cafe", now=utc(2026, 10, 1, 8, 30)).job
    store.remove_saved_jobs([removed.id], now=utc(2026, 10, 1, 12))
    flagged = add_job(store, 12, now=utc(2026, 10, 1, 8, 45)).job
    store.set_attention(flagged.id, "page needs sign-in")
    # Local only: this must never appear in anything sent to the web app.
    store.set_prompt("LOCAL-ONLY PROMPT", "prompt.txt")
    store.set_original_resume("LOCAL-ONLY RESUME", "resume.pdf")

    server = FakeServer()
    asyncio.run(SyncEngine(store, server).sync())  # type: ignore[arg-type]
    body = {
        "schemaVersion": server.push_calls[0]["schemaVersion"],
        "accountId": ACCOUNT,
        "origin": "desktop",
        "generatedResumes": [row for call in server.push_calls for row in call["generatedResumes"]],
        "savedJobs": [row for call in server.push_calls for row in call["savedJobs"]],
    }
    assert "LOCAL-ONLY" not in json.dumps(body)
    (target / "desktop-push-body.json").write_text(json.dumps(body, indent=2), encoding="utf-8")

    # The sample database is exported BEFORE the sync marks rows clean, as a user's file might be.
    fresh = Database(work / "fresh.db", ACCOUNT)
    fresh.upgrade()
    copy = Store(fresh)
    for n, (company, role) in enumerate(
        (("Acme Robotics", "Senior Platform Engineer"), ("Globex", "Staff Engineer")), 1
    ):
        add_resume(copy, n, now=utc(2026, 10, 1, 8 + n), company=company, role=role)
    copy.set_status(copy.list_resumes()[0].id, "applied", now=utc(2026, 10, 2, 9))
    add_job(copy, 20, now=utc(2026, 10, 1, 8))
    copy.set_prompt("LOCAL-ONLY PROMPT", "prompt.txt")
    fresh.export_copy(target / "desktop-sample.db")
    print("wrote", sorted(path.name for path in target.glob("desktop-*")), {"ids": [one.id, two.id, three.id]})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
