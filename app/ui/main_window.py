"""The main window: a collapsible navy sidebar and the board."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QEasingCurve, QParallelAnimationGroup, QPropertyAnimation, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from app.data.store import ResumeFilter
from app.services.provider import LocalProvider, RemoteProvider
from app.sync.api_client import ApiError
from app.sync.engine import SyncStatus
from app.ui import icons
from app.ui.context import AppContext, spawn
from app.ui.dialogs import SettingsDialog, SyncPanel, sync_chip_icon, sync_chip_text
from app.ui.pages.admin import AdminPage
from app.ui.pages.dashboard import DashboardPage
from app.ui.pages.generated import GeneratedPage
from app.ui.pages.generator import GeneratorPage
from app.ui.pages.job_search import JobSearchPage
from app.ui.pages.saved_jobs import SavedJobsPage
from app.ui.theme import SIDEBAR_COLLAPSED, SIDEBAR_EXPANDED, tokens
from app.ui.widgets.common import ToastHost, button, confirm, format_datetime, label, repolish

NAV = [
    ("dashboard", "Dashboard", "dashboard"),
    ("job_search", "Job Search", "search"),
    ("saved_jobs", "Saved Jobs", "bookmark"),
    ("generator", "Resume Generating", "wand"),
    ("generated", "Generated Resumes", "files"),
    ("admin", "Admin", "shield"),
]
#: Pages that exist in the read-only "view as" mode.
VIEWABLE = ("dashboard", "saved_jobs", "generated")
SYNC_DEBOUNCE_MS = 1500
#: Opening Saved Jobs or the generator checks the shared list soon after.
SHARED_REFRESH_MS = 800


class NavItem(QPushButton):
    def __init__(self, key: str, text: str, icon_name: str) -> None:
        super().__init__()
        self.key = key
        self.icon_name = icon_name
        self.setObjectName("NavItem")
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(38)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 0, 10, 0)
        layout.setSpacing(10)
        self.picture = QLabel()
        self.picture.setFixedSize(18, 18)
        layout.addWidget(self.picture)
        self.caption = QLabel(text)
        self.caption.setStyleSheet("background: transparent;")
        layout.addWidget(self.caption, 1)
        self.running = QLabel()
        self.running.setObjectName("NavRunning")
        self.running.setFixedSize(8, 8)
        self.running.setToolTip("A generation run is in progress")
        self.running.hide()
        layout.addWidget(self.running)
        self.badge = QLabel()
        self.badge.setObjectName("NavBadge")
        self.badge.setFixedHeight(18)
        self.badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.badge.hide()
        layout.addWidget(self.badge)
        self.toggled.connect(lambda _checked: self._paint())
        self._paint()

    def _paint(self) -> None:
        t = tokens()
        active = self.isChecked()
        enabled = self.isEnabled()
        color = "#FFFFFF" if active else (t.sidebar_text if enabled else "#475569")
        self.picture.setPixmap(icons.pixmap(self.icon_name, color, 18))
        weight = "600" if active else "400"
        self.caption.setStyleSheet(f"background: transparent; color: {color}; font-weight: {weight};")
        self.badge.setProperty("active", active)
        repolish(self.badge)

    def setEnabled(self, enabled: bool) -> None:  # noqa: N802
        super().setEnabled(enabled)
        self._paint()

    def set_badge(self, count: int | None) -> None:
        self.badge.setVisible(bool(count))
        if count:
            self.badge.setText(str(count) if count < 1000 else "999+")


class MainWindow(QMainWindow):
    logout_requested = Signal()

    def __init__(self, ctx: AppContext, chatgpt_url: str | None = None) -> None:
        super().__init__()
        self.ctx = ctx
        self.setWindowTitle("Resume AI")
        self.resize(1366, 768)
        self.setMinimumSize(1000, 620)
        self.toasts = ToastHost(self)
        self.local = LocalProvider(ctx.store)
        self.view_provider: RemoteProvider | None = None
        self.view_pages: dict[str, QWidget] = {}
        self.current_key = "dashboard"
        self.sync_panel: SyncPanel | None = None
        t = tokens()

        root = QWidget()
        root.setObjectName("Workspace")
        self.setCentralWidget(root)
        outer = QHBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ------------------------------------------------------------------ sidebar
        self.sidebar = QFrame()
        self.sidebar.setObjectName("Sidebar")
        self.sidebar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        side = QVBoxLayout(self.sidebar)
        side.setContentsMargins(8, 10, 8, 10)
        side.setSpacing(4)

        top = QHBoxLayout()
        top.setSpacing(8)
        self.hamburger = QPushButton()
        self.hamburger.setObjectName("Hamburger")
        self.hamburger.setIcon(icons.icon("menu", "#FFFFFF", 20))
        self.hamburger.setIconSize(QSize(20, 20))
        self.hamburger.setFixedSize(32, 32)
        self.hamburger.setCursor(Qt.CursorShape.PointingHandCursor)
        self.hamburger.clicked.connect(lambda _checked=False: self.toggle_sidebar())
        top.addWidget(self.hamburger)
        self.brand = QWidget()
        brand_layout = QVBoxLayout(self.brand)
        brand_layout.setContentsMargins(0, 0, 0, 0)
        brand_layout.setSpacing(0)
        brand_layout.addWidget(label("RESUME AI", "Brand"))
        brand_layout.addWidget(label("Automated Resume Builder", "BrandSub"))
        top.addWidget(self.brand, 1)
        side.addLayout(top)

        # Everything below the hamburger: hidden entirely when the sidebar is collapsed.
        self.sidebar_content = QWidget()
        content = QVBoxLayout(self.sidebar_content)
        content.setContentsMargins(0, 10, 0, 0)
        content.setSpacing(4)

        self.nav: dict[str, NavItem] = {}
        for key, text, icon_name in NAV:
            item = NavItem(key, text, icon_name)
            item.clicked.connect(lambda _checked=False, k=key: self.show_page(k))
            self.nav[key] = item
            content.addWidget(item)
        content.addStretch(1)

        self.sync_chip = QPushButton()
        self.sync_chip.setObjectName("SyncChip")
        self.sync_chip.setCursor(Qt.CursorShape.PointingHandCursor)
        self.sync_chip.setToolTip("Sync details")
        self.sync_chip.clicked.connect(lambda _checked=False: self.open_sync_panel())
        content.addWidget(self.sync_chip)

        divider = QFrame()
        divider.setObjectName("SidebarDivider")
        content.addSpacing(6)
        content.addWidget(divider)
        content.addSpacing(6)

        user_row = QHBoxLayout()
        user_row.setSpacing(8)
        self.avatar = QLabel(ctx.user.initials)
        self.avatar.setObjectName("Avatar")
        self.avatar.setFixedSize(32, 32)
        self.avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        user_row.addWidget(self.avatar)
        names = QVBoxLayout()
        names.setSpacing(0)
        self.user_name = label(ctx.user.full_name or ctx.user.email, "UserName")
        self.user_email = label(ctx.user.email, "UserEmail")
        names.addWidget(self.user_name)
        names.addWidget(self.user_email)
        user_row.addLayout(names, 1)
        self.settings_button = QPushButton()
        self.settings_button.setObjectName("SidebarIconButton")
        self.settings_button.setIcon(icons.icon("settings", t.sidebar_text, 16))
        self.settings_button.setToolTip("Settings")
        self.settings_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.settings_button.clicked.connect(lambda _checked=False: self.open_settings())
        user_row.addWidget(self.settings_button)
        content.addLayout(user_row)

        self.logout_button = QPushButton("  Logout")
        self.logout_button.setObjectName("SidebarButton")
        self.logout_button.setIcon(icons.icon("logout", "#F87171", 16))
        self.logout_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.logout_button.clicked.connect(lambda _checked=False: self.request_logout())
        content.addWidget(self.logout_button)
        side.addWidget(self.sidebar_content, 1)
        # Keeps the ☰ button at the top when everything else is hidden.
        self.sidebar_filler = QWidget()
        self.sidebar_filler.setStyleSheet("background: transparent;")
        self.sidebar_filler.hide()
        side.addWidget(self.sidebar_filler, 1)
        outer.addWidget(self.sidebar)

        # ------------------------------------------------------------------ board
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)

        self.view_banner = QFrame()
        self.view_banner.setObjectName("ViewAsBanner")
        self.view_banner.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        banner = QHBoxLayout(self.view_banner)
        banner.setContentsMargins(24, 8, 16, 8)
        self.view_text = label("")
        banner.addWidget(self.view_text, 1)
        self.exit_view = button("Exit User View", "danger")
        self.exit_view.clicked.connect(lambda _checked=False: self.exit_view_as())
        banner.addWidget(self.exit_view)
        self.view_banner.hide()
        right_layout.addWidget(self.view_banner)

        self.offline_banner = QFrame()
        self.offline_banner.setObjectName("OfflineBanner")
        self.offline_banner.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        offline = QHBoxLayout(self.offline_banner)
        offline.setContentsMargins(24, 6, 16, 6)
        self.offline_text = label("Offline: working from local data. Changes will sync when the connection is back.")
        offline.addWidget(self.offline_text, 1)
        self.offline_action = button("Retry now", "ghost")
        self.offline_action.clicked.connect(lambda _checked=False: self._banner_action())
        offline.addWidget(self.offline_action)
        self.session_ended = False
        self.offline_banner.hide()
        right_layout.addWidget(self.offline_banner)

        self.stack = QStackedWidget()
        right_layout.addWidget(self.stack, 1)
        outer.addWidget(right, 1)

        # ------------------------------------------------------------------ pages
        self.dashboard = DashboardPage(ctx, self.local)
        self.job_search = JobSearchPage(ctx, self.toasts, ctx.web_profile)
        self.saved_jobs = SavedJobsPage(ctx, self.local, self.toasts)
        self.generator = GeneratorPage(ctx, self.toasts, ctx.web_profile, chatgpt_url)
        self.generated = GeneratedPage(ctx, self.local, self.toasts)
        self.admin = AdminPage(ctx, self.toasts)
        self.pages: dict[str, QWidget] = {
            "dashboard": self.dashboard,
            "job_search": self.job_search,
            "saved_jobs": self.saved_jobs,
            "generator": self.generator,
            "generated": self.generated,
            "admin": self.admin,
        }
        for page in self.pages.values():
            self.stack.addWidget(page)

        self.dashboard.drill.connect(lambda filters, text: self.open_generated(filters, text))
        self.dashboard.open_job_search.connect(lambda: self.show_page("job_search"))
        self.saved_jobs.open_in_job_search.connect(self.open_in_browser)
        self.generated.open_url.connect(self.open_in_browser)
        self.generator.open_generated.connect(lambda: self.show_page("generated"))
        self.generator.running_changed.connect(self._running_changed)
        self.admin.view_profile.connect(lambda uid, name, email: spawn(self.enter_view_as(uid, name, email)))

        # ------------------------------------------------------------------ behaviour
        self._width_animation = QParallelAnimationGroup(self)
        for prop in (b"minimumWidth", b"maximumWidth"):
            animation = QPropertyAnimation(self.sidebar, prop, self)
            animation.setDuration(150)
            animation.setEasingCurve(QEasingCurve.Type.InOutCubic)
            self._width_animation.addAnimation(animation)
        self.collapsed = False
        self.set_collapsed(ctx.settings.sidebar_collapsed, animate=False)
        QShortcut(QKeySequence("Ctrl+B"), self, activated=self.toggle_sidebar)

        self._sync_debounce = QTimer(self)
        self._sync_debounce.setSingleShot(True)
        self._sync_debounce.timeout.connect(lambda: spawn(self.sync_now()))
        self._sync_timer = QTimer(self)
        self._sync_timer.timeout.connect(lambda: spawn(self.sync_now()))
        self._retry_timer = QTimer(self)
        self._retry_timer.setSingleShot(True)
        self._retry_timer.timeout.connect(lambda: spawn(self.sync_now()))
        self._chip_timer = QTimer(self)
        self._chip_timer.setInterval(30_000)
        self._chip_timer.timeout.connect(lambda: self._paint_chip(self.ctx.sync.status))
        self._chip_timer.start()
        self._apply_sync_interval()

        ctx.sync.on_change = self._sync_changed
        ctx.sync.on_data_changed = self._remote_changed
        ctx.sync.on_jobs_shared = self._jobs_shared
        ctx.listeners.append(self._local_changed)

        self.set_admin(ctx.user.is_admin)
        self.refresh_badges()
        self._paint_chip(ctx.sync.status)
        self.show_page("dashboard")

    # ------------------------------------------------------------------
    # Sidebar
    # ------------------------------------------------------------------

    def toggle_sidebar(self) -> None:
        self.set_collapsed(not self.collapsed)

    def set_collapsed(self, collapsed: bool, animate: bool = True) -> None:
        """Collapsed: a thin strip with only the ☰ button. The board takes the freed width."""
        self.collapsed = collapsed
        self.ctx.settings.sidebar_collapsed = collapsed
        self.hamburger.setToolTip("Expand menu" if collapsed else "Collapse menu")
        target = SIDEBAR_COLLAPSED if collapsed else SIDEBAR_EXPANDED
        self._width_animation.stop()
        # No tab icons, labels or user block in the collapsed strip.
        self.sidebar_content.setVisible(not collapsed)
        self.brand.setVisible(not collapsed)
        self.sidebar_filler.setVisible(collapsed)
        if not animate:
            self.sidebar.setMinimumWidth(target)
            self.sidebar.setMaximumWidth(target)
            return
        start = self.sidebar.width()
        for index in range(self._width_animation.animationCount()):
            animation = self._width_animation.animationAt(index)
            animation.setStartValue(start)  # type: ignore[attr-defined]
            animation.setEndValue(target)  # type: ignore[attr-defined]
        self._width_animation.start()

    def set_admin(self, is_admin: bool) -> None:
        self.nav["admin"].setVisible(is_admin)
        if not is_admin and self.current_key == "admin":
            self.show_page("dashboard")

    def update_user(self) -> None:
        self.avatar.setText(self.ctx.user.initials)
        self.user_name.setText(self.ctx.user.full_name or self.ctx.user.email)
        self.user_email.setText(self.ctx.user.email)
        self.set_admin(self.ctx.user.is_admin)

    def refresh_badges(self) -> None:
        if self.view_provider is not None:
            self.nav["saved_jobs"].set_badge(len(self.view_provider.jobs))
            self.nav["generated"].set_badge(len(self.view_provider.rows))
            return
        self.nav["saved_jobs"].set_badge(self.ctx.store.saved_job_count())
        self.nav["generated"].set_badge(self.ctx.store.resume_count())

    def _running_changed(self, running: bool) -> None:
        self.nav["generator"].running.setVisible(running)
        self.saved_jobs.set_run_active(running)

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    @property
    def viewing(self) -> bool:
        return self.view_provider is not None

    def current_page(self) -> QWidget:
        return self.stack.currentWidget()

    def show_page(self, key: str) -> None:
        if key not in self.pages:
            return
        if self.viewing and key == "admin":
            self.exit_view_as(to="admin")
            return
        if self.viewing and key not in VIEWABLE:
            self.nav[self.current_key].setChecked(True)
            return
        if key == "admin" and not self.ctx.user.is_admin:
            return
        self.current_key = key
        for name, item in self.nav.items():
            item.setChecked(name == key)
        page = self.view_pages[key] if self.viewing else self.pages[key]
        self.stack.setCurrentWidget(page)
        refresh = getattr(page, "refresh", None)
        if callable(refresh) and key != "generator":
            refresh()
        if key in ("saved_jobs", "generator") and not self.viewing:
            self.request_sync(SHARED_REFRESH_MS)  # pick up what other profiles found meanwhile

    def open_generated(self, filters: ResumeFilter, description: str) -> None:
        page = self.view_pages["generated"] if self.viewing else self.generated
        page.apply_external(filters, description)  # type: ignore[attr-defined]
        self.show_page("generated")

    def open_in_browser(self, url: str) -> None:
        """Show a link in the Job Search browser (never in the generator's, which may be mid-run)."""
        if self.viewing:
            return
        self.job_search.open_url(url)
        self.show_page("job_search")

    # ------------------------------------------------------------------
    # Admin: view as another user (read-only)
    # ------------------------------------------------------------------

    async def enter_view_as(self, user_id: str, name: str, email: str) -> bool:
        try:
            provider = await RemoteProvider.load(self.ctx.api, user_id)
        except ApiError as error:
            self.toasts.show(error.message, "error")
            return False
        self._drop_view_pages()
        self.view_provider = provider
        pages: dict[str, QWidget] = {
            "dashboard": DashboardPage(self.ctx, provider, "Overview of this user's activity"),
            "saved_jobs": SavedJobsPage(self.ctx, provider, self.toasts),
            "generated": GeneratedPage(self.ctx, provider, self.toasts),
        }
        pages["dashboard"].drill.connect(lambda filters, text: self.open_generated(filters, text))  # type: ignore[attr-defined]
        for page in pages.values():
            self.stack.addWidget(page)
        self.view_pages = pages

        synced = format_datetime(provider.last_synced_at) if provider.last_synced_at else "never"
        shown = provider.full_name or name
        note = "  ·  showing the newest 2,000 resumes" if provider.truncated else ""
        self.view_text.setText(f"Viewing as {shown} ({provider.email or email})  ·  Last synced: {synced}{note}")
        self.view_banner.show()
        for key, item in self.nav.items():
            allowed = key in VIEWABLE or key == "admin"
            item.setEnabled(allowed)
            item.setToolTip("" if allowed else "Not available while viewing another user")
        self.refresh_badges()
        self.show_page("dashboard")
        return True

    def _drop_view_pages(self) -> None:
        for page in self.view_pages.values():
            self.stack.removeWidget(page)
            page.deleteLater()
        self.view_pages = {}

    def exit_view_as(self, to: str = "dashboard") -> None:
        """Back to my own data."""
        if not self.viewing:
            return
        self.view_provider = None
        self.view_banner.hide()
        for item in self.nav.values():
            item.setEnabled(True)
            item.setToolTip("")
        self.current_key = to
        self.show_page(to)
        self._drop_view_pages()
        self.refresh_badges()

    # ------------------------------------------------------------------
    # Sync
    # ------------------------------------------------------------------

    def _apply_sync_interval(self) -> None:
        self._sync_timer.start(self.ctx.settings.sync_interval_minutes * 60_000)

    def request_sync(self, delay_ms: int = SYNC_DEBOUNCE_MS) -> None:
        """Sync soon. Several changes in a row become one sync."""
        self._sync_debounce.start(delay_ms)

    async def sync_now(self) -> SyncStatus:
        self._retry_timer.stop()
        status = await self.ctx.sync.sync()
        delay = self.ctx.sync.retry_delay
        if delay is not None:
            self._retry_timer.start(delay * 1000)  # back off: 1, 2, 5, then every 15 minutes
        return status

    def _sync_changed(self, status: SyncStatus) -> None:
        self._paint_chip(status)
        # A session that ended for good (password changed, signed out elsewhere) needs a new sign-in;
        # being offline does not.
        self.session_ended = status.phase == "error" and self.ctx.sync.last_error_code == "UNAUTHENTICATED"
        if self.session_ended:
            self.offline_text.setText("Your session has ended. Sign in again to sync; your local data is safe.")
            self.offline_action.setText("Sign in")
        else:
            self.offline_text.setText(
                "Offline: working from local data. Changes will sync when the connection is back."
            )
            self.offline_action.setText("Retry now")
        self.offline_banner.setVisible(status.phase == "offline" or self.session_ended)
        self.ctx.online = status.phase in ("idle", "syncing")
        if self.sync_panel is not None and self.sync_panel.isVisible():
            self.sync_panel.update_status(status)

    def _banner_action(self) -> None:
        if self.session_ended:
            self.shutdown()
            self.logout_requested.emit()
        else:
            spawn(self.sync_now())

    def _paint_chip(self, status: SyncStatus) -> None:
        name, color = sync_chip_icon(status)
        self.sync_chip.setIcon(icons.icon(name, color, 15))
        self.sync_chip.setText("  " + sync_chip_text(status))

    def _remote_changed(self) -> None:
        """A pull changed local rows (a status set on the web): refresh what is on screen."""
        self.refresh_badges()
        if not self.viewing:
            refresh = getattr(self.current_page(), "refresh", None)
            if callable(refresh) and self.current_key != "generator":
                refresh()

    def _jobs_shared(self, count: int) -> None:
        """Jobs other profiles found arrived in this profile's saved list."""
        self.refresh_badges()
        self.toasts.show(f"{count} new job{'s' if count != 1 else ''} from the shared list added to Saved Jobs", "info")
        if self.viewing:
            return
        self.generator.jobs_changed()  # a running generator takes them on; an idle one redraws its queue
        if self.current_key == "saved_jobs":
            self.saved_jobs.refresh()

    def _local_changed(self, kind: str) -> None:
        if kind == "settings":
            self._apply_sync_interval()
            return
        self.refresh_badges()
        self.ctx.sync.refresh_status()
        self.request_sync()
        if self.viewing:
            return
        # Keep whatever is on screen current (the dashboard after a status change, say).
        if self.current_key == "dashboard":
            self.dashboard.refresh()
        elif self.current_key == "saved_jobs" and kind == "jobs":
            self.saved_jobs.refresh()
        elif self.current_key == "generated" and kind == "resumes":
            self.generated.refresh()

    def open_sync_panel(self) -> None:
        self.sync_panel = SyncPanel(self, self.ctx, self.toasts, self.sync_now)
        self.sync_panel.exec()
        self.sync_panel = None

    def open_settings(self) -> None:
        dialog = SettingsDialog(self, self.ctx, self.toasts, self.sync_now)
        dialog.exec()

    # ------------------------------------------------------------------
    # Leaving
    # ------------------------------------------------------------------

    def request_logout(self) -> None:
        text = "You will need your e-mail and password to sign in again. Your local data stays on this PC."
        if self.generator.running:
            text = "A generation run is in progress and will be stopped. " + text
        if confirm(self, "Log out?", text, "Log out"):
            self.shutdown()
            self.logout_requested.emit()

    def shutdown(self) -> None:
        """Stop timers and any run, before the window goes away."""
        if self.generator.running:
            self.generator.stop()
        for timer in (self._sync_debounce, self._sync_timer, self._retry_timer, self._chip_timer):
            timer.stop()
        self.ctx.sync.on_change = None
        self.ctx.sync.on_data_changed = None
        self.ctx.sync.on_jobs_shared = None
        self.ctx.listeners.clear()

    def closeEvent(self, event: Any) -> None:  # noqa: N802
        if self.generator.running and not confirm(
            self,
            "Quit while generating?",
            "A generation run is in progress. Quitting stops it; the current job stays in Saved Jobs.",
            "Quit",
            danger=True,
        ):
            event.ignore()
            return
        self.shutdown()
        event.accept()
