"""Settings and the Sync panel."""

from __future__ import annotations

from pathlib import Path

import shiboken6
from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QComboBox, QFileDialog, QGridLayout, QHBoxLayout, QLineEdit, QSpinBox, QWidget

from app.config import APP_VERSION, is_safe_web_url, user_data_dir
from app.sync.api_client import ApiError
from app.sync.engine import SyncStatus
from app.ui.context import AppContext, spawn
from app.ui.widgets.common import Modal, ToastHost, button, format_ago, format_datetime, label


def open_folder(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))


def export_database(ctx: AppContext, parent: QWidget, toasts: ToastHost) -> Path | None:
    """A single-file copy of the local database, for the web page's Import DB button."""
    suggested = str(ctx.settings.downloads_dir / "resume_ai.db")
    target, _ = QFileDialog.getSaveFileName(parent, "Export DB copy", suggested, "SQLite database (*.db)")
    if not target:
        return None
    try:
        path = ctx.store.database.export_copy(Path(target))
    except OSError:
        toasts.show("The copy could not be written there.", "error")
        return None
    toasts.show(f"Database copy saved: {path.name}", "success", "Open folder", lambda: open_folder(path.parent))
    return path


def sync_chip_text(status: SyncStatus) -> str:
    """The sidebar chip: "Synced 12 min ago" / "Syncing…" / "Offline: 3 changes waiting" / "Sync error"."""
    if status.phase == "syncing":
        return "Syncing…"
    if status.phase == "offline":
        waiting = f": {status.pending} change{'s' if status.pending != 1 else ''} waiting" if status.pending else ""
        return f"Offline{waiting}"
    if status.phase == "error":
        return "Sync error"
    if status.last_synced_at is None:
        return "Not synced yet"
    if status.pending:
        return f"{status.pending} change{'s' if status.pending != 1 else ''} waiting"
    return f"Synced {format_ago(status.last_synced_at)}"


def sync_chip_icon(status: SyncStatus) -> tuple[str, str]:
    if status.phase == "syncing":
        return "refresh", "#93C5FD"
    if status.phase == "offline":
        return "cloud-off", "#FBBF24"
    if status.phase == "error":
        return "alert", "#F87171"
    if status.rejected:
        return "alert", "#FBBF24"
    return "cloud-check", "#4ADE80"


class SyncPanel(Modal):
    def __init__(self, parent: QWidget, ctx: AppContext, toasts: ToastHost, sync_now) -> None:  # type: ignore[no-untyped-def]
        super().__init__(parent, 460)
        self.ctx = ctx
        self.toasts = toasts
        self._sync_now = sync_now
        self.add_title("Sync with the web app", ctx.settings.web_app_url)

        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(8)
        self.values: dict[str, object] = {}
        for row, (key, caption) in enumerate(
            (
                ("state", "Status"),
                ("push", "Last push"),
                ("pull", "Last pull"),
                ("pending", "Waiting to sync"),
                ("error", "Last error"),
            )
        ):
            grid.addWidget(label(caption, "FieldLabel"), row, 0)
            value = label("", wrap=True, selectable=True)
            grid.addWidget(value, row, 1)
            self.values[key] = value
        grid.setColumnStretch(1, 1)
        self.body.addLayout(grid)

        self.rejected_title = label("", "FieldLabel")
        self.rejected_list = label("", "Small", wrap=True)
        self.body.addWidget(self.rejected_title)
        self.body.addWidget(self.rejected_list)

        self.body.addWidget(
            label(
                "If automatic sync cannot be used, export a copy of the database and use Import DB on the web app's "
                "Generated Resumes page.",
                "Small",
                wrap=True,
            )
        )
        export = button("Export DB copy…", icon_name="download")
        export.clicked.connect(lambda _checked=False: export_database(ctx, self, toasts))
        self.sync_button = button("Sync now", "primary", "refresh")
        self.sync_button.clicked.connect(lambda _checked=False: self._run())
        close = button("Close")
        close.clicked.connect(self.accept)
        self.add_buttons(close, export, self.sync_button)
        self.update_status(ctx.sync.status)

    def update_status(self, status: SyncStatus) -> None:
        words = {"idle": "Up to date", "syncing": "Syncing…", "offline": "Offline", "error": "Error"}
        if status.phase == "idle" and status.pending:
            words["idle"] = "Changes waiting"
        self.values["state"].setText(words.get(status.phase, status.phase))  # type: ignore[attr-defined]
        self.values["push"].setText(  # type: ignore[attr-defined]
            f"{format_datetime(status.last_push_at)} ({format_ago(status.last_push_at)})"
            if status.last_push_at
            else "never"
        )
        self.values["pull"].setText(  # type: ignore[attr-defined]
            f"{format_datetime(status.last_pull_at)} ({format_ago(status.last_pull_at)})"
            if status.last_pull_at
            else "never"
        )
        self.values["pending"].setText(f"{status.pending} change{'s' if status.pending != 1 else ''}")  # type: ignore[attr-defined]
        self.values["error"].setText(status.last_error or "none")  # type: ignore[attr-defined]
        self.sync_button.setEnabled(status.phase != "syncing")

        lines = [f"•  {row.company} - {row.role}: {row.sync_error}" for row in self.ctx.store.rejected_resumes()]
        lines += [
            f"•  Saved job {job.company or '?'} - {job.role or '?'}: {job.sync_error}"
            for job in self.ctx.store.rejected_saved_jobs()
        ]
        self.rejected_title.setVisible(bool(lines))
        self.rejected_list.setVisible(bool(lines))
        if lines:
            self.rejected_title.setText(f"{len(lines)} record(s) the web app refused")
            shown = lines[:8] + ([f"…and {len(lines) - 8} more"] if len(lines) > 8 else [])
            self.rejected_list.setText("\n".join(shown))

    def _run(self) -> None:
        self.sync_button.setEnabled(False)
        spawn(self._sync())

    async def _sync(self) -> None:
        status = await self._sync_now()
        self.update_status(status)


class SettingsDialog(Modal):
    def __init__(self, parent: QWidget, ctx: AppContext, toasts: ToastHost, sync_now) -> None:  # type: ignore[no-untyped-def]
        super().__init__(parent, 520)
        self.ctx = ctx
        self.toasts = toasts
        self._sync_now = sync_now
        settings = ctx.settings
        self.add_title("Settings", f"Resume AI {APP_VERSION}")

        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(10)
        grid.setColumnStretch(1, 1)
        row = 0

        def add(caption: str, widget: QWidget, hint: str = "") -> None:
            nonlocal row
            grid.addWidget(label(caption, "FieldLabel"), row, 0)
            grid.addWidget(widget, row, 1)
            row += 1
            if hint:
                grid.addWidget(label(hint, "Small", wrap=True), row, 1)
                row += 1

        self.web_url = QLineEdit(settings.web_app_url)
        add("Web app URL", self.web_url)

        self.candidate = QLineEdit(settings.candidate_name)
        self.candidate.setPlaceholderText(ctx.user.full_name)
        add("Name on resumes", self.candidate, "Leave empty to use your account's full name.")

        self.interval = QSpinBox()
        self.interval.setRange(5, 720)
        self.interval.setSuffix(" min")
        self.interval.setValue(settings.sync_interval_minutes)
        add("Sync every", self.interval)

        self.seconds = QSpinBox()
        self.seconds.setRange(1, 30)
        self.seconds.setSuffix(" s")
        self.seconds.setValue(settings.automation_seconds)
        add("Automation step delay", self.seconds)

        downloads = QWidget()
        downloads_layout = QHBoxLayout(downloads)
        downloads_layout.setContentsMargins(0, 0, 0, 0)
        self.downloads = QLineEdit(str(settings.downloads_dir))
        self.downloads.setReadOnly(True)
        browse = button("Browse…")
        browse.clicked.connect(lambda _checked=False: self._browse())
        downloads_layout.addWidget(self.downloads, 1)
        downloads_layout.addWidget(browse)
        add("Downloads folder", downloads)

        self.period = QComboBox()
        for key, text in (("day", "Day"), ("week", "Week"), ("month", "Month")):
            self.period.addItem(text, key)
        self.period.setCurrentIndex(max(0, self.period.findData(settings.dashboard_period)))
        add("Default Dashboard period", self.period)
        self.body.addLayout(grid)

        tools = QHBoxLayout()
        tools.setSpacing(8)
        sync = button("Sync now", icon_name="refresh")
        sync.clicked.connect(lambda _checked=False: spawn(self._sync_now()))
        folder = button("Open data folder", icon_name="folder")
        folder.clicked.connect(lambda _checked=False: open_folder(user_data_dir(ctx.user.id)))
        export = button("Export DB copy…", icon_name="download")
        export.clicked.connect(lambda _checked=False: export_database(ctx, self, toasts))
        for widget in (sync, folder, export):
            tools.addWidget(widget)
        self.body.addLayout(tools)

        test_row = QHBoxLayout()
        self.test_button = button("Test connection", icon_name="globe")
        self.test_button.clicked.connect(lambda _checked=False: spawn(self.test_connection()))
        test_row.addWidget(self.test_button)
        self.test_result = label("", "Small", wrap=True)
        test_row.addWidget(self.test_result, 1)
        self.body.addLayout(test_row)

        cancel = button("Cancel")
        cancel.clicked.connect(self.reject)
        save = button("Save", "primary")
        save.clicked.connect(lambda _checked=False: self.save())
        self.add_buttons(cancel, save)

    def _browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Downloads folder", self.downloads.text())
        if folder:
            self.downloads.setText(folder)

    def save(self) -> None:
        settings = self.ctx.settings
        url = self.web_url.text().strip().rstrip("/")
        if not is_safe_web_url(url):
            self.test_result.setText("The web app URL must start with https:// (or be a local address).")
            return
        changed_url = url != settings.web_app_url
        settings.web_app_url = url
        settings.candidate_name = self.candidate.text()
        settings.sync_interval_minutes = self.interval.value()
        settings.automation_seconds = self.seconds.value()
        settings.downloads_dir = Path(self.downloads.text())
        settings.dashboard_period = str(self.period.currentData())
        if changed_url:
            self.ctx.api.base_url = url
        self.ctx.changed("settings")
        self.accept()

    async def test_connection(self) -> None:
        """Web app reachable, signed in, Groq check available."""
        url = self.web_url.text().strip().rstrip("/")
        if not is_safe_web_url(url):
            # The access token is sent with the request: never to a plain-http address.
            self.test_result.setText("The web app URL must start with https:// (or be a local address).")
            return
        self.test_button.setEnabled(False)
        self.test_result.setText("Testing…")
        # A client of its own: a sync or a validation in flight keeps talking to the saved address.
        probe = self.ctx.api if url == self.ctx.api.base_url else self.ctx.api.with_base_url(url)
        try:
            config = await probe.get_config()
        except ApiError as error:
            if shiboken6.isValid(self):
                self.test_result.setText(f"✗ {error.message}")
            return
        finally:
            if shiboken6.isValid(self):
                self.test_button.setEnabled(True)
        if not shiboken6.isValid(self):
            return
        groq = "AI job check available" if config.get("groqAvailable") else "AI job check not configured on the server"
        self.test_result.setText(f"✓ Web app reachable  ·  ✓ Signed in as {self.ctx.user.email}  ·  {groq}")
