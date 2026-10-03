"""The tabbed browser shown inside the board.

Job Search and Resume Generating each have their own instance (separate tab
sets) on the same profile, so browsing jobs never disturbs a running
generation.

What it guarantees:
  * a link that opens a new window opens a new TAB here (`createWindow`);
  * a site asking for notifications, location, camera, microphone or the
    clipboard is refused at once and silently, so no popup can block a run;
  * JavaScript dialogs (alert / confirm / prompt / "leave this page?") are
    dismissed automatically, so they cannot freeze a page;
  * the address bar is read-only; the home buttons only go to allowed sites.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from typing import Any
from urllib.parse import urlsplit

from PySide6.QtCore import QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QHBoxLayout, QLineEdit, QPushButton, QTabWidget, QVBoxLayout, QWidget

from app.ui import icons
from app.ui.theme import tokens


def plain_tip(text: str) -> str:
    """Page-derived text for a tooltip. Qt renders a tooltip that looks like HTML as HTML; this never does."""
    return text.replace("<", "‹").replace(">", "›")[:300]


def host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def host_matches(host: str, allowed: Iterable[str]) -> bool:
    """Exact host or a subdomain of it. `jobright.ai.evil.example` does not match `jobright.ai`."""
    host = host.lower().rstrip(".")
    return any(host == item or host.endswith("." + item) for item in allowed)


class BrowserPage(QWebEnginePage):
    """A page that never shows a prompt and opens new windows as tabs."""

    def __init__(self, profile: QWebEngineProfile, browser: BrowserWidget) -> None:
        super().__init__(profile, browser)
        self._browser = browser
        self.denied_permissions = 0
        self.dismissed_dialogs = 0
        if hasattr(self, "permissionRequested"):
            self.permissionRequested.connect(self._deny_permission)
        else:  # Qt < 6.8
            self.featurePermissionRequested.connect(self._deny_feature)

    def createWindow(self, _window_type: QWebEnginePage.WebWindowType) -> QWebEnginePage:
        view = self._browser.add_tab(None, foreground=True, opener=self._browser.view_of(self))
        return view.page()

    def _deny_permission(self, permission: Any) -> None:
        self.denied_permissions += 1
        permission.deny()

    def _deny_feature(self, origin: QUrl, feature: Any) -> None:
        self.denied_permissions += 1
        self.setFeaturePermission(origin, feature, QWebEnginePage.PermissionPolicy.PermissionDeniedByUser)

    def javaScriptAlert(self, _origin: QUrl, _message: str) -> None:
        self.dismissed_dialogs += 1

    def javaScriptConfirm(self, _origin: QUrl, message: str) -> bool:
        self.dismissed_dialogs += 1
        # "Leave this page?" (beforeunload) is answered yes, so a tab can always be closed or
        # navigated. Every other confirm() is dismissed as Cancel: the app never agrees to
        # anything on a page's behalf.
        return message.strip().lower().startswith("are you sure you want to leave")

    def javaScriptPrompt(self, _origin: QUrl, _message: str, _default: str) -> tuple[bool, str]:
        self.dismissed_dialogs += 1
        return (False, "")

    def javaScriptConsoleMessage(self, *_args: Any) -> None:
        return  # page consoles can echo page text; never forward them to our log


class BrowserTab(QWebEngineView):
    def __init__(self, profile: QWebEngineProfile, browser: BrowserWidget, opener: BrowserTab | None) -> None:
        super().__init__(browser)
        self.opener = opener
        #: Keep the page rendering while its view is hidden (another sidebar tab is in front).
        self.keep_active = browser.keep_pages_active
        self.setPage(BrowserPage(profile, browser))
        self.loading = False
        self.load_ok: bool | None = None
        self.loads = 0
        self.loadStarted.connect(self._started)
        self.loadFinished.connect(self._finished)

    def hideEvent(self, event: Any) -> None:
        super().hideEvent(event)
        if self.keep_active:
            # Qt marks the page hidden with its view, and a hidden page is throttled to one timer
            # tick a second and stops painting; ChatGPT then never finishes writing its reply.
            QTimer.singleShot(0, self, self._stay_active)

    def _stay_active(self) -> None:
        page = self.page()
        if page is not None and not page.isVisible():
            page.setVisible(True)

    def load(self, url: QUrl) -> None:  # type: ignore[override]
        self._expect_load()
        super().load(url)

    def reload(self) -> None:
        self._expect_load()
        super().reload()

    def _expect_load(self) -> None:
        # `loadStarted` arrives later, through the event loop. Mark the tab as loading now, so a
        # `wait_loaded()` called straight after load()/reload() waits for THIS load.
        self.loading = True
        self.load_ok = None

    def _started(self) -> None:
        self.loading = True

    def _finished(self, ok: bool) -> None:
        self.loading = False
        self.load_ok = ok
        self.loads += 1

    def current_url(self) -> str:
        return self.url().toString()

    async def wait_loaded(self, timeout: float = 45.0, settle: float = 0.6) -> bool:
        """Wait until the page has finished loading. False on failure or timeout."""
        loop = asyncio.get_running_loop()
        if self.loading or self.loads == 0:
            done: asyncio.Future[bool] = loop.create_future()

            def finished(ok: bool) -> None:
                if not done.done():
                    done.set_result(ok)

            self.loadFinished.connect(finished)
            try:
                ok = await asyncio.wait_for(done, timeout)
            except TimeoutError:
                return False
            finally:
                try:
                    self.loadFinished.disconnect(finished)
                except (RuntimeError, TypeError):
                    pass
            if not ok:
                return False
        if settle > 0:
            await asyncio.sleep(settle)  # single-page sites render after the load event
        return self.load_ok is not False


class BrowserWidget(QWidget):
    #: (new tab, the tab whose page opened it or None)
    tab_opened = Signal(object, object)
    tab_loaded = Signal(object, bool)
    tab_closed = Signal(object)

    def __init__(
        self,
        profile: QWebEngineProfile,
        *,
        allowed_hosts: Iterable[str] | None = None,
        closable_tabs: bool = True,
        keep_pages_active: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.profile = profile
        self.keep_pages_active = keep_pages_active
        #: Where the home buttons may go. None means "anywhere" (the generator's browser).
        self.allowed_hosts = tuple(allowed_hosts) if allowed_hosts is not None else None
        t = tokens()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        bar = QWidget()
        bar.setObjectName("BrowserBar")
        bar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        row = QHBoxLayout(bar)
        row.setContentsMargins(8, 6, 8, 6)
        row.setSpacing(4)
        self.back_button = self._tool("arrow-left", "Back", self.back)
        self.forward_button = self._tool("arrow-right", "Forward", self.forward)
        self.reload_button = self._tool("reload", "Reload", self.reload)
        for item in (self.back_button, self.forward_button, self.reload_button):
            row.addWidget(item)
        self.address = QLineEdit()
        self.address.setObjectName("AddressBar")
        self.address.setReadOnly(True)
        self.address.setPlaceholderText("No page open")
        self.address.addAction(icons.icon("lock", t.placeholder, 13), QLineEdit.ActionPosition.LeadingPosition)
        row.addWidget(self.address, 1)
        layout.addWidget(bar)

        self.tabs = QTabWidget()
        self.tabs.setObjectName("BrowserTabs")
        self.tabs.setDocumentMode(True)
        self.tabs.setTabsClosable(closable_tabs)
        self.tabs.setMovable(True)
        self.tabs.setElideMode(Qt.TextElideMode.ElideRight)
        self.tabs.tabCloseRequested.connect(self._close_index)
        self.tabs.currentChanged.connect(lambda _index: self._sync_bar())
        layout.addWidget(self.tabs, 1)
        self._sync_bar()

    def _tool(self, name: str, tip: str, action: Callable[[], None]) -> QPushButton:
        item = QPushButton()
        item.setProperty("variant", "ghost")
        item.setIcon(icons.icon(name, tokens().text_secondary, 16))
        item.setIconSize(QSize(16, 16))
        item.setToolTip(tip)
        item.setCursor(Qt.CursorShape.PointingHandCursor)
        item.clicked.connect(lambda _checked=False: action())
        return item

    # -- tabs ----------------------------------------------------------------------

    def views(self) -> list[BrowserTab]:
        return [self.tabs.widget(index) for index in range(self.tabs.count())]  # type: ignore[misc]

    def current(self) -> BrowserTab | None:
        widget = self.tabs.currentWidget()
        return widget if isinstance(widget, BrowserTab) else None

    def view_of(self, page: QWebEnginePage) -> BrowserTab | None:
        for view in self.views():
            if view.page() is page:
                return view
        return None

    def allows(self, url: str) -> bool:
        if self.allowed_hosts is None:
            return True
        return host_matches(host_of(url), self.allowed_hosts)

    def add_tab(self, url: str | None, *, foreground: bool = True, opener: BrowserTab | None = None) -> BrowserTab:
        view = BrowserTab(self.profile, self, opener)
        index = self.tabs.addTab(view, "New tab")
        view.titleChanged.connect(lambda title, v=view: self._set_title(v, title))
        view.urlChanged.connect(lambda _url, v=view: self._sync_bar() if v is self.current() else None)
        view.loadFinished.connect(lambda ok, v=view: self._loaded(v, ok))
        if foreground:
            self.tabs.setCurrentIndex(index)
        if url:
            view.load(QUrl(url))
        self.tab_opened.emit(view, opener)
        return view

    def open_home(self, url: str) -> BrowserTab | None:
        """Go to a site's home page: reuse a tab already on that site, else open one."""
        if not self.allows(url):
            return None
        wanted = host_of(url)
        for view in self.views():
            if host_matches(host_of(view.current_url()), [wanted.removeprefix("www.")]):
                self.tabs.setCurrentWidget(view)
                return view
        return self.add_tab(url)

    def show_tab(self, view: BrowserTab) -> None:
        if self.tabs.indexOf(view) >= 0:
            self.tabs.setCurrentWidget(view)

    def close_tab(self, view: BrowserTab) -> None:
        index = self.tabs.indexOf(view)
        if index >= 0:
            self._close_index(index)

    def close_all(self) -> None:
        for view in self.views():
            self.close_tab(view)

    def _close_index(self, index: int) -> None:
        view = self.tabs.widget(index)
        self.tabs.removeTab(index)
        if isinstance(view, BrowserTab):
            self.tab_closed.emit(view)
            view.stop()
            page = view.page()
            view.setPage(None)  # type: ignore[arg-type]
            page.deleteLater()
            view.deleteLater()
        self._sync_bar()

    def _set_title(self, view: BrowserTab, title: str) -> None:
        index = self.tabs.indexOf(view)
        if index >= 0:
            self.tabs.setTabText(index, title[:40] or "New tab")
            self.tabs.setTabToolTip(index, plain_tip(title))

    def _loaded(self, view: BrowserTab, ok: bool) -> None:
        if view is self.current():
            self._sync_bar()
        self.tab_loaded.emit(view, ok)

    # -- toolbar -------------------------------------------------------------------

    def _sync_bar(self) -> None:
        view = self.current()
        self.address.setText(view.current_url() if view is not None else "")
        self.address.setCursorPosition(0)
        history = view.history() if view is not None else None
        self.back_button.setEnabled(bool(history and history.canGoBack()))
        self.forward_button.setEnabled(bool(history and history.canGoForward()))
        self.reload_button.setEnabled(view is not None)

    def back(self) -> None:
        view = self.current()
        if view is not None:
            view.back()

    def forward(self) -> None:
        view = self.current()
        if view is not None:
            view.forward()

    def reload(self) -> None:
        view = self.current()
        if view is not None:
            view.reload()
