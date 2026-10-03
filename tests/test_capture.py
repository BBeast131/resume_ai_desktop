"""Job Search capture: which opened pages are saved, and what the toast says."""

from __future__ import annotations

from pathlib import Path

from app.automation.capture import capture_job
from tests.factories import make_store
from tests.fake_api import FakeApi
from tests.helpers import FIXTURES, load_html


def html(name: str) -> str:
    return (FIXTURES / "pages" / name).read_text(encoding="utf-8")


JOBRIGHT_HOME = "https://jobright.ai/jobs/recommend"
HIRING_CAFE = "https://hiring.cafe/"


async def test_a_jobright_detail_page_is_saved_with_jobrights_description(web_page, tmp_path: Path):
    store, api = make_store(tmp_path), FakeApi()
    await load_html(web_page, html("jobright_detail.html"), "https://jobright.ai/jobs/info/abc123")
    result = await capture_job(web_page, JOBRIGHT_HOME, store, api)

    assert result.saved and result.kind == "success" and result.toast.startswith("Saved: ")
    job = store.list_saved_jobs()[0]
    assert job.source_site == "jobright" and job.url == "https://jobright.ai/jobs/info/abc123"
    assert job.jd_text and len(job.jd_text) > 100  # kept: the real site may be a bare form
    assert job.role and job.company and f"{job.role} at {job.company}" in result.toast
    assert job.extraction_confidence == 0.9
    # The check saw only the first part of the description.
    assert len(api.check_calls[0]["jdText"]) <= 4000

    # Opening the same job again is a duplicate, not a second row.
    again = await capture_job(web_page, JOBRIGHT_HOME, store, api)
    assert again.code == "duplicate" and again.toast == "Already saved" and store.saved_job_count() == 1


async def test_a_jobright_job_from_linkedin_is_not_saved(web_page, tmp_path: Path):
    store = make_store(tmp_path)
    await load_html(web_page, html("jobright_linkedin.html"), "https://jobright.ai/jobs/info/li1")
    result = await capture_job(web_page, JOBRIGHT_HOME, store, FakeApi())
    assert (result.code, result.toast) == ("linkedin", "Skipped: LinkedIn job") and store.saved_job_count() == 0


async def test_a_tab_on_linkedin_is_never_saved(web_page, tmp_path: Path):
    store = make_store(tmp_path)
    await load_html(web_page, html("linkedin_job.html"), "https://www.linkedin.com/jobs/view/123")
    result = await capture_job(web_page, HIRING_CAFE, store, FakeApi())
    assert result.code == "linkedin" and store.saved_job_count() == 0


async def test_hiring_cafe_real_site_with_a_description_is_saved(web_page, tmp_path: Path):
    store = make_store(tmp_path)
    await load_html(web_page, html("greenhouse_job.html"), "https://boards.greenhouse.io/acme/jobs/4011")
    result = await capture_job(web_page, HIRING_CAFE, store, FakeApi())
    assert result.saved
    job = store.list_saved_jobs()[0]
    assert job.source_site == "hiring_cafe" and job.jd_text is None
    assert job.url == "https://boards.greenhouse.io/acme/jobs/4011"


async def test_a_form_only_page_is_not_saved(web_page, tmp_path: Path):
    store = make_store(tmp_path)
    await load_html(web_page, html("form_only.html"), "https://jobs.example.com/apply/55")
    result = await capture_job(web_page, HIRING_CAFE, store, FakeApi())
    assert (result.code, result.toast) == ("form_only_no_jd", "Skipped: no job description")
    assert store.saved_job_count() == 0


async def test_a_blocked_page_is_not_saved_and_the_toast_says_why(web_page, tmp_path: Path):
    store = make_store(tmp_path)
    await load_html(web_page, html("blocked_login.html"), "https://careers.example.com/job/9")
    result = await capture_job(web_page, HIRING_CAFE, store, FakeApi())
    assert result.code == "needs_user_action" and result.kind == "warning"
    assert result.toast == "Not saved: page needs sign-in" and store.saved_job_count() == 0


async def test_the_ai_check_can_overrule_and_corrects_role_and_company(web_page, tmp_path: Path):
    store, api = make_store(tmp_path), FakeApi()
    await load_html(web_page, html("greenhouse_job.html"), "https://boards.greenhouse.io/acme/jobs/4011")
    api.check_reply = {
        "role": "Platform Engineer",
        "company": "Acme Corp",
        "isJobPosting": True,
        "hasJobDescription": True,
        "confidence": 0.8,
        "notes": "",
        "source": "groq",
        "roleCorrected": True,
        "companyCorrected": True,
    }
    result = await capture_job(web_page, HIRING_CAFE, store, api)
    assert result.toast == "Saved: Platform Engineer at Acme Corp"

    other = make_store(tmp_path / "second")
    api.check_reply = {**api.check_reply, "hasJobDescription": False}
    result = await capture_job(web_page, HIRING_CAFE, other, api)
    assert result.code == "form_only_no_jd" and other.saved_job_count() == 0


async def test_capture_still_works_when_the_check_is_unavailable(web_page, tmp_path: Path):
    store, api = make_store(tmp_path), FakeApi()
    api.offline = True
    await load_html(web_page, html("ldjson_job.html"), "https://careers.example.com/jobs/77")
    result = await capture_job(web_page, HIRING_CAFE, store, api)
    assert result.saved
    assert store.list_saved_jobs()[0].extraction_confidence is None  # marks "not checked"
