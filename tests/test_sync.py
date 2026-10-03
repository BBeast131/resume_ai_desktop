"""The sync engine against an in-memory server."""

from __future__ import annotations

import json

from app.data.store import Store
from app.sync.api_client import ApiError
from app.sync.engine import BATCH_MAX_BYTES, SyncEngine, plan_batches, resume_payload
from tests.factories import add_job, add_resume, make_store, utc
from tests.fake_server import FakeServer


def engine_for(store: Store, server: FakeServer) -> SyncEngine:
    return SyncEngine(store, server)  # type: ignore[arg-type]


async def test_dirty_rows_are_pushed_and_marked_clean(tmp_path):
    store, server = make_store(tmp_path), FakeServer()
    resume = add_resume(store, 1)
    job = add_job(store, 2).job
    engine = engine_for(store, server)
    assert engine.status.pending == 2

    status = await engine.sync()

    assert status.phase == "idle" and status.pending == 0 and status.last_push_at is not None
    assert set(server.resumes) == {resume.id} and set(server.saved_jobs) == {job.id}
    stored = store.get_resume(resume.id)
    assert stored.dirty is False and stored.synced_at is not None and stored.server_id.startswith("srv-")
    assert store.get_saved_job(job.id).dirty is False


async def test_only_changed_rows_are_sent_again(tmp_path):
    store, server = make_store(tmp_path), FakeServer()
    one, _two = add_resume(store, 1), add_resume(store, 2)
    engine = engine_for(store, server)
    await engine.sync()
    server.push_calls.clear()

    await engine.sync()
    assert server.push_calls == []  # nothing dirty: nothing sent

    store.set_status(one.id, "applied", now=utc(2026, 10, 3))
    assert engine.refresh_status().pending == 1
    await engine.sync()
    assert [row["id"] for call in server.push_calls for row in call["generatedResumes"]] == [one.id]
    assert server.resumes[one.id]["status"] == "applied"


async def test_batches_of_fifty(tmp_path):
    store, server = make_store(tmp_path), FakeServer()
    for n in range(120):
        add_resume(store, n)
    await engine_for(store, server).sync()
    sizes = [len(call["generatedResumes"]) for call in server.push_calls if call["generatedResumes"]]
    assert sizes == [50, 50, 20]
    assert len(server.resumes) == 120 and store.pending_count() == 0


def test_batches_respect_the_size_cap(tmp_path):
    store = make_store(tmp_path)
    rows = [resume_payload(add_resume(store, n, jd_text="x" * 49_000)) for n in range(120)]
    batches = plan_batches(rows, max_bytes=1_000_000)
    assert sum(len(batch) for batch in batches) == 120
    assert all(len(batch) <= 20 for batch in batches)  # ~50 KB a row: 20 rows to the megabyte
    assert all(len(json.dumps(batch).encode()) <= 1_000_000 for batch in batches)
    # With the real cap, a batch of the largest rows the web app accepts still fits under Vercel's limit.
    assert all(len(json.dumps(batch).encode()) <= BATCH_MAX_BYTES for batch in plan_batches(rows))


async def test_partial_rejection(tmp_path):
    store, server = make_store(tmp_path), FakeServer()
    good, bad = add_resume(store, 1), add_resume(store, 2)
    server.reject[bad.id] = "role: Role is required"
    engine = engine_for(store, server)

    status = await engine.sync()

    assert store.get_resume(good.id).dirty is False
    rejected = store.get_resume(bad.id)
    assert rejected.dirty is True and rejected.sync_error == "role: Role is required"
    # A rejected row is reported, but it is not "waiting" and does not make the sync an error.
    assert status.phase == "idle" and status.pending == 0 and status.rejected == 1
    # ... and it was sent once, not in a loop.
    assert sum(1 for call in server.push_calls for row in call["generatedResumes"] if row["id"] == bad.id) == 1

    # Once the server accepts it, the error clears.
    server.reject.clear()
    status = await engine.sync()
    assert store.get_resume(bad.id).sync_error is None and status.rejected == 0


async def test_offline_then_recovery(tmp_path):
    store, server = make_store(tmp_path), FakeServer()
    add_resume(store, 1)
    add_resume(store, 2)
    engine = engine_for(store, server)

    server.offline = True
    status = await engine.sync()
    assert status.phase == "offline" and status.pending == 2 and status.last_error
    assert engine.retry_delay == 60
    await engine.sync()
    assert engine.retry_delay == 120  # backs off

    add_resume(store, 3)  # work continues while offline
    server.offline = False
    status = await engine.sync()
    assert status.phase == "idle" and status.pending == 0 and status.last_error is None
    assert len(server.resumes) == 3 and engine.retry_delay is None


async def test_no_duplicate_after_a_lost_answer(tmp_path):
    store, server = make_store(tmp_path), FakeServer()
    resume = add_resume(store, 1)
    engine = engine_for(store, server)

    server.drop_next_response = True  # the server stored it, the answer never arrived
    status = await engine.sync()
    assert status.phase == "offline" and store.get_resume(resume.id).dirty is True

    await engine.sync()  # the retry sends the same id: the server answers "unchanged"
    assert list(server.resumes) == [resume.id]
    assert store.get_resume(resume.id).dirty is False


async def test_a_row_edited_during_the_push_stays_dirty(tmp_path):
    store, server = make_store(tmp_path), FakeServer()
    resume = add_resume(store, 1)
    engine = engine_for(store, server)

    original = server.sync_push

    async def edit_while_sending(body):
        result = await original(body)
        if body["generatedResumes"]:
            store.set_status(resume.id, "applied", now=utc(2026, 10, 3))
        return result

    server.sync_push = edit_while_sending  # type: ignore[method-assign]
    await engine.sync()
    server.sync_push = original  # type: ignore[method-assign]

    assert store.get_resume(resume.id).dirty is True
    await engine.sync()
    assert server.resumes[resume.id]["status"] == "applied"
    assert store.get_resume(resume.id).dirty is False


async def test_pull_brings_back_a_status_changed_on_the_web(tmp_path):
    store, server = make_store(tmp_path), FakeServer()
    resume = add_resume(store, 1, now=utc(2026, 10, 1))
    engine = engine_for(store, server)
    changed: list[bool] = []
    engine.on_data_changed = lambda: changed.append(True)
    await engine.sync()

    server.web_set_status(resume.id, "shortlisted", "2026-10-05T09:00:00.000Z")
    await engine.sync()

    local = store.get_resume(resume.id)
    assert local.status == "shortlisted"
    assert local.shortlisted_at == utc(2026, 10, 5, 9) and local.applied_at == utc(2026, 10, 5, 9)
    assert local.dirty is False  # a merge is not a local edit
    assert changed == [True]
    assert [event.to_status for event in store.status_events(resume.id)] == ["shortlisted"]


async def test_newest_status_change_wins(tmp_path):
    store, server = make_store(tmp_path), FakeServer()
    resume = add_resume(store, 1, now=utc(2026, 10, 1))
    engine = engine_for(store, server)
    await engine.sync()

    # Web: applied on the 3rd. Desktop (offline): rejected on the 4th. The later one wins everywhere.
    server.web_set_status(resume.id, "applied", "2026-10-03T00:00:00.000Z")
    store.set_status(resume.id, "rejected", now=utc(2026, 10, 4))
    await engine.sync()
    assert store.get_resume(resume.id).status == "rejected"
    assert server.resumes[resume.id]["status"] == "rejected"
    # Milestones merged, earliest kept.
    assert store.get_resume(resume.id).applied_at == utc(2026, 10, 3)

    # And the other way round: an older desktop change loses to a newer web change.
    store.set_status(resume.id, "applied", now=utc(2026, 10, 5))
    server.web_set_status(resume.id, "shortlisted", "2026-10-06T00:00:00.000Z")
    await engine.sync()
    await engine.sync()
    assert store.get_resume(resume.id).status == "shortlisted"
    assert server.resumes[resume.id]["status"] == "shortlisted"


async def test_deleted_on_the_web_is_hidden_but_kept(tmp_path):
    store, server = make_store(tmp_path), FakeServer()
    resume = add_resume(store, 1)
    engine = engine_for(store, server)
    await engine.sync()

    server.web_delete(resume.id)
    await engine.sync()

    assert store.list_resumes() == []
    kept = store.get_resume(resume.id)
    assert kept is not None and kept.deleted_at is not None

    # Even a row that was dirty when it was deleted on the web is not sent back.
    store.set_status(resume.id, "applied")
    await engine.sync()
    assert resume.id not in server.resumes and store.pending_count() == 0


async def test_a_new_pc_gets_its_history_back(tmp_path):
    server = FakeServer()
    first = make_store(tmp_path / "pc1")
    add_resume(first, 1)
    add_resume(first, 2)
    await engine_for(first, server).sync()

    second = make_store(tmp_path / "pc2")
    await engine_for(second, server).sync()
    assert sorted(r.company for r in second.list_resumes()) == ["Acme 1", "Acme 2"]
    assert second.pending_count() == 0
    assert server.pull_calls[-1]["slim"] is False  # the first pull asks for full records

    # Later, a record made on the first PC still arrives, although pulls are now status-only.
    add_resume(first, 3)
    await engine_for(first, server).sync()
    await engine_for(second, server).sync()
    assert len(second.list_resumes()) == 3


async def test_an_api_error_is_reported_and_retried(tmp_path):
    store, server = make_store(tmp_path), FakeServer()
    add_resume(store, 1)
    engine = engine_for(store, server)
    seen: list[str] = []
    engine.on_change = lambda status: seen.append(status.phase)

    server.fail_with = ApiError("The web app had a problem. Try again in a moment.", "INTERNAL_ERROR", 500)
    status = await engine.sync()
    assert status.phase == "error" and "problem" in (status.last_error or "")
    assert seen == ["syncing", "error"]

    server.fail_with = None
    assert (await engine.sync()).phase == "idle"


async def test_never_sends_the_prompt_or_the_original_resume(tmp_path):
    store, server = make_store(tmp_path), FakeServer()
    store.set_prompt("SECRET PROMPT TEXT", "prompt.txt")
    store.set_original_resume("SECRET ORIGINAL RESUME", "resume.pdf")
    add_resume(store, 1)
    add_job(store, 2, jd_text="jobright description kept locally")
    await engine_for(store, server).sync()
    sent = str(server.push_calls)
    assert "SECRET" not in sent
    assert "jobright description kept locally" not in sent
