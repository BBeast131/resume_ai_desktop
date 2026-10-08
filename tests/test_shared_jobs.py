"""The shared job list: every profile's finds, each profile with its own progress."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from alembic import command
from sqlalchemy import select, text

from app.data.db import Database, _alembic_config
from app.data.models import SavedJob
from app.data.store import Store
from app.data.types import to_iso, utcnow
from app.sync.engine import SHARED_HISTORY, SyncEngine, public_job_payload
from tests.factories import add_job, add_resume, make_store
from tests.fake_server import FakeServer, SharedPool

ANNA = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
BOB = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def days_ago(days: float):
    return utcnow() - timedelta(days=days)


def profile(tmp_path: Path, account: str, pool: SharedPool) -> tuple[Store, FakeServer, SyncEngine]:
    store = make_store(tmp_path, account)
    server = FakeServer(pool)
    return store, server, SyncEngine(store, server)  # type: ignore[arg-type]


def shared(n: int, **overrides):
    values = {
        "url": f"https://jobright.ai/jobs/info/{n}",
        "urlKey": f"jobright.ai/jobs/info/{n}",
        "sourceSite": "jobright",
        "role": f"Engineer {n}",
        "company": f"Company {n}",
        "jdText": "A job description.",
        "foundAt": to_iso(days_ago(10 - n / 10)),
    }
    values.update(overrides)
    return values


# ---------------------------------------------------------------------------
# What goes on the list
# ---------------------------------------------------------------------------


def test_jobs_in_the_list_and_recent_finds_are_published_once(tmp_path):
    store = make_store(tmp_path)
    live = add_job(store, 1, now=days_ago(40)).job
    recent = add_job(store, 2, now=days_ago(3)).job
    old = add_job(store, 3, now=days_ago(30)).job
    store.remove_saved_jobs([recent.id, old.id])

    pending = store.unpublished_jobs(since=utcnow() - SHARED_HISTORY, limit=50)
    assert [job.id for job in pending] == [live.id, recent.id]  # an old, finished job stays private

    store.mark_published([live.id])
    assert [job.id for job in store.unpublished_jobs(since=utcnow() - SHARED_HISTORY, limit=50)] == [recent.id]
    assert store.get_saved_job(live.id).dirty is True  # publishing is not a change to sync


def test_the_published_payload_keeps_when_the_job_was_found(tmp_path):
    store = make_store(tmp_path)
    job = add_job(store, 1, now=days_ago(2), role="R" * 300, jd_text="x" * 40_000).job
    payload = public_job_payload(store.get_saved_job(job.id))
    assert payload["urlKey"] == "jobright.ai/jobs/info/1" and payload["foundAt"] == to_iso(job.created_at)
    assert len(payload["role"]) == 160 and len(payload["jdText"]) == 30_000


# ---------------------------------------------------------------------------
# What comes in from the list
# ---------------------------------------------------------------------------


def test_import_skips_what_this_profile_already_has_or_has_done(tmp_path):
    store = make_store(tmp_path)
    add_job(store, 1)  # in the list
    removed = add_job(store, 2).job
    store.remove_saved_jobs([removed.id])  # removed by this profile: stays removed
    add_resume(store, 9, url_key="jobright.ai/jobs/info/3")  # generated from the same link
    add_resume(store, 8, company="Company 4", role="Engineer 4")  # generated from another board
    add_job(
        store, 5, url="https://hiring.cafe/job/5", url_key="hiring.cafe/job/5", company="Company 5", role="Engineer 5"
    )

    context = store.shared_import_context()
    offered = [shared(n) for n in range(1, 8)]
    added = store.import_shared_jobs([*offered, shared(6)], context, now=days_ago(0))

    assert added == 2
    rows = {job.url_key: job for job in store.list_saved_jobs()}
    assert set(rows) == {
        "jobright.ai/jobs/info/1",
        "hiring.cafe/job/5",
        "jobright.ai/jobs/info/6",
        "jobright.ai/jobs/info/7",
    }
    six = rows["jobright.ai/jobs/info/6"]
    assert six.shared and six.dirty and six.published_at is not None
    assert to_iso(six.created_at) == offered[5]["foundAt"]  # keeps when it was first found
    assert not rows["jobright.ai/jobs/info/1"].shared
    assert store.get_saved_job(removed.id).deleted_at is not None


def test_imported_jobs_queue_in_the_order_they_were_found(tmp_path):
    store = make_store(tmp_path)
    own = add_job(store, 50, now=days_ago(5)).job
    store.import_shared_jobs(
        [shared(1, foundAt=to_iso(days_ago(1))), shared(2, foundAt=to_iso(days_ago(9)))], store.shared_import_context()
    )
    assert [job.url_key for job in store.queue(include_attention=False)] == [
        "jobright.ai/jobs/info/2",
        own.url_key,
        "jobright.ai/jobs/info/1",
    ]


def test_bad_rows_from_the_server_are_ignored(tmp_path):
    store = make_store(tmp_path)
    added = store.import_shared_jobs(
        [shared(1, foundAt="yesterday"), shared(2, urlKey=""), shared(3, url=None), shared(4, sourceSite="monster")],
        store.shared_import_context(),
    )
    assert added == 1 and store.list_saved_jobs()[0].source_site == "other"


def test_finding_a_shared_job_yourself_makes_it_your_own(tmp_path):
    store = make_store(tmp_path)
    store.import_shared_jobs([shared(1)], store.shared_import_context())
    job = store.list_saved_jobs()[0]
    store.remove_saved_jobs([job.id])
    result = add_job(store, 1)
    assert result.outcome == "restored" and result.job.shared is False


# ---------------------------------------------------------------------------
# Two profiles, end to end through sync
# ---------------------------------------------------------------------------


async def test_two_profiles_share_finds_but_keep_their_own_progress(tmp_path):
    pool = SharedPool()
    anna, anna_server, anna_sync = profile(tmp_path, ANNA, pool)
    bob, bob_server, bob_sync = profile(tmp_path, BOB, pool)
    arrived: list[int] = []
    bob_sync.on_jobs_shared = arrived.append

    first = add_job(anna, 1, now=days_ago(3)).job
    add_job(anna, 2, now=days_ago(2))
    add_job(anna, 3, now=days_ago(1))
    add_resume(bob, 9, company="Company 2", role="Engineer 2")  # Bob already did this one elsewhere
    await anna_sync.sync()
    assert [row["urlKey"] for row in pool.rows] == [f"jobright.ai/jobs/info/{n}" for n in (1, 2, 3)]

    status = await bob_sync.sync()
    assert status.phase == "idle" and status.pending == 0  # the new rows were pushed in the same sync
    assert arrived == [2]
    assert [job.url_key for job in bob.queue(include_attention=False)] == [
        "jobright.ai/jobs/info/1",
        "jobright.ai/jobs/info/3",
    ]
    assert {row["url"] for row in bob_server.saved_jobs.values()} == {
        "https://jobright.ai/jobs/info/1",
        "https://jobright.ai/jobs/info/3",
    }
    assert pool.pull_calls[0]["since"] is not None  # a first read starts a fortnight back

    # Anna makes a resume for job 1; Bob still has it. Bob removes job 3; Anna still has it.
    add_resume(anna, 1, saved_job_id=first.id, url_key=first.url_key)
    bob.remove_saved_jobs([job.id for job in bob.list_saved_jobs() if job.url_key.endswith("/3")])
    await anna_sync.sync()
    await bob_sync.sync()
    assert [job.url_key for job in anna.list_saved_jobs()] == ["jobright.ai/jobs/info/2", "jobright.ai/jobs/info/3"]
    assert [job.url_key for job in bob.list_saved_jobs()] == ["jobright.ai/jobs/info/1"]

    # Bob finds a new job; Anna gets it on her next sync, and nothing comes back twice.
    add_job(bob, 4)
    await bob_sync.sync()
    await anna_sync.sync()
    assert [job.url_key for job in anna.list_saved_jobs()][-1] == "jobright.ai/jobs/info/4"
    assert anna.get_saved_job(anna.list_saved_jobs()[-1].id).shared
    assert pool.pull_calls[-1]["since"] is None and anna.sync_state().shared_cursor == 4
    await anna_sync.sync()
    assert len(anna.list_saved_jobs()) == 3 and len(pool.rows) == 4


async def test_an_older_web_app_without_the_shared_list_does_not_break_sync(tmp_path):
    pool = SharedPool()
    pool.missing = True
    store, server, engine = profile(tmp_path, ANNA, pool)
    job = add_job(store, 1).job
    status = await engine.sync()
    assert status.phase == "idle" and status.last_error is None
    assert job.id in server.saved_jobs
    assert store.get_saved_job(job.id).published_at is None  # shared once the web app has the list

    pool.missing = False
    await engine.sync()
    assert [row["urlKey"] for row in pool.rows] == [job.url_key]


async def test_a_large_first_read_is_paged(tmp_path):
    pool = SharedPool()
    pool.push([shared(n, foundAt=to_iso(days_ago(1))) | {"urlKey": f"k/{n}", "company": f"C{n}"} for n in range(50)])
    pool.push(
        [shared(n, foundAt=to_iso(days_ago(1))) | {"urlKey": f"k/{n}", "company": f"C{n}"} for n in range(50, 120)][:50]
    )
    store, _server, engine = profile(tmp_path, BOB, pool)
    await engine.sync()
    assert store.saved_job_count() == 100 and store.sync_state().shared_cursor == 100
    assert [call["after"] for call in pool.pull_calls[:3]] == [0, 50, 100]


# ---------------------------------------------------------------------------
# An existing database gets the new columns
# ---------------------------------------------------------------------------


def test_an_existing_database_is_upgraded_in_place(tmp_path):
    database = Database(tmp_path / "old.db", ANNA)
    config = _alembic_config()
    with database.engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "0001")
    with database.engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO saved_jobs (id, source_site, url, url_key, created_at, updated_at, dirty) "
                "VALUES ('j1', 'jobright', 'https://jobright.ai/jobs/info/1', 'jobright.ai/jobs/info/1', "
                "'2026-10-01T00:00:00.000Z', '2026-10-01T00:00:00.000Z', 1)"
            )
        )

    database.upgrade()
    store = Store(database)
    with database.session() as session:
        job = session.scalar(select(SavedJob))
    assert job is not None and job.shared is False and job.published_at is None
    assert store.sync_state().shared_cursor is None
    assert database.meta("schema_version") == "1"  # the web importer still accepts the file
