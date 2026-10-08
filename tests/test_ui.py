"""The screens: navigation, the collapsible sidebar, read-only view-as, the sync chip, and friends."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLabel, QPushButton

from app.data.types import to_iso, utcnow
from app.services.auth import AuthError, AuthService, MemoryVault
from app.services.provider import LocalProvider, RemoteProvider
from app.sync.engine import SyncStatus
from app.ui.auth_window import AuthWindow
from app.ui.dialogs import SyncPanel, sync_chip_text
from app.ui.main_window import MainWindow
from app.ui.pages.generator import JsonFailedDialog, StartDialog, SummaryDialog
from app.ui.styles import apply_theme
from app.ui.theme import SIDEBAR_COLLAPSED, SIDEBAR_EXPANDED
from tests.factories import RESUME, add_job, add_resume, utc
from tests.fake_api import FakeApi, make_context


@pytest.fixture(autouse=True)
def _theme(qapp):
    apply_theme(qapp)


@pytest.fixture
def ctx(tmp_path: Path):
    return make_context(tmp_path)


@pytest.fixture
def window(ctx, qtbot):
    window = MainWindow(ctx)
    qtbot.addWidget(window)
    window.show()
    yield window
    window.shutdown()


def visible_buttons(widget, text: str) -> list[QPushButton]:
    return [item for item in widget.findChildren(QPushButton) if item.text().strip() == text and item.isVisible()]


async def settle(seconds: float = 0.05) -> None:
    await asyncio.sleep(seconds)


# ---------------------------------------------------------------------------
# Login / Register
# ---------------------------------------------------------------------------


class FakeAuth(AuthService):
    def __init__(self, config, fail: AuthError | None = None, confirm: bool = False) -> None:
        super().__init__(config, vault=MemoryVault())
        self.fail = fail
        self.confirm = confirm
        self.calls: list[tuple[str, ...]] = []

    async def sign_in(self, email: str, password: str):  # type: ignore[override]
        self.calls.append(("sign_in", email))
        if self.fail:
            raise self.fail
        return object()

    async def sign_up(self, full_name: str, email: str, password: str):  # type: ignore[override]
        self.calls.append(("sign_up", full_name, email))
        if self.fail:
            raise self.fail
        return None if self.confirm else object()


async def test_login_validates_before_calling_the_server(ctx, qtbot):
    auth = FakeAuth(ctx.config)
    form = AuthWindow(ctx.config, auth)
    qtbot.addWidget(form)
    form.show()

    form.submit_login()
    assert auth.calls == []
    assert form.login_form.errors[form.login_email].text() == "Enter a valid e-mail address."
    assert form.login_form.errors[form.login_password].text() == "Enter your password."

    signed_in: list[bool] = []
    form.signed_in.connect(lambda: signed_in.append(True))
    form.login_email.setText("john@example.com")
    form.login_password.setText("secret-password")
    form.submit_login()
    await settle()
    assert auth.calls == [("sign_in", "john@example.com")] and signed_in == [True]
    # The password does not linger in the field after signing in.
    assert form.login_password.text() == ""


async def test_login_shows_server_errors_in_plain_words(ctx, qtbot):
    auth = FakeAuth(ctx.config, fail=AuthError("The e-mail or password is not correct.", "invalid_credentials"))
    form = AuthWindow(ctx.config, auth)
    qtbot.addWidget(form)
    form.show()
    form.login_email.setText("john@example.com")
    form.login_password.setText("wrong")
    form.submit_login()
    await settle()
    assert form.login_form.message.text() == "The e-mail or password is not correct."
    assert form.sign_in_button.isEnabled()


async def test_register_checks_every_field_and_handles_email_confirmation(ctx, qtbot):
    auth = FakeAuth(ctx.config, confirm=True)
    form = AuthWindow(ctx.config, auth)
    qtbot.addWidget(form)
    form.show()
    form.to_register.click()
    assert form.pages[1].isVisible() and not form.pages[0].isVisible()

    form.register_name.setText("J")
    form.register_email.setText("not-an-email")
    form.register_password.setText("short")
    form.register_confirm.setText("different")
    form.submit_register()
    errors = form.register_form.errors
    assert "at least 2" in errors[form.register_name].text()
    assert errors[form.register_email].text() == "Enter a valid e-mail address."
    assert "at least 8" in errors[form.register_password].text()
    assert errors[form.register_confirm].text() == "The two passwords do not match."
    assert auth.calls == []

    form.register_name.setText("John Doe")
    form.register_email.setText("john@example.com")
    form.register_password.setText("long-enough-pw")
    form.register_confirm.setText("long-enough-pw")
    form.submit_register()
    await settle()
    assert auth.calls == [("sign_up", "John Doe", "john@example.com")]
    # E-mail confirmation required: back on the login form with the instruction.
    assert form.pages[0].isVisible()
    assert form.login_form.message.text() == "Check your inbox to confirm your e-mail, then sign in."
    assert form.login_email.text() == "john@example.com"


def test_password_fields_have_a_show_hide_eye(ctx, qtbot):
    form = AuthWindow(ctx.config, FakeAuth(ctx.config))
    qtbot.addWidget(form)
    field = form.login_password
    assert field.echoMode() == field.EchoMode.Password
    field.toggle()
    assert field.echoMode() == field.EchoMode.Normal
    field.toggle()
    assert field.echoMode() == field.EchoMode.Password


def test_missing_configuration_disables_sign_in_and_says_what_to_do(tmp_path, qtbot):
    from app.config import AppConfig

    config = AppConfig()
    form = AuthWindow(config, FakeAuth(config))
    qtbot.addWidget(form)
    assert not form.sign_in_button.isEnabled()
    assert "SUPABASE_URL" in form.login_form.message.text()


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------


def test_login_lands_on_the_dashboard(window):
    assert window.current_page() is window.dashboard
    assert window.nav["dashboard"].isChecked()
    assert [key for key, item in window.nav.items() if item.isVisible()] == [
        "dashboard",
        "job_search",
        "saved_jobs",
        "generator",
        "generated",
    ]
    assert window.user_name.text() == "John Doe" and window.avatar.text() == "JD"


def test_admin_tab_only_for_admins(tmp_path, qtbot):
    admin = MainWindow(make_context(tmp_path, role="admin"))
    qtbot.addWidget(admin)
    admin.show()
    assert admin.nav["admin"].isVisible()
    admin.shutdown()


def test_sidebar_collapses_to_only_the_hamburger_and_remembers_it(window, ctx, qtbot):
    assert window.sidebar.maximumWidth() == SIDEBAR_EXPANDED
    assert window.hamburger.toolTip() == "Collapse menu"
    window.show_page("saved_jobs")

    window.toggle_sidebar()
    qtbot.waitUntil(lambda: window.sidebar.maximumWidth() == SIDEBAR_COLLAPSED, timeout=2000)
    assert window.collapsed and window.hamburger.isVisible()
    assert not window.sidebar_content.isVisible() and not window.brand.isVisible()
    assert not any(item.isVisible() for item in window.nav.values())
    assert not window.logout_button.isVisible() and not window.user_name.isVisible()
    assert window.hamburger.toolTip() == "Expand menu"
    # The board is untouched.
    assert window.current_page() is window.saved_jobs
    assert ctx.settings.sidebar_collapsed is True

    # Remembered across restarts.
    again = MainWindow(ctx)
    qtbot.addWidget(again)
    assert again.collapsed and again.sidebar.maximumWidth() == SIDEBAR_COLLAPSED
    again.shutdown()

    window.toggle_sidebar()
    qtbot.waitUntil(lambda: window.sidebar.maximumWidth() == SIDEBAR_EXPANDED, timeout=2000)
    assert window.sidebar_content.isVisible() and ctx.settings.sidebar_collapsed is False


def test_ctrl_b_toggles_the_sidebar(window, qtbot):
    qtbot.keyClick(window, Qt.Key.Key_B, Qt.KeyboardModifier.ControlModifier)
    assert window.collapsed


def test_badges_follow_the_data(window, ctx):
    assert not window.nav["saved_jobs"].badge.isVisible()
    add_job(ctx.store, 1)
    add_job(ctx.store, 2)
    add_resume(ctx.store, 1)
    ctx.changed("jobs")
    assert window.nav["saved_jobs"].badge.text() == "2" and window.nav["generated"].badge.text() == "1"


# ---------------------------------------------------------------------------
# Sync chip and panel
# ---------------------------------------------------------------------------


def test_sync_chip_states():
    now = utcnow()
    assert sync_chip_text(SyncStatus("idle", last_push_at=now - timedelta(minutes=12))) == "Synced 12 min ago"
    assert sync_chip_text(SyncStatus("syncing")) == "Syncing…"
    assert sync_chip_text(SyncStatus("offline", pending=3)) == "Offline: 3 changes waiting"
    assert sync_chip_text(SyncStatus("offline", pending=1)) == "Offline: 1 change waiting"
    assert sync_chip_text(SyncStatus("error")) == "Sync error"
    assert sync_chip_text(SyncStatus("idle")) == "Not synced yet"
    assert sync_chip_text(SyncStatus("idle", pending=2, last_push_at=now)) == "2 changes waiting"


async def test_chip_and_offline_banner_follow_the_engine(window, ctx):
    add_resume(ctx.store, 1)
    ctx.api.offline = True
    await window.sync_now()
    assert window.sync_chip.text().strip() == "Offline: 1 change waiting"
    assert window.offline_banner.isVisible()
    assert window._retry_timer.isActive()  # backs off and tries again by itself

    ctx.api.offline = False
    await window.sync_now()
    assert window.sync_chip.text().strip() == "Synced just now"
    assert not window.offline_banner.isVisible() and not window._retry_timer.isActive()


async def test_a_local_change_schedules_a_sync(window, ctx):
    resume = add_resume(ctx.store, 1)
    await window.sync_now()
    ctx.api.push_calls.clear()
    window.generated.refresh()
    window.generated.set_status(resume.id, "applied")
    assert window._sync_debounce.isActive()
    assert window.sync_chip.text().strip() == "1 change waiting"
    window._sync_debounce.stop()
    await window.sync_now()
    assert ctx.api.resumes[resume.id]["status"] == "applied"


async def test_sync_panel_lists_rejected_rows(window, ctx, qtbot):
    resume = add_resume(ctx.store, 1)
    ctx.api.reject[resume.id] = "jdText: The job description must be at least 100 characters."
    await window.sync_now()
    panel = SyncPanel(window, ctx, window.toasts, window.sync_now)
    qtbot.addWidget(panel)
    assert "1 record(s) the web app refused" in panel.rejected_title.text()
    assert "at least 100 characters" in panel.rejected_list.text()
    assert panel.values["pending"].text() == "0 changes"


# ---------------------------------------------------------------------------
# Saved Jobs and Generated Resumes
# ---------------------------------------------------------------------------


def test_saved_jobs_table_and_removal(window, ctx, monkeypatch):
    add_job(ctx.store, 1, now=utc(2026, 10, 1))
    blocked = add_job(ctx.store, 2, now=utc(2026, 10, 2)).job
    ctx.store.set_attention(blocked.id, "page needs sign-in")
    window.show_page("saved_jobs")
    page = window.saved_jobs
    assert page.table.rowCount() == 2 and page.table.item(0, 1).text() == "Company 1"
    assert page.count.text() == "2 jobs"

    page.attention_only.setChecked(True)
    assert page.table.rowCount() == 1 and page.table.item(0, 1).text() == "Company 2"
    page.attention_only.setChecked(False)

    monkeypatch.setattr("app.ui.pages.saved_jobs.confirm", lambda *args, **kwargs: True)
    page._remove([page.rows[0]])
    assert ctx.store.saved_job_count() == 1 and page.table.rowCount() == 1
    assert window.nav["saved_jobs"].badge.text() == "1"


def test_check_duplicates_button_removes_and_reports(window, ctx):
    add_resume(ctx.store, 9, company="Globex", role="Platform Engineer")
    add_job(ctx.store, 1, now=utc(2026, 10, 1), company="Acme", role="Engineer")
    add_job(ctx.store, 2, now=utc(2026, 10, 2), company="Acme Inc", role="Engineer")
    add_job(ctx.store, 3, now=utc(2026, 10, 3), company="ACME", role="engineer")
    add_job(ctx.store, 4, now=utc(2026, 10, 4), company="Globex", role="Platform Engineer")
    add_job(ctx.store, 5, now=utc(2026, 10, 5), company="Initech", role="Engineer")
    window.show_page("saved_jobs")
    page = window.saved_jobs
    assert page.check_duplicates.isVisible() and page.table.rowCount() == 5

    page.check_duplicates.click()

    assert page.toasts.history[-1] == ("success", "3 duplicate jobs removed (2 already saved, 1 already generated)")
    assert page.table.rowCount() == 2 and page.count.text() == "2 jobs"
    assert [page.table.item(row, 1).text() for row in range(2)] == ["Acme", "Initech"]
    assert window.nav["saved_jobs"].badge.text() == "2"

    page.check_duplicates.click()
    assert page.toasts.history[-1] == ("info", "No duplicates found")
    assert page.table.rowCount() == 2


def test_check_duplicates_reports_a_single_job_in_the_singular(window, ctx):
    add_job(ctx.store, 1, now=utc(2026, 10, 1), company="Acme", role="Engineer")
    add_job(ctx.store, 2, now=utc(2026, 10, 2), company="Acme", role="Engineer")
    window.show_page("saved_jobs")
    window.saved_jobs.check_duplicates.click()
    assert window.saved_jobs.toasts.history[-1] == ("success", "1 duplicate job removed (1 already saved)")


def test_check_duplicates_waits_for_a_generation_run(window, ctx):
    add_job(ctx.store, 1, now=utc(2026, 10, 1), company="Acme", role="Engineer")
    add_job(ctx.store, 2, now=utc(2026, 10, 2), company="Acme", role="Engineer")
    window.show_page("saved_jobs")
    page = window.saved_jobs

    page.set_run_active(True)
    assert not page.check_duplicates.isEnabled() and "finished" in page.check_duplicates.toolTip()
    page._check_duplicates()
    assert ctx.store.saved_job_count() == 2

    page.set_run_active(False)
    assert page.check_duplicates.isEnabled()
    page.check_duplicates.click()
    assert ctx.store.saved_job_count() == 1


async def test_jobs_from_the_shared_list_arrive_with_a_shared_chip(window, ctx):
    ctx.api.pool.push(
        [
            {
                "url": "https://jobright.ai/jobs/info/77",
                "urlKey": "jobright.ai/jobs/info/77",
                "sourceSite": "jobright",
                "role": "Platform Engineer",
                "company": "Globex",
                "jdText": None,
                "foundAt": to_iso(utcnow() - timedelta(days=1)),
            }
        ]
    )
    window.show_page("saved_jobs")
    assert window._sync_debounce.isActive()  # opening the page checks the shared list
    window._sync_debounce.stop()
    await window.sync_now()

    page = window.saved_jobs
    assert page.table.rowCount() == 1 and page.table.item(0, 1).text() == "Globex"
    assert page.table.cellWidget(0, 3).findChild(QLabel, "SharedChip") is not None
    assert window.toasts.history[-1] == ("info", "1 new job from the shared list added to Saved Jobs")
    assert window.nav["saved_jobs"].badge.text() == "1"
    assert window.generator.queue_title.text() == "Job Queue (1)"


def test_empty_states_explain_what_to_do(window):
    window.show_page("saved_jobs")
    assert window.saved_jobs.stack.currentWidget() is window.saved_jobs.empty
    assert "Job Search" in window.saved_jobs.empty.text.text()
    window.show_page("generated")
    assert window.generated.stack.currentWidget() is window.generated.empty
    window.show_page("dashboard")
    assert window.dashboard.stack.currentIndex() == 1
    window.dashboard.empty.action.click()
    assert window.current_page() is window.job_search


def test_status_change_updates_milestones_and_the_dashboard(window, ctx):
    resume = add_resume(ctx.store, 1)
    window.show_page("generated")
    combo = window.generated.table.cellWidget(0, 8).findChild(type(window.generated.drawer.status_combo))
    combo.setCurrentIndex(combo.findData("shortlisted"))
    stored = ctx.store.get_resume(resume.id)
    assert stored.status == "shortlisted" and stored.applied_at is not None and stored.shortlisted_at is not None
    window.show_page("dashboard")
    assert window.dashboard.shortlisted_tile.value.text() == "1"
    assert window.dashboard.applied_tile.value.text() == "1"
    assert window.dashboard.generated_tile.value.text() == "1"


async def test_documents_come_from_the_web_renderer_and_are_cached(window, ctx):
    add_resume(ctx.store, 1, company="Acme", role="Staff Engineer")
    window.show_page("generated")
    page = window.generated

    target = await page.download(page.rows[0], "pdf")
    assert target is not None and target.name == "Anthony Fox_Staff Engineer_Acme.pdf"
    assert target.parent == ctx.settings.downloads_dir and target.read_bytes().startswith(b"%PDF")
    assert "Saved to Downloads" in page.toasts.history[-1][1]

    # Offline: the last rendered file is used, no request is made.
    ctx.api.offline = True
    again = await page.download(page.rows[0], "pdf")
    assert again == target and len(ctx.api.render_calls) == 1

    # A format never rendered cannot be produced offline, and the user is told.
    assert await page.download(page.rows[0], "docx") is None
    assert page.toasts.history[-1][0] == "error"


async def test_details_drawer_shows_preview_description_and_json(window, ctx):
    add_resume(ctx.store, 1, company="Acme", role="Staff Engineer")
    window.show_page("generated")
    page = window.generated
    page.show_details(page.rows[0].id)
    drawer = page.drawer
    assert drawer.isVisible() and drawer.title.text() == "Acme - Staff Engineer"
    assert drawer.chat_field.text().startswith("https://chatgpt.com/c/") and drawer.chat_field.isReadOnly()
    assert [drawer.tabs.tabText(i) for i in range(3)] == ["Composed Resume", "Job Description", "Resume JSON"]
    # The list holds light rows; the description, the JSON and the PDF arrive a moment later.
    for _ in range(100):
        if drawer.pdf_document.pageCount():
            break
        await settle(0.05)
    assert "distributed systems" in drawer.jd_text.toPlainText()
    assert json.loads(drawer.json_text.toPlainText()) == RESUME
    assert drawer.pdf_document.pageCount() == 1

    drawer.status_combo.setCurrentIndex(drawer.status_combo.findData("applied"))
    assert ctx.store.list_resumes()[0].status == "applied"
    drawer.close_drawer()
    assert not drawer.isVisible()


def test_dashboard_cards_open_generated_resumes_with_the_same_rows(window, ctx):
    now = utcnow()
    one = add_resume(ctx.store, 1, now=now)
    add_resume(ctx.store, 2, now=now)
    add_resume(ctx.store, 3, now=now - timedelta(days=60))
    ctx.store.set_status(one.id, "applied", now=now)
    window.dashboard.set_period("month")
    window.dashboard.refresh()
    assert window.dashboard.generated_tile.value.text() == "2"

    window.dashboard._drill_card("applied")
    assert window.current_page() is window.generated
    assert [row.id for row in window.generated.rows] == [one.id]
    assert window.generated.chip_row.isVisible() and window.generated.chip.text().startswith("Applied · ")
    window.generated.clear_external()
    assert len(window.generated.rows) == 3

    window.dashboard._drill_card("generated")
    assert len(window.generated.rows) == 2


def test_dashboard_period_is_remembered(window, ctx):
    assert window.dashboard.selector.current() == "week"
    window.dashboard.set_period("day")
    assert ctx.settings.dashboard_period == "day"


# ---------------------------------------------------------------------------
# Resume Generating
# ---------------------------------------------------------------------------


def test_start_is_disabled_until_a_prompt_and_a_queue_exist(window, ctx, tmp_path):
    page = window.generator
    window.show_page("generator")
    assert not page.start.isEnabled() and page.start.toolTip() == "Import a prompt first."
    assert not page.download.isEnabled()

    prompt = tmp_path / "Anthony-resume-prompt.txt"
    prompt.write_text("You write resumes.\n{{PASTE_ORIGINAL_RESUME_HERE}}", encoding="utf-8")
    assert page.load_prompt(prompt)
    assert page.chips.text() == "Prompt: Anthony-resume-prompt.txt  ·  Resume: none"
    assert not page.start.isEnabled() and "queue is empty" in page.start.toolTip()

    add_job(ctx.store, 1)
    ctx.changed("jobs")
    assert page.queue_title.text() == "Job Queue (1)"
    # (No embedded browser in this test window, which is the remaining reason.)
    assert page.start_problem() == "The embedded browser is not available."


async def test_importing_and_clearing_the_original_resume(window, ctx, tmp_path):
    page = window.generator
    window.show_page("generator")
    resume = tmp_path / "resume.txt"
    resume.write_text("Anthony Fox\nStaff Engineer with ten years of experience." * 3, encoding="utf-8")
    assert await page.load_resume(resume)
    assert "Resume: resume.txt" in page.chips.text() and page.clear_resume.isVisible()
    page.remove_resume()
    assert "Resume: none" in page.chips.text() and ctx.store.assets().original_resume_text is None

    bad = tmp_path / "empty.txt"
    bad.write_text("", encoding="utf-8")
    assert not await page.load_resume(bad)
    assert page.toasts.history[-1][0] == "error"


def test_the_mode_modal_remembers_the_choice(window, ctx, qtbot):
    dialog = StartDialog(window, ctx.settings.start_mode, attention_count=0, seconds=5)
    qtbot.addWidget(dialog)
    assert dialog.mode == "automation" and dialog.automation.selected and not dialog.human.selected
    assert not dialog.include.isVisibleTo(dialog) and dialog.include_attention is False
    dialog.human.clicked.emit()
    assert dialog.mode == "human" and dialog.human.selected
    ctx.settings.start_mode = dialog.mode

    again = StartDialog(window, ctx.settings.start_mode, attention_count=2, seconds=5)
    qtbot.addWidget(again)
    assert again.mode == "human" and again.human.selected
    assert again.include.text() == "Include jobs needing attention (2)" and not again.include.isChecked()

    # The choice is read AFTER the dialog has closed: it must still be there.
    again.include.setChecked(True)
    again.show()
    again.accept()
    assert again.include_attention is True


def test_json_failure_modal(window, qtbot):
    issues = [{"path": "contact.email", "message": "Required"}]
    dialog = JsonFailedDialog(window, 2, issues, '{"contact": {"fullName": "A"}}')
    qtbot.addWidget(dialog)
    assert dialog.attempt.text() == "Attempt 2 of 3" and dialog.retry.isEnabled()
    last = JsonFailedDialog(window, 3, issues, "{}")
    qtbot.addWidget(last)
    assert not last.retry.isEnabled()


def test_run_summary_lists_skips_and_jobs_needing_attention(window, qtbot):
    from app.automation.pipeline import RunSummary, SkipRecord

    summary = RunSummary(
        generated=3,
        skipped=[
            SkipRecord("A", "B", "linkedin"),
            SkipRecord("C", "D", "linkedin"),
            SkipRecord("E", "F", "invalid_json"),
        ],
        attention=[SkipRecord("Engineer", "Acme", "needs_user_action", "page needs sign-in")],
    )
    dialog = SummaryDialog(window, summary)
    qtbot.addWidget(dialog)
    assert dialog.generated.text() == "3 resumes generated" and dialog.skipped.text() == "3 skipped"
    texts = [item.text() for item in dialog.findChildren(type(dialog.generated))]
    assert any("LinkedIn job: 2" in text for text in texts)
    assert any("Engineer at Acme: page needs sign-in" in text for text in texts)


async def test_review_refuses_next_while_the_data_breaks_the_limits(window, ctx):
    from app.automation.pipeline import ReviewData

    page = window.generator
    window.show_page("generator")
    page.board.setCurrentWidget(page.run_area)
    job = add_job(ctx.store, 1).job
    data = ReviewData("Acme", "Engineer", "too short", "https://example.com/job", None)

    task = asyncio.ensure_future(page.review(job, data, "human"))
    await settle()
    # The alert is non-modal and sits over the review panel.
    assert page.alert_card.isVisible() and page.review_panel.isVisible()
    assert page.alert_title.text() == "Please check extracted JD and role, company name"
    assert not page.alert_card.isModal() if hasattr(page.alert_card, "isModal") else True

    page._review_next()
    await settle()
    assert not task.done()
    assert "too short" in page.review_panel.error.text()

    # The fields beneath the alert can be edited; then Next goes through.
    page.review_panel.jd.setPlainText("A real job description. " * 10)
    page.review_panel.company.setText("Acme Robotics")
    page._review_next()
    result = await asyncio.wait_for(task, 5)
    assert result.company == "Acme Robotics" and len(result.jd_text) > 100
    assert not page.alert_card.isVisible() and not page.review_panel.isVisible()


async def test_automation_countdown_continues_by_itself_and_stop_ends_it(window, ctx):
    from app.automation.pipeline import ReviewData

    page = window.generator
    window.show_page("generator")
    page.board.setCurrentWidget(page.run_area)
    ctx.settings.automation_seconds = 1
    job = add_job(ctx.store, 1).job
    data = ReviewData("Acme", "Engineer", "A real job description. " * 10, "https://example.com/job", None)

    task = asyncio.ensure_future(page.review(job, data, "automation"))
    await settle()
    assert page.countdown_card.isVisible() and page.countdown_title.text() == "Automation Running"
    assert page.countdown_text.text() == "Continuing in 1 seconds…"
    assert [b.text() for b in page.countdown_card.buttons] == ["Stop"]
    result = await asyncio.wait_for(task, 5)
    assert result is not None and not page.countdown_card.isVisible()

    # Editing a field while the countdown runs hands control to the user.
    ctx.settings.automation_seconds = 30
    task = asyncio.ensure_future(page.review(job, data, "automation"))
    await settle()
    page.review_panel.company.setText("Acme Robotics")
    page.review_panel.company.textEdited.emit("Acme Robotics")
    assert not page.countdown_card.isVisible() and page.alert_title.text() == "Countdown paused"
    page._resolve(None)  # Cancel Job
    assert await asyncio.wait_for(task, 5) is None


async def test_a_blocked_page_card_times_out_to_skip_in_automation_and_waits_in_human_check(window, ctx, monkeypatch):
    page = window.generator
    window.show_page("generator")
    page.board.setCurrentWidget(page.run_area)
    job = add_job(ctx.store, 1).job

    object.__setattr__(ctx.config, "attention_grace_seconds", 1)
    answer = await asyncio.wait_for(page.blocked(job, "page needs sign-in", "automation"), 5)
    assert answer == "skip"

    task = asyncio.ensure_future(page.blocked(job, "page needs sign-in", "human"))
    await settle(0.3)
    assert not task.done()
    assert page.alert_title.text() == "This page needs you: page needs sign-in."
    assert page.alert_body.text() == "Fix it in the browser on the left, then press Retry."
    assert [b.text() for b in page.alert_card.buttons] == ["Cancel Job", "Skip for now", "Retry"]
    page.alert_card.buttons[2].click()
    assert await asyncio.wait_for(task, 5) == "retry"


# ---------------------------------------------------------------------------
# Admin and view-as
# ---------------------------------------------------------------------------


def remote_view() -> dict:
    return {
        "user": {"id": "22222222-2222-4222-8222-222222222222", "fullName": "John Smith", "email": "john@example.com"},
        "sync": {
            "lastPushAt": "2026-10-01T08:00:00.000Z",
            "lastPullAt": None,
            "lastImportAt": None,
            "schemaVersion": 1,
        },
        "savedJobs": [
            {
                "id": "j1",
                "sourceSite": "jobright",
                "url": "https://jobright.ai/jobs/info/1",
                "role": "Engineer",
                "company": "Globex",
                "attentionReason": None,
                "createdAt": "2026-09-30T08:00:00.000Z",
                "deletedAt": None,
            }
        ],
        "generatedResumes": [
            {
                "id": "r1",
                "serverId": "s1",
                "candidateName": "John Smith",
                "role": "Backend Engineer",
                "company": "Globex",
                "jdUrl": "https://example.com/1",
                "applyUrl": None,
                "chatUrl": "https://chatgpt.com/c/abcdef123456",
                "status": "applied",
                "createdAt": utcnow().isoformat(),
                "statusChangedAt": utcnow().isoformat(),
                "appliedAt": utcnow().isoformat(),
                "shortlistedAt": None,
                "rejectedAt": None,
                "updatedAt": utcnow().isoformat(),
            }
        ],
        "truncated": False,
    }


@pytest.fixture
def admin_window(tmp_path, qtbot):
    api = FakeApi()
    api.admin_view = remote_view()
    api.users = [
        {
            "id": "22222222-2222-4222-8222-222222222222",
            "fullName": "John Smith",
            "email": "john@example.com",
            "role": "user",
            "createdAt": "2026-06-02T10:00:00.000Z",
            "applicationCount": 15,
        },
    ]
    ctx = make_context(tmp_path, role="admin", api=api)
    add_resume(ctx.store, 1, company="MyOwn")
    window = MainWindow(ctx)
    qtbot.addWidget(window)
    window.show()
    yield window
    window.shutdown()


async def test_admin_user_list(admin_window):
    window = admin_window
    window.show_page("admin")
    await window.admin.load()
    table = window.admin.table
    assert table.rowCount() == 1
    assert [table.horizontalHeaderItem(i).text() for i in range(6)] == [
        "#",
        "Full Name",
        "Email",
        "Joined At",
        "Generated",
        "Action",
    ]
    assert table.item(0, 1).text() == "John Smith" and table.item(0, 4).text() == "15"


async def test_view_as_shows_the_other_users_data_read_only(admin_window):
    window = admin_window
    assert await window.enter_view_as("22222222-2222-4222-8222-222222222222", "John Smith", "john@example.com")

    assert window.view_banner.isVisible()
    assert window.view_text.text().startswith("Viewing as John Smith (john@example.com)  ·  Last synced: ")
    assert window.exit_view.text() == "Exit User View"
    # Their dashboard, not mine.
    dashboard = window.current_page()
    assert dashboard is not window.dashboard and dashboard.applied_tile.value.text() == "1"

    # The browser pages are unavailable.
    assert not window.nav["job_search"].isEnabled() and not window.nav["generator"].isEnabled()
    window.show_page("generator")
    assert window.current_page() is dashboard

    # Saved Jobs: no Remove, no selection, no "open in browser".
    window.show_page("saved_jobs")
    saved = window.current_page()
    assert saved.table.rowCount() == 1 and saved.table.item(0, 1).text() == "Globex"
    assert saved.table.isColumnHidden(7) and not saved.remove_selected.isVisible()
    assert saved.table.cellWidget(0, 7) is None

    # Generated Resumes: a status chip instead of a select, no Open buttons, downloads still work.
    window.show_page("generated")
    generated = window.current_page()
    assert generated.table.rowCount() == 1 and generated.table.item(0, 2).text() == "Globex"
    assert generated.table.cellWidget(0, 8).findChild(type(window.generated.drawer.status_combo)) is None
    assert generated.drawer.status_combo is None and generated.drawer.status_chip is not None
    assert all(not item.isVisibleTo(generated.drawer) for item in generated.drawer.open_buttons)
    generated.set_status("r1", "rejected")
    assert generated.rows[0].status == "applied"  # nothing can be changed

    target = await generated.download(generated.rows[0], "pdf")
    assert target is not None and target.name == "Anthony Fox_Backend Engineer_Globex.pdf"

    # Nothing of theirs was written into my database.
    assert [row.company for row in window.ctx.store.list_resumes()] == ["MyOwn"]

    window.exit_view_as()
    assert not window.view_banner.isVisible() and window.current_page() is window.dashboard
    assert window.nav["job_search"].isEnabled() and window.view_pages == {}


async def test_view_as_is_refused_for_non_admins_by_the_server(window, ctx):
    assert not window.nav["admin"].isVisible()
    assert await window.enter_view_as("22222222-2222-4222-8222-222222222222", "John", "j@example.com") is False
    assert not window.view_banner.isVisible()
    assert window.toasts.history[-1] == ("error", "Administrator access is required for this operation.")


def test_remote_provider_filters_like_the_local_one(tmp_path):
    from app.data.store import ResumeFilter
    from app.services.provider import remote_resume_row

    row = remote_resume_row(remote_view()["generatedResumes"][0])
    provider = RemoteProvider(api=None, user_id="u", full_name="J", email="e", rows=[row])  # type: ignore[arg-type]
    assert provider.read_only is True
    assert provider.resumes(ResumeFilter(statuses=("applied",))) == [row]
    assert provider.resumes(ResumeFilter(statuses=("rejected",))) == []
    assert provider.resumes(ResumeFilter(search="globex")) == [row]
    assert LocalProvider.read_only is False


def test_usable_at_1366_by_768(window):
    window.resize(1366, 768)
    QApplication.processEvents()
    assert window.minimumWidth() <= 1366 and window.minimumHeight() <= 768
    for key in ("dashboard", "saved_jobs", "generator", "generated"):
        window.show_page(key)
        QApplication.processEvents()
        assert window.current_page().width() >= 1000


async def test_an_ended_session_asks_to_sign_in_again(window, ctx):
    from app.sync.api_client import ApiError

    ctx.api.fail_with = ApiError("Your session has ended. Sign in again.", "UNAUTHENTICATED", 401)
    asked: list[bool] = []
    window.logout_requested.connect(lambda: asked.append(True))
    await window.sync_now()
    assert window.offline_banner.isVisible() and window.offline_action.text() == "Sign in"
    assert "session has ended" in window.offline_text.text()
    window.offline_action.click()
    assert asked == [True]


async def test_rejected_saved_jobs_are_listed_in_the_sync_panel(window, ctx, qtbot):
    job = add_job(ctx.store, 1).job
    original = ctx.api.sync_push

    async def refuse_jobs(body):
        result = await original(body)
        result["savedJobs"] = [
            {
                "id": row["id"],
                "result": "rejected",
                "code": "invalid_row",
                "reason": "url: URL must be a valid http or https address",
            }
            for row in body["savedJobs"]
        ]
        return result

    ctx.api.sync_push = refuse_jobs
    status = await window.sync_now()
    assert status.rejected == 1 and status.pending == 0
    assert ctx.store.get_saved_job(job.id).sync_error.startswith("url:")
    panel = SyncPanel(window, ctx, window.toasts, window.sync_now)
    qtbot.addWidget(panel)
    assert "Saved job Company 1 - Engineer 1: url:" in panel.rejected_list.text()


async def test_test_connection_never_sends_the_token_over_plain_http(window, ctx, qtbot):
    from app.ui.dialogs import SettingsDialog

    dialog = SettingsDialog(window, ctx, window.toasts, window.sync_now)
    qtbot.addWidget(dialog)
    calls: list[str] = []
    ctx.api.with_base_url = lambda url: calls.append(url) or ctx.api
    dialog.web_url.setText("http://evil.example")
    await dialog.test_connection()
    assert calls == [] and "https://" in dialog.test_result.text()

    dialog.web_url.setText("https://other.example")
    await dialog.test_connection()
    assert calls == ["https://other.example"] and dialog.test_result.text().startswith("✓ Web app reachable")
    assert ctx.api.base_url == "https://web.example"  # the saved address was never swapped


def test_a_database_error_never_carries_resume_text(tmp_path):
    import sqlalchemy

    from tests.factories import make_store

    store = make_store(tmp_path)
    with store.database.engine.begin() as connection:
        connection.exec_driver_sql("DROP TABLE user_assets")
    try:
        store.set_original_resume("SECRET RESUME TEXT", "resume.pdf")
    except sqlalchemy.exc.SQLAlchemyError as error:
        assert "SECRET" not in str(error)
    else:
        raise AssertionError("expected a database error")


async def test_document_cache_uses_short_names_and_refreshes_after_a_day(tmp_path, monkeypatch):
    from app.services import documents as documents_module

    ctx = make_context(tmp_path)
    resume = add_resume(ctx.store, 1)

    async def get():
        return await ctx.documents.get(
            record_id=resume.id, resume_json=resume.resume_json, company="Acme", role="Engineer", fmt="pdf"
        )

    first = await get()
    # A short fixed name on disk (Windows paths are limited); the real name travels beside it.
    assert first.path.name == "resume.pdf" and first.filename == "Anthony Fox_Engineer_Acme.pdf"
    assert (await get()).path == first.path and len(ctx.api.render_calls) == 1  # fresh: no second request

    monkeypatch.setattr(documents_module, "CACHE_FRESH_SECONDS", 0)
    await get()
    assert len(ctx.api.render_calls) == 2  # old: rendered again, so a template change arrives
    ctx.api.offline = True
    assert (await get()).path == first.path  # unreachable: the old file still serves
