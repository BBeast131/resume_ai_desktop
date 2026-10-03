"""Phase 0 spike: can an embedded Chromium do what Resume AI needs?

Run it, sign in to the three sites yourself, press the two test buttons on a
ChatGPT tab, tick the checklist, and save the report.

    python spike/browser_spike.py              # the window
    python spike/browser_spike.py --selftest   # headless check against a local fixture

What it proves or disproves:
  1. jobright.ai, hiring.cafe and chatgpt.com load and let you sign in.
  2. Links that open a new tab open one INSIDE this window (createWindow).
  3. Logins survive a restart (persistent profile).
  4. A long prompt can be pasted into ChatGPT's composer in chunks, sent, and
     the reply read back, with the same page script the Chrome extension uses.
  5. Site permission pop-ups (notifications and the like) are refused silently.

Nothing here types a password, solves a CAPTCHA or submits an application.
The report holds host names, counts and yes/no answers only.
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

# Must be set before QApplication exists. ChatGPT only writes its reply into the
# page while the page is painted; these stop Chromium throttling a tab that is
# covered or in the background.
_FLAGS = (
    "--disable-renderer-backgrounding --disable-background-timer-throttling --disable-backgrounding-occluded-windows"
)
os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = (os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "") + " " + _FLAGS).strip()

from PySide6.QtCore import QStandardPaths, Qt, QTimer, QUrl  # noqa: E402
from PySide6.QtWebEngineCore import (  # noqa: E402
    QWebEnginePage,
    QWebEngineProfile,
    QWebEngineScript,
    qWebEngineChromiumVersion,
)
from PySide6.QtWebEngineWidgets import QWebEngineView  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

HERE = Path(__file__).resolve().parent
BRIDGE_JS = (HERE / "bridge.js").read_text(encoding="utf-8")
MAIN_WORLD = QWebEngineScript.ScriptWorldId.MainWorld.value

SITES = {
    "JobRight.ai": "https://jobright.ai/",
    "HiringCafe": "https://hiring.cafe/",
    "ChatGPT": "https://chatgpt.com/",
}

SEND_TEST_PROMPT = (
    "This is an automated connection test from a desktop app. "
    'Reply with exactly this JSON and nothing else: {"ok": true, "test": "resume-ai-spike"}'
)


def paste_test_text() -> str:
    """About 24,000 harmless characters: three chunks' worth."""
    lines = [
        f"Resume AI spike paste test, line {index:04d}: the quick brown fox jumps over the lazy dog."
        for index in range(1, 281)
    ]
    return "\n".join(lines)


def chrome_user_agent(profile: QWebEngineProfile) -> str:
    """The engine's own user agent with the QtWebEngine token removed.

    Keeps the real Chromium version, so sites see an ordinary desktop Chrome
    rather than an embedded browser they may refuse.
    """
    parts = [part for part in profile.httpUserAgent().split(" ") if not part.startswith("QtWebEngine/")]
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Running page scripts
# ---------------------------------------------------------------------------


def run_js(page: QWebEnginePage, code: str, callback: Callable[[Any], None] | None = None) -> None:
    if callback is None:
        page.runJavaScript(code, MAIN_WORLD)
    else:
        page.runJavaScript(code, MAIN_WORLD, callback)


def call_async(
    page: QWebEnginePage,
    expression: str,
    on_done: Callable[[dict[str, Any]], None],
    timeout_ms: int = 60_000,
) -> None:
    """Run an expression that returns a Promise and deliver its result.

    runJavaScript does not wait for promises, so the result is parked on the
    page under a random key and polled for.
    """
    key = "k" + uuid.uuid4().hex
    start = (
        "(() => { window.__raiJobs = window.__raiJobs || {};"
        f" window.__raiJobs['{key}'] = {{ done: false }};"
        f" Promise.resolve().then(() => ({expression}))"
        f" .then((value) => {{ window.__raiJobs['{key}'] = {{ done: true, value }}; }},"
        f" (error) => {{ window.__raiJobs['{key}'] = {{ done: true, error: String(error) }}; }});"
        " return true; })()"
    )
    read = (
        f"(() => {{ const job = (window.__raiJobs || {{}})['{key}'];"
        f" if (job && job.done) delete window.__raiJobs['{key}'];"
        " return JSON.stringify(job || null); })()"
    )
    deadline = time.monotonic() + timeout_ms / 1000
    timer = QTimer(page)
    timer.setInterval(200)

    def poll() -> None:
        if time.monotonic() > deadline:
            timer.stop()
            on_done({"error": "timeout"})
            return

        def got(raw: Any) -> None:
            if not timer.isActive():
                return
            job = json.loads(raw) if isinstance(raw, str) and raw else None
            if job is None:
                # The page navigated and lost the job.
                timer.stop()
                on_done({"error": "page changed before the script finished"})
            elif job.get("done"):
                timer.stop()
                on_done(job)

        run_js(page, read, got)

    timer.timeout.connect(poll)
    run_js(page, start, lambda _ok: timer.start())


def ensure_bridge(page: QWebEnginePage, then: Callable[[], None]) -> None:
    run_js(
        page,
        "Boolean(window.__RAI_BRIDGE__)",
        lambda present: then() if present else run_js(page, BRIDGE_JS, lambda _ok: then()),
    )


def looks_like_complete_json(text: str) -> bool:
    """Port of looksLikeCompleteJson in chatgpt-bridge.ts."""
    start = text.find("{")
    if start < 0:
        return False
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        ch = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            if depth < 0:
                return False
            if depth == 0:
                return text[index + 1 :].strip(" \t\r\n`") == ""
    return False


class ReplyWaiter:
    """Wait for ChatGPT's reply with the extension's completion rules.

    Done when ChatGPT is not answering and the text has held still for 1.8 s,
    or when the text is one complete JSON object unchanged for 3 s.
    """

    def __init__(
        self,
        page: QWebEnginePage,
        baseline: int,
        on_progress: Callable[[str], None],
        on_done: Callable[[str | None, str], None],
        timeout_s: float = 180,
    ) -> None:
        self.page = page
        self.baseline = baseline
        self.on_progress = on_progress
        self.on_done = on_done
        self.deadline = time.monotonic() + timeout_s
        self.last_text = ""
        self.unchanged_since = time.monotonic()
        self.last_length = 0
        self.stable_since = 0.0
        self.timer = QTimer(page)
        self.timer.setInterval(1200)
        self.timer.timeout.connect(self.poll)
        self.timer.start()

    def poll(self) -> None:
        if time.monotonic() > self.deadline:
            self.timer.stop()
            self.on_done(None, "timed out")
            return
        run_js(self.page, f"JSON.stringify(window.__RAI_BRIDGE__.readBeyond({self.baseline}))", self.got)

    def got(self, raw: Any) -> None:
        if not self.timer.isActive() or not isinstance(raw, str):
            return
        poll = json.loads(raw)
        if poll.get("status") != "ok":
            return
        text = poll.get("text", "")
        now = time.monotonic()
        self.on_progress(poll.get("diag", ""))

        if text != self.last_text:
            self.last_text = text
            self.unchanged_since = now
        elif text and now - self.unchanged_since >= 3.0 and looks_like_complete_json(text):
            self.finish(text, "complete JSON")
            return

        if poll.get("streaming"):
            self.stable_since = 0.0
            self.last_length = len(text)
            return
        if text and len(text) == self.last_length:
            if self.stable_since == 0.0:
                self.stable_since = now
            if now - self.stable_since >= 1.8:
                self.finish(text, "stable")
        else:
            self.stable_since = 0.0
            self.last_length = len(text)

    def finish(self, text: str, how: str) -> None:
        self.timer.stop()
        self.on_done(text, how)


# ---------------------------------------------------------------------------
# Browser
# ---------------------------------------------------------------------------


class SpikePage(QWebEnginePage):
    """A page that opens new windows as tabs and refuses pop-ups that block."""

    def __init__(self, profile: QWebEngineProfile, browser: Browser) -> None:
        super().__init__(profile, browser)
        self.browser = browser
        self.permissionRequested.connect(self.deny_permission)

    def createWindow(self, _window_type: QWebEnginePage.WebWindowType) -> QWebEnginePage:  # noqa: N802
        self.browser.stats["new_tabs_from_pages"] += 1
        return self.browser.add_tab(None, foreground=True).page()

    def deny_permission(self, permission: Any) -> None:
        self.browser.stats["permissions_denied"] += 1
        permission.deny()

    # JavaScript dialogs would freeze the page until someone answers them.
    def javaScriptAlert(self, _origin: QUrl, _message: str) -> None:  # noqa: N802
        self.browser.stats["dialogs_dismissed"] += 1

    def javaScriptConfirm(self, _origin: QUrl, _message: str) -> bool:  # noqa: N802
        self.browser.stats["dialogs_dismissed"] += 1
        return False

    def javaScriptPrompt(self, _origin: QUrl, _message: str, _default: str) -> tuple[bool, str]:  # noqa: N802
        self.browser.stats["dialogs_dismissed"] += 1
        return (False, "")


class Browser(QWidget):
    def __init__(self, profile: QWebEngineProfile, log: Callable[[str], None]) -> None:
        super().__init__()
        self.profile = profile
        self.log = log
        self.stats = {"new_tabs_from_pages": 0, "permissions_denied": 0, "dialogs_dismissed": 0}
        self.hosts_loaded: dict[str, bool] = {}

        self.address = QLineEdit()
        self.address.setReadOnly(True)
        back = QPushButton("←")
        forward = QPushButton("→")
        reload_button = QPushButton("⟳")
        for button in (back, forward, reload_button):
            button.setFixedWidth(36)
        back.clicked.connect(lambda: self.current() and self.current().back())
        forward.clicked.connect(lambda: self.current() and self.current().forward())
        reload_button.clicked.connect(lambda: self.current() and self.current().reload())

        bar = QHBoxLayout()
        for widget in (back, forward, reload_button, self.address):
            bar.addWidget(widget)

        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.setDocumentMode(True)
        self.tabs.tabCloseRequested.connect(self.close_tab)
        self.tabs.currentChanged.connect(lambda _index: self.sync_address())

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(bar)
        layout.addWidget(self.tabs)

    def current(self) -> QWebEngineView | None:
        widget = self.tabs.currentWidget()
        return widget if isinstance(widget, QWebEngineView) else None

    def add_tab(self, url: str | None, foreground: bool = True) -> QWebEngineView:
        view = QWebEngineView()
        view.setPage(SpikePage(self.profile, self))
        index = self.tabs.addTab(view, "New tab")
        view.titleChanged.connect(
            lambda title, v=view: self.tabs.setTabText(self.tabs.indexOf(v), (title or "Tab")[:24])
        )
        view.urlChanged.connect(lambda _url, v=view: self.sync_address() if v is self.current() else None)
        view.loadFinished.connect(lambda ok, v=view: self.loaded(v, ok))
        if foreground:
            self.tabs.setCurrentIndex(index)
        if url:
            view.setUrl(QUrl(url))
        return view

    def close_tab(self, index: int) -> None:
        widget = self.tabs.widget(index)
        self.tabs.removeTab(index)
        if widget is not None:
            widget.deleteLater()

    def sync_address(self) -> None:
        view = self.current()
        self.address.setText(view.url().toString() if view else "")

    def loaded(self, view: QWebEngineView, ok: bool) -> None:
        host = view.url().host()
        if host:
            self.hosts_loaded[host] = self.hosts_loaded.get(host, False) or ok
            self.log(f"Loaded {host}: {'ok' if ok else 'FAILED'}")


def make_profile(app: QApplication, storage_dir: Path | None) -> QWebEngineProfile:
    if storage_dir is None:
        # Off the record: used by the self-test only.
        profile = QWebEngineProfile(app)
    else:
        profile = QWebEngineProfile("resume-ai-spike", app)
        profile.setPersistentStoragePath(str(storage_dir / "storage"))
        profile.setCachePath(str(storage_dir / "cache"))
        profile.setPersistentCookiesPolicy(QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies)
    # Never remember a granted permission: every request reaches deny_permission.
    profile.setPersistentPermissionsPolicy(QWebEngineProfile.PersistentPermissionsPolicy.AskEveryTime)
    profile.setHttpUserAgent(chrome_user_agent(profile))
    return profile


# ---------------------------------------------------------------------------
# Window
# ---------------------------------------------------------------------------

CHECKS = [
    ("jobright_login", "I am signed in to jobright.ai in this window"),
    ("hiringcafe_login", "I am signed in to hiring.cafe (or it works without signing in)"),
    ("chatgpt_login", "I am signed in to ChatGPT in this window"),
    ("job_new_tab", "Clicking a job opened a new tab here, not a separate window"),
    ("pages_look_right", "The pages look and behave normally"),
    ("still_signed_in", "After closing and reopening this app I was still signed in"),
]


class SpikeWindow(QMainWindow):
    def __init__(self, profile: QWebEngineProfile, storage_dir: Path) -> None:
        super().__init__()
        self.setWindowTitle("Resume AI: browser spike (Phase 0)")
        self.resize(1400, 860)
        self.storage_dir = storage_dir
        self.results: dict[str, Any] = {}

        self.log_box = QPlainTextEdit()
        self.log_box.setReadOnly(True)
        self.browser = Browser(profile, self.log)

        site_bar = QHBoxLayout()
        for name, url in SITES.items():
            button = QPushButton(name)
            button.clicked.connect(lambda _checked=False, u=url: self.browser.add_tab(u))
            site_bar.addWidget(button)
        site_bar.addStretch(1)

        paste_button = QPushButton("1. Test paste (no send)")
        paste_button.setToolTip("On a ChatGPT tab: pastes a long harmless text into the message box. Nothing is sent.")
        paste_button.clicked.connect(self.test_paste)
        send_button = QPushButton("2. Send test message and read reply")
        send_button.setToolTip("On a ChatGPT tab: sends one short test message and reads the answer.")
        send_button.clicked.connect(self.test_send)
        save_button = QPushButton("Save report")
        save_button.clicked.connect(self.save_report)

        self.checks: dict[str, QCheckBox] = {}
        side = QVBoxLayout()
        side.addWidget(
            QLabel(
                "<b>Steps</b><br>Open each site and sign in yourself.<br>On jobright, click a job."
                "<br>Open ChatGPT, then press 1 and 2.<br>Tick what is true, then Save report."
            )
        )
        side.addWidget(paste_button)
        side.addWidget(send_button)
        for key, label in CHECKS:
            box = QCheckBox(label)
            box.setStyleSheet("QCheckBox { padding: 2px 0; }")
            self.checks[key] = box
            side.addWidget(box)
        side.addWidget(save_button)
        side.addWidget(QLabel("<b>Log</b>"))
        side.addWidget(self.log_box, 1)
        side_widget = QWidget()
        side_widget.setLayout(side)
        side_widget.setMinimumWidth(400)

        left = QVBoxLayout()
        left.setContentsMargins(0, 0, 0, 0)
        left.addLayout(site_bar)
        left.addWidget(self.browser, 1)
        left_widget = QWidget()
        left_widget.setLayout(left)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(left_widget)
        splitter.addWidget(side_widget)
        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 1)
        self.setCentralWidget(splitter)

        self.log(f"Chromium {qWebEngineChromiumVersion()}")
        self.log(f"User agent has no QtWebEngine token: {'QtWebEngine' not in profile.httpUserAgent()}")
        self.log(f"Profile folder: {storage_dir}")
        self.browser.add_tab(SITES["JobRight.ai"])

    def log(self, message: str) -> None:
        self.log_box.appendPlainText(f"{time.strftime('%H:%M:%S')}  {message}")

    def chatgpt_page(self) -> QWebEnginePage | None:
        view = self.browser.current()
        if view is None or "chatgpt.com" not in view.url().host():
            self.log("Open a ChatGPT tab and select it first.")
            return None
        return view.page()

    # -- test 1: paste only ------------------------------------------------

    def test_paste(self) -> None:
        page = self.chatgpt_page()
        if page is None:
            return
        text = paste_test_text()
        self.log(f"Pasting {len(text):,} characters in chunks (nothing is sent)…")

        def done(job: dict[str, Any]) -> None:
            value = job.get("value") or {}
            self.results["paste"] = {
                "status": value.get("status"),
                "diagnostic": value.get("diagnostic"),
                "error": job.get("error"),
            }
            self.log(f"Paste result: {value.get('status')}  {value.get('diagnostic') or job.get('error') or ''}")
            self.log("Expected: 'chunked:Nx:A/B' with A equal to B, and no attachment chip in the message box.")

        ensure_bridge(
            page,
            lambda: call_async(
                page, f"window.__RAI_BRIDGE__.submitPrompt({json.dumps(text)}, {{ send: false }})", done, 90_000
            ),
        )

    # -- test 2: send and read ---------------------------------------------

    def test_send(self) -> None:
        page = self.chatgpt_page()
        if page is None:
            return

        def with_baseline(baseline: Any) -> None:
            count = int(baseline or 0)
            self.log(f"Sending a short test message (replies already on the page: {count})…")

            def sent(job: dict[str, Any]) -> None:
                value = job.get("value") or {}
                self.log(f"Send result: {value.get('status')}  {value.get('diagnostic') or job.get('error') or ''}")
                if value.get("status") != "ok":
                    self.results["send"] = {"status": value.get("status"), "error": job.get("error")}
                    return
                last = {"diag": ""}

                def progress(diag: str) -> None:
                    if diag != last["diag"]:
                        last["diag"] = diag
                        self.log(f"Waiting for ChatGPT ({diag})")

                def finished(text: str | None, how: str) -> None:
                    parsed_ok = False
                    if text:
                        try:
                            parsed_ok = json.loads(text).get("ok") is True
                        except (ValueError, AttributeError):
                            parsed_ok = False
                    url_path = page.url().path()
                    self.results["send"] = {
                        "status": "ok" if text else "no reply",
                        "finished_by": how,
                        "reply_chars": len(text or ""),
                        "reply_is_expected_json": parsed_ok,
                        "conversation_url_seen": url_path.startswith("/c/"),
                    }
                    self.log(
                        f"Reply read ({how}): {len(text or '')} characters, expected JSON: {parsed_ok}, "
                        f"conversation link: {url_path.startswith('/c/')}"
                    )

                self._waiter = ReplyWaiter(page, count, progress, finished)

            call_async(page, f"window.__RAI_BRIDGE__.submitPrompt({json.dumps(SEND_TEST_PROMPT)})", sent, 90_000)

        ensure_bridge(page, lambda: run_js(page, "window.__RAI_BRIDGE__.countReplies()", with_baseline))

    # -- report ---------------------------------------------------------------

    def save_report(self) -> None:
        report = {
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "chromium": qWebEngineChromiumVersion(),
            "user_agent_has_qt_token": "QtWebEngine" in self.browser.profile.httpUserAgent(),
            "hosts_loaded": self.browser.hosts_loaded,
            "browser": self.browser.stats,
            "tests": self.results,
            "checklist": {key: box.isChecked() for key, box in self.checks.items()},
        }
        path = HERE / "spike_report.json"
        path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        self.log(f"Report saved: {path}")


# ---------------------------------------------------------------------------
# Self-test (headless, against the local fixture)
# ---------------------------------------------------------------------------


def selftest(app: QApplication) -> int:
    profile = make_profile(app, None)
    logs: list[str] = []
    browser = Browser(profile, logs.append)
    browser.resize(1100, 700)
    browser.show()
    view = browser.add_tab((HERE / "fixtures" / "composer.html").as_uri())
    page = view.page()
    results: dict[str, bool] = {}
    text = paste_test_text()

    def finish() -> None:
        results["user agent has no QtWebEngine token"] = "QtWebEngine" not in profile.httpUserAgent()
        width = max(len(name) for name in results)
        for name, passed in results.items():
            print(f"{name.ljust(width)}  {'PASS' if passed else 'FAIL'}")
        app.exit(0 if all(results.values()) else 1)

    def step_permission() -> None:
        def clicked(_value: Any) -> None:
            def read(value: Any) -> None:
                results["permission request denied without a prompt"] = (
                    value == "denied" and browser.stats["permissions_denied"] == 1
                )
                finish()

            QTimer.singleShot(
                800, lambda: run_js(page, "document.getElementById('permission-result').textContent", read)
            )

        run_js(page, "document.getElementById('ask-permission').click(); true", clicked)

    def step_new_tab() -> None:
        before = browser.tabs.count()

        def check() -> None:
            results["target=_blank opens a tab in this browser"] = (
                browser.tabs.count() == before + 1 and browser.stats["new_tabs_from_pages"] == 1
            )
            browser.tabs.setCurrentIndex(0)
            step_permission()

        run_js(page, "document.getElementById('newtab').click(); true", lambda _value: QTimer.singleShot(1500, check))

    def step_send() -> None:
        def sent(job: dict[str, Any]) -> None:
            value = job.get("value") or {}
            results["long prompt sent with Enter"] = value.get("status") == "ok" and "sent with Enter" in (
                value.get("diagnostic") or ""
            )

            def finished(reply: str | None, _how: str) -> None:
                try:
                    data = json.loads(reply or "")
                except ValueError:
                    data = {}
                results["reply read as valid JSON (auto-linked e-mail intact)"] = data.get("email") == "a@b.co"
                results["whole prompt arrived (nothing lost)"] = data.get("received") == len(text)
                step_new_tab()

            browser._waiter = ReplyWaiter(page, 0, lambda _diag: None, finished, timeout_s=30)

        call_async(page, f"window.__RAI_BRIDGE__.submitPrompt({json.dumps(text)})", sent, 60_000)

    def step_paste() -> None:
        def pasted(job: dict[str, Any]) -> None:
            value = job.get("value") or {}
            diagnostic = value.get("diagnostic") or ""
            results["paste without sending"] = value.get("status") == "ok" and value.get("sent") is False

            def measured(raw: Any) -> None:
                state = json.loads(raw)
                results["chunked paste landed in full"] = state["length"] == len(text) and f"/{len(text)}" in diagnostic
                results["no attachment chip (every chunk under 10,000)"] = state["chips"] == 0
                results["nothing sent yet"] = state["sent"] == 0
                step_send()

            run_js(
                page,
                "JSON.stringify({ length: window.__docLength(),"
                " chips: document.getElementById('chips').children.length, sent: window.__sent.length })",
                measured,
            )

        ensure_bridge(
            page,
            lambda: call_async(
                page, f"window.__RAI_BRIDGE__.submitPrompt({json.dumps(text)}, {{ send: false }})", pasted, 60_000
            ),
        )

    def on_load(ok: bool) -> None:
        view.loadFinished.disconnect(on_load)
        if not ok:
            print("fixture failed to load")
            app.exit(2)
            return
        step_paste()

    view.loadFinished.connect(on_load)
    QTimer.singleShot(120_000, lambda: (print("self-test timed out"), app.exit(3)))
    return app.exec()


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("ResumeAI")
    app.setOrganizationName("ResumeAI")

    if "--selftest" in sys.argv:
        return selftest(app)

    base = Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation))
    storage_dir = base / "spike-profile"
    storage_dir.mkdir(parents=True, exist_ok=True)
    window = SpikeWindow(make_profile(app, storage_dir), storage_dir)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
