"""Job Search: browse jobs and save them to Saved Jobs.

Two modes, switched at the top of the page and remembered:

* **Auto** — JobRight.ai and HiringCafe.com; every job you open is saved.
* **Manual** — the address bar is editable and any job site can be opened;
  nothing is saved until you press "Save job" above the browser. The page is
  read like an auto-mode save (URL, company, role, description); when
  something is missing or doubtful, a short review dialog asks you first.
"""

from __future__ import annotations

import asyncio
import logging

from PySide6.QtCore import QSize, Qt
from PySide6.QtWebEngineCore import QWebEngineProfile
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QVBoxLayout, QWidget

from app.automation.capture import CaptureResult, capture_job
from app.automation.job_rules import MIN_JD_CHARS, is_jobright_detail_url, url_key
from app.automation.manual_save import ManualDraft, ManualSaveResult, read_manual, save_manual
from app.browser.browser import BrowserTab, BrowserWidget, host_matches, host_of, plain_tip
from app.ui import icons
from app.ui.context import AppContext, spawn
from app.ui.theme import tokens
from app.ui.widgets.common import Modal, PageHeader, SegmentedControl, ToastHost, button, label

log = logging.getLogger(__name__)

SITES = {"jobright": ("JobRight.ai", "https://jobright.ai/"), "hiring_cafe": ("HiringCafe.com", "https://hiring.cafe/")}
MODES = {"auto": "Auto", "manual": "Manual"}
SUBTITLES = {
    "auto": "Search jobs on JobRight.ai or HiringCafe.com and click jobs to save them.",
    "manual": "Open any job site, review a job, then press Save job to add it to Saved Jobs.",
}


class ManualSaveDialog(Modal):
    """Review what was read from the page; complete or correct it before saving."""

    def __init__(self, parent: QWidget, draft: ManualDraft) -> None:
        super().__init__(parent, 640)
        self.draft = draft
        self.add_title("Review before saving", draft.doubt or "")
        self.body.addWidget(label("Job URL", "FieldLabel"))
        url = QLineEdit(draft.url)
        url.setReadOnly(True)
        url.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        url.setCursorPosition(0)
        self.body.addWidget(url)
        self.body.addWidget(label("Company", "FieldLabel"))
        self.company = QLineEdit(draft.company)
        self.company.setPlaceholderText("Company name")
        self.body.addWidget(self.company)
        self.body.addWidget(label("Role", "FieldLabel"))
        self.role = QLineEdit(draft.role)
        self.role.setPlaceholderText("Job title")
        self.body.addWidget(self.role)
        self.body.addWidget(label("Job description", "FieldLabel"))
        self.jd = QPlainTextEdit(draft.jd_text)
        self.jd.setPlaceholderText("Paste the job description from the page")
        self.jd.setMinimumHeight(220)
        self.body.addWidget(self.jd)
        self.hint = label("", "Small")
        self.body.addWidget(self.hint)
        self.cancel = button("Cancel")
        self.save = button("Save job", "primary")
        self.cancel.clicked.connect(self.reject)
        self.save.clicked.connect(self.accept)
        self.add_buttons(self.cancel, self.save)
        for field in (self.company, self.role):
            field.textChanged.connect(lambda _text: self._validate())
        self.jd.textChanged.connect(self._validate)
        self._validate()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        # Straight to the first thing still missing.
        role, company, jd = self.values()
        target = self.company if not company else self.role if not role else self.jd
        target.setFocus()
        if target is self.jd and jd:
            self.jd.moveCursor(self.jd.textCursor().MoveOperation.Start)

    def values(self) -> tuple[str, str, str]:
        """(role, company, description) as edited."""
        return self.role.text().strip(), self.company.text().strip(), self.jd.toPlainText().strip()

    def _validate(self) -> None:
        role, company, jd = self.values()
        missing = [name for name, value in (("company", company), ("role", role)) if not value]
        if len(jd) < MIN_JD_CHARS:
            missing.append(f"a job description (at least {MIN_JD_CHARS} characters)")
        self.hint.setText(("Still needed: " + ", ".join(missing)) if missing else f"{len(jd):,} characters")
        self.save.setEnabled(not missing)


class JobSearchPage(QWidget):
    def __init__(self, ctx: AppContext, toasts: ToastHost, profile: QWebEngineProfile | None) -> None:
        super().__init__()
        self.ctx = ctx
        self.toasts = toasts
        self._opened_once = False
        #: URL keys already handled in this session, so a page is never captured twice.
        self._seen: set[str] = set()
        self.results: list[CaptureResult] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)
        self.mode = ctx.settings.job_search_mode
        self._saving = False
        self.mode_switch = SegmentedControl(list(MODES.items()), self.mode)
        self.mode_switch.setToolTip("Auto saves every job you open. Manual lets you open any site and save by hand.")
        self.mode_switch.changed.connect(self.set_mode)
        self.selector = SegmentedControl([(key, name) for key, (name, _url) in SITES.items()], "jobright")
        self.selector.changed.connect(self.open_site)
        self.header = PageHeader("Job Search", SUBTITLES[self.mode], self.selector)
        self.header.right.insertWidget(0, self.mode_switch)
        layout.addWidget(self.header)

        # Manual mode only: the save bar above the browser.
        self.manual_bar = QFrame()
        self.manual_bar.setObjectName("ManualSaveBar")
        self.manual_bar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        bar = QHBoxLayout(self.manual_bar)
        bar.setContentsMargins(14, 8, 8, 8)
        bar.setSpacing(10)
        bar_icon = QLabel()
        bar_icon.setPixmap(icons.pixmap("bookmark", tokens().primary, 16))
        bar.addWidget(bar_icon)
        self.page_host = label("", "ManualSaveHost")
        self.page_note = label("", "Muted")
        bar.addWidget(self.page_host)
        bar.addWidget(self.page_note, 1)
        self.save_button = button("Save job", "primary", "plus", icon_color="#FFFFFF")
        self.save_button.setIconSize(QSize(16, 16))
        self.save_button.clicked.connect(lambda _checked=False: self.save_current())
        bar.addWidget(self.save_button)
        layout.addWidget(self.manual_bar)

        card = QFrame()
        card.setObjectName("Card")
        card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(1, 1, 1, 1)
        self.browser: BrowserWidget | None = None
        if profile is not None:
            self.browser = BrowserWidget(profile, allowed_hosts=ctx.config.job_search_allowed_hosts)
            self.browser.tab_opened.connect(self._tab_opened)
            self.browser.current_changed.connect(self._refresh_save_bar)
            card_layout.addWidget(self.browser)
        layout.addWidget(card, 1)
        self._apply_mode()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        # The first site loads when the page is first shown, not at start-up.
        # Manual mode starts empty: the address bar is where you begin.
        if not self._opened_once and self.browser is not None and self.mode == "auto":
            self._opened_once = True
            self.open_site(self.selector.current())

    # -- mode ----------------------------------------------------------------------------

    def set_mode(self, mode: str) -> None:
        if mode not in MODES or mode == self.mode:
            self.mode_switch.set_current(self.mode)
            return
        self.mode = mode
        self.ctx.settings.job_search_mode = mode
        self._apply_mode()
        if mode == "auto" and self.browser is not None and not self.browser.views() and self.isVisible():
            self.open_site(self.selector.current())  # back to Auto with nothing open: its site
        self.toasts.show(
            "Manual mode: open any job site and press Save job."
            if mode == "manual"
            else "Auto mode: jobs you open on JobRight or HiringCafe are saved.",
            "info",
        )

    def _apply_mode(self) -> None:
        manual = self.mode == "manual"
        self.mode_switch.set_current(self.mode)
        self.header.subtitle.setText(SUBTITLES[self.mode])
        self.manual_bar.setVisible(manual)
        # JobRight / HiringCafe belong to Auto mode; Manual mode uses the address bar.
        self.selector.setVisible(not manual)
        if self.browser is not None:
            self.browser.set_address_editable(manual)
        self._refresh_save_bar()

    # -- manual save ---------------------------------------------------------------------

    def _refresh_save_bar(self) -> None:
        if self.mode != "manual":
            return
        view = self.browser.current() if self.browser is not None else None
        url = view.current_url() if view is not None else ""
        web = url.startswith(("http://", "https://"))
        host = host_of(url) if web else ""
        self.page_host.setText(host.removeprefix("www.") or "No page open")
        self.page_host.setToolTip(plain_tip(url))
        state = self.ctx.store.saved_state(url_key(url)) if web else None
        if self._saving:
            note, text, enabled = "Reading the job on this page…", "Saving…", False
        elif not web:
            note, text, enabled = "Type a job site's address above, or use the site buttons.", "Save job", False
        elif state == "saved":
            note, text, enabled = "This job is already in Saved Jobs.", "Saved", False
        elif state == "generated":
            note, text, enabled = "A resume was already generated for this job.", "Generated", False
        else:
            note, text, enabled = "Review the job, then save it to Saved Jobs.", "Save job", True
        self.page_note.setText(note)
        self.save_button.setText(text)
        self.save_button.setIcon(icons.icon("check" if state and not self._saving else "plus", "#FFFFFF", 16))
        self.save_button.setEnabled(enabled)

    def save_current(self) -> None:
        """Save the job on the current tab (manual mode)."""
        if self.mode != "manual" or self._saving or self.browser is None:
            return
        view = self.browser.current()
        if view is None or not view.current_url().startswith(("http://", "https://")):
            return
        spawn(self._save(view))

    async def _save(self, view: BrowserTab) -> ManualSaveResult | None:
        self._saving = True
        self._refresh_save_bar()
        try:
            draft = await read_manual(view.page(), self.ctx.api)
        except RuntimeError:
            return None  # the tab was closed while it was being read
        finally:
            self._saving = False
            self._refresh_save_bar()
        if draft.refused is not None:
            self.toasts.show(draft.refused, "warning")
            return None
        if draft.ready:
            return self._store(draft, draft.role, draft.company, draft.jd_text)
        self.review(draft)
        return None

    def review(self, draft: ManualDraft) -> ManualSaveDialog:
        dialog = ManualSaveDialog(self, draft)
        dialog.accepted.connect(lambda: self._store(draft, *dialog.values()))
        dialog.open()
        return dialog

    def _store(self, draft: ManualDraft, role: str, company: str, jd_text: str) -> ManualSaveResult:
        result = save_manual(
            self.ctx.store, url=draft.url, role=role, company=company, jd_text=jd_text, confidence=draft.confidence
        )
        self.toasts.show(result.toast, result.kind)
        if result.saved:
            self.ctx.changed("jobs")
        self._refresh_save_bar()
        return result

    # -- sites ---------------------------------------------------------------------------

    def open_site(self, key: str) -> None:
        if self.browser is None or key not in SITES:
            return
        self._opened_once = True
        self.selector.set_current(key)
        if self.browser.open_home(SITES[key][1]) is None:
            self.toasts.show("That site is not in the allowed list (JOB_SEARCH_ALLOWED_HOSTS).", "warning")

    def open_url(self, url: str) -> None:
        """Show a page here without saving it (a saved job's link, a ChatGPT conversation)."""
        if self.browser is not None and url.startswith(("https://", "http://")):
            self._opened_once = True
            self.browser.add_tab(url)

    # -- capture -----------------------------------------------------------------------

    def _tab_opened(self, view: BrowserTab, opener: BrowserTab | None) -> None:
        if opener is not None and self.mode == "auto":
            # A job I clicked opened this tab: that is the capture trigger.
            spawn(self._capture(view, opener.current_url()))
        # jobright can also show a job by changing the address inside one tab.
        view.urlChanged.connect(lambda url, v=view: self._url_changed(v, url.toString()))

    def _url_changed(self, view: BrowserTab, url: str) -> None:
        if self.mode == "auto" and is_jobright_detail_url(url) and url_key(url) not in self._seen:
            spawn(self._capture(view, url))

    async def _capture(self, view: BrowserTab, opener_url: str) -> CaptureResult | None:
        try:
            loaded = await view.wait_loaded(self.ctx.config.page_load_timeout_seconds)
            # A job link often hops through a redirect page: wait until the address settles.
            for _hop in range(4):
                address = view.current_url()
                await asyncio.sleep(0.8)
                if view.current_url() == address and not view.loading:
                    break
                loaded = await view.wait_loaded(self.ctx.config.page_load_timeout_seconds)
            url = view.current_url()
        except RuntimeError:
            return None  # the tab was closed while loading
        if host_matches(host_of(url), self.ctx.config.login_hosts):
            return None  # a sign-in window the site opened: not a job, nothing to read or report
        if not loaded:
            result = CaptureResult("needs_user_action", "Not saved: page took too long to load", "warning")
            self._report(result)
            return result
        key = url_key(url)
        if key in self._seen:
            return None
        self._seen.add(key)
        try:
            result = await capture_job(view.page(), opener_url, self.ctx.store, self.ctx.api)
        except RuntimeError:
            self._seen.discard(key)
            return None
        if not result.saved:
            # Not saved: let a later visit to the same page try again (after signing in, say).
            self._seen.discard(key)
        self._report(result)
        return result

    def _report(self, result: CaptureResult) -> None:
        self.results.append(result)
        del self.results[:-100]
        self.toasts.show(result.toast, result.kind)
        if result.saved:
            self.ctx.changed("jobs")
