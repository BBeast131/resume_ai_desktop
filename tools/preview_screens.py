from __future__ import annotations

from collections.abc import Iterator

from PySide6.QtWidgets import QWidget

from app.services.provider import LocalProvider
from app.ui.widgets.common import ToastHost


def build(ctx, only: set[str]) -> Iterator[tuple[str, QWidget]]:
    def want(name: str) -> bool:
        return not only or name in only

    provider = LocalProvider(ctx.store)

    if want("login") or want("register"):
        from app.ui.auth_window import AuthWindow

        window = AuthWindow(ctx.config, ctx.auth)
        window.resize(980, 700)
        window.show()
        if want("login"):
            yield "login", window
        window.show_page(1)
        if want("register"):
            yield "register", window
        window.hide()

    def page(name: str, factory) -> Iterator[tuple[str, QWidget]]:
        if not want(name):
            return
        widget = factory()
        widget.setObjectName("Workspace")
        widget.resize(1130, 720)
        widget.show()
        yield name, widget
        widget.hide()

    from app.ui.pages.dashboard import DashboardPage
    from app.ui.pages.generated import GeneratedPage
    from app.ui.pages.saved_jobs import SavedJobsPage

    yield from page("dashboard", lambda: DashboardPage(ctx, provider))

    def saved():
        widget = SavedJobsPage(ctx, provider, ToastHost(QWidget()))
        return widget

    yield from page("saved_jobs", saved)

    if want("generated") or want("drawer"):
        widget = GeneratedPage(ctx, provider, ToastHost(QWidget()))
        widget.setObjectName("Workspace")
        widget.resize(1130, 720)
        widget.show()
        if want("generated"):
            yield "generated", widget
        if want("drawer"):
            widget.show_details(widget.rows[0].id)
            from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer

            loop = QEventLoop()
            QTimer.singleShot(600, loop.quit)
            loop.exec()
            QCoreApplication.processEvents()
            yield "drawer", widget
        widget.hide()

    if want("main") or want("collapsed") or want("generator") or want("start_modal") or want("admin"):
        from app.ui.main_window import MainWindow

        window = MainWindow(ctx)
        window.resize(1366, 768)
        window.show()
        if want("main"):
            yield "main", window
        if want("generator") or want("start_modal"):
            ctx.store.set_prompt("You write resumes.", "Anthony-resume-prompt.txt")
            window.generator.refresh()
            window.show_page("generator")
            if want("generator"):
                yield "generator", window
        if want("admin"):
            window.show_page("admin")
            from PySide6.QtCore import QEventLoop, QTimer

            loop = QEventLoop()
            QTimer.singleShot(300, loop.quit)
            loop.exec()
            yield "admin", window
        if want("collapsed"):
            window.show_page("saved_jobs")
            window.set_collapsed(True, animate=False)
            yield "collapsed", window
            window.set_collapsed(False, animate=False)
        window.hide()
