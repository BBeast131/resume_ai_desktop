"""The duplicate check on the saved list: the key and the single pass (the button is in test_ui)."""

from __future__ import annotations

import time

import pytest
from sqlalchemy import insert, select, update

from app.data.models import GeneratedResume, SavedJob, SkippedJob
from app.data.types import new_id
from app.services.dedupe import company_key, job_key, role_key
from tests.factories import add_job, add_resume, make_store, utc

# ---------------------------------------------------------------------------
# The key
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("Acme, Inc.", "ACME"),
        ("Acme Corporation", "acme"),
        ("AT&T", "AT and T"),
        ("Société Générale", "Societe Generale"),
        ("  Stripe  LLC ", "stripe"),
    ],
)
def test_company_key_ignores_case_punctuation_and_legal_suffix(left, right):
    assert company_key(left) == company_key(right) != ""


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("Sr. Software Engineer", "Senior Software Engineer"),
        ("Software Engineer (Remote)", "software engineer"),
        ("Software Engineer - Backend", "Software Engineer, Backend"),
        ("Engineering Mgr", "Engineering Manager"),
    ],
)
def test_role_key_ignores_notes_and_abbreviations(left, right):
    assert role_key(left) == role_key(right) != ""


def test_different_jobs_keep_different_keys():
    assert job_key("Acme", "Senior Engineer") != job_key("Acme", "Engineer")
    assert job_key("Acme", "Engineer") != job_key("Acme Labs", "Engineer")
    assert job_key("Acme", "C++ Developer") != job_key("Acme", "C# Developer")
    assert job_key("Co", "Engineer") is not None  # a company that is only a suffix word keeps its name


@pytest.mark.parametrize(("company", "role"), [(None, "Engineer"), ("Acme", None), ("", ""), ("...", "Engineer")])
def test_a_job_without_company_or_role_has_no_key(company, role):
    assert job_key(company, role) is None


# ---------------------------------------------------------------------------
# The pass over the saved list
# ---------------------------------------------------------------------------


def test_the_oldest_copy_stays_and_later_copies_go(tmp_path):
    store = make_store(tmp_path)
    first = add_job(store, 1, now=utc(2026, 10, 1), company="Acme, Inc.", role="Sr. Engineer").job
    second = add_job(store, 2, now=utc(2026, 10, 2), site="hiringcafe", company="ACME", role="Senior Engineer").job
    third = add_job(store, 3, now=utc(2026, 10, 3), company="Acme", role="Senior Engineer (Remote)").job
    other = add_job(store, 4, now=utc(2026, 10, 4), company="Acme", role="Staff Engineer").job

    report = store.remove_duplicate_saved_jobs(now=utc(2026, 10, 5))

    assert (report.examined, report.removed, report.same_as_saved, report.already_generated) == (4, 2, 2, 0)
    assert [job.id for job in store.list_saved_jobs()] == [first.id, other.id]
    for removed in (second, third):
        row = store.get_saved_job(removed.id)
        assert row.deleted_at == utc(2026, 10, 5) and row.dirty  # a soft delete the next sync carries


def test_a_job_that_already_has_a_resume_is_removed(tmp_path):
    store = make_store(tmp_path)
    add_resume(store, 9, company="Globex LLC", role="Platform Engineer")
    done = add_job(store, 1, now=utc(2026, 10, 1), company="Globex", role="Platform Engineer").job
    again = add_job(store, 2, now=utc(2026, 10, 2), company="Globex", role="Platform Engineer").job
    fresh = add_job(store, 3, now=utc(2026, 10, 3), company="Globex", role="Data Engineer").job

    report = store.remove_duplicate_saved_jobs()

    assert (report.removed, report.already_generated, report.same_as_saved) == (2, 2, 0)
    assert [job.id for job in store.list_saved_jobs()] == [fresh.id]
    assert {done.id, again.id}.isdisjoint(job.id for job in store.list_saved_jobs())


def test_a_deleted_resume_does_not_count(tmp_path):
    store = make_store(tmp_path)
    resume = add_resume(store, 9, company="Globex", role="Platform Engineer")
    with store.database.session() as session:
        session.execute(
            update(GeneratedResume).where(GeneratedResume.id == resume.id).values(deleted_at=utc(2026, 10, 1))
        )
    add_job(store, 1, company="Globex", role="Platform Engineer")
    assert store.remove_duplicate_saved_jobs().removed == 0


def test_jobs_without_a_company_or_role_are_never_duplicates(tmp_path):
    store = make_store(tmp_path)
    add_job(store, 1, company=None, role="Engineer")
    add_job(store, 2, company=None, role="Engineer")
    add_job(store, 3, company="Acme", role=None)
    add_job(store, 4, company="Acme", role=None)
    report = store.remove_duplicate_saved_jobs()
    assert (report.examined, report.removed) == (4, 0) and store.saved_job_count() == 4


def test_each_removed_job_is_logged_and_a_second_check_finds_nothing(tmp_path):
    store = make_store(tmp_path)
    add_resume(store, 9, company="Globex", role="Platform Engineer")
    add_job(store, 1, now=utc(2026, 10, 1), company="Globex", role="Platform Engineer")
    add_job(store, 2, now=utc(2026, 10, 2), company="Acme", role="Engineer")
    add_job(store, 3, now=utc(2026, 10, 3), company="Acme", role="Engineer")

    assert store.remove_duplicate_saved_jobs().removed == 2
    with store.database.session() as session:
        logged = session.execute(select(SkippedJob.url, SkippedJob.reason, SkippedJob.detail)).all()
    assert sorted((url, reason, detail) for url, reason, detail in logged) == [
        ("https://jobright.ai/jobs/info/1", "duplicate", "A resume was already generated for this job"),
        ("https://jobright.ai/jobs/info/3", "duplicate", "The same job is already in the saved list"),
    ]
    again = store.remove_duplicate_saved_jobs()
    assert (again.examined, again.removed) == (1, 0)


def test_light_lists_leave_the_job_description_out_but_keep_it_readable(tmp_path):
    store = make_store(tmp_path)
    job = add_job(store, 1, jd_text="A long job description. " * 20).job
    light = store.list_saved_jobs(light=True)
    assert [item.id for item in light] == [job.id] and light[0].company == "Company 1"
    assert [item.id for item in store.queue(include_attention=False, light=True)] == [job.id]
    assert store.get_saved_job(job.id).jd_text.startswith("A long job description.")


def test_the_check_stays_fast_on_a_large_list(tmp_path):
    store = make_store(tmp_path)
    total, distinct = 20_000, 15_000
    rows = [
        {
            "id": new_id(),
            "url": f"https://jobright.ai/jobs/info/{n}",
            "url_key": f"jobright.ai/jobs/info/{n}",
            "source_site": "jobright",
            "role": f"Senior Engineer {n % distinct}",
            "company": f"Company {n % distinct % 700}, Inc.",
            "jd_text": "x" * 4000,
            "created_at": utc(2026, 1, 1 + n % 28, n % 24, n % 60),
            "updated_at": utc(2026, 10, 1),
        }
        for n in range(total)
    ]
    with store.database.session() as session:
        for start in range(0, total, 2000):
            session.execute(insert(SavedJob), rows[start : start + 2000])

    started = time.perf_counter()
    report = store.remove_duplicate_saved_jobs()
    elapsed = time.perf_counter() - started

    assert (report.examined, report.removed) == (total, total - distinct)
    assert store.saved_job_count() == distinct
    assert elapsed < 5.0, f"the duplicate check took {elapsed:.2f}s for {total} saved jobs"
