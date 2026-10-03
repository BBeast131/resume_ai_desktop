"""Screenshots of the generator mid-run and of the dialogs (needs the embedded browser)."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    os.environ["RESUME_AI_DATA_DIR"] = str(out / "_data2")
    import shutil

    shutil.rmtree(out / "_data2", ignore_errors=True)

    import qasync
    from PySide6.QtCore import QEventLoop, QUrl
    from PySide6.QtWebEngineCore import QWebEngineProfile
    from PySide6.QtWidgets import QApplication

    app = QApplication(sys.argv[:1])
    loop = qasync.QEventLoop(app, set_running_loop=True, already_running=True)
    asyncio.set_event_loop(loop)

    from app.automation import chatgpt
    from app.automation import pipeline as pipeline_module
    from app.automation.pipeline import RunSummary, SkipRecord
    from app.ui.dialogs import SettingsDialog, SyncPanel
    from app.ui.main_window import MainWindow
    from app.ui.pages.generator import JsonFailedDialog, StartDialog, SummaryDialog
    from app.ui.styles import apply_theme
    from tests.fake_api import make_context
    from tests.helpers import FIXTURES
    from tools.preview import sample

    apply_theme(app)
    chatgpt.POLL_SECONDS, chatgpt.STABLE_SECONDS, chatgpt.COMPLETE_JSON_QUIET_SECONDS = 0.2, 0.4, 0.4
    pipeline_module.WEB_SCHEMES = ("http://", "https://", "file://")
    ctx = make_context(out / "_data2", role="admin")
    sample(ctx)
    for job in ctx.store.list_saved_jobs():
        ctx.store.remove_saved_jobs([job.id])
    job_url = QUrl.fromLocalFile(str(FIXTURES / "pipeline" / "real_job.html")).toString()
    for n, (company, role) in enumerate(
        [("Acme Robotics", "Senior Platform Engineer"), ("OpenAI", "Software Engineer"), ("Coinbase", "Staff Engineer")]
    ):
        ctx.store.add_saved_job(url=job_url, url_key=f"k{n}", source_site="jobright", role=role, company=company)
    ctx.store.set_prompt("You write resumes.", "Anthony-resume-prompt.txt")
    ctx.web_profile = QWebEngineProfile(app)
    ctx.settings.automation_seconds = 5
    window = MainWindow(ctx, chatgpt_url=QUrl.fromLocalFile(str(FIXTURES / "chatgpt" / "composer.html")).toString())
    window.resize(1366, 768)
    window.show()
    window.show_page("generator")

    def shot(name: str, widget=None) -> None:
        app.processEvents()
        (widget or window).grab().save(str(out / f"{name}.png"))
        print("wrote", name)

    def run(coroutine, timeout: float = 60.0):
        task = loop.create_task(asyncio.wait_for(coroutine, timeout))
        spinner = QEventLoop()
        task.add_done_callback(lambda _t: spinner.quit())
        if not task.done():
            spinner.exec()
        return task.result()

    async def until(predicate, seconds: float = 30.0) -> None:
        for _ in range(int(seconds * 10)):
            if predicate():
                return
            await asyncio.sleep(0.1)

    page = window.generator
    shot("gen_ready")

    task = page.begin("human")
    run(until(lambda: page.alert_card.isVisible()))
    run(asyncio.sleep(0.5))
    shot("gen_review_human")
    page.stop()
    run(asyncio.wait({task}, timeout=20))
    page.summary_dialog.accept()

    task = page.begin("automation")
    run(until(lambda: page.countdown_card.isVisible()))
    run(asyncio.sleep(1.2))
    shot("gen_countdown")
    run(until(lambda: page.stepper.current == 2 and "Waiting" in page.status.text(), 30))
    shot("gen_chatgpt")
    page.stop()
    run(asyncio.wait({task}, timeout=20))
    page.summary_dialog.accept()

    for name, dialog in (
        ("start_modal", StartDialog(window, "automation", 1, 5)),
        ("json_modal", JsonFailedDialog(window, 2, [{"path": "contact.email", "message": "Required"}], "{}")),
        (
            "summary",
            SummaryDialog(
                window,
                RunSummary(
                    generated=9,
                    skipped=[SkipRecord("A", "B", "linkedin"), SkipRecord("C", "D", "form_only_no_jd")],
                    attention=[SkipRecord("Staff Engineer", "Coinbase", "needs_user_action", "page needs sign-in")],
                ),
            ),
        ),
        ("sync_panel", SyncPanel(window, ctx, window.toasts, window.sync_now)),
        ("settings", SettingsDialog(window, ctx, window.toasts, window.sync_now)),
    ):
        dialog.show()
        app.processEvents()
        shot(name)  # the window with the scrim
        shot(name + "_card", dialog)
        dialog.done(0)

    from tests.test_ui import remote_view

    ctx.api.admin_view = remote_view()
    run(window.enter_view_as("22222222-2222-4222-8222-222222222222", "John Smith", "john@example.com"))
    shot("view_as")
    window.shutdown()
    os._exit(0)


if __name__ == "__main__":
    raise SystemExit(main())
