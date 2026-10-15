"""The generation pipeline, end to end against local fixture pages.

The job pages and the ChatGPT page are local HTML; the web app is a fake.
The screen is a scripted host, so each test decides what "the user" does.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtCore import QUrl

from app.automation import chatgpt
from app.automation import pipeline as pipeline_module
from app.automation.pipeline import Pipeline, ReviewData, Stopped
from app.browser.browser import BrowserWidget
from app.data.models import SavedJob
from tests.factories import RESUME
from tests.fake_api import FakeApi, make_context
from tests.helpers import FIXTURES


def url(name: str) -> str:
    return QUrl.fromLocalFile(str(FIXTURES / "pipeline" / name)).toString()


CHATGPT = QUrl.fromLocalFile(str(FIXTURES / "chatgpt" / "composer.html")).toString()
LOGGED_OUT = QUrl.fromLocalFile(str(FIXTURES / "chatgpt" / "logged_out.html")).toString()


class Host:
    """A scripted screen."""

    def __init__(self, browser: BrowserWidget) -> None:
        self.browser = browser
        self.steps: list[tuple[int, str]] = []
        self.logs: list[str] = []
        self.marks: list[tuple[str, str]] = []
        self.reviews: list[ReviewData] = []
        self.blocked_calls: list[str] = []
        self.json_calls: list[tuple[int, int]] = []
        self.chat_blocked: list[str] = []
        # Scripted answers
        self.on_review: Any = lambda data: data
        self.blocked_answers: list[str] = []
        self.json_answers: list[str] = []
        self.chat_answers: list[str] = []
        self.web_answers: list[str] = []
        self.takeovers: list[tuple[int, str]] = []
        self.ready: list[tuple[str, str]] = []

        async def keep_going(_step: int) -> str:
            return "continue"

        self.on_taken_over: Any = keep_going

    def set_step(self, step: int, status: str) -> None:
        self.steps.append((step, status))

    def log(self, text: str) -> None:
        self.logs.append(text)

    def mark(self, job_id: str, state: str) -> None:
        self.marks.append((job_id, state))

    async def review(self, job: SavedJob, data: ReviewData, mode: str) -> ReviewData | None:
        self.reviews.append(data)
        return self.on_review(data)

    async def blocked(self, job: SavedJob, message: str, mode: str) -> str:
        self.blocked_calls.append(message)
        # Automation with no one at the keyboard: the grace period ends in "skip".
        return self.blocked_answers.pop(0) if self.blocked_answers else "skip"

    async def chatgpt_blocked(self, message: str) -> str:
        self.chat_blocked.append(message)
        return self.chat_answers.pop(0) if self.chat_answers else "stop"

    async def json_failed(self, attempt: int, issues: list[dict[str, Any]], raw: str, mode: str) -> str:
        self.json_calls.append((attempt, len(issues)))
        return self.json_answers.pop(0) if self.json_answers else "skip"

    async def web_unreachable(self, message: str) -> str:
        return self.web_answers.pop(0) if self.web_answers else "stop"

    async def countdown(self, seconds: int, text: str) -> None:
        return None

    async def taken_over(self, step: int, title: str, text: str) -> str:
        self.takeovers.append((step, title))
        return await self.on_taken_over(step)

    async def resume_ready(self, resume_id: str, title: str) -> None:
        self.ready.append((resume_id, title))

    def states(self) -> list[str]:
        return [state for _job, state in self.marks if state != "current"]


@pytest.fixture
def rig(tmp_path: Path, qtbot, web_profile, monkeypatch):
    monkeypatch.setattr(chatgpt, "POLL_SECONDS", 0.2)
    monkeypatch.setattr(chatgpt, "STABLE_SECONDS", 0.4)
    monkeypatch.setattr(chatgpt, "COMPLETE_JSON_QUIET_SECONDS", 0.4)

    # The fixtures are local files: tell the pipeline which of them stand for jobright and LinkedIn.
    def site_of(address: str) -> str:
        if "jobright" in address:
            return "jobright"
        if "linkedin" in address:
            return "linkedin"
        return "other"

    monkeypatch.setattr(pipeline_module, "site_of", site_of)
    monkeypatch.setattr(pipeline_module, "WEB_SCHEMES", ("http://", "https://", "file://"))

    api = FakeApi()
    ctx = make_context(tmp_path, api=api)
    ctx.store.set_prompt("You write resumes.\n\n{{PASTE_ORIGINAL_RESUME_HERE}}", "prompt.txt")
    ctx.store.set_original_resume("Anthony Fox. Ten years of platform engineering.", "resume.txt")
    browser = BrowserWidget(web_profile)
    qtbot.addWidget(browser)
    browser.resize(900, 600)
    browser.show()  # a hidden page has its timers throttled to one per second
    host = Host(browser)

    def add(name: str, n: int = 1, **extra: Any) -> SavedJob:
        result = ctx.store.add_saved_job(
            url=url(name),
            url_key=f"{name}-{n}",
            source_site="other",
            role=extra.pop("role", "Engineer"),
            company=extra.pop("company", "Acme"),
            **extra,
        )
        return result.job

    def make(mode: str = "automation", **kwargs: Any) -> Pipeline:
        return Pipeline(ctx, host, mode, chatgpt_url=kwargs.pop("chatgpt_url", CHATGPT), **kwargs)  # type: ignore[arg-type]

    return ctx, api, host, add, make


async def test_a_job_becomes_a_validated_resume(rig):
    ctx, api, host, add, make = rig
    job = add("real_job.html")

    summary = await make().run()

    assert summary.generated == 1 and not summary.skipped and not summary.stopped
    resumes = ctx.store.list_resumes()
    assert len(resumes) == 1
    saved = resumes[0]
    assert (saved.role, saved.company) == ("Senior Platform Engineer", "Acme Robotics")
    assert saved.jd_url == url("real_job.html") and saved.apply_url is None
    assert "Kubernetes" in saved.jd_text
    # The NORMALISED resume the web app returned is what is stored.
    assert json.loads(saved.resume_json) == RESUME
    assert saved.candidate_name == "Anthony Fox"
    # The job left the saved list in the same transaction.
    assert ctx.store.saved_job_count() == 0 and ctx.store.get_saved_job(job.id).deleted_at is not None
    assert host.states() == ["done"]
    # Stepper went 1 → 2 → 3, and the tabs were closed afterwards.
    assert [step for step, _ in host.steps][0] == 1 and {1, 2, 3} <= {step for step, _ in host.steps}
    assert host.browser.views() == []
    # ChatGPT's reply went to the web app for validation exactly once.
    assert len(api.validate_calls) == 1 and '"ok"' in api.validate_calls[0]


async def test_the_whole_prompt_reaches_chatgpt(rig):
    ctx, api, host, add, make = rig
    add("real_job.html")
    await make().run()
    # The fixture echoes how many characters it received; it must be the whole composed message.
    received = json.loads(api.validate_calls[0])["received"]
    assert received > 1000
    assert any(text.startswith("Pasting prompt") or "Waiting for ChatGPT" in text for _step, text in host.steps)
    assert any(line.startswith("Prompt: ") for line in host.logs)
    # The log holds counts only: no job description, no resume text.
    assert not any("Kubernetes" in line or "platform engineering" in line for line in host.logs)


async def test_jobright_job_resolves_to_the_real_site(rig):
    ctx, api, host, add, make = rig
    add("jobright_job.html", jd_text="jobright snapshot " * 20)
    summary = await make().run()
    assert summary.generated == 1
    saved = ctx.store.list_resumes()[0]
    # The real site had a description: both URLs point to it.
    assert saved.jd_url == url("real_job.html") and saved.apply_url == url("real_job.html")
    assert saved.source_url == url("jobright_job.html")
    assert "shown on the job board" not in saved.jd_text


async def test_form_only_real_site_falls_back_to_the_jobright_description(rig):
    ctx, api, host, add, make = rig
    add("jobright_form.html")
    summary = await make().run()
    assert summary.generated == 1
    saved = ctx.store.list_resumes()[0]
    assert saved.jd_url == url("jobright_form.html") and saved.apply_url == url("form_job.html")
    assert "shown on the job board" in saved.jd_text


async def test_a_jobright_job_that_leads_to_linkedin_is_cancelled(rig):
    ctx, api, host, add, make = rig
    add("jobright_to_linkedin.html", 1)
    add("real_job.html", 2)
    summary = await make().run()
    assert summary.generated == 1
    assert [(record.reason) for record in summary.skipped] == ["linkedin"]
    assert host.states() == ["skipped", "done"]
    assert [skip.reason for skip in ctx.store.list_skips()] == ["linkedin"]
    # It stays in Saved Jobs, marked Skipped, and is left out of the next run.
    assert [job.skip_reason for job in ctx.store.list_saved_jobs()] == ["LinkedIn job"]
    assert ctx.store.queue(include_attention=False) == []


async def test_a_form_with_no_description_is_cancelled(rig):
    ctx, api, host, add, make = rig
    add("form_job.html")
    summary = await make().run()
    assert summary.generated == 0 and [record.reason for record in summary.skipped] == ["form_only_no_jd"]
    assert api.validate_calls == []  # never reached ChatGPT


async def test_a_blocked_job_is_set_aside_in_automation_and_the_queue_continues(rig):
    ctx, api, host, add, make = rig
    blocked = add("login_wall.html", 1)
    add("real_job.html", 2)

    summary = await make("automation").run()

    assert summary.generated == 1
    assert [record.reason for record in summary.attention] == ["needs_user_action"]
    assert host.blocked_calls == ["page needs sign-in"]
    assert host.states() == ["attention", "done"]
    # It stays in Saved Jobs, flagged, and is logged; it never reached ChatGPT.
    kept = ctx.store.list_saved_jobs()
    assert [job.id for job in kept] == [blocked.id] and kept[0].attention_reason == "page needs sign-in"
    assert [skip.reason for skip in ctx.store.list_skips()] == ["needs_user_action"]
    assert len(api.validate_calls) == 1

    # A later Automation run leaves it alone; one that includes such jobs, or Human Check, picks it up.
    assert make("automation").queue() == []
    assert [job.id for job in make("automation", include_attention=True).queue()] == [blocked.id]
    assert [job.id for job in make("human").queue()] == [blocked.id]


async def test_a_blocked_job_waits_for_the_user_in_human_check(rig):
    ctx, api, host, add, make = rig
    add("login_wall.html")

    async def user_signs_in_then_retries(job, message, mode):
        host.blocked_calls.append(message)
        assert mode == "human"
        # "Fix it in the browser on the left, then press Retry."
        tab = host.browser.views()[0]
        tab.load(QUrl(url("real_job.html")))
        await tab.wait_loaded(20, settle=0.2)
        return "retry"

    host.blocked = user_signs_in_then_retries  # type: ignore[method-assign]
    summary = await make("human").run()
    assert summary.generated == 1 and host.blocked_calls == ["page needs sign-in"]
    assert ctx.store.list_resumes()[0].company == "Acme Robotics"


async def test_cancel_job_in_review_removes_it_and_continues(rig):
    ctx, api, host, add, make = rig
    add("real_job.html", 1)
    add("real_job.html", 2)
    answers = [None, "keep"]
    host.on_review = lambda data: None if answers.pop(0) is None else data
    summary = await make("human").run()
    assert summary.generated == 1 and [record.reason for record in summary.skipped] == ["user_cancelled"]
    assert [job.skip_reason for job in ctx.store.list_saved_jobs()] == ["Cancelled by you"]
    assert [skip.reason for skip in ctx.store.list_skips()] == ["user_cancelled"]


async def test_edits_made_in_review_are_what_gets_saved(rig):
    ctx, api, host, add, make = rig
    add("real_job.html")

    def edit(data: ReviewData) -> ReviewData:
        data.company = "Acme Robotics Inc."
        data.role = "Platform Engineer"
        return data

    host.on_review = edit
    await make("human").run()
    saved = ctx.store.list_resumes()[0]
    assert (saved.company, saved.role) == ("Acme Robotics Inc.", "Platform Engineer")


async def test_review_shows_what_the_ai_check_corrected(rig):
    ctx, api, host, add, make = rig
    add("real_job.html")
    api.check_reply = {
        "role": "Platform Engineer",
        "company": "Acme Robotics",
        "isJobPosting": True,
        "hasJobDescription": True,
        "confidence": 0.4,
        "notes": "",
        "source": "groq",
        "roleCorrected": True,
        "companyCorrected": False,
    }
    await make().run()
    review = host.reviews[0]
    assert review.role == "Platform Engineer" and review.check.role_corrected and review.check.low_confidence
    # Only the first part of the description is sent for the check.
    assert len(api.check_calls[0]["jdText"]) <= 4000


async def test_incomplete_data_is_set_aside_in_automation(rig):
    ctx, api, host, add, make = rig
    add("real_job.html")

    def blank_company(data: ReviewData) -> ReviewData:
        data.company = ""
        return data

    host.on_review = blank_company
    summary = await make("automation").run()
    assert summary.generated == 0 and [record.detail for record in summary.attention] == ["Company is empty"]
    assert ctx.store.list_saved_jobs()[0].attention_reason == "Company is empty"
    assert api.validate_calls == []


async def test_invalid_json_is_retried_in_the_same_conversation(rig):
    ctx, api, host, add, make = rig
    add("real_job.html")
    bad = {"ok": False, "resume": None, "repaired": False, "issues": [{"path": "contact.email", "message": "Required"}]}
    api.validate_replies = [bad, {"ok": True, "resume": RESUME, "repaired": True, "issues": []}]

    summary = await make("automation").run()

    assert summary.generated == 1
    assert len(api.validate_calls) == 2
    # The second reply answers the correction request, which is far shorter than the prompt: same conversation.
    first, second = (json.loads(text)["received"] for text in api.validate_calls)
    assert second < first
    assert any("attempt 2 of 3" in text for _step, text in host.steps)


async def test_three_failed_attempts_skip_the_job(rig):
    ctx, api, host, add, make = rig
    add("real_job.html")
    api.validate_replies = [
        {"ok": False, "resume": None, "repaired": False, "issues": [{"path": "summary", "message": "Required"}]}
    ]

    summary = await make("automation").run()

    assert summary.generated == 0 and [record.reason for record in summary.skipped] == ["invalid_json"]
    assert len(api.validate_calls) == 3  # up to 3 attempts in total
    assert ctx.store.resume_count() == 0
    assert [skip.reason for skip in ctx.store.list_skips()] == ["invalid_json"]


async def test_human_check_decides_at_the_validation_modal(rig):
    ctx, api, host, add, make = rig
    add("real_job.html", 1)
    add("real_job.html", 2)
    bad = {"ok": False, "resume": None, "repaired": False, "issues": [{"path": "summary", "message": "Required"}]}
    api.validate_replies = [bad, {"ok": True, "resume": RESUME, "repaired": False, "issues": []}]
    host.json_answers = ["skip"]

    summary = await make("human").run()

    assert host.json_calls == [(1, 1)]
    assert [record.reason for record in summary.skipped] == ["invalid_json"] and summary.generated == 1


async def test_a_reply_without_json_never_reaches_the_web_app(rig, monkeypatch):
    ctx, api, host, add, make = rig
    add("real_job.html")
    monkeypatch.setattr(chatgpt, "contains_json_object", lambda text: False)
    summary = await make("automation").run()
    assert api.validate_calls == [] and [record.reason for record in summary.skipped] == ["invalid_json"]


async def test_web_app_unreachable_pauses_and_never_saves_unvalidated_json(rig):
    ctx, api, host, add, make = rig
    add("real_job.html")
    calls = {"n": 0}
    original = api.validate_resume

    async def flaky(text: str):
        calls["n"] += 1
        if calls["n"] == 1:
            raise pipeline_module.ApiError("The web app could not be reached.", "OFFLINE")
        return await original(text)

    api.validate_resume = flaky  # type: ignore[method-assign]
    host.web_answers = ["retry"]
    summary = await make().run()
    assert summary.generated == 1 and calls["n"] == 2

    # And when the user stops instead, nothing is saved and the job is still queued.
    add("real_job.html", 2)
    calls["n"] = 0
    host.web_answers = ["stop"]
    summary = await make().run()
    assert summary.stopped and ctx.store.resume_count() == 1 and ctx.store.saved_job_count() == 1


async def test_chatgpt_login_page_pauses_the_run(rig):
    ctx, api, host, add, make = rig
    add("real_job.html", 1)
    add("real_job.html", 2)
    summary = await make("automation", chatgpt_url=LOGGED_OUT).run()
    assert summary.stopped and summary.generated == 0
    assert host.chat_blocked == ["ChatGPT is asking you to sign in."]
    # Nothing was skipped or lost: both jobs are still in the queue.
    assert ctx.store.saved_job_count() == 2 and ctx.store.list_skips() == []


async def test_stop_aborts_while_waiting_for_chatgpt_and_leaves_no_half_saved_record(rig, monkeypatch):
    ctx, api, host, add, make = rig
    add("real_job.html")
    run = make()

    async def never_finishes(page, baseline, timeout, on_progress=None, cancel=None):
        on_progress and on_progress("Waiting for ChatGPT (0 reply blocks, 0 chars, Stop shown)")
        await cancel.wait()
        raise chatgpt.Cancelled()

    monkeypatch.setattr(chatgpt, "wait_for_reply", never_finishes)
    task = asyncio.ensure_future(run.run())
    for _ in range(200):
        if any("Waiting for ChatGPT" in text for _step, text in host.steps):
            break
        await asyncio.sleep(0.05)
    run.stop()
    summary = await asyncio.wait_for(task, 20)

    assert summary.stopped and summary.generated == 0
    assert ctx.store.resume_count() == 0 and ctx.store.saved_job_count() == 1
    assert host.browser.views() == []  # tabs closed
    with pytest.raises(Stopped):
        run._check()


async def test_a_chatgpt_timeout_skips_the_job_and_the_run_continues(rig, monkeypatch):
    ctx, api, host, add, make = rig
    add("real_job.html", 1)
    add("real_job.html", 2)
    real = chatgpt.wait_for_reply
    calls = {"n": 0}

    async def first_times_out(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise chatgpt.ReplyTimeout("ChatGPT did not finish in time.")
        return await real(*args, **kwargs)

    monkeypatch.setattr(chatgpt, "wait_for_reply", first_times_out)
    summary = await make().run()
    assert [record.reason for record in summary.skipped] == ["chatgpt_failed"] and summary.generated == 1


async def test_jobs_run_oldest_first(rig):
    ctx, api, host, add, make = rig
    from tests.factories import utc

    newer = ctx.store.add_saved_job(
        url=url("real_job.html"), url_key="b", source_site="other", role="B", company="B", now=utc(2026, 10, 2)
    ).job
    older = ctx.store.add_saved_job(
        url=url("real_job.html"), url_key="a", source_site="other", role="A", company="A", now=utc(2026, 10, 1)
    ).job
    await make().run()
    assert [job_id for job_id, state in host.marks if state == "current"] == [older.id, newer.id]


async def test_a_chatgpt_login_page_in_the_middle_of_a_job_pauses_instead_of_skipping(rig, monkeypatch):
    ctx, api, host, add, make = rig
    add("real_job.html")
    real = chatgpt.submit_prompt
    calls = {"n": 0}

    async def blocked_once(page, prompt, on_progress=None, cancel=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise chatgpt.ChatGPTBlocked("login")
        return await real(page, prompt, on_progress=on_progress, cancel=cancel)

    monkeypatch.setattr(chatgpt, "submit_prompt", blocked_once)
    host.chat_answers = ["retry"]
    summary = await make().run()
    assert host.chat_blocked == ["ChatGPT is asking you to sign in."]
    assert summary.generated == 1 and not summary.skipped  # the job was not lost

    # ...and when the user stops instead, the job is still in the queue.
    add("real_job.html", 2)
    calls["n"] = 0
    host.chat_answers = ["stop"]
    summary = await make().run()
    assert summary.stopped and ctx.store.saved_job_count() == 1 and ctx.store.list_skips() == []


async def test_an_unexpected_error_in_one_job_keeps_it_saved_and_the_run_goes_on(rig, monkeypatch):
    ctx, api, host, add, make = rig
    add("real_job.html", 1)
    add("real_job.html", 2)
    real = pipeline_module.extract_page
    calls = {"n": 0}

    async def breaks_once(page, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("Internal C++ object already deleted: SECRET PAGE TEXT")
        return await real(page, **kwargs)

    monkeypatch.setattr(pipeline_module, "extract_page", breaks_once)
    summary = await make().run()
    assert summary.generated == 1 and len(summary.attention) == 1
    kept = ctx.store.list_saved_jobs()
    assert len(kept) == 1 and "unexpected problem" in kept[0].attention_reason
    assert "SECRET" not in str(host.logs) and "SECRET" not in kept[0].attention_reason


async def test_three_chatgpt_failures_in_a_row_pause_the_run(rig, monkeypatch):
    ctx, api, host, add, make = rig
    for n in range(5):
        add("real_job.html", n)

    async def always_times_out(*args, **kwargs):
        raise chatgpt.ReplyTimeout("ChatGPT did not finish in time.")

    monkeypatch.setattr(chatgpt, "wait_for_reply", always_times_out)
    summary = await make().run()
    assert summary.stopped and len(summary.skipped) == 3
    assert host.chat_blocked == ["ChatGPT has not answered 3 jobs in a row."]
    assert len(ctx.store.queue(include_attention=False)) == 2  # an outage does not empty the queue
    assert ctx.store.skipped_count() == 3  # the three it skipped stay in Saved Jobs, marked Skipped


async def test_stop_during_the_last_wait_saves_nothing(rig, monkeypatch):
    ctx, api, host, add, make = rig
    add("real_job.html")
    run = make()

    async def stop_now(tab):
        run.stop()
        return None

    monkeypatch.setattr(run, "_conversation_url", stop_now)
    summary = await run.run()
    assert summary.stopped and ctx.store.resume_count() == 0 and ctx.store.saved_job_count() == 1


async def test_the_ai_check_can_rescue_a_page_that_looks_form_only(rig, monkeypatch):
    ctx, api, host, add, make = rig
    add("real_job.html", 1)
    real = pipeline_module.extract_page

    async def looks_like_a_form(page, **kwargs):
        extraction = await real(page, **kwargs)
        extraction.has_jobposting_ldjson = False
        extraction.prose_len = 300
        extraction.form_field_count = 6
        assert extraction.form_only and not extraction.has_jd
        return extraction

    monkeypatch.setattr(pipeline_module, "extract_page", looks_like_a_form)
    summary = await make().run()
    assert summary.generated == 1  # the check said: this is a real job description

    add("real_job.html", 2)
    api.check_reply = {
        "role": "Engineer",
        "company": "Acme",
        "isJobPosting": True,
        "hasJobDescription": False,
        "confidence": 0.9,
        "notes": "",
        "source": "groq",
        "roleCorrected": False,
        "companyCorrected": False,
    }
    summary = await make().run()
    assert [record.reason for record in summary.skipped] == ["form_only_no_jd"]


async def test_automation_shows_a_countdown_before_every_step_transition(rig):
    ctx, api, host, add, make = rig
    add("real_job.html", 1)
    add("real_job.html", 2)
    seen: list[str] = []

    async def countdown(seconds, text):
        seen.append(text)

    host.countdown = countdown  # type: ignore[method-assign]
    await make("automation").run()
    # Per job: before validating (② → ③); and once between the two jobs. (① → ② is the review countdown.)
    assert seen == ["Continuing in {n} seconds…"] * 3

    seen.clear()
    add("real_job.html", 3)
    await make("human").run()
    assert seen == []  # Human Check never counts down


async def test_apply_is_only_ever_clicked_on_jobright(rig, monkeypatch):
    ctx, api, host, add, make = rig
    add("jobright_job.html")
    clicks: list[str] = []
    real = pipeline_module.click_apply_control

    async def spy(page):
        clicks.append(page.url().toString())
        return await real(page)

    monkeypatch.setattr(pipeline_module, "click_apply_control", spy)
    # The saved URL no longer counts as a jobright page: nothing may be clicked there.
    monkeypatch.setattr(pipeline_module, "site_of", lambda address: "other")
    await make().run()
    assert clicks == []


# ---------------------------------------------------------------------------
# Several sources: the ATS API, and when the AI check may overrule them
# ---------------------------------------------------------------------------

ATS_JD = (
    "About Acme Robotics\nWe build warehouse robots.\n\nResponsibilities\n• Run Kubernetes clusters\n"
    "• Own the deployment pipeline\n\nRequirements\n• 6+ years of platform engineering\n• Go or Python\n"
) * 16


def _ats_posting(monkeypatch, company: str = "Acme Robotics, Inc.") -> list[list[str]]:
    from app.automation import ats_api
    from app.automation.ats_api import AtsPosting, AtsRef

    asked: list[list[str]] = []

    async def lookup(urls, http=None):
        asked.append(list(urls))
        return AtsPosting(
            "greenhouse", "Senior Platform Engineer", company, True, ATS_JD, "https://boards.greenhouse.io/acme/jobs/1"
        )

    monkeypatch.setattr(ats_api, "candidate_refs", lambda urls: [AtsRef("greenhouse", "u", "a")])
    monkeypatch.setattr(ats_api, "lookup", lookup)
    return asked


async def test_the_ats_api_copy_is_used_and_strong_sources_stand_against_the_ai_check(rig, monkeypatch):
    ctx, api, host, add, make = rig
    asked = _ats_posting(monkeypatch)
    api.check_reply = {
        "role": "Totally Different Role",
        "company": "Wrong Company",
        "isJobPosting": True,
        "hasJobDescription": True,
        "confidence": 0.95,
        "roleCorrected": True,
        "companyCorrected": True,
        "source": "groq",
    }
    add("real_job.html", role="Senior Platform Engineer", company="Acme Robotics")

    summary = await make().run()

    assert summary.generated == 1 and asked
    saved = ctx.store.list_resumes()[0]
    assert (saved.role, saved.company) == ("Senior Platform Engineer", "Acme Robotics, Inc.")
    assert saved.jd_text == ATS_JD.strip()
    assert any(line.startswith("Read the posting from greenhouse") for line in host.logs)
    assert any(line.startswith("Kept the role from") for line in host.logs)
    assert any(line.startswith("Kept the company from") for line in host.logs)
    sources = [line for line in host.logs if line.startswith("Sources: ")]
    assert sources and "description: ats" in sources[-1]
    # The log names sources and lengths, never the text.
    assert not any("warehouse robots" in line for line in host.logs)


async def test_the_ai_check_still_corrects_a_weakly_supported_role(rig):
    ctx, api, host, add, make = rig
    api.check_reply = {
        "role": "Platform Engineer (Corrected)",
        "company": "Acme Robotics",
        "isJobPosting": True,
        "hasJobDescription": True,
        "confidence": 0.9,
        "roleCorrected": True,
        "companyCorrected": False,
        "source": "groq",
    }
    # The listing and the page disagree on the role: no strong decision, so the check may correct it.
    add("real_job.html", role="Office Manager", company="Acme Robotics")
    summary = await make().run()
    assert summary.generated == 1
    assert ctx.store.list_resumes()[0].role == "Platform Engineer (Corrected)"


async def test_a_page_without_a_description_is_rescued_by_the_ats_api(rig, monkeypatch):
    ctx, api, host, add, make = rig
    _ats_posting(monkeypatch)
    add("form_job.html", role="Senior Platform Engineer", company="Acme Robotics")
    summary = await make().run()
    assert summary.generated == 1 and not summary.skipped
    assert ctx.store.list_resumes()[0].jd_text == ATS_JD.strip()


async def test_a_jobright_job_without_an_apply_button_keeps_the_original_posting_as_apply_url(rig):
    ctx, api, host, add, make = rig
    add("jobright_noapply.html")
    summary = await make().run()
    assert summary.generated == 1
    saved = ctx.store.list_resumes()[0]
    assert saved.jd_url == url("jobright_noapply.html")
    # The extension opens this, so the application is tracked on the site where it is filed.
    assert saved.apply_url == "https://careers.acme-robotics.test/jobs/77"
    assert any(line.startswith("No Apply button found") for line in host.logs)


async def test_jobright_apply_is_pressed_before_any_ats_lookup(rig, monkeypatch):
    ctx, api, host, add, make = rig
    from app.automation import extractor as extractor_module

    order: list[str] = []
    original_click = pipeline_module.click_apply_control

    async def click(page):
        order.append("click")
        return await original_click(page)

    async def lookup(urls, http=None):
        order.append("ats")
        return None

    from app.automation import ats_api

    monkeypatch.setattr(pipeline_module, "click_apply_control", click)
    monkeypatch.setattr(ats_api, "candidate_refs", lambda urls: ["ref"])
    monkeypatch.setattr(ats_api, "lookup", lookup)
    assert extractor_module  # the real page reader runs
    add("jobright_job.html")
    summary = await make().run()
    assert summary.generated == 1
    assert order[0] == "click" and "ats" in order
    saved = ctx.store.list_resumes()[0]
    assert saved.apply_url == url("real_job.html")


async def test_a_job_saved_by_hand_with_its_description_uses_it_when_the_page_shows_none(rig):
    ctx, api, host, add, make = rig
    saved_jd = (
        "About the role: build the platform. Responsibilities: own services end to end. "
        "Requirements: five years of experience with Python and cloud infrastructure. "
    ) * 3
    add("form_job.html", role="Platform Engineer", company="Acme Robotics", jd_text=saved_jd)
    summary = await make().run()
    assert summary.generated == 1 and not summary.skipped
    assert ctx.store.list_resumes()[0].jd_text == saved_jd.strip()
    assert "Using the job description saved with this job." in host.logs


async def test_a_quick_sync_before_each_job_drops_jobs_another_pc_already_did(rig):
    ctx, api, host, add, make = rig
    first = add("real_job.html", 1)
    done_elsewhere = add("real_job.html", 2)
    calls: list[int] = []

    async def before_job() -> None:
        calls.append(len(calls))
        if len(calls) == 2:
            # The sync before the second job finds it generated on the other PC.
            ctx.store.remove_saved_jobs([done_elsewhere.id])

    pipeline = make()
    pipeline.before_job = before_job
    summary = await pipeline.run()
    assert summary.generated == 1 and not summary.skipped
    assert (first.id, "done") in host.marks
    assert all(job_id != done_elsewhere.id for job_id, _state in host.marks)  # never started
    assert ctx.store.list_resumes()[0].url_key == first.url_key
    assert len(calls) >= 2


async def test_a_failing_sync_before_a_job_does_not_stop_the_run(rig):
    ctx, api, host, add, make = rig
    add("real_job.html")

    async def before_job() -> None:
        raise RuntimeError("offline")

    pipeline = make()
    pipeline.before_job = before_job
    summary = await pipeline.run()
    assert summary.generated == 1


# ---------------------------------------------------------------------------
# Skipped jobs stay in Saved Jobs; Human Check "Stop step"
# ---------------------------------------------------------------------------


async def test_a_skipped_job_stays_in_saved_jobs_marked_skipped_and_is_left_out_next_time(rig):
    ctx, api, host, add, make = rig
    job = add("form_job.html")
    summary = await make().run()
    assert [record.reason for record in summary.skipped] == ["form_only_no_jd"]
    kept = ctx.store.get_saved_job(job.id)
    assert kept.deleted_at is None and kept.skip_reason == "Application form with no job description"
    assert ctx.store.saved_job_count() == 1 and ctx.store.skipped_count() == 1
    assert ctx.store.list_skips()[0].reason == "form_only_no_jd"  # still in the skipped log

    # The next run leaves it out, unless asked to retry skipped jobs.
    again = await make().run()
    assert again.generated == 0 and not again.skipped
    retry = await make(include_skipped=True).run()
    assert [record.reason for record in retry.skipped] == ["form_only_no_jd"]


async def _take_over_when(host: Host, pipeline: Pipeline, ready: Any) -> None:
    for _ in range(600):
        if pipeline.can_take_over and ready():
            assert pipeline.take_over()
            return
        await asyncio.sleep(0.05)
    raise AssertionError("the step never became pausable")


async def test_stop_step_in_chatgpt_lets_me_finish_and_then_shows_the_resume(rig):
    ctx, api, host, add, make = rig
    add("real_job.html")

    async def finish_by_hand(step: int) -> str:
        await asyncio.sleep(1.5)  # I look at ChatGPT; its reply is complete meanwhile
        return "continue"

    host.on_taken_over = finish_by_hand
    pipeline = make("human")
    assert not pipeline.can_take_over and not pipeline.take_over()  # nothing running yet
    waiting = lambda: any(status.startswith("Waiting for ChatGPT") for _step, status in host.steps)  # noqa: E731
    trigger = asyncio.ensure_future(_take_over_when(host, pipeline, waiting))
    summary = await pipeline.run()
    await trigger

    assert summary.generated == 1 and not summary.stopped and not summary.skipped
    assert host.takeovers == [(2, "Automatic ChatGPT step stopped")]
    assert len(host.ready) == 1 and host.ready[0][0] == summary.last_resume_id
    assert ctx.store.resume_count() == 1 and ctx.store.saved_job_count() == 0
    assert "Stopped the ChatGPT step: you finish it." in host.logs


async def test_stop_step_while_reading_the_job_reads_the_page_in_front(rig):
    ctx, api, host, add, make = rig
    add("real_job.html", role="", company="")
    pipeline = make("human")
    trigger = asyncio.ensure_future(_take_over_when(host, pipeline, lambda: bool(host.steps)))
    summary = await pipeline.run()
    await trigger

    assert host.takeovers[0] == (1, "Automatic reading stopped")
    assert summary.generated == 1 and len(host.ready) == 1
    review = host.reviews[0]
    assert review.company and review.role and len(review.jd_text) >= 100  # read from the page in front


async def test_skip_after_stop_step_keeps_the_job_marked_skipped(rig):
    ctx, api, host, add, make = rig
    job = add("real_job.html")

    async def skip(_step: int) -> str:
        return "skip"

    host.on_taken_over = skip
    pipeline = make("human")
    trigger = asyncio.ensure_future(_take_over_when(host, pipeline, lambda: bool(host.steps)))
    summary = await pipeline.run()
    await trigger
    assert [record.reason for record in summary.skipped] == ["user_cancelled"]
    assert ctx.store.get_saved_job(job.id).skip_reason == "Cancelled by you"
    assert ctx.store.resume_count() == 0 and not host.ready


async def test_stop_step_is_human_check_only(rig):
    ctx, api, host, add, make = rig
    add("real_job.html")
    pipeline = make("automation")
    results: list[bool] = []

    async def press() -> None:
        for _ in range(100):
            results.append(pipeline.take_over())
            await asyncio.sleep(0.05)

    presser = asyncio.ensure_future(press())
    summary = await pipeline.run()
    presser.cancel()
    assert summary.generated == 1 and not any(results) and not host.takeovers
