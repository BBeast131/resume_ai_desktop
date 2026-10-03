"""Resume AI: entry point.

    python -m app.main

Order matters at start-up: the Chromium flags must be set before the
QApplication exists, and the asyncio loop (qasync) is started exactly once and
never restarted, because stopping it makes QtWebEngine tear its profile down.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from logging.handlers import RotatingFileHandler

from app.browser.profile import apply_chromium_flags

apply_chromium_flags()

import qasync  # noqa: E402
from PySide6.QtCore import QLockFile, QTimer  # noqa: E402
from PySide6.QtWebEngineCore import QWebEngineProfile  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from app.config import APP_DISPLAY_NAME, APP_NAME, APP_VERSION, AppConfig, app_data_dir, load_config  # noqa: E402
from app.data.db import Database  # noqa: E402
from app.data.store import Store  # noqa: E402
from app.services.auth import AuthError, AuthService, UserProfile, load_last_user  # noqa: E402
from app.services.documents import DocumentService  # noqa: E402
from app.services.settings import Settings  # noqa: E402
from app.sync.api_client import ApiError, WebApi  # noqa: E402
from app.sync.engine import SyncEngine  # noqa: E402
from app.ui.auth_window import AuthWindow  # noqa: E402
from app.ui.context import AppContext, spawn  # noqa: E402
from app.ui.main_window import MainWindow  # noqa: E402
from app.ui.styles import apply_theme  # noqa: E402

log = logging.getLogger("app")


def setup_logging() -> None:
    """A small rotating log under the data folder.

    It holds ids, counts, lengths, host names and timings. Resume text, job
    descriptions, prompts, passwords and tokens are never written to it.
    """
    folder = app_data_dir() / "logs"
    folder.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(folder / "resume_ai.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    # httpx logs full request URLs at INFO; keep those out.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


class Controller:
    """Owns the windows: the login card first, the main window after sign-in."""

    def __init__(self, app: QApplication, config: AppConfig) -> None:
        self.app = app
        self.config = config
        self.auth = AuthService(config)
        self.auth_window: AuthWindow | None = None
        self.window: MainWindow | None = None
        self.ctx: AppContext | None = None
        self.database: Database | None = None
        self.profile: QWebEngineProfile | None = None

    # -- start-up ------------------------------------------------------------------

    async def start(self) -> None:
        try:
            await self._start()
        except Exception as error:  # noqa: BLE001 - never leave the user with no window at all
            self._fatal("Resume AI could not start", error)
            if self.window is None and self.auth_window is None:
                self.show_login()

    def _fatal(self, title: str, error: BaseException) -> None:
        log.error("fatal %s", type(error).__name__)  # the type only: a message can quote user data
        QMessageBox.critical(
            None,
            title,
            f"{title}.\n\nProblem: {type(error).__name__}\n\n"
            "If this keeps happening, close every Resume AI window and start it again. "
            f"Your data folder is {app_data_dir()}.",
        )

    async def _start(self) -> None:
        last = load_last_user()
        if last is not None and self.auth.has_saved_session():
            try:
                session = await self.auth.refresh()
            except AuthError as error:
                if error.code == "network":
                    # A saved session and no internet: open on local data and say "Offline".
                    log.info("start.offline")
                    self.open_main(last)
                    return
                log.info("start.session_ended code=%s", error.code)
            else:
                self.open_main(session.user)
                return
        self.show_login()

    def show_login(self) -> None:
        self.auth_window = AuthWindow(self.config, self.auth)
        self.auth_window.signed_in.connect(self._signed_in)
        self.auth_window.show()

    def _signed_in(self) -> None:
        assert self.auth.session is not None
        try:
            self.open_main(self.auth.session.user)
        except Exception as error:  # noqa: BLE001 - e.g. the database file is locked or damaged
            self._fatal("Your data could not be opened", error)
            return  # the login window stays
        if self.auth_window is not None:
            self.auth_window.close()
            self.auth_window.deleteLater()
            self.auth_window = None

    # -- the main window --------------------------------------------------------------

    def open_main(self, user: UserProfile) -> None:
        settings = Settings(self.config, user.id)
        self.database = Database.for_user(user.id)
        store = Store(self.database)
        api = WebApi(settings.web_app_url, self.auth)
        self.profile = self._make_profile(user, settings)
        self.ctx = AppContext(
            config=self.config,
            settings=settings,
            auth=self.auth,
            api=api,
            store=store,
            sync=SyncEngine(store, api),
            documents=DocumentService(api, user.id),
            user=user,
            web_profile=self.profile,
        )
        self.ctx.load_cached_server_config()
        self.window = MainWindow(self.ctx)
        self.window.logout_requested.connect(lambda: spawn(self.logout()))
        self.window.show()
        log.info("main.opened version=%s", APP_VERSION)
        spawn(self._after_open())

    def _make_profile(self, user: UserProfile, settings: Settings) -> QWebEngineProfile:
        from app.browser.profile import make_profile

        return make_profile(user.id, self.app, settings.downloads_dir)

    async def _after_open(self) -> None:
        """Things that need the network; the window is already usable on local data."""
        ctx, window = self.ctx, self.window
        if ctx is None or window is None:
            return
        await ctx.refresh_server_config()
        try:
            account = await ctx.api.get_account()
        except ApiError:
            account = {}
        profile = account.get("profile") if isinstance(account, dict) else None
        if isinstance(profile, dict):
            # The role decides whether the Admin tab is shown. The server checks it again on every admin request.
            role = str(profile.get("role") or "user")
            name = str(profile.get("fullName") or "") or ctx.user.full_name
            ctx.user.role = role
            ctx.user.full_name = name
            self.auth.set_role(role, name)
            if self.window is window:
                window.update_user()
        if self.window is window:
            window.generator.refresh()
            await window.sync_now()

    # -- leaving ----------------------------------------------------------------------

    async def logout(self) -> None:
        window, self.window = self.window, None
        ctx = self.ctx
        if window is not None:
            window.hide()
            # Let a stopping run reach its safe point before its screen and database go away.
            task = window.generator.run_task
            if task is not None and not task.done():
                await asyncio.wait({task}, timeout=15)
        auth = self.auth
        # A new sign-in gets its own AuthService and API client: nothing still in flight for the
        # previous account can ever pick up the next account's token.
        self.auth = AuthService(self.config)
        await auth.sign_out()
        self._release(window)
        if ctx is not None:
            await ctx.api.close()
        await auth.close()
        self.show_login()

    def _release(self, window: MainWindow | None) -> None:
        if window is not None:
            for browser in (window.generator.browser, window.job_search.browser):
                if browser is not None:
                    browser.close_all()  # pages must be gone before their profile
            window.deleteLater()
        if self.database is not None:
            self.database.close()
            self.database = None
        profile, self.profile = self.profile, None
        if profile is not None:
            QTimer.singleShot(2000, profile, profile.deleteLater)
        self.ctx = None

    def quit_cleanup(self) -> None:
        """The app is closing: tabs first, then the database, so nothing is torn down under a live page."""
        window, self.window = self.window, None
        if window is not None:
            window.shutdown()
        self._release(window)


def main() -> int:
    setup_logging()
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_DISPLAY_NAME)
    app.setApplicationVersion(APP_VERSION)
    apply_theme(app)

    # One instance at a time: two would share one browser profile, one rotating sign-in token
    # and one database file.
    app_data_dir().mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(app_data_dir() / "resume_ai.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(100):
        QMessageBox.information(None, APP_DISPLAY_NAME, "Resume AI is already running.")
        return 0

    def log_uncaught(kind: type[BaseException], error: BaseException, trace: object) -> None:
        log.error("uncaught %s", kind.__name__)  # the type only: never the message

    sys.excepthook = log_uncaught

    config = load_config()
    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)
    controller = Controller(app, config)
    app.aboutToQuit.connect(controller.quit_cleanup)

    with loop:
        loop.create_task(controller.start())
        loop.run_forever()  # once; returns when the last window is closed
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
