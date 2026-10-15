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
  * the address bar is read-only unless the page makes it editable (Job Search's
    manual mode); the home buttons only go to allowed sites;
  * Ctrl+F finds text in the page, as in Chrome (Enter / Shift+Enter for the
    next / previous match, Esc to close).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from typing import Any
from urllib.parse import quote_plus, urlsplit

from PySide6.QtCore import QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QPushButton, QTabWidget, QVBoxLayout, QWidget

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


SEARCH_URL = "https://www.google.com/search?q="


def typed_url(text: str) -> str | None:
    """What the address bar should open for what was typed: a web address, or a web search.

    Only http(s) pages are ever opened; `javascript:`, `file:` and other schemes are refused.
    """
    text = text.strip()
    if not text:
        return None
    lower = text.lower()
    if lower.startswith(("http://", "https://")):
        return text if host_of(text) else None
    head = lower.split("/", 1)[0]
    if ":" in head and not head.split(":", 1)[1].isdigit():
        return None  # another scheme (javascript:, file:, chrome:...)
    if " " not in text and ("." in head or head.startswith("localhost")):
        candidate = "https://" + text
        return candidate if host_of(candidate) else None
    return SEARCH_URL + quote_plus(text)


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
    #: The current tab changed, or its address or loading state did.
    current_changed = Signal()

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
        self.address.setProperty("editable", False)
        self._address_icon = self.address.addAction(
            icons.icon("lock", t.placeholder, 13), QLineEdit.ActionPosition.LeadingPosition
        )
        self.address.returnPressed.connect(self._address_entered)
        row.addWidget(self.address, 1)
        self.find_button = self._tool("search", "Find in page (Ctrl+F)", self.open_find)
        row.addWidget(self.find_button)
        layout.addWidget(bar)

        # ---- find in page (Ctrl+F) ----
        self.find_bar = QWidget()
        self.find_bar.setObjectName("FindBar")
        self.find_bar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        find_row = QHBoxLayout(self.find_bar)
        find_row.setContentsMargins(8, 4, 8, 4)
        find_row.setSpacing(4)
        find_row.addStretch(1)
        self.find_input = QLineEdit()
        self.find_input.setObjectName("FindInput")
        self.find_input.setPlaceholderText("Find in page")
        self.find_input.setClearButtonEnabled(True)
        self.find_input.setFixedWidth(260)
        self.find_input.textChanged.connect(lambda _text: self._find(backward=False, fresh=True))
        self.find_input.returnPressed.connect(lambda: self.find_next())
        self.find_input.installEventFilter(self)
        find_row.addWidget(self.find_input)
        self.find_count = QLabel("")
        self.find_count.setObjectName("FindCount")
        self.find_count.setMinimumWidth(56)
        self.find_count.setAlignment(Qt.AlignmentFlag.AlignCenter)
        find_row.addWidget(self.find_count)
        self.find_previous_button = self._tool("chevron-up", "Previous match (Shift+Enter)", self.find_previous)
        self.find_next_button = self._tool("chevron-down", "Next match (Enter)", self.find_next)
        self.find_close_button = self._tool("x", "Close (Esc)", self.close_find)
        for item in (self.find_previous_button, self.find_next_button, self.find_close_button):
            find_row.addWidget(item)
        self.find_bar.hide()
        layout.addWidget(self.find_bar)
        #: Matches in the current tab for the text in the find box: (active, total).
        self.find_result: tuple[int, int] = (0, 0)
        self._find_shortcut = QShortcut(QKeySequence(QKeySequence.StandardKey.Find), self)
        self._find_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._find_shortcut.activated.connect(self.open_find)

        self.tabs = QTabWidget()
        self.tabs.setObjectName("BrowserTabs")
        self.tabs.setDocumentMode(True)
        self.tabs.setTabsClosable(closable_tabs)
        self.tabs.setMovable(True)
        self.tabs.setElideMode(Qt.TextElideMode.ElideRight)
        self.tabs.tabCloseRequested.connect(self._close_index)
        self.tabs.currentChanged.connect(lambda _index: self._tab_changed())
        layout.addWidget(self.tabs, 1)
        self._sync_bar()

    # -- address bar ---------------------------------------------------------------

    @property
    def address_editable(self) -> bool:
        return not self.address.isReadOnly()

    def set_address_editable(self, editable: bool) -> None:
        """Let the user type an address (any http(s) site) or keep the bar read-only."""
        t = tokens()
        self.address.setReadOnly(not editable)
        self.address.setProperty("editable", editable)
        self.address.setPlaceholderText("Type a web address or search" if editable else "No page open")
        self._address_icon.setIcon(icons.icon("globe" if editable else "lock", t.placeholder, 13))
        self.address.style().unpolish(self.address)
        self.address.style().polish(self.address)
        self._sync_bar(force=True)

    def navigate(self, text: str) -> BrowserTab | None:
        """Open what was typed in the current tab (a new one when none is open)."""
        url = typed_url(text)
        if url is None:
            return None
        view = self.current()
        if view is None:
            return self.add_tab(url)
        view.load(QUrl(url))
        view.setFocus()
        return view

    def _address_entered(self) -> None:
        if not self.address_editable:
            return
        if self.navigate(self.address.text()) is None:
            self._sync_bar(force=True)  # nothing to open: show the current address again

    def keyPressEvent(self, event: Any) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape and self.address.hasFocus():
            self._sync_bar(force=True)
            return
        super().keyPressEvent(event)

    # -- find in page ---------------------------------------------------------------

    def open_find(self) -> None:
        """Show the find box (Ctrl+F), with its text selected, and search the current tab."""
        self.find_bar.show()
        self.find_input.setFocus()
        self.find_input.selectAll()
        if self.find_input.text():
            self._find(backward=False, fresh=True)

    def close_find(self) -> None:
        self.find_bar.hide()
        view = self.current()
        if view is not None:
            view.page().findText("")  # clears the highlights
            view.setFocus()
        self.find_result = (0, 0)
        self.find_count.setText("")  # the text stays in the box for next time, as in Chrome
        self.find_input.setProperty("invalid", False)

    def _found(self, view: BrowserTab, result: Any) -> None:
        if view is self.current() and self.find_bar.isVisible():
            self._show_find_result(result.activeMatch(), result.numberOfMatches())

    def find_next(self) -> None:
        self._find(backward=False)

    def find_previous(self) -> None:
        self._find(backward=True)

    def find_text(self, text: str) -> None:
        """Search the current tab for `text` (what typing in the find box does)."""
        if not self.find_bar.isVisible():
            self.find_bar.show()
        self.find_input.setText(text)  # textChanged runs the search

    def _find(self, *, backward: bool, fresh: bool = False) -> None:
        view = self.current()
        text = self.find_input.text()
        if view is None:
            self._show_find_result(0, 0)
            return
        page = view.page()
        if not text:
            page.findText("")
            self._show_find_result(0, 0)
            return
        flags = QWebEnginePage.FindFlag(0)
        if backward:
            flags |= QWebEnginePage.FindFlag.FindBackward
        if fresh:
            page.findText("")  # a new word starts from the top
        page.findText(text, flags)

    def _show_find_result(self, active: int, total: int) -> None:
        self.find_result = (active, total)
        text = self.find_input.text()
        if not text:
            self.find_count.setText("")
        else:
            self.find_count.setText(f"{active}/{total}" if total else "0/0")
        self.find_input.setProperty("invalid", bool(text) and total == 0)
        self.find_input.style().unpolish(self.find_input)
        self.find_input.style().polish(self.find_input)

    def eventFilter(self, watched: Any, event: Any) -> bool:  # noqa: N802
        if watched is self.find_input and event.type() == event.Type.KeyPress:
            if event.key() == Qt.Key.Key_Escape:
                self.close_find()
                return True
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and (
                event.modifiers() & Qt.KeyboardModifier.ShiftModifier
            ):
                self.find_previous()
                return True
        return super().eventFilter(watched, event)

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
        view.page().findTextFinished.connect(lambda result, v=view: self._found(v, result))
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

    def _tab_changed(self) -> None:
        self._sync_bar()
        if self.find_bar.isVisible():
            self._find(backward=False, fresh=True)  # the open search follows you to the other tab

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

    def _sync_bar(self, force: bool = False) -> None:
        view = self.current()
        typing = self.address_editable and self.address.hasFocus() and self.address.isModified()
        if force or not typing:
            self.address.setText(view.current_url() if view is not None else "")
            self.address.setCursorPosition(0)
        history = view.history() if view is not None else None
        self.back_button.setEnabled(bool(history and history.canGoBack()))
        self.forward_button.setEnabled(bool(history and history.canGoForward()))
        self.reload_button.setEnabled(view is not None)
        self.current_changed.emit()

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
