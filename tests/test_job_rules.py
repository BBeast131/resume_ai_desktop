from __future__ import annotations

import ast
from dataclasses import FrozenInstanceError, dataclass
from pathlib import Path

import pytest

from app.automation import job_rules
from app.automation.job_rules import (
    BLOCK_MESSAGES,
    FORM_ONLY_MIN_FIELDS,
    FORM_ONLY_PROSE_MAX,
    CaptureDecision,
    blocked_toast,
    decide_capture,
    is_form_only,
    is_jobright_detail_url,
    saved_toast,
    site_of,
    url_key,
)


def test_module_does_not_import_qt():
    tree = ast.parse(Path(job_rules.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= {"__future__", "re", "dataclasses", "typing", "urllib"}


def test_block_messages_cover_every_reason():
    assert set(BLOCK_MESSAGES) == {"captcha", "login", "consent", "http_error", "timeout", "empty"}
    assert BLOCK_MESSAGES["login"] == "page needs sign-in"
    assert BLOCK_MESSAGES["captcha"] == "page asks to verify you are human"
    assert all(text and text == text.strip() and not text.endswith(".") for text in BLOCK_MESSAGES.values())


# --- site_of -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "site"),
    [
        ("https://jobright.ai/jobs/info/abc", "jobright"),
        ("https://www.jobright.ai/", "jobright"),
        ("https://app.JobRight.ai/jobs", "jobright"),
        ("https://hiring.cafe/viewjob/abc", "hiring_cafe"),
        ("https://www.hiringcafe.com/", "hiring_cafe"),
        ("http://jobs.hiring.cafe/x", "hiring_cafe"),
        ("https://www.linkedin.com/jobs/view/123", "linkedin"),
        ("https://uk.linkedin.com/jobs/view/123", "linkedin"),
        ("https://lnkd.in/abc", "linkedin"),
        ("https://chatgpt.com/c/123", "chatgpt"),
        ("https://chat.openai.com/", "chatgpt"),
        ("https://boards.greenhouse.io/acme/jobs/1", "other"),
        # Lookalikes: the name appears in the host, but the site is someone else's.
        ("https://jobright.ai.evil.example/jobs/info/abc", "other"),
        ("https://notjobright.ai/", "other"),
        ("https://hiring.cafe.evil.example/", "other"),
        ("https://evil.example/?next=https://jobright.ai/", "other"),
        ("https://evil.example/linkedin.com", "other"),
        ("https://jobright.ai@evil.example/", "other"),
        ("javascript:alert(1)", "other"),
        ("ftp://jobright.ai/", "other"),
        ("not a url", "other"),
        ("", "other"),
    ],
)
def test_site_of(url, site):
    assert site_of(url) == site


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://jobright.ai/jobs/info/66f0a1b2c3d4", True),
        ("https://jobright.ai/jobs/info/66f0a1b2c3d4/?utm_source=x", True),
        ("https://www.jobright.ai/jobs/info/66f0a1b2c3d4#top", True),
        ("https://jobright.ai/jobs/recommend?jobId=66f0a1b2c3d4", True),
        ("https://jobright.ai/jobs/recommend", False),
        ("https://jobright.ai/jobs/info/", False),
        ("https://jobright.ai/", False),
        ("https://jobright.ai/jobs", False),
        ("https://jobright.ai.evil.example/jobs/info/66f0", False),
        ("https://hiring.cafe/jobs/info/66f0", False),
    ],
)
def test_is_jobright_detail_url(url, expected):
    assert is_jobright_detail_url(url) is expected


# --- url_key -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("left", "right"),
    [
        # www, case of the host, trailing slash, fragment.
        ("https://www.Example.com/careers/123/", "https://example.com/careers/123#apply"),
        ("http://example.com/careers/123", "https://example.com/careers/123"),
        ("https://example.com:443/careers/123", "https://example.com/careers/123"),
        ("https://example.com/Careers/ABC", "https://example.com/careers/abc"),
        # Tracking parameters go; identifying ones stay; order does not matter.
        (
            "https://example.com/job?utm_source=li&utm_medium=x&gclid=1&fbclid=2&ref=a&source=b&src=c&jobId=77",
            "https://example.com/job?jobId=77",
        ),
        ("https://example.com/job?b=2&a=1", "https://example.com/job?a=1&b=2"),
        ("https://example.com/job?JobId=77", "https://example.com/job?jobid=77"),
        ("https://www.indeed.com/viewjob?jk=abc123&tk=zzz&from=serp&vjs=3", "https://indeed.com/viewjob?jk=abc123"),
        # The posting and its application step are one job (job-page.test.ts).
        ("https://example.com/careers/123?utm_source=x", "https://example.com/careers/123/apply"),
        (
            "https://jobs.lever.co/palantir/22053072-4c22-49c4-8299-28e107ceeb98",
            "https://jobs.lever.co/palantir/22053072-4C22-49C4-8299-28E107CEEB98/apply?lever-source=LinkedIn",
        ),
        (
            "https://jobs.ashbyhq.com/ramp/b68aca53-16c0-4ced-ab1d-e8beb2940b4f",
            "https://jobs.ashbyhq.com/ramp/b68aca53-16c0-4ced-ab1d-e8beb2940b4f/application",
        ),
        (
            "https://job-boards.greenhouse.io/anthropic/jobs/5186067008?gh_src=abc",
            "https://job-boards.greenhouse.io/embed/job_app?for=anthropic&token=5186067008",
        ),
        ("https://boards.greenhouse.io/acme?gh_jid=123", "https://boards.greenhouse.io/acme/jobs/123#app"),
        (
            "https://apply.workable.com/huggingface/j/F8427A442D/",
            "https://apply.workable.com/huggingface/j/f8427a442d/apply",
        ),
        ("https://jobright.ai/jobs/info/66f0?utm_campaign=x", "https://www.jobright.ai/jobs/info/66f0/"),
        ("  https://example.com/a  ", "https://example.com/a"),
    ],
)
def test_url_key_same_posting(left, right):
    assert url_key(left) == url_key(right)
    assert url_key(left)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        (
            "https://job-boards.greenhouse.io/anthropic/jobs/5186067008",
            "https://job-boards.greenhouse.io/anthropic/jobs/5183051008",
        ),
        # `for` and `token` are the whole identity of an embedded Greenhouse form.
        (
            "https://job-boards.greenhouse.io/embed/job_app?for=spycloud&token=7644006003",
            "https://job-boards.greenhouse.io/embed/job_app?for=spycloud&token=7644006004",
        ),
        ("https://acme.com/careers?gh_jid=1", "https://acme.com/careers?gh_jid=2"),
        ("https://example.com/job?jobId=1", "https://example.com/job?jobId=2"),
        ("https://example.com/job?job_id=1", "https://example.com/job?job_id=2"),
        ("https://example.com/job?id=1", "https://example.com/job?id=2"),
        ("https://indeed.com/viewjob?jk=a", "https://indeed.com/viewjob?jk=b"),
        ("https://example.com/jobs/1", "https://example.com/jobs/2"),
        ("https://a.example.com/jobs/1", "https://b.example.com/jobs/1"),
        ("https://example.com:8443/jobs/1", "https://example.com/jobs/1"),
        ("https://careers.example.com/#/jobs/1", "https://careers.example.com/#/jobs/2"),
        ("https://jobright.ai/jobs/info/aaa", "https://jobright.ai/jobs/info/bbb"),
    ],
)
def test_url_key_different_postings(left, right):
    assert url_key(left) != url_key(right)


def test_url_key_values():
    assert url_key("https://job-boards.greenhouse.io/anthropic/jobs/5186067008?gh_src=abc") == (
        "greenhouse:anthropic:5186067008"
    )
    assert url_key("https://jobs.lever.co/Palantir/22053072-4c22-49c4-8299-28e107ceeb98/apply") == (
        "lever:palantir:22053072-4c22-49c4-8299-28e107ceeb98"
    )
    assert url_key("https://acme.workable.com/j/ab12cd34ef") == "workable:AB12CD34EF"
    assert url_key("https://WWW.Example.com/Jobs/42/?utm_source=x&b=2&a=1#frag") == "example.com/jobs/42?a=1&b=2"
    assert url_key("https://example.com") == "example.com"
    # A host that only looks like an ATS gets no ATS key.
    assert url_key("https://greenhouse.io.evil.example/acme/jobs/1") == "greenhouse.io.evil.example/acme/jobs/1"
    # Identifiers outside the safe character set are not treated as ATS ids.
    assert not url_key("https://job-boards.greenhouse.io/ac%2Fme/jobs/1").startswith("greenhouse:")


def test_url_key_of_unusable_input():
    assert url_key("") == ""
    assert url_key("   ") == ""
    assert url_key("not a url") == "not a url"
    assert url_key("javascript:alert(1)") == "javascript:alert(1)"
    assert url_key("http://[bad") == "http://[bad"
    assert url_key("not a url") != url_key("another one")


# --- form-only -----------------------------------------------------------------


def test_form_only_thresholds():
    assert (FORM_ONLY_PROSE_MAX, FORM_ONLY_MIN_FIELDS) == (600, 3)
    assert is_form_only(False, 0, 3) is True
    assert is_form_only(False, 599, 3) is True
    assert is_form_only(False, 600, 3) is False  # enough prose around the form
    assert is_form_only(False, 599, 2) is False  # too few fields to be an application form
    assert is_form_only(True, 0, 30) is False  # the posting is in the page's data
    assert is_form_only(False, 5000, 30) is False
    assert is_form_only(False, 0, 0) is False


# --- decide_capture ------------------------------------------------------------


@dataclass
class Page:
    """The few things decide_capture reads; stands in for extractor.Extraction."""

    url: str
    role: str = "Staff Engineer"
    company: str = "Acme"
    jd_text: str = "A real description. " * 40
    is_linkedin: bool = False
    linkedin_source: bool = False
    blocked: bool = False
    block_reason: str | None = None
    form_only: bool = False
    has_jd: bool = True


JOBRIGHT = "https://jobright.ai/jobs/info/66f0"
REAL_SITE = "https://boards.greenhouse.io/acme/jobs/1"


def test_jobright_detail_page_is_saved_with_its_own_description():
    page = Page(JOBRIGHT, jd_text="  jobright's copy of the description  ")
    assert decide_capture(page, "jobright") == CaptureDecision(
        save=True, code="saved", toast="Saved: Staff Engineer at Acme", jd_text="jobright's copy of the description"
    )


def test_jobright_detail_page_without_readable_description_is_still_saved():
    decision = decide_capture(Page(JOBRIGHT, jd_text="", has_jd=False), "jobright")
    assert (decision.save, decision.code, decision.jd_text) == (True, "saved", None)


def test_jobright_page_from_linkedin_is_not_saved():
    decision = decide_capture(Page(JOBRIGHT, linkedin_source=True), "jobright")
    assert decision == CaptureDecision(False, "linkedin", "Skipped: LinkedIn job", None)


def test_jobright_page_that_is_not_a_job_is_not_saved():
    decision = decide_capture(Page("https://jobright.ai/jobs/recommend"), "jobright")
    assert (decision.save, decision.code, decision.toast) == (False, "not_a_job", "Skipped: not a job page")


def test_blocked_jobright_page_says_why():
    decision = decide_capture(Page(JOBRIGHT, blocked=True, block_reason="login", has_jd=False), "jobright")
    assert decision == CaptureDecision(False, "needs_user_action", "Not saved: page needs sign-in", None)


@pytest.mark.parametrize("opener", ["jobright", "hiring_cafe", "other"])
def test_linkedin_tab_is_never_saved(opener):
    by_url = decide_capture(Page("https://www.linkedin.com/jobs/view/123"), opener)
    by_flag = decide_capture(Page("https://example.com/redirected", is_linkedin=True), opener)
    assert by_url == by_flag == CaptureDecision(False, "linkedin", "Skipped: LinkedIn job", None)


def test_blocked_linkedin_tab_is_still_a_linkedin_skip():
    page = Page("https://www.linkedin.com/authwall", blocked=True, block_reason="login", has_jd=False)
    assert decide_capture(page, "hiring_cafe").code == "linkedin"


def test_real_job_site_from_hiring_cafe_with_a_description_is_saved():
    decision = decide_capture(Page(REAL_SITE), "hiring_cafe")
    # The real site's description is read again by the pipeline; only jobright's copy is stored at capture.
    assert decision == CaptureDecision(True, "saved", "Saved: Staff Engineer at Acme", None)


def test_hiring_cafe_job_view_with_a_description_is_saved():
    assert decide_capture(Page("https://hiring.cafe/viewjob/abc"), "hiring_cafe").code == "saved"
    linkedin = decide_capture(Page("https://hiring.cafe/viewjob/abc", linkedin_source=True), "hiring_cafe")
    assert (linkedin.save, linkedin.code) == (False, "linkedin")


def test_form_only_page_is_not_saved():
    page = Page(REAL_SITE, form_only=True, has_jd=False, jd_text="")
    assert decide_capture(page, "hiring_cafe") == CaptureDecision(
        False, "form_only_no_jd", "Skipped: no job description", None
    )


@pytest.mark.parametrize(
    ("reason", "toast"),
    [
        ("login", "Not saved: page needs sign-in"),
        ("captcha", "Not saved: page asks to verify you are human"),
        ("consent", "Not saved: page is behind a consent wall"),
        ("http_error", "Not saved: page is not available"),
        ("timeout", "Not saved: page took too long to load"),
        ("empty", "Not saved: no job details could be read"),
    ],
)
def test_blocked_page_is_not_saved_and_says_why(reason, toast):
    page = Page(REAL_SITE, blocked=True, block_reason=reason, has_jd=False, jd_text="")
    assert decide_capture(page, "hiring_cafe") == CaptureDecision(False, "needs_user_action", toast, None)
    assert toast == blocked_toast(reason)


def test_blocked_form_keeps_its_real_reason():
    page = Page(REAL_SITE, blocked=True, block_reason="login", form_only=True, has_jd=False)
    assert decide_capture(page, "hiring_cafe").code == "needs_user_action"


def test_page_without_a_description_is_not_saved():
    decision = decide_capture(Page(REAL_SITE, has_jd=False, jd_text="too short"), "hiring_cafe")
    assert (decision.save, decision.code, decision.toast) == (False, "not_a_job", "Skipped: no job description")


def test_real_site_opened_from_a_jobright_page_is_not_saved_twice():
    decision = decide_capture(Page(REAL_SITE), "jobright")
    assert (decision.save, decision.code, decision.jd_text) == (False, "not_a_job", None)
    assert decision.toast.startswith("Skipped: ")


def test_chatgpt_tab_is_not_a_job():
    assert decide_capture(Page("https://chatgpt.com/c/1"), "other").code == "not_a_job"


def test_toasts():
    assert saved_toast("Staff  Engineer", " Acme ") == "Saved: Staff Engineer at Acme"
    assert saved_toast("Staff Engineer", "") == "Saved: Staff Engineer"
    assert saved_toast("", "") == "Saved: job"
    assert blocked_toast(None) == blocked_toast("something-new") == "Not saved: no job details could be read"


def test_capture_decision_is_frozen():
    decision = CaptureDecision(True, "saved", "Saved: x", None)
    with pytest.raises(FrozenInstanceError):
        decision.save = False  # type: ignore[misc]
