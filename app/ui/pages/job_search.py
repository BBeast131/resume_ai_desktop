"""Job Search: browse JobRight.ai or HiringCafe.com; every job you open is saved."""

from __future__ import annotations

import asyncio
import logging

from PySide6.QtCore import Qt
from PySide6.QtWebEngineCore import QWebEngineProfile
from PySide6.QtWidgets import QFrame, QVBoxLayout, QWidget

from app.automation.capture import CaptureResult, capture_job
from app.automation.job_rules import is_jobright_detail_url, url_key
from app.browser.browser import BrowserTab, BrowserWidget, host_matches, host_of
from app.ui.context import AppContext, spawn
from app.ui.widgets.common import PageHeader, SegmentedControl, ToastHost

log = logging.getLogger(__name__)

SITES = {"jobright": ("JobRight.ai", "https://jobright.ai/"), "hiring_cafe": ("HiringCafe.com", "https://hiring.cafe/")}


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
        self.selector = SegmentedControl([(key, name) for key, (name, _url) in SITES.items()], "jobright")
        self.selector.changed.connect(self.open_site)
        layout.addWidget(
            PageHeader(
                "Job Search", "Search jobs on JobRight.ai or HiringCafe.com and click jobs to save them.", self.selector
            )
        )

        card = QFrame()
        card.setObjectName("Card")
        card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(1, 1, 1, 1)
        self.browser: BrowserWidget | None = None
        if profile is not None:
            self.browser = BrowserWidget(profile, allowed_hosts=ctx.config.job_search_allowed_hosts)
            self.browser.tab_opened.connect(self._tab_opened)
            card_layout.addWidget(self.browser)
        layout.addWidget(card, 1)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        # The first site loads when the page is first shown, not at start-up.
        if not self._opened_once and self.browser is not None:
            self._opened_once = True
            self.open_site(self.selector.current())

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
        if opener is not None:
            # A job I clicked opened this tab: that is the capture trigger.
            spawn(self._capture(view, opener.current_url()))
        # jobright can also show a job by changing the address inside one tab.
        view.urlChanged.connect(lambda url, v=view: self._url_changed(v, url.toString()))

    def _url_changed(self, view: BrowserTab, url: str) -> None:
        if is_jobright_detail_url(url) and url_key(url) not in self._seen:
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
