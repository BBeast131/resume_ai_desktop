"""A whole run through the real screen: MainWindow + embedded browser + fixture pages."""

from __future__ import annotations

import asyncio

import pytest
from PySide6.QtCore import QUrl

from app.automation import chatgpt
from app.automation import pipeline as pipeline_module
from app.ui.main_window import MainWindow
from app.ui.styles import apply_theme
from tests.fake_api import make_context
from tests.helpers import FIXTURES

CHATGPT = QUrl.fromLocalFile(str(FIXTURES / "chatgpt" / "composer.html")).toString()
JOB = QUrl.fromLocalFile(str(FIXTURES / "pipeline" / "real_job.html")).toString()
LOGIN = QUrl.fromLocalFile(str(FIXTURES / "pipeline" / "login_wall.html")).toString()


@pytest.fixture
def window(tmp_path, qtbot, qapp, web_profile, monkeypatch):
    apply_theme(qapp)
    monkeypatch.setattr(chatgpt, "POLL_SECONDS", 0.2)
    monkeypatch.setattr(chatgpt, "STABLE_SECONDS", 0.4)
    monkeypatch.setattr(chatgpt, "COMPLETE_JSON_QUIET_SECONDS", 0.4)
    monkeypatch.setattr(pipeline_module, "WEB_SCHEMES", ("http://", "https://", "file://"))
    ctx = make_context(tmp_path)
    ctx.web_profile = web_profile
    ctx.settings.automation_seconds = 1
    ctx.store.set_prompt("You write resumes.\n{{PASTE_ORIGINAL_RESUME_HERE}}", "prompt.txt")
    window = MainWindow(ctx, chatgpt_url=CHATGPT)
    qtbot.addWidget(window)
    window.show()
    window.show_page("generator")
    yield window
    window.shutdown()
    window.generator.browser.close_all()
    window.job_search.browser.close_all()


def add(window, url: str, n: int = 1):
    job = window.ctx.store.add_saved_job(
        url=url, url_key=f"job-{n}", source_site="other", role="Engineer", company="Acme"
    ).job
    window.ctx.changed("jobs")
    return job


async def test_automation_run_from_start_to_summary(window):
    ctx, page = window.ctx, window.generator
    add(window, JOB)
    assert page.start.isEnabled() and page.start.text() == "Start"

    task = page.begin("automation")
    assert page.running and page.start.text() == "Stop"
    assert window.nav["generator"].running.isVisible()
    # The run keeps going while I look at another tab.
    window.show_page("dashboard")
    summary = await asyncio.wait_for(task, 120)

    assert summary.generated == 1 and not summary.stopped
    assert ctx.store.resume_count() == 1 and ctx.store.saved_job_count() == 0
    assert not page.running and page.start.text() == "Start"
    assert not window.nav["generator"].running.isVisible()
    assert page.board.currentIndex() == 0  # back to the placeholder
    assert page.download.isEnabled()  # the last resume of this run can be downloaded
    assert window.nav["generated"].badge.text() == "1"
    assert page.summary_dialog.generated.text() == "1 resume generated"
    page.summary_dialog.accept()
    # The log has steps and counts, never the job description.
    log = page.log_view.toPlainText()
    assert "[2/3]" in log and "Saved." in log and "Kubernetes" not in log
    # The resume is on its way to the web app.
    assert window._sync_debounce.isActive() or ctx.api.resumes


async def test_human_check_waits_for_next(window):
    ctx, page = window.ctx, window.generator
    add(window, JOB)
    task = page.begin("human")
    for _ in range(300):
        if page.alert_card.isVisible():
            break
        await asyncio.sleep(0.1)
    assert page.alert_title.text() == "Please check extracted JD and role, company name"
    assert page.review_panel.company.text() == "Acme Robotics"
    assert page.stepper.current == 1
    await asyncio.sleep(1.5)
    assert not task.done()  # it stays until I click

    page.review_panel.company.setText("Acme Robotics Inc.")
    page.review_panel.next_button.click()
    summary = await asyncio.wait_for(task, 120)
    assert summary.generated == 1
    assert ctx.store.list_resumes()[0].company == "Acme Robotics Inc."
    page.summary_dialog.accept()


async def test_stop_during_the_countdown(window):
    ctx, page = window.ctx, window.generator
    ctx.settings.automation_seconds = 30
    add(window, JOB)
    task = page.begin("automation")
    for _ in range(300):
        if page.countdown_card.isVisible():
            break
        await asyncio.sleep(0.1)
    assert page.countdown_title.text() == "Automation Running"
    page.start.click()  # the button is Stop now
    summary = await asyncio.wait_for(task, 30)
    assert summary.stopped and summary.generated == 0
    assert ctx.store.saved_job_count() == 1 and ctx.store.resume_count() == 0
    assert page.start.text() == "Start" and not page.countdown_card.isVisible()
    assert page.browser.views() == []
    page.summary_dialog.accept()


async def test_a_blocked_job_is_set_aside_and_shown_in_saved_jobs(window):
    ctx, page = window.ctx, window.generator
    object.__setattr__(ctx.config, "attention_grace_seconds", 1)
    add(window, LOGIN, 1)
    add(window, JOB, 2)
    summary = await asyncio.wait_for(page.begin("automation"), 180)
    assert summary.generated == 1 and len(summary.attention) == 1
    page.summary_dialog.accept()

    window.show_page("saved_jobs")
    table = window.saved_jobs.table
    assert table.rowCount() == 1
    badge = table.cellWidget(0, 4).findChild(type(window.user_name))
    assert badge.text() == "Needs attention: page needs sign-in"
    # The queue marks it too.
    window.show_page("generator")
    assert [item.state for item in page.queue_items.values()] == ["attention"]


async def test_job_search_captures_a_job_opened_in_a_new_tab(window, monkeypatch):
    import app.automation.capture as capture_module

    # The fixtures are local files; treat the opener as hiring.cafe.
    monkeypatch.setattr(capture_module, "site_of", lambda url: "hiring_cafe" if "listing" in url else "other")
    listing = FIXTURES / "pipeline" / "listing.html"
    listing.write_text(
        '<!doctype html><title>Jobs</title><a id="job" target="_blank" href="real_job.html">Senior Platform Engineer</a>',
        encoding="utf-8",
    )
    window.show_page("job_search")
    search = window.job_search
    tab = search.browser.add_tab(QUrl.fromLocalFile(str(listing)).toString())
    assert await tab.wait_loaded(20, settle=0.1)
    assert search.results == []  # a tab I opened myself is not a capture

    from app.browser.jsbridge import run_js

    await run_js(tab.page(), "document.getElementById('job').click()")
    for _ in range(200):
        if search.results:
            break
        await asyncio.sleep(0.1)
    assert search.results[-1].code == "saved"
    assert search.results[-1].toast == "Saved: Senior Platform Engineer at Acme Robotics"
    assert window.toasts.history[-1] == ("success", "Saved: Senior Platform Engineer at Acme Robotics")
    assert window.nav["saved_jobs"].badge.text() == "1"
    assert any(view.current_url().endswith("real_job.html") for view in search.browser.views())  # left open

    # The saved job shows up in the generator's queue.
    assert window.generator.queue_title.text() == "Job Queue (1)"


async def test_a_sign_in_popup_is_not_treated_as_a_job(window, monkeypatch):
    object.__setattr__(window.ctx.config, "login_hosts", ("accounts.example",))
    search = window.job_search
    window.show_page("job_search")

    class FakeTab:
        loading = False

        async def wait_loaded(self, timeout):
            return True

        def current_url(self):
            return "https://accounts.example/o/oauth2/auth"

    assert await search._capture(FakeTab(), "https://jobright.ai/") is None  # type: ignore[arg-type]
    assert search.results == [] and window.toasts.history == []
