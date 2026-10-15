"""Job Search manual mode: any site, saved only when the user presses Save job."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from PySide6.QtWidgets import QLineEdit, QWidget

from app.automation.job_rules import url_key
from app.automation.manual_save import (
    DOUBT_NO_DESCRIPTION,
    DOUBT_NOT_A_POSTING,
    MISSING_DETAILS,
    TOAST_ALREADY_SAVED,
    TOAST_MANUAL_JOBRIGHT_LIST,
    TOAST_MANUAL_LINKEDIN,
    ManualDraft,
    read_manual,
    save_manual,
)
from app.browser.browser import SEARCH_URL, typed_url
from app.ui.pages.job_search import JobSearchPage, ManualSaveDialog
from app.ui.widgets.common import ToastHost
from tests.factories import make_store
from tests.fake_api import FakeApi, make_context
from tests.helpers import FIXTURES, load_html

GREENHOUSE = "https://boards.greenhouse.io/acme/jobs/4011"
JD = "We are looking for a platform engineer. Responsibilities include building services. " * 4


def html(name: str) -> str:
    return (FIXTURES / "pages" / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# The address bar
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("typed", "opened"),
    [
        ("https://careers.acme.test/jobs/1", "https://careers.acme.test/jobs/1"),
        ("  boards.greenhouse.io/acme/jobs/4011 ", "https://boards.greenhouse.io/acme/jobs/4011"),
        ("www.workday.com", "https://www.workday.com"),
        ("localhost:3000/jobs", "https://localhost:3000/jobs"),
        ("platform engineer remote", SEARCH_URL + "platform+engineer+remote"),
        ("javascript:alert(1)", None),
        ("file:///C:/secret.txt", None),
        ("chrome://settings", None),
        ("https://", None),
        ("   ", None),
    ],
)
def test_typed_addresses_open_only_web_pages(typed, opened):
    assert typed_url(typed) == opened


# ---------------------------------------------------------------------------
# Reading and saving a page
# ---------------------------------------------------------------------------


async def test_a_complete_job_page_is_saved_with_url_company_role_and_description(web_page, tmp_path: Path):
    store, api = make_store(tmp_path), FakeApi()
    await load_html(web_page, html("greenhouse_job.html"), GREENHOUSE)
    draft = await read_manual(web_page, api)
    assert draft.ready and draft.refused is None and draft.doubt is None
    assert draft.role and draft.company and len(draft.jd_text) >= 100

    result = save_manual(store, url=draft.url, role=draft.role, company=draft.company, jd_text=draft.jd_text)
    assert result.saved and result.toast == f"Saved: {draft.role} at {draft.company}"
    job = store.list_saved_jobs()[0]
    assert (job.url, job.role, job.company, job.source_site) == (GREENHOUSE, draft.role, draft.company, "other")
    assert job.jd_text == draft.jd_text.strip()
    assert store.saved_state(url_key(GREENHOUSE)) == "saved"

    again = save_manual(store, url=draft.url, role=draft.role, company=draft.company, jd_text=draft.jd_text)
    assert (again.code, again.toast) == ("duplicate_saved", TOAST_ALREADY_SAVED) and store.saved_job_count() == 1


async def test_hiring_cafe_and_jobright_pages_keep_their_source(web_page, tmp_path: Path):
    store, api = make_store(tmp_path), FakeApi()
    await load_html(web_page, html("jobright_detail.html"), "https://jobright.ai/jobs/info/abc123")
    draft = await read_manual(web_page, api)
    assert draft.ready and draft.source_site == "jobright"
    save_manual(store, url=draft.url, role=draft.role, company=draft.company, jd_text=draft.jd_text)
    assert store.list_saved_jobs()[0].source_site == "jobright"


async def test_pages_that_can_never_become_a_resume_are_refused(web_page):
    api = FakeApi()
    await load_html(web_page, html("linkedin_job.html"), "https://www.linkedin.com/jobs/view/123")
    assert (await read_manual(web_page, api)).refused == TOAST_MANUAL_LINKEDIN

    await load_html(web_page, html("jobright_linkedin.html"), "https://jobright.ai/jobs/info/li1")
    assert (await read_manual(web_page, api)).refused == TOAST_MANUAL_LINKEDIN

    await load_html(web_page, html("jobright_detail.html"), "https://jobright.ai/jobs/recommend")
    assert (await read_manual(web_page, api)).refused == TOAST_MANUAL_JOBRIGHT_LIST

    await load_html(web_page, html("blocked_login.html"), "https://careers.example.com/job/9")
    assert (await read_manual(web_page, api)).refused == "Not saved: page needs sign-in"


async def test_a_page_without_a_description_asks_the_user_to_complete_it(web_page, tmp_path: Path):
    store, api = make_store(tmp_path), FakeApi()
    await load_html(web_page, html("form_only.html"), "https://jobs.example.com/apply/55")
    draft = await read_manual(web_page, api)
    assert draft.refused is None and not draft.ready and draft.doubt == DOUBT_NO_DESCRIPTION and not draft.jd_text

    refused = save_manual(store, url=draft.url, role="Engineer", company="Example", jd_text="too short")
    assert refused.code == "invalid" and store.saved_job_count() == 0
    saved = save_manual(store, url=draft.url, role="Engineer", company="Example", jd_text=JD)
    assert saved.saved and store.list_saved_jobs()[0].jd_text == JD.strip()


async def test_the_ai_check_doubting_the_page_opens_the_review_instead_of_saving(web_page):
    api = FakeApi()
    api.check_reply = {
        "role": "Platform Engineer",
        "company": "Acme Corp",
        "isJobPosting": False,
        "hasJobDescription": False,
        "confidence": 0.3,
        "notes": "",
        "source": "groq",
    }
    await load_html(web_page, html("greenhouse_job.html"), GREENHOUSE)
    draft = await read_manual(web_page, api)
    assert draft.doubt == DOUBT_NOT_A_POSTING and not draft.ready
    assert (draft.role, draft.company) == ("Platform Engineer", "Acme Corp")  # the corrected values


async def test_reading_still_works_when_the_ai_check_is_unavailable(web_page):
    api = FakeApi()
    api.offline = True
    await load_html(web_page, html("ldjson_job.html"), "https://careers.example.com/jobs/77")
    draft = await read_manual(web_page, api)
    assert draft.ready and draft.confidence is None


# ---------------------------------------------------------------------------
# The Job Search page
# ---------------------------------------------------------------------------


@pytest.fixture
def search(tmp_path: Path, qtbot, web_profile):
    ctx = make_context(tmp_path, api=FakeApi())
    ctx.web_profile = web_profile
    holder = QWidget()
    qtbot.addWidget(holder)
    toasts = ToastHost(holder)
    page = JobSearchPage(ctx, toasts, web_profile)
    page.setParent(holder)
    holder.resize(1100, 760)
    holder.show()
    page.resize(1100, 760)
    page.show()
    yield ctx, page, toasts
    if page.browser is not None:
        page.browser.close_all()


def test_auto_is_the_default_and_keeps_the_address_bar_read_only(search):
    ctx, page, _toasts = search
    assert page.mode == "auto" and page.mode_switch.current() == "auto"
    assert page.manual_bar.isHidden() and not page.selector.isHidden()
    assert page.browser.address.isReadOnly()
    # The switch sits on the left of the site buttons.
    assert page.header.right.indexOf(page.mode_switch) < page.header.right.indexOf(page.selector)


def test_manual_mode_opens_the_address_bar_shows_save_and_is_remembered(search, tmp_path: Path, qtbot, web_profile):
    ctx, page, toasts = search
    page.mode_switch.changed.emit("manual")
    assert page.mode == "manual" and ctx.settings.job_search_mode == "manual"
    assert not page.manual_bar.isHidden() and not page.browser.address.isReadOnly()
    assert page.selector.isHidden()  # the site buttons are Auto mode only
    assert not page.save_button.isEnabled()  # nothing open yet
    assert page.header.subtitle.text().startswith("Open any job site")
    assert toasts.history[-1][0] == "info"

    again = JobSearchPage(ctx, toasts, None)
    qtbot.addWidget(again)
    assert again.mode == "manual" and not again.manual_bar.isHidden()

    page.mode_switch.changed.emit("auto")
    assert page.manual_bar.isHidden() and page.browser.address.isReadOnly()
    assert not page.selector.isHidden()
    assert ctx.settings.job_search_mode == "auto"


async def test_typing_an_address_opens_it_only_in_manual_mode(search):
    _ctx, page, _toasts = search
    browser = page.browser
    tabs = len(browser.views())
    first = browser.current()
    before = first.url().toString() if first is not None else ""

    browser.address.setText("careers.acme.test/jobs/1")
    browser.address.returnPressed.emit()
    after = browser.current()
    assert len(browser.views()) == tabs and (after.url().toString() if after else "") == before  # auto: read-only

    page.set_mode("manual")
    browser.address.setText("javascript:alert(1)")
    browser.address.returnPressed.emit()
    assert (browser.current().url().toString() if browser.current() else "") == before
    assert browser.address.text() == before  # the refused text is replaced by the current address

    browser.address.setText("acme.test/jobs/1")
    browser.address.returnPressed.emit()
    view = browser.current()
    assert view is not None and view.page().requestedUrl().toString() == "https://acme.test/jobs/1"


async def test_manual_mode_never_saves_by_itself(search, monkeypatch):
    _ctx, page, _toasts = search
    captured: list[str] = []

    async def capture(view, opener_url):  # noqa: ANN001
        captured.append(opener_url)

    monkeypatch.setattr(page, "_capture", capture)
    page.set_mode("manual")
    opener = page.browser.add_tab(None)
    page.browser.add_tab(None, opener=opener)
    page._url_changed(opener, "https://jobright.ai/jobs/info/abc123")
    await asyncio.sleep(0.05)
    assert captured == []

    page.set_mode("auto")
    page.browser.add_tab(None, opener=opener)
    await asyncio.sleep(0.05)
    assert len(captured) == 1


async def test_save_job_saves_the_current_page_and_then_shows_saved(search):
    ctx, page, toasts = search
    page.set_mode("manual")
    view = page.browser.add_tab(None)
    await load_html(view.page(), html("greenhouse_job.html"), GREENHOUSE)
    await asyncio.sleep(0.1)
    assert page.save_button.isEnabled() and page.save_button.text() == "Save job"
    assert page.page_host.text() == "boards.greenhouse.io"

    result = await page._save(view)
    assert result is not None and result.saved
    assert toasts.history[-1][0] == "success" and toasts.history[-1][1].startswith("Saved: ")
    job = ctx.store.list_saved_jobs()[0]
    assert job.url == GREENHOUSE and job.role and job.company and job.jd_text
    assert page.save_button.text() == "Saved" and not page.save_button.isEnabled()


async def test_a_refused_page_says_why_and_saves_nothing(search):
    ctx, page, toasts = search
    page.set_mode("manual")
    view = page.browser.add_tab(None)
    await load_html(view.page(), html("linkedin_job.html"), "https://www.linkedin.com/jobs/view/123")
    assert await page._save(view) is None
    assert toasts.history[-1] == ("warning", TOAST_MANUAL_LINKEDIN) and ctx.store.saved_job_count() == 0


async def test_an_incomplete_page_opens_the_review_dialog_and_saves_the_edits(search, monkeypatch):
    ctx, page, toasts = search
    page.set_mode("manual")
    opened: list[ManualSaveDialog] = []
    real_review = page.review
    monkeypatch.setattr(page, "review", lambda draft: opened.append(real_review(draft)) or opened[-1])
    view = page.browser.add_tab(None)
    await load_html(view.page(), html("form_only.html"), "https://jobs.example.com/apply/55")
    assert await page._save(view) is None
    assert len(opened) == 1 and ctx.store.saved_job_count() == 0

    dialog = opened[0]
    assert not dialog.save.isEnabled() and "Still needed" in dialog.hint.text()
    assert dialog.findChildren(QLineEdit)[0].text() == "https://jobs.example.com/apply/55"
    dialog.company.setText("Example Labs")
    dialog.role.setText("Data Engineer")
    dialog.jd.setPlainText(JD)
    assert dialog.save.isEnabled()
    dialog.save.click()
    job = ctx.store.list_saved_jobs()[0]
    assert (job.url, job.company, job.role, job.jd_text) == (
        "https://jobs.example.com/apply/55",
        "Example Labs",
        "Data Engineer",
        JD.strip(),
    )
    assert toasts.history[-1] == ("success", "Saved: Data Engineer at Example Labs")


async def test_cancelling_the_review_saves_nothing(search):
    ctx, page, _toasts = search
    dialog = page.review(ManualDraft("https://jobs.example.com/apply/55", "other", doubt=MISSING_DETAILS))
    dialog.cancel.click()
    assert ctx.store.saved_job_count() == 0


def test_manual_mode_starts_without_a_site_and_auto_opens_its_site(tmp_path: Path, qtbot, web_profile):
    ctx = make_context(tmp_path, api=FakeApi())
    ctx.settings.job_search_mode = "manual"
    holder = QWidget()
    qtbot.addWidget(holder)
    page = JobSearchPage(ctx, ToastHost(holder), web_profile)
    page.setParent(holder)
    holder.show()
    page.show()
    assert page.browser.views() == [] and page.selector.isHidden()
    page.set_mode("auto")
    assert len(page.browser.views()) == 1 and not page.selector.isHidden()
    page.browser.close_all()
