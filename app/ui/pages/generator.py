"""Resume Generating: the queue on the left, the browser and the review panel on the right.

This page is the pipeline's screen (`PipelineHost`). The pipeline decides what
happens to a job; this page shows it and collects the user's answers.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import shiboken6
from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QTextCharFormat, QTextCursor
from PySide6.QtWebEngineCore import QWebEngineProfile
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from app.automation.pipeline import SKIP_LABELS, Mode, Pipeline, ReviewData, RunSummary, validation_problem
from app.browser.browser import BrowserWidget
from app.data.models import SavedJob
from app.services.documents import DocumentError, save_copy
from app.services.resume_text import ResumeTextError, extract_resume_text, read_prompt_file
from app.ui import icons
from app.ui.context import AppContext, spawn
from app.ui.theme import tokens
from app.ui.widgets.common import Modal, PageHeader, ToastHost, button, label, repolish, shadow

STEPS = ("Extract Job Info", "Generate with ChatGPT", "Validate & Save")
QUEUE_ICONS = {
    "pending": ("circle", "placeholder"),
    "current": ("dot", "primary"),
    "done": ("check-circle", "success"),
    "skipped": ("skip", "muted"),
    "attention": ("alert", "warning"),
}


class Stepper(QWidget):
    """① Extract Job Info → ② Generate with ChatGPT → ③ Validate & Save"""

    def __init__(self) -> None:
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        layout.addStretch(1)
        self.dots: list[QLabel] = []
        self.labels: list[QLabel] = []
        for index, text in enumerate(STEPS):
            dot = QLabel(str(index + 1))
            dot.setObjectName("StepDot")
            dot.setFixedSize(24, 24)
            dot.setAlignment(Qt.AlignmentFlag.AlignCenter)
            caption = label(text, "StepLabel")
            self.dots.append(dot)
            self.labels.append(caption)
            layout.addWidget(dot)
            layout.addWidget(caption)
            if index < len(STEPS) - 1:
                line = QFrame()
                line.setObjectName("StepLine")
                line.setFixedWidth(36)
                layout.addWidget(line)
        layout.addStretch(1)
        self.current = 0
        self.set_step(1)

    def set_step(self, step: int) -> None:
        self.current = step
        for index, (dot, caption) in enumerate(zip(self.dots, self.labels, strict=True), start=1):
            state = "done" if index < step else "active" if index == step else "todo"
            dot.setProperty("state", state)
            caption.setProperty("state", "active" if index == step else "todo")
            if state == "done":
                dot.setPixmap(icons.pixmap("check", "#FFFFFF", 13))
            else:
                dot.setPixmap(icons.pixmap("circle", "#00000000", 1))
                dot.setText(str(index))
            repolish(dot)
            repolish(caption)


class QueueItem(QWidget):
    def __init__(self, job: SavedJob) -> None:
        super().__init__()
        self.job_id = job.id
        self.state = "attention" if job.attention_reason else "pending"
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 9, 10, 9)
        layout.setSpacing(10)
        self.icon = QLabel()
        self.icon.setFixedSize(18, 18)
        layout.addWidget(self.icon)
        text = QVBoxLayout()
        text.setSpacing(1)
        self.company = label(job.company or "Unknown company")
        self.company.setStyleSheet("font-weight: 600;")
        self.role = label(job.role or "Unknown role", "Small")
        text.addWidget(self.company)
        text.addWidget(self.role)
        layout.addLayout(text, 1)
        if job.attention_reason:
            self.setToolTip(f"Needs attention: {job.attention_reason}")
        self.set_state(self.state)

    def set_state(self, state: str) -> None:
        self.state = state
        name, color_key = QUEUE_ICONS.get(state, QUEUE_ICONS["pending"])
        t = tokens()
        color = {
            "placeholder": t.placeholder,
            "primary": t.primary,
            "success": t.success,
            "muted": t.muted,
            "warning": t.warning,
        }[color_key]
        self.icon.setPixmap(icons.pixmap(name, color, 18))


class ReviewPanel(QFrame):
    """Extracted Information: editable company, role and job description."""

    next_clicked = Signal()
    cancel_clicked = Signal()
    edited = Signal()

    def __init__(self, limits: dict[str, int]) -> None:
        super().__init__()
        self.limits = limits
        self.setObjectName("Card")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.data: ReviewData | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(6)
        # Room for the countdown or the alert that sits over the top of this panel,
        # so no field is ever hidden beneath it.
        self.inset = QWidget()
        self.inset.setFixedHeight(0)
        layout.addWidget(self.inset)
        layout.addWidget(label("Extracted Information", "SectionTitle"))
        self.hint = label("", "Small", wrap=True)
        self.hint.hide()
        layout.addWidget(self.hint)

        layout.addWidget(label("Company", "FieldLabel"))
        self.company = QLineEdit()
        layout.addWidget(self.company)
        self.company_hint = label("", "Small")
        layout.addWidget(self.company_hint)

        layout.addWidget(label("Role", "FieldLabel"))
        self.role = QLineEdit()
        layout.addWidget(self.role)
        self.role_hint = label("", "Small")
        layout.addWidget(self.role_hint)

        row = QHBoxLayout()
        row.addWidget(label("Job Description", "FieldLabel"))
        row.addStretch(1)
        self.counter = label("0 characters", "Small")
        row.addWidget(self.counter)
        layout.addLayout(row)
        self.jd = QPlainTextEdit()
        self.jd.setPlaceholderText("The job description goes here.")
        layout.addWidget(self.jd, 1)

        self.error = label("", "ErrorText", wrap=True)
        self.error.hide()
        layout.addWidget(self.error)

        buttons = QHBoxLayout()
        buttons.setSpacing(10)
        self.cancel_button = button("Cancel Job", size="large")
        self.next_button = button("Next  →", "primary", size="large")
        self.cancel_button.clicked.connect(lambda _checked=False: self.cancel_clicked.emit())
        self.next_button.clicked.connect(lambda _checked=False: self.next_clicked.emit())
        buttons.addWidget(self.cancel_button, 1)
        buttons.addWidget(self.next_button, 1)
        layout.addLayout(buttons)

        self.company.textEdited.connect(lambda _text: self._changed())
        self.role.textEdited.connect(lambda _text: self._changed())
        self.jd.textChanged.connect(self._count)
        self._loading = False
        self.setMinimumWidth(320)

    def load(self, data: ReviewData) -> None:
        self._loading = True
        self.data = data
        self.company.setText(data.company)
        self.role.setText(data.role)
        self.jd.setPlainText(data.jd_text)
        self._loading = False
        self.error.hide()
        check = data.check
        amber = f"color: {tokens().warning}; font-weight: 600;"
        self.company_hint.setText("corrected by AI" if check and check.company_corrected else "")
        self.role_hint.setText("corrected by AI" if check and check.role_corrected else "")
        for widget in (self.company_hint, self.role_hint):
            widget.setStyleSheet(f"color: {tokens().primary}; font-weight: 600;")
            widget.setVisible(bool(widget.text()))
        notes: list[str] = []
        if check is not None and check.unchecked:
            notes.append("The AI check was unavailable: these are the page's own values. Please check them.")
        elif check is not None and check.low_confidence:
            notes.append("Low confidence: the AI check was not sure about the role or company.")
        if check is not None and not check.unchecked and not check.has_job_description:
            notes.append("The AI check doubts this is a full job description.")
        self.hint.setText(" ".join(notes))
        self.hint.setStyleSheet(amber)
        self.hint.setVisible(bool(notes))
        self._count()

    def collect(self) -> ReviewData:
        assert self.data is not None
        self.data.company = self.company.text().strip()
        self.data.role = self.role.text().strip()
        self.data.jd_text = self.jd.toPlainText().strip()
        return self.data

    def problem(self) -> str | None:
        return validation_problem(self.collect(), self.limits)

    def show_problem(self, text: str | None) -> None:
        self.error.setText(text or "")
        self.error.setVisible(bool(text))

    def _count(self) -> None:
        length = len(self.jd.toPlainText().strip())
        low, high = self.limits["jobDescriptionMin"], self.limits["jobDescriptionMax"]
        self.counter.setText(f"{length:,} characters")
        bad = length < low or length > high
        self.counter.setStyleSheet(f"color: {tokens().danger if bad else tokens().muted};")
        self.counter.setToolTip(f"Between {low:,} and {high:,} characters")
        if not self._loading:
            self._changed()

    def _changed(self) -> None:
        if not self._loading:
            self.error.hide()
            self.edited.emit()


class FloatCard(QFrame):
    """A card that floats over the board: the countdown and the alerts. Never modal."""

    def __init__(self, parent: QWidget, name: str = "FloatCard", width: int = 330) -> None:
        super().__init__(parent)
        self.setObjectName(name)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedWidth(width)
        shadow(self, blur=30, alpha=60, offset=8)
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(16, 14, 16, 14)
        self.body.setSpacing(8)
        self.hide()


class GeneratorPage(QWidget):
    open_generated = Signal()
    running_changed = Signal(bool)

    def __init__(
        self, ctx: AppContext, toasts: ToastHost, profile: QWebEngineProfile | None, chatgpt_url: str | None = None
    ) -> None:
        super().__init__()
        self.ctx = ctx
        self.toasts = toasts
        self.profile = profile
        self.chatgpt_url = chatgpt_url
        self.pipeline: Pipeline | None = None
        self.run_task: asyncio.Future[Any] | None = None
        self.last_resume_id: str | None = None
        self.queue_items: dict[str, QueueItem] = {}
        self._answer: asyncio.Future[Any] | None = None
        self._last_logged_step = (0, "")
        self._countdown_timer = QTimer(self)
        self._countdown_timer.setInterval(100)
        self._countdown_timer.timeout.connect(self._countdown_tick)
        self._countdown_total = 0.0
        self._countdown_left = 0.0
        self._countdown_done: Any = None
        self._countdown_text = ""
        t = tokens()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 16)
        layout.setSpacing(12)
        layout.addWidget(PageHeader("Resume Generator", "Generate customized resumes for your saved jobs using AI"))

        # ---- toolbar ----
        toolbar = QHBoxLayout()
        toolbar.setSpacing(10)
        self.import_prompt = button(
            "Import Prompt", icon_name="file-text", size="large", tooltip="A .txt or .md prompt file"
        )
        self.import_prompt.clicked.connect(lambda _checked=False: self.choose_prompt())
        self.import_resume = button(
            "Import Resume",
            icon_name="file-user",
            size="large",
            tooltip="Your original resume: .pdf, .docx or .txt (optional)",
        )
        self.import_resume.clicked.connect(lambda _checked=False: self.choose_resume())
        self.clear_resume = button("", "ghost", "x", tooltip="Remove the imported resume")
        self.clear_resume.clicked.connect(lambda _checked=False: self.remove_resume())
        self.download = button("Download", icon_name="download", size="large")
        menu = QMenu(self.download)
        menu.addAction(icons.icon("pdf"), "PDF", lambda: spawn(self.download_last("pdf")))
        menu.addAction(icons.icon("docx"), "DOCX", lambda: spawn(self.download_last("docx")))
        self.download.setMenu(menu)
        self.start = button("Start", "primary", "play", size="large")
        self.start.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self.start.setMinimumWidth(130)
        self.start.clicked.connect(lambda _checked=False: self.start_or_stop())
        for widget in (self.import_prompt, self.import_resume, self.clear_resume, self.download):
            toolbar.addWidget(widget)
        toolbar.addStretch(1)
        toolbar.addWidget(self.start)
        layout.addLayout(toolbar)

        self.chips = label("", "Small")
        layout.addWidget(self.chips)

        # ---- body ----
        body = QHBoxLayout()
        body.setSpacing(14)
        queue_card = QFrame()
        queue_card.setObjectName("Card")
        queue_card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        queue_card.setFixedWidth(250)
        queue_layout = QVBoxLayout(queue_card)
        queue_layout.setContentsMargins(1, 1, 1, 1)
        queue_layout.setSpacing(0)
        self.queue_title = label("Job Queue (0)", "QueueTitle")
        queue_layout.addWidget(self.queue_title)
        self.queue = QListWidget()
        self.queue.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        self.queue.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        queue_layout.addWidget(self.queue, 1)
        self.queue_empty = label("No saved jobs.\nSave some from Job Search.", "Muted")
        self.queue_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.queue_empty.setContentsMargins(12, 24, 12, 24)
        queue_layout.addWidget(self.queue_empty)
        body.addWidget(queue_card)

        self.board = QStackedWidget()
        body.addWidget(self.board, 1)

        placeholder = QFrame()
        placeholder.setObjectName("Placeholder")
        placeholder.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        placeholder_layout = QVBoxLayout(placeholder)
        placeholder_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        picture = QLabel()
        picture.setPixmap(icons.pixmap("browser", t.muted, 54))
        picture.setAlignment(Qt.AlignmentFlag.AlignCenter)
        placeholder_layout.addWidget(picture)
        title = label("Browser will open here")
        title.setStyleSheet(f"font-size: 15px; font-weight: 600; color: {t.text_secondary}; background: transparent;")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        placeholder_layout.addWidget(title)
        text = label(
            "Job site / ChatGPT will be displayed in this area\nduring the resume generation process.", "Muted"
        )
        text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        text.setStyleSheet(f"color: {t.muted}; background: transparent;")
        placeholder_layout.addWidget(text)
        self.board.addWidget(placeholder)

        self.run_area = QWidget()
        run_layout = QVBoxLayout(self.run_area)
        run_layout.setContentsMargins(0, 0, 0, 0)
        run_layout.setSpacing(8)
        self.stepper = Stepper()
        run_layout.addWidget(self.stepper)
        self.status = label("", "Muted")
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        run_layout.addWidget(self.status)
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        browser_card = QFrame()
        browser_card.setObjectName("Card")
        browser_card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        browser_layout = QVBoxLayout(browser_card)
        browser_layout.setContentsMargins(1, 1, 1, 1)
        # A run keeps going while another sidebar tab is in front, so its pages must keep rendering.
        self.browser = (
            BrowserWidget(profile, keep_pages_active=True, closable_tabs=False) if profile is not None else None
        )  # type: ignore[assignment]
        if self.browser is not None:
            browser_layout.addWidget(self.browser)
        self.splitter.addWidget(browser_card)
        self.review_panel = ReviewPanel(ctx.limits)
        self.review_panel.next_clicked.connect(self._force_next)
        self.review_panel.cancel_clicked.connect(lambda: self._resolve(None))
        self.review_panel.edited.connect(self._review_edited)
        self.review_panel.hide()
        self.splitter.addWidget(self.review_panel)
        self.splitter.setStretchFactor(0, 3)
        self.splitter.setStretchFactor(1, 2)
        run_layout.addWidget(self.splitter, 1)
        self.board.addWidget(self.run_area)
        layout.addLayout(body, 1)

        # ---- floating cards (children of the run area; none of them is modal) ----
        self.countdown_card = FloatCard(self.run_area, width=300)
        head = QHBoxLayout()
        bolt = QLabel()
        bolt.setPixmap(icons.pixmap("zap", t.primary, 18))
        head.addWidget(bolt)
        self.countdown_title = label("Automation Running", "SectionTitle")
        head.addWidget(self.countdown_title, 1)
        self.countdown_card.body.addLayout(head)
        self.countdown_text = label("", "Muted", wrap=True)
        self.countdown_card.body.addWidget(self.countdown_text)
        bar_row = QHBoxLayout()
        self.countdown_bar = QProgressBar()
        self.countdown_bar.setRange(0, 1000)
        self.countdown_bar.setTextVisible(False)
        bar_row.addWidget(self.countdown_bar, 1)
        self.countdown_seconds = label("", "Small")
        bar_row.addWidget(self.countdown_seconds)
        self.countdown_card.body.addLayout(bar_row)
        self.countdown_buttons = QHBoxLayout()
        self.countdown_card.body.addLayout(self.countdown_buttons)

        self.alert_card = FloatCard(self.run_area, "AlertCard", width=380)
        alert_head = QHBoxLayout()
        self.alert_icon = QLabel()
        self.alert_icon.setPixmap(icons.pixmap("alert", t.warning, 22))
        alert_head.addWidget(self.alert_icon, 0, Qt.AlignmentFlag.AlignTop)
        alert_text = QVBoxLayout()
        self.alert_title = label("", "SectionTitle", wrap=True)
        self.alert_body = label("", "Muted", wrap=True)
        alert_text.addWidget(self.alert_title)
        alert_text.addWidget(self.alert_body)
        alert_head.addLayout(alert_text, 1)
        self.alert_card.body.addLayout(alert_head)
        self.alert_buttons = QHBoxLayout()
        self.alert_buttons.setSpacing(8)
        self.alert_card.body.addLayout(self.alert_buttons)

        # ---- run log ----
        log_row = QHBoxLayout()
        self.log_toggle = button("Run log", "ghost", "chevron-right")
        self.log_toggle.clicked.connect(lambda _checked=False: self.toggle_log())
        log_row.addWidget(self.log_toggle)
        log_row.addStretch(1)
        layout.addLayout(log_row)
        self.log_view = QPlainTextEdit()
        self.log_view.setObjectName("RunLog")
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(2000)
        self.log_view.setFixedHeight(120)
        self.log_view.setVisible(ctx.settings.run_log_open)
        layout.addWidget(self.log_view)
        self._sync_log_toggle()

        ctx.listeners.append(self._on_changed)
        self.refresh()

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self.run_task is not None and not self.run_task.done()

    def jobs_changed(self) -> None:
        """Saved jobs arrived from outside this page (the shared list)."""
        self._on_changed("jobs")

    def _on_changed(self, kind: str) -> None:
        if kind == "jobs":
            if self.running:
                self._append_new_jobs()  # a job saved in Job Search while the run is going
            else:
                self.refresh_queue()
        self._sync_controls()

    def _append_new_jobs(self) -> None:
        jobs = self.ctx.store.list_saved_jobs(light=True)
        for job in jobs:
            if job.id not in self.queue_items:
                self._add_queue_item(job)
        self.queue_title.setText(f"Job Queue ({len(self.queue_items)})")
        self.queue.setVisible(bool(self.queue_items))
        self.queue_empty.setVisible(not self.queue_items)

    def _add_queue_item(self, job: SavedJob) -> None:
        widget = QueueItem(job)
        item = QListWidgetItem(self.queue)
        item.setSizeHint(widget.sizeHint())
        self.queue.setItemWidget(item, widget)
        self.queue_items[job.id] = widget

    def refresh(self) -> None:
        self.refresh_queue()
        self._sync_controls()

    def refresh_queue(self) -> None:
        jobs = self.ctx.store.list_saved_jobs(light=True)
        self.queue.clear()
        self.queue_items.clear()
        for job in jobs:
            self._add_queue_item(job)
        self.queue_title.setText(f"Job Queue ({len(jobs)})")
        self.queue.setVisible(bool(jobs))
        self.queue_empty.setVisible(not jobs)

    def start_problem(self) -> str | None:
        """Why Start cannot be pressed, in plain words; None when it can."""
        assets = self.ctx.store.assets()
        if not (assets.prompt_text or "").strip():
            return "Import a prompt first."
        if self.ctx.store.saved_job_count() == 0:
            return "The queue is empty. Save some jobs in Job Search first."
        if not self.ctx.output_schema:
            return "The web app has not been reached yet, so the resume format is unknown. Check your connection."
        if self.browser is None:
            return "The embedded browser is not available."
        return None

    def _sync_controls(self) -> None:
        assets = self.ctx.store.assets()
        prompt = assets.prompt_filename or "none"
        resume = assets.original_resume_filename or "none"
        self.chips.setText(f"Prompt: {prompt}  ·  Resume: {resume}")
        self.clear_resume.setVisible(bool(assets.original_resume_filename) and not self.running)
        self.import_prompt.setEnabled(not self.running)
        self.import_resume.setEnabled(not self.running)
        self.download.setEnabled(self.last_resume_id is not None)
        self.download.setToolTip(
            "The PDF and DOCX of the last resume generated in this run"
            if self.last_resume_id
            else "Available once this run has generated a resume"
        )
        if self.running:
            self.start.setText("Stop")
            self.start.setProperty("variant", "danger")
            self.start.setIcon(icons.icon("stop", "#FFFFFF", 14))
            self.start.setEnabled(True)
            self.start.setToolTip("Stop at the next safe point")
        else:
            problem = self.start_problem()
            self.start.setText("Start")
            self.start.setProperty("variant", "primary")
            self.start.setIcon(icons.icon("play", "#FFFFFF", 14))
            self.start.setEnabled(problem is None)
            self.start.setToolTip(problem or "Start generating resumes for the saved jobs")
        repolish(self.start)

    # ------------------------------------------------------------------
    # Imports and downloads
    # ------------------------------------------------------------------

    def choose_prompt(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Import Prompt", "", "Prompt files (*.txt *.md)")
        if path:
            self.load_prompt(Path(path))

    def load_prompt(self, path: Path) -> bool:
        try:
            text = read_prompt_file(path)
        except ResumeTextError as error:
            self.toasts.show(str(error), "error")
            return False
        self.ctx.store.set_prompt(text, path.name)
        self.toasts.show(f"Prompt imported: {path.name} ({len(text):,} characters)", "success")
        self._sync_controls()
        return True

    def choose_resume(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Import Resume", "", "Resume files (*.pdf *.docx *.txt *.md)")
        if path:
            spawn(self.load_resume(Path(path)))

    async def load_resume(self, path: Path) -> bool:
        self.import_resume.setEnabled(False)
        try:
            # Reading a long PDF takes a moment: keep it off the UI thread.
            text = await asyncio.get_running_loop().run_in_executor(None, extract_resume_text, path)
        except ResumeTextError as error:
            self.toasts.show(str(error), "error")
            return False
        finally:
            self.import_resume.setEnabled(not self.running)
        self.ctx.store.set_original_resume(text, path.name)
        self.toasts.show(f"Resume imported: {path.name} ({len(text):,} characters)", "success")
        self._sync_controls()
        return True

    def remove_resume(self) -> None:
        self.ctx.store.set_original_resume(None, None)
        self._sync_controls()

    async def download_last(self, fmt: str) -> None:
        resume = self.ctx.store.get_resume(self.last_resume_id) if self.last_resume_id else None
        if resume is None:
            return
        try:
            source = await self.ctx.documents.get(
                record_id=resume.id,
                resume_json=resume.resume_json,
                company=resume.company,
                role=resume.role,
                fmt=fmt,  # type: ignore[arg-type]
            )
            folder = self.ctx.settings.downloads_dir
            target = save_copy(source, folder)
        except DocumentError as error:
            self.toasts.show(error.message, "error")
            return
        except OSError:
            self.toasts.show("Could not write to the Downloads folder. Choose another one in Settings.", "error")
            return
        self.toasts.show(
            f"Saved to {folder.name}\\{target.name}",
            "success",
            "Open folder",
            lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder))),
        )

    # ------------------------------------------------------------------
    # Start / Stop
    # ------------------------------------------------------------------

    def start_or_stop(self) -> None:
        if self.running:
            self.stop()
            return
        if self.start_problem() is not None:
            return
        dialog = StartDialog(
            self, self.ctx.settings.start_mode, self.ctx.store.attention_count(), self.ctx.settings.automation_seconds
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self.ctx.settings.start_mode = dialog.mode
        self.begin(dialog.mode, dialog.include_attention)

    def begin(self, mode: Mode, include_attention: bool = False) -> asyncio.Future[Any]:
        self.last_resume_id = None
        self.refresh_queue()
        self.board.setCurrentWidget(self.run_area)
        self.review_panel.hide()
        self.log_view.clear()
        self.log(f"Run started: {'Automation' if mode == 'automation' else 'Human Check'} mode")
        kwargs: dict[str, Any] = {"include_attention": include_attention}
        if self.chatgpt_url:
            kwargs["chatgpt_url"] = self.chatgpt_url
        self.pipeline = Pipeline(self.ctx, self, mode, **kwargs)
        self.pipeline.on_generated = self._generated
        self.run_task = spawn(self._run())
        self._sync_controls()
        self.running_changed.emit(True)
        return self.run_task

    def _generated(self, resume_id: str) -> None:
        self.last_resume_id = resume_id
        self._sync_controls()

    async def _run(self) -> RunSummary:
        assert self.pipeline is not None
        try:
            summary = await self.pipeline.run()
        finally:
            self._hide_cards()
            self.review_panel.hide()
            self.status.setText("")
            self.board.setCurrentIndex(0)
            self.run_task = None
            self.pipeline = None
            self.refresh_queue()
            self._sync_controls()
            self.running_changed.emit(False)
        self.log(
            f"Run finished: {summary.generated} generated, {len(summary.skipped)} skipped, "
            f"{len(summary.attention)} need attention"
        )
        self.show_summary(summary)
        return summary

    def stop(self) -> None:
        if self.pipeline is None:
            return
        self.log("Stop requested…")
        self.status.setText("Stopping at the next safe point…")
        self.pipeline.stop()
        self._countdown_finish(False)
        self._resolve("stop")

    def show_summary(self, summary: RunSummary) -> None:
        dialog = SummaryDialog(self, summary)
        dialog.open_generated.connect(self.open_generated.emit)
        self.summary_dialog = dialog
        dialog.open()

    # ------------------------------------------------------------------
    # PipelineHost
    # ------------------------------------------------------------------

    def set_step(self, step: int, status: str) -> None:
        self.stepper.set_step(step)
        self.status.setText(status)
        noisy = status.startswith(("Pasting prompt", "Waiting for ChatGPT"))
        if not noisy and (step, status) != self._last_logged_step:
            self._last_logged_step = (step, status)
            self.log(f"[{step}/3] {status}")

    def log(self, text: str) -> None:
        self.log_view.appendPlainText(f"{datetime.now().strftime('%H:%M:%S')}  {text}")

    def mark(self, job_id: str, state: str) -> None:
        widget = self.queue_items.get(job_id)
        if widget is None:
            return
        widget.set_state(state)
        if state == "current":
            for index in range(self.queue.count()):
                if self.queue.itemWidget(self.queue.item(index)) is widget:
                    self.queue.scrollToItem(self.queue.item(index))

    def _wait(self) -> asyncio.Future[Any]:
        self._answer = asyncio.get_running_loop().create_future()
        return self._answer

    def _resolve(self, value: Any) -> None:
        answer, self._answer = self._answer, None
        if answer is not None and not answer.done():
            answer.set_result(value)

    async def review(self, job: SavedJob, data: ReviewData, mode: Mode) -> ReviewData | None:
        self.review_panel.load(data)
        self.review_panel.show()
        self._place_cards()
        future = self._wait()
        if mode == "automation":
            self._countdown_start(
                self.ctx.settings.automation_seconds,
                "Continuing in {n} seconds…",
                on_done=lambda finished: self._review_next() if finished else None,
                title="Automation Running",
                buttons=[("Stop", "danger", self.stop)],
            )
        else:
            self._show_alert(
                "Please check extracted JD and role, company name",
                "Make sure the extracted information is correct before continuing",
                [("Cancel Job", "", lambda: self._resolve(None)), ("Next  →", "primary", self._review_next)],
                anchor="review",
            )
        try:
            result = await future
        finally:
            self._hide_cards()
            self.review_panel.hide()
        return None if result in (None, "stop") else self.review_panel.collect()

    def _review_next(self) -> None:
        mode = self.pipeline.mode if self.pipeline else "human"
        problem = self.review_panel.problem()
        if problem is not None and mode == "human":
            # Refuse Next and say why; the alert stays so the fields beneath can be fixed.
            self.review_panel.show_problem(problem)
            return
        self._resolve("next")

    def _review_edited(self) -> None:
        # Typing in a field means the user took over: the countdown must not send half an edit.
        if self._countdown_timer.isActive() and self._answer is not None and self.review_panel.isVisible():
            self._countdown_finish(False)
            self._show_alert(
                "Countdown paused",
                "You edited the extracted information. Press Next when it is correct.",
                [("Cancel Job", "", lambda: self._resolve(None)), ("Next  →", "primary", self._force_next)],
                anchor="review",
            )

    def _force_next(self) -> None:
        problem = self.review_panel.problem()
        if problem is not None:
            self.review_panel.show_problem(problem)
            return
        self._resolve("next")

    async def blocked(self, job: SavedJob, message: str, mode: Mode) -> str:
        future = self._wait()
        if mode == "human":
            self._show_alert(
                f"This page needs you: {message}.",
                "Fix it in the browser on the left, then press Retry.",
                [
                    ("Cancel Job", "", lambda: self._resolve("cancel")),
                    ("Skip for now", "", lambda: self._resolve("skip")),
                    ("Retry", "primary", lambda: self._resolve("retry")),
                ],
            )
        else:
            self._countdown_start(
                self.ctx.config.attention_grace_seconds,
                f"{message[:1].upper()}{message[1:]}. Setting this job aside in {{n}} seconds…",
                on_done=lambda finished: self._resolve("skip") if finished else None,
                title="Needs your attention",
                buttons=[
                    ("Skip", "", lambda: self._resolve("skip")),
                    ("I fixed it, retry", "primary", lambda: self._resolve("retry")),
                ],
            )
        try:
            result = await future
        finally:
            self._hide_cards()
        return "skip" if result in (None, "stop") else str(result)

    async def chatgpt_blocked(self, message: str) -> str:
        future = self._wait()
        self._show_alert(
            message,
            "The run is paused. Finish it yourself in the browser below, then press Retry.",
            [("Stop run", "", lambda: self._resolve("stop")), ("Retry", "primary", lambda: self._resolve("retry"))],
        )
        try:
            result = await future
        finally:
            self._hide_cards()
        return "retry" if result == "retry" else "stop"

    async def web_unreachable(self, message: str) -> str:
        future = self._wait()
        self._show_alert(
            "The web app could not validate the resume",
            f"{message} Nothing is saved until it has been validated.",
            [("Stop run", "", lambda: self._resolve("stop")), ("Retry", "primary", lambda: self._resolve("retry"))],
        )
        try:
            result = await future
        finally:
            self._hide_cards()
        return "retry" if result == "retry" else "stop"

    async def json_failed(self, attempt: int, issues: list[dict[str, Any]], raw: str, mode: Mode) -> str:
        dialog = JsonFailedDialog(self, attempt, issues, raw)
        self.json_dialog = dialog
        future = self._wait()
        dialog.finished.connect(lambda _code: self._resolve(dialog.choice))
        dialog.open()
        try:
            result = await future
        finally:
            if shiboken6.isValid(dialog) and dialog.isVisible():
                dialog.done(0)
        return "retry" if result == "retry" else "skip"

    async def countdown(self, seconds: int, text: str) -> None:
        future = self._wait()
        self._countdown_start(
            seconds,
            text,
            on_done=lambda finished: self._resolve("done"),
            title="Automation Running",
            buttons=[("Stop", "danger", self.stop)],
        )
        try:
            await future
        finally:
            self._hide_cards()

    # ------------------------------------------------------------------
    # Floating cards
    # ------------------------------------------------------------------

    def _fill_buttons(self, row: QHBoxLayout, buttons: list[tuple[str, str, Any]]) -> list[QPushButton]:
        while row.count():
            item = row.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        made = []
        for text, variant, action in buttons:
            widget = button(text, variant)
            widget.clicked.connect(lambda _checked=False, run=action: run())
            row.addWidget(widget, 1)
            made.append(widget)
        return made

    def _show_alert(self, title: str, text: str, buttons: list[tuple[str, str, Any]], anchor: str = "board") -> None:
        self.alert_title.setText(title)
        self.alert_body.setText(text)
        self.alert_card.buttons = self._fill_buttons(self.alert_buttons, buttons)  # type: ignore[attr-defined]
        self.alert_card.show()
        self.alert_card.raise_()
        self._place_cards()
        QTimer.singleShot(0, self, self._place_cards)  # again once the layout has settled

    def _countdown_start(
        self, seconds: float, text: str, on_done: Any, title: str, buttons: list[tuple[str, str, Any]]
    ) -> None:
        self._countdown_total = max(0.1, float(seconds))
        self._countdown_left = self._countdown_total
        self._countdown_done = on_done
        self._countdown_text = text
        self.countdown_title.setText(title)
        self.countdown_card.buttons = self._fill_buttons(self.countdown_buttons, buttons)  # type: ignore[attr-defined]
        self._countdown_paint()
        self.countdown_card.show()
        self.countdown_card.raise_()
        self._place_cards()
        QTimer.singleShot(0, self, self._place_cards)
        self._countdown_timer.start()

    def _countdown_paint(self) -> None:
        whole = max(0, int(self._countdown_left + 0.999))
        self.countdown_text.setText(self._countdown_text.replace("{n}", str(whole)))
        self.countdown_seconds.setText(f"{whole}s")
        self.countdown_bar.setValue(int(1000 * self._countdown_left / self._countdown_total))

    def _countdown_tick(self) -> None:
        self._countdown_left -= 0.1
        if self._countdown_left <= 0:
            self._countdown_finish(True)
            return
        self._countdown_paint()

    def _countdown_finish(self, finished: bool) -> None:
        if not self._countdown_timer.isActive() and self._countdown_done is None:
            return
        self._countdown_timer.stop()
        self.countdown_card.hide()
        self._place_cards()
        done, self._countdown_done = self._countdown_done, None
        if done is not None:
            done(finished)

    def _hide_cards(self) -> None:
        self._countdown_timer.stop()
        self._countdown_done = None
        self.countdown_card.hide()
        self.alert_card.hide()
        self.review_panel.inset.setFixedHeight(0)

    @staticmethod
    def _fit(card: FloatCard, width: int) -> None:
        """Size a card to its (wrapped) text at the given width."""
        card.setFixedWidth(width)
        card.setFixedHeight(max(40, card.layout().totalHeightForWidth(width)))

    def _place_cards(self) -> None:
        area = self.run_area
        top = self.splitter.y() + 8
        panel = self.review_panel
        over_panel = panel.isVisible()
        inset = 0

        if over_panel:
            # Over the top of the review panel, in space the panel keeps free for it.
            left = self.splitter.x() + panel.x() + 8
            width = max(260, panel.width() - 16)
            if self.countdown_card.isVisible():
                self._fit(self.countdown_card, width)
                self.countdown_card.move(left, top)
                inset = max(inset, self.countdown_card.height() + 6)
            if self.alert_card.isVisible():
                self._fit(self.alert_card, width)
                self.alert_card.move(left, top)
                inset = max(inset, self.alert_card.height() + 6)
        else:
            if self.countdown_card.isVisible():
                # The top-right corner of the board; it does not block the view.
                self._fit(self.countdown_card, 300)
                self.countdown_card.move(area.width() - self.countdown_card.width() - 12, top)
            if self.alert_card.isVisible():
                self._fit(self.alert_card, min(460, max(300, area.width() - 40)))
                self.alert_card.move((area.width() - self.alert_card.width()) // 2, top)

        if panel.inset.height() != inset:
            panel.inset.setFixedHeight(inset)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._place_cards()

    # ------------------------------------------------------------------
    # Run log
    # ------------------------------------------------------------------

    def toggle_log(self) -> None:
        self.log_view.setVisible(not self.log_view.isVisible())
        self.ctx.settings.run_log_open = self.log_view.isVisible()
        self._sync_log_toggle()

    def _sync_log_toggle(self) -> None:
        name = "chevron-down" if self.log_view.isVisible() else "chevron-right"
        self.log_toggle.setIcon(icons.icon(name, tokens().text_secondary, 14))


# ---------------------------------------------------------------------------
# Dialogs
# ---------------------------------------------------------------------------


class ModeCard(QFrame):
    clicked = Signal()

    def __init__(self, icon_name: str, title: str, text: str) -> None:
        super().__init__()
        self.setObjectName("ModeCard")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.icon_name = icon_name
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(14)
        self.icon = QLabel()
        layout.addWidget(self.icon)
        body = QVBoxLayout()
        body.setSpacing(2)
        self.title = label(title, "SectionTitle")
        body.addWidget(self.title)
        body.addWidget(label(text, "Muted", wrap=True))
        layout.addLayout(body, 1)
        self.radio = QLabel()
        layout.addWidget(self.radio, 0, Qt.AlignmentFlag.AlignTop)
        self.set_selected(False)

    def set_selected(self, selected: bool) -> None:
        t = tokens()
        self.selected = selected
        self.setProperty("selected", selected)
        self.icon.setPixmap(icons.pixmap(self.icon_name, t.primary if selected else t.muted, 24))
        self.radio.setPixmap(
            icons.pixmap("dot" if selected else "circle", t.primary if selected else t.placeholder, 18)
        )
        repolish(self)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self.clicked.emit()
        super().mouseReleaseEvent(event)


class StartDialog(Modal):
    def __init__(self, parent: QWidget, mode: str, attention_count: int, seconds: int) -> None:
        super().__init__(parent, 440)
        self.mode: Mode = "human" if mode == "human" else "automation"
        self.add_title("Start Resume Generation", "Choose how you want to process the saved jobs.")
        self.automation = ModeCard(
            "zap", "Automation", f"Automatically continue after {seconds} seconds for each step."
        )
        self.human = ModeCard("user", "Human Check", "Review extracted job data before continuing.")
        self.automation.clicked.connect(lambda: self.select("automation"))
        self.human.clicked.connect(lambda: self.select("human"))
        self.body.addWidget(self.automation)
        self.body.addWidget(self.human)
        self.include = QCheckBox(f"Include jobs needing attention ({attention_count})")
        self.include.setToolTip("Jobs that were set aside because their page needed you")
        self.include.setVisible(attention_count > 0)
        self.body.addWidget(self.include)
        cancel = button("Cancel")
        self.start = button("Start", "primary")
        cancel.clicked.connect(self.reject)
        self.start.clicked.connect(self.accept)
        self.add_buttons(cancel, self.start)
        self.start.setDefault(True)
        self.select(self.mode)

    def select(self, mode: str) -> None:
        self.mode = "human" if mode == "human" else "automation"
        self.automation.set_selected(self.mode == "automation")
        self.human.set_selected(self.mode == "human")

    @property
    def include_attention(self) -> bool:
        # isHidden(), not isVisible(): this is read after the dialog has closed.
        return not self.include.isHidden() and self.include.isChecked()


class JsonViewer(Modal):
    """ChatGPT's reply, read-only, with the lines the problems point at highlighted."""

    def __init__(self, parent: QWidget, raw: str, issues: list[dict[str, Any]]) -> None:
        super().__init__(parent, 720)
        self.add_title("ChatGPT's reply", f"{len(issues)} problem(s) found by the web app's resume format check.")
        problems = QPlainTextEdit()
        problems.setReadOnly(True)
        problems.setObjectName("Mono")
        problems.setFixedHeight(110)
        problems.setPlainText(
            "\n".join(f"{item.get('path', '(root)')}: {item.get('message', '')}" for item in issues[:50])
        )
        self.body.addWidget(problems)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setObjectName("Mono")
        self.text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.text.setMinimumHeight(320)
        try:
            shown = json.dumps(json.loads(raw), indent=2, ensure_ascii=False)
        except ValueError:
            shown = raw
        self.text.setPlainText(shown[:400_000])
        self.body.addWidget(self.text)
        self._highlight(issues)
        close = button("Close", "primary")
        close.clicked.connect(self.accept)
        self.add_buttons(close)

    def _highlight(self, issues: list[dict[str, Any]]) -> None:
        marker = QTextCharFormat()
        marker.setBackground(QColor("#FEE2E2"))
        document = self.text.document()
        seen: set[str] = set()
        for issue in issues[:50]:
            parts = [
                part
                for part in str(issue.get("path", "")).split(".")
                if part and not part.isdigit() and part != "(root)"
            ]
            if not parts:
                continue
            key = f'"{parts[-1]}"'
            if key in seen:
                continue
            seen.add(key)
            cursor = document.find(key)
            while not cursor.isNull():
                cursor.select(QTextCursor.SelectionType.LineUnderCursor)
                cursor.mergeCharFormat(marker)
                cursor = document.find(key, cursor)


class JsonFailedDialog(Modal):
    def __init__(self, parent: QWidget, attempt: int, issues: list[dict[str, Any]], raw: str) -> None:
        super().__init__(parent, 420)
        self.choice = "skip"
        self.add_icon("alert", "#DC2626", "#FEE2E2")
        title = label("JSON Validation Failed", "ModalTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.body.addWidget(title)
        text = label("Missing or invalid fields detected.", "Muted")
        text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.body.addWidget(text)
        self.attempt = label(f"Attempt {attempt} of 3", "Muted")
        self.attempt.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.body.addWidget(self.attempt)
        skip = button("Skip Job")
        view = button("View JSON")
        self.retry = button("Retry", "primary")
        self.retry.setEnabled(attempt < 3)
        if attempt >= 3:
            self.retry.setToolTip("All 3 attempts have been used")
        skip.clicked.connect(lambda _checked=False: self._choose("skip"))
        self.retry.clicked.connect(lambda _checked=False: self._choose("retry"))
        view.clicked.connect(lambda _checked=False: JsonViewer(self, raw, issues).exec())
        self.add_buttons(skip, view, self.retry)

    def _choose(self, choice: str) -> None:
        self.choice = choice
        self.accept()


class SummaryDialog(Modal):
    open_generated = Signal()

    def __init__(self, parent: QWidget, summary: RunSummary) -> None:
        super().__init__(parent, 460)
        self.add_title("Run stopped" if summary.stopped else "Run finished")
        self.generated = label(
            f"{summary.generated} resume{'s' if summary.generated != 1 else ''} generated", "SectionTitle"
        )
        self.body.addWidget(self.generated)

        counts: dict[str, int] = {}
        for record in summary.skipped:
            counts[record.reason] = counts.get(record.reason, 0) + 1
        self.skipped = label(f"{len(summary.skipped)} skipped")
        self.body.addWidget(self.skipped)
        for reason, count in counts.items():
            self.body.addWidget(label(f"   •  {SKIP_LABELS.get(reason, reason)}: {count}", "Muted"))

        if summary.attention:
            self.body.addWidget(label(f"{len(summary.attention)} need your attention (still in Saved Jobs)"))
            for record in summary.attention[:8]:
                self.body.addWidget(
                    label(f"   •  {record.role} at {record.company}: {record.detail}", "Muted", wrap=True)
                )
            self.body.addWidget(label("Run again in Human Check mode to handle them one by one.", "Small", wrap=True))

        close = button("Close")
        close.clicked.connect(self.accept)
        view = button("Open Generated Resumes", "primary")
        view.clicked.connect(lambda _checked=False: (self.accept(), self.open_generated.emit()))
        self.add_buttons(close, view)
