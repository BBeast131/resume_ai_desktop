from __future__ import annotations

import sqlite3

import pytest

from app.data.store import ResumeFilter
from app.data.types import from_iso, to_iso
from tests.factories import ACCOUNT, add_job, add_resume, make_store, utc


def test_file_is_stamped_for_the_web_importer(tmp_path):
    store = make_store(tmp_path)
    raw = sqlite3.connect(store.database.path)
    meta = dict(raw.execute("select key, value from meta").fetchall())
    assert meta["schema_version"] == "1"
    assert meta["account_id"] == ACCOUNT
    assert raw.execute("pragma journal_mode").fetchone()[0].lower() == "wal"


def test_timestamps_are_iso_text_that_sorts(tmp_path):
    store = make_store(tmp_path)
    add_resume(store, now=utc(2026, 10, 2, 14, 5))
    raw = sqlite3.connect(store.database.path)
    assert raw.execute("select created_at from generated_resumes").fetchone()[0] == "2026-10-02T14:05:00.000Z"
    assert from_iso(to_iso(utc(2026, 1, 2, 3, 4))) == utc(2026, 1, 2, 3, 4)


def test_saved_jobs_are_deduplicated_by_url_key(tmp_path):
    store = make_store(tmp_path)
    assert add_job(store, 1).outcome == "saved"
    assert add_job(store, 1).outcome == "duplicate_saved"
    assert store.saved_job_count() == 1


def test_a_job_already_generated_is_a_duplicate(tmp_path):
    store = make_store(tmp_path)
    add_resume(store, 7)
    assert add_job(store, 7).outcome == "duplicate_generated"


def test_saved_jobs_list_oldest_first_by_default(tmp_path):
    store = make_store(tmp_path)
    add_job(store, 2, now=utc(2026, 10, 2))
    add_job(store, 1, now=utc(2026, 10, 1))
    assert [job.role for job in store.list_saved_jobs()] == ["Engineer 1", "Engineer 2"]
    assert [job.role for job in store.list_saved_jobs(newest_first=True)] == ["Engineer 2", "Engineer 1"]
    assert [job.role for job in store.list_saved_jobs(search="company 2")] == ["Engineer 2"]


def test_removal_is_a_soft_delete_that_syncs(tmp_path):
    store = make_store(tmp_path)
    job = add_job(store, 1).job
    store.database.session  # noqa: B018
    assert store.remove_saved_jobs([job.id]) == 1
    assert store.saved_job_count() == 0
    removed = store.get_saved_job(job.id)
    assert removed.deleted_at is not None and removed.dirty is True
    # Saving the same URL again brings the row back.
    assert add_job(store, 1).outcome == "restored"
    assert store.saved_job_count() == 1


def test_saving_a_resume_removes_its_job_in_one_transaction(tmp_path):
    store = make_store(tmp_path)
    job = add_job(store, 1).job
    resume = add_resume(store, 1, saved_job_id=job.id)
    assert store.saved_job_count() == 0
    assert store.resume_count() == 1
    assert resume.status == "generated" and resume.dirty is True


def test_invalid_json_is_never_stored_and_the_job_stays(tmp_path):
    store = make_store(tmp_path)
    job = add_job(store, 1).job
    with pytest.raises(ValueError):
        add_resume(store, 1, saved_job_id=job.id, resume_json="{not json")
    assert store.resume_count() == 0
    assert store.saved_job_count() == 1


def test_milestones_are_set_once_and_never_cleared(tmp_path):
    store = make_store(tmp_path)
    resume = add_resume(store, now=utc(2026, 10, 1))

    store.set_status(resume.id, "applied", now=utc(2026, 10, 2))
    store.set_status(resume.id, "shortlisted", now=utc(2026, 10, 5))
    current = store.set_status(resume.id, "generated", now=utc(2026, 10, 6))
    assert current.status == "generated"
    assert current.applied_at == utc(2026, 10, 2)
    assert current.shortlisted_at == utc(2026, 10, 5)
    assert current.status_changed_at == utc(2026, 10, 6)

    # Reaching a stage again does not move its first date.
    again = store.set_status(resume.id, "applied", now=utc(2026, 10, 9))
    assert again.applied_at == utc(2026, 10, 2)
    assert [event.to_status for event in store.status_events(resume.id)] == [
        "applied",
        "shortlisted",
        "generated",
        "applied",
    ]


def test_shortlisted_or_rejected_implies_applied(tmp_path):
    store = make_store(tmp_path)
    one = add_resume(store, 1)
    two = add_resume(store, 2)
    shortlisted = store.set_status(one.id, "shortlisted", now=utc(2026, 10, 3))
    rejected = store.set_status(two.id, "rejected", now=utc(2026, 10, 4))
    assert shortlisted.applied_at == utc(2026, 10, 3)
    assert rejected.applied_at == utc(2026, 10, 4) and rejected.rejected_at == utc(2026, 10, 4)


def test_unknown_status_is_refused(tmp_path):
    store = make_store(tmp_path)
    resume = add_resume(store)
    with pytest.raises(ValueError):
        store.set_status(resume.id, "hired")


def test_resume_filters(tmp_path):
    store = make_store(tmp_path)
    a = add_resume(store, 1, now=utc(2026, 10, 1))
    add_resume(store, 2, now=utc(2026, 10, 3))
    store.set_status(a.id, "applied", now=utc(2026, 10, 4))

    assert [r.company for r in store.list_resumes()] == ["Acme 2", "Acme 1"]
    assert [r.company for r in store.list_resumes(ResumeFilter(newest_first=False))] == ["Acme 1", "Acme 2"]
    assert [r.company for r in store.list_resumes(ResumeFilter(statuses=("applied",)))] == ["Acme 1"]
    assert [r.company for r in store.list_resumes(ResumeFilter(search="acme 2"))] == ["Acme 2"]
    in_range = ResumeFilter(date_from=utc(2026, 10, 2), date_to=utc(2026, 10, 4))
    assert [r.company for r in store.list_resumes(in_range)] == ["Acme 2"]
    by_applied = ResumeFilter(date_field="applied_at", date_from=utc(2026, 10, 4), date_to=utc(2026, 10, 5))
    assert [r.company for r in store.list_resumes(by_applied)] == ["Acme 1"]


def test_skip_log_and_setting_a_job_aside(tmp_path):
    store = make_store(tmp_path)
    cancelled = add_job(store, 1).job
    blocked = add_job(store, 2).job

    store.skip_saved_job(cancelled.id, reason="user_cancelled")
    store.skip_saved_job(blocked.id, reason="needs_user_action", detail="page needs sign-in", keep_saved=True)

    remaining = store.list_saved_jobs()
    assert [job.id for job in remaining] == [blocked.id]
    assert remaining[0].attention_reason == "page needs sign-in"
    assert {skip.reason for skip in store.list_skips()} == {"user_cancelled", "needs_user_action"}

    # An Automation run leaves jobs needing attention alone unless asked.
    assert store.queue(include_attention=False) == []
    assert [job.id for job in store.queue(include_attention=True)] == [blocked.id]
    assert [job.id for job in store.list_saved_jobs(attention_only=True)] == [blocked.id]

    store.set_attention(blocked.id, None)
    assert len(store.queue(include_attention=False)) == 1


def test_prompt_and_resume_are_local_only(tmp_path):
    store = make_store(tmp_path)
    store.set_prompt("You are a resume writer.", "prompt.txt")
    store.set_original_resume("My resume", "resume.pdf")
    assets = store.assets()
    assert assets.prompt_filename == "prompt.txt" and assets.original_resume_text == "My resume"
    store.set_original_resume(None, None)
    assert store.assets().original_resume_text is None
    # user_assets has no sync columns: nothing here can ever be marked for sync.
    raw = sqlite3.connect(store.database.path)
    columns = {row[1] for row in raw.execute("pragma table_info(user_assets)")}
    assert "dirty" not in columns and "synced_at" not in columns


def test_export_copy_is_a_complete_single_file(tmp_path):
    store = make_store(tmp_path)
    add_resume(store)
    copy = store.database.export_copy(tmp_path / "out" / "resume_ai.db")
    raw = sqlite3.connect(copy)
    assert raw.execute("select count(*) from generated_resumes").fetchone()[0] == 1
