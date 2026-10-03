"""Generated Resumes: every resume, with its status, links and documents."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import shiboken6
from PySide6.QtCore import QEasingCurve, QEvent, QPoint, QPropertyAnimation, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtPdf import QPdfDocument
from PySide6.QtPdfWidgets import QPdfView
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QStackedWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from app.data.store import ResumeFilter
from app.services.documents import DocumentError, Format, RenderedDocument, save_copy
from app.services.provider import DataProvider, ResumeRow
from app.services.stats import local_to_utc, period_for
from app.ui import icons
from app.ui.context import AppContext, spawn
from app.ui.pages.saved_jobs import cell, fit_columns, make_table, text_item
from app.ui.theme import STATUS_LABELS, tokens
from app.ui.widgets.common import (
    CopyButton,
    EmptyState,
    PageHeader,
    SearchBox,
    StatusChip,
    StatusCombo,
    ToastHost,
    button,
    format_datetime,
    label,
    labelled_combo,
    shadow,
)

COLUMNS = ["#", "Date", "Company", "Role", "ChatGPT URL", "JD URL", "Resume JSON", "Documents", "Status", "", ""]
PAGE_SIZE = 50
STATUS_OPTIONS = [("all", "All")] + [
    (key, STATUS_LABELS[key]) for key in ("generated", "applied", "shortlisted", "rejected")
]
DATE_OPTIONS = [("any", "Any time"), ("day", "Today"), ("week", "This week"), ("month", "This month")]


def pretty_json(text: str | None) -> str:
    if not text:
        return ""
    try:
        return json.dumps(json.loads(text), indent=2, ensure_ascii=False)
    except ValueError:
        return text


class GeneratedPage(QWidget):
    #: Ask the main window to show a URL in a browser tab.
    open_url = Signal(str)

    def __init__(self, ctx: AppContext, provider: DataProvider, toasts: ToastHost) -> None:
        super().__init__()
        self.ctx = ctx
        self.provider = provider
        self.toasts = toasts
        self.rows: list[ResumeRow] = []
        self.page_index = 0
        #: A filter set from outside (a dashboard card): shown as a chip the user can dismiss.
        self.external: ResumeFilter | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(14)
        layout.addWidget(PageHeader("Generated Resumes", "All generated resumes with status and download options"))

        toolbar = QHBoxLayout()
        toolbar.setSpacing(10)
        self.search = SearchBox("Search company or role…")
        self.search.search.connect(lambda _text: self._filters_changed())
        toolbar.addWidget(self.search, 1)
        status_holder, self.status_filter = labelled_combo("Status:", STATUS_OPTIONS)
        self.status_filter.currentIndexChanged.connect(lambda _index: self._filters_changed())
        toolbar.addWidget(status_holder)
        date_holder, self.date_filter = labelled_combo("Date:", DATE_OPTIONS)
        self.date_filter.setToolTip("When the resume was generated")
        self.date_filter.currentIndexChanged.connect(lambda _index: self._filters_changed())
        toolbar.addWidget(date_holder)
        sort_holder, self.sort = labelled_combo("Sort:", [("newest", "Newest first"), ("oldest", "Oldest first")])
        self.sort.currentIndexChanged.connect(lambda _index: self._filters_changed())
        toolbar.addWidget(sort_holder)
        layout.addLayout(toolbar)

        self.chip_row = QWidget()
        chip_layout = QHBoxLayout(self.chip_row)
        chip_layout.setContentsMargins(0, 0, 0, 0)
        chip_layout.setSpacing(6)
        self.chip = label("", "Chip")
        chip_layout.addWidget(self.chip)
        clear = button("Clear filter", "ghost", "x")
        clear.clicked.connect(lambda _checked=False: self.clear_external())
        chip_layout.addWidget(clear)
        chip_layout.addStretch(1)
        self.chip_row.hide()
        layout.addWidget(self.chip_row)

        self.stack = QStackedWidget()
        layout.addWidget(self.stack, 1)

        self.table = make_table(COLUMNS, row_height=46)
        self.table.setSelectionMode(self.table.SelectionMode.NoSelection)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        if provider.read_only:
            self.table.setColumnHidden(10, True)
        self.stack.addWidget(self.table)

        self.empty = EmptyState(
            "files",
            "No generated resumes yet",
            "Resumes appear here as soon as the generator has saved them."
            if not provider.read_only
            else "This user has no generated resumes in their last sync.",
        )
        self.stack.addWidget(self.empty)
        self.no_match = EmptyState("search", "Nothing matches", "No resume matches these filters.")
        self.stack.addWidget(self.no_match)

        footer = QHBoxLayout()
        self.count = label("", "Muted")
        footer.addWidget(self.count)
        footer.addStretch(1)
        self.previous_button = button("Previous", icon_name="chevron-left")
        self.previous_button.clicked.connect(lambda _checked=False: self._go(-1))
        self.next_button = button("Next", icon_name="chevron-right")
        self.next_button.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self.next_button.clicked.connect(lambda _checked=False: self._go(1))
        self.page_label = label("", "Muted")
        footer.addWidget(self.previous_button)
        footer.addWidget(self.page_label)
        footer.addWidget(self.next_button)
        layout.addLayout(footer)

        self.drawer = DetailDrawer(self)
        self.drawer.hide()
        self.refresh()

    # -- filters ---------------------------------------------------------------------

    def current_filter(self) -> ResumeFilter:
        base = self.external or ResumeFilter()
        status = str(self.status_filter.currentData())
        statuses = base.statuses if self.external is not None else (() if status == "all" else (status,))
        date_from, date_to = base.date_from, base.date_to
        key = str(self.date_filter.currentData())
        if self.external is None and key != "any":
            period = period_for(key, datetime.now())  # type: ignore[arg-type]
            date_from, date_to = local_to_utc(period.start, None), local_to_utc(period.end, None)
        return replace(
            base,
            search=self.search.text().strip(),
            statuses=statuses,
            newest_first=self.sort.currentData() == "newest",
            date_from=date_from,
            date_to=date_to,
        )

    def apply_external(self, filters: ResumeFilter, description: str) -> None:
        """Show what a dashboard card counted: its statuses, its period, its date field."""
        self.external = filters
        self.chip.setText(description)
        self.chip_row.show()
        self.status_filter.setEnabled(False)
        self.date_filter.setEnabled(False)
        self.page_index = 0
        self.refresh()

    def clear_external(self) -> None:
        self.external = None
        self.chip_row.hide()
        self.status_filter.setEnabled(True)
        self.date_filter.setEnabled(True)
        self._filters_changed()

    def _filters_changed(self) -> None:
        self.page_index = 0
        self.refresh()

    def _go(self, step: int) -> None:
        self.page_index = max(0, self.page_index + step)
        self._fill()

    # -- data ------------------------------------------------------------------------

    def refresh(self) -> None:
        filters = self.current_filter()
        self.rows = self.provider.resumes(filters)
        filtered = bool(filters.search or filters.statuses or filters.date_from or filters.date_to)
        if not self.rows:
            self.stack.setCurrentWidget(self.no_match if filtered else self.empty)
        else:
            self.stack.setCurrentWidget(self.table)
        self._fill()
        if self.drawer.isVisible() and self.drawer.row is not None:
            current = next((row for row in self.rows if row.id == self.drawer.row.id), None)
            if current is not None:
                self.drawer.update_status(current.status)

    def _fill(self) -> None:
        total = len(self.rows)
        pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        self.page_index = min(self.page_index, pages - 1)
        start = self.page_index * PAGE_SIZE
        visible = self.rows[start : start + PAGE_SIZE]

        self.count.setText(f"{total} resume{'s' if total != 1 else ''}")
        self.page_label.setText(f"Page {self.page_index + 1} of {pages}")
        self.previous_button.setEnabled(self.page_index > 0)
        self.next_button.setEnabled(self.page_index < pages - 1)
        for widget in (self.previous_button, self.next_button, self.page_label):
            widget.setVisible(pages > 1)

        t = tokens()
        table = self.table
        table.setRowCount(0)
        table.setRowCount(len(visible))
        for index, row in enumerate(visible):
            number = text_item(str(start + index + 1))
            number.setData(Qt.ItemDataRole.UserRole, row.id)
            table.setItem(index, 0, number)
            table.setItem(index, 1, text_item(format_datetime(row.created_at)))
            table.setItem(index, 2, text_item(row.company))
            table.setItem(index, 3, text_item(row.role))

            chat = CopyButton(
                lambda value=row.chat_url or "": value,
                tooltip=row.chat_url or "No ChatGPT link was captured",
                compact=True,
            )
            chat.setEnabled(bool(row.chat_url))
            table.setCellWidget(index, 4, cell(chat))
            table.setCellWidget(
                index, 5, cell(CopyButton(lambda value=row.jd_url: value, tooltip=row.jd_url, compact=True))
            )
            json_copy = button("Copy", "soft", tooltip="Copy the resume JSON")
            json_copy.clicked.connect(
                lambda _checked=False, item=row, widget=json_copy: spawn(self._copy_json(item, widget))
            )
            table.setCellWidget(index, 6, cell(json_copy))

            pdf = button("PDF", "soft", "pdf", tooltip="Save the PDF to your Downloads folder")
            docx = button("DOCX", "soft", "docx", tooltip="Save the Word document to your Downloads folder")
            pdf.clicked.connect(lambda _checked=False, item=row, widget=pdf: spawn(self.download(item, "pdf", widget)))
            docx.clicked.connect(
                lambda _checked=False, item=row, widget=docx: spawn(self.download(item, "docx", widget))
            )
            table.setCellWidget(index, 7, cell(pdf, docx))

            if self.provider.read_only:
                table.setCellWidget(index, 8, cell(StatusChip(row.status)))
            else:
                combo = StatusCombo(row.status)
                combo.status_changed.connect(lambda status, item=row: self.set_status(item.id, status))
                table.setCellWidget(index, 8, cell(combo))

            details = button("Show details", tooltip="Resume preview, job description and JSON")
            details.clicked.connect(lambda _checked=False, item=row: self.show_details(item.id))
            table.setCellWidget(index, 9, cell(details))

            if not self.provider.read_only:
                mark = QLabel()
                if row.sync_error:
                    mark.setPixmap(icons.pixmap("alert", t.warning, 15))
                    mark.setToolTip(f"The web app refused this record: {row.sync_error}")
                elif row.synced:
                    mark.setPixmap(icons.pixmap("check", t.success, 15))
                    mark.setToolTip("Synced to the web app")
                else:
                    mark.setPixmap(icons.pixmap("refresh", t.placeholder, 15))
                    mark.setToolTip("Waiting to sync")
                table.setCellWidget(index, 10, cell(mark, align=Qt.AlignmentFlag.AlignCenter))

        fit_columns(self.table, (2, 3))

    # -- actions ---------------------------------------------------------------------

    def set_status(self, resume_id: str, status: str) -> None:
        if self.provider.read_only:
            return
        self.ctx.store.set_status(resume_id, status)
        self.toasts.show(f"Status set to {STATUS_LABELS.get(status, status)}", "success")
        self.ctx.changed("status")
        self.refresh()

    async def _full(self, row: ResumeRow) -> ResumeRow | None:
        if row.resume_json and row.jd_text is not None:
            return row
        try:
            return await self.provider.resume_detail(row.id)
        except Exception:  # noqa: BLE001 - an ApiError, already worded for the user below
            if self.alive():
                self.toasts.show("That record could not be loaded from the web app.", "error")
            return None

    async def _copy_json(self, row: ResumeRow, widget: QWidget | None = None) -> None:
        full = await self._full(row)
        if full is None or not full.resume_json:
            return
        QGuiApplication.clipboard().setText(pretty_json(full.resume_json))
        self.toasts.show("Resume JSON copied", "success")

    def alive(self) -> bool:
        """False once this page has been deleted (an admin left view-as while something was loading)."""
        return shiboken6.isValid(self)

    async def render(self, row: ResumeRow, fmt: Format) -> RenderedDocument | None:
        """The rendered file in the local cache (not yet copied to Downloads)."""
        full = await self._full(row)
        if full is None or not full.resume_json or not self.alive():
            return None
        prefix = "view-" if self.provider.read_only else ""
        try:
            return await self.ctx.documents.get(
                record_id=prefix + full.id, resume_json=full.resume_json, company=full.company, role=full.role, fmt=fmt
            )
        except DocumentError as error:
            if self.alive():
                self.toasts.show(error.message, "error")
            return None

    async def download(self, row: ResumeRow, fmt: Format, widget: QWidget | None = None) -> Path | None:
        if widget is not None:
            widget.setEnabled(False)
        try:
            source = await self.render(row, fmt)
            if source is None or not self.alive():
                return None
            folder = self.ctx.settings.downloads_dir
            try:
                target = save_copy(source, folder)
            except OSError:
                self.toasts.show(f"Could not write to {folder}. Choose another Downloads folder in Settings.", "error")
                return None
            self.toasts.show(
                f"Saved to {folder.name}\\{target.name}",
                "success",
                "Open folder",
                lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder))),
            )
            return target
        finally:
            if widget is not None and shiboken6.isValid(widget):
                widget.setEnabled(True)  # (the table may have been rebuilt while the file was rendering)

    def show_details(self, resume_id: str) -> None:
        row = next((item for item in self.rows if item.id == resume_id), None)
        if row is not None:
            self.drawer.open(row)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self.drawer.reposition()
        QTimer.singleShot(0, self, lambda: fit_columns(self.table, (2, 3)))

    def eventFilter(self, watched, event: QEvent) -> bool:  # noqa: N802
        return super().eventFilter(watched, event)


class DetailDrawer(QFrame):
    """The details panel that slides in from the right."""

    def __init__(self, page: GeneratedPage) -> None:
        super().__init__(page)
        self.page_ = page
        self.row: ResumeRow | None = None
        self.setObjectName("Drawer")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        shadow(self, blur=40, alpha=50, offset=0)
        read_only = page.provider.read_only

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 18, 22, 18)
        layout.setSpacing(12)

        top = QHBoxLayout()
        self.title = label("", "DrawerTitle", wrap=True, selectable=True)
        top.addWidget(self.title, 1)
        close = button("", "ghost", "x", tooltip="Close (Esc)")
        close.clicked.connect(lambda _checked=False: self.close_drawer())
        top.addWidget(close, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(top)

        self.generated = label("", "Small")
        layout.addWidget(self.generated)

        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(10)
        grid.setColumnStretch(1, 1)

        grid.addWidget(label("Status", "FieldLabel"), 0, 0)
        self.status_holder = QHBoxLayout()
        self.status_combo: StatusCombo | None = None
        self.status_chip: StatusChip | None = None
        if read_only:
            self.status_chip = StatusChip("generated")
            self.status_holder.addWidget(self.status_chip)
        else:
            self.status_combo = StatusCombo("generated")
            self.status_combo.status_changed.connect(self._status_changed)
            self.status_holder.addWidget(self.status_combo)
        self.status_holder.addStretch(1)
        grid.addLayout(self.status_holder, 0, 1, 1, 3)

        self.chat_field = QLineEdit()
        self.chat_field.setReadOnly(True)
        self.jd_field = QLineEdit()
        self.jd_field.setReadOnly(True)
        self.open_buttons = []
        for line, (caption, field) in enumerate((("ChatGPT URL", self.chat_field), ("JD URL", self.jd_field)), start=1):
            grid.addWidget(label(caption, "FieldLabel"), line, 0)
            grid.addWidget(field, line, 1)
            grid.addWidget(CopyButton(field.text), line, 2)
            open_button = button("Open", "soft", "external", icon_px=13)
            open_button.clicked.connect(lambda _checked=False, source=field: self._open(source.text()))
            open_button.setVisible(not read_only)  # the browser is not available when viewing another user
            self.open_buttons.append(open_button)
            grid.addWidget(open_button, line, 3)

        grid.addWidget(label("Documents", "FieldLabel"), 3, 0)
        documents = QHBoxLayout()
        self.pdf_button = button("PDF", "soft", "pdf", tooltip="Save the PDF to your Downloads folder")
        self.docx_button = button("DOCX", "soft", "docx", tooltip="Save the Word document to your Downloads folder")
        self.pdf_button.clicked.connect(lambda _checked=False: self._download("pdf"))
        self.docx_button.clicked.connect(lambda _checked=False: self._download("docx"))
        documents.addWidget(self.pdf_button)
        documents.addWidget(self.docx_button)
        documents.addStretch(1)
        grid.addLayout(documents, 3, 1, 1, 3)
        layout.addLayout(grid)

        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)

        # Composed Resume: the PDF itself, as the web app renders it.
        preview = QWidget()
        preview_layout = QVBoxLayout(preview)
        preview_layout.setContentsMargins(8, 8, 8, 8)
        zoom = QHBoxLayout()
        self.preview_note = label("", "Small")
        zoom.addWidget(self.preview_note, 1)
        zoom_out = button("", "ghost", "zoom-out", tooltip="Zoom out")
        zoom_in = button("", "ghost", "zoom-in", tooltip="Zoom in")
        fit = button("Fit width", "ghost")
        zoom_out.clicked.connect(lambda _checked=False: self._zoom(1 / 1.2))
        zoom_in.clicked.connect(lambda _checked=False: self._zoom(1.2))
        fit.clicked.connect(lambda _checked=False: self.pdf_view.setZoomMode(QPdfView.ZoomMode.FitToWidth))
        for widget in (zoom_out, zoom_in, fit):
            zoom.addWidget(widget)
        preview_layout.addLayout(zoom)
        self.pdf_document = QPdfDocument(self)
        self.pdf_view = QPdfView()
        self.pdf_view.setDocument(self.pdf_document)
        self.pdf_view.setPageMode(QPdfView.PageMode.MultiPage)
        self.pdf_view.setZoomMode(QPdfView.ZoomMode.FitToWidth)
        preview_layout.addWidget(self.pdf_view, 1)
        self.tabs.addTab(preview, "Composed Resume")

        jd = QWidget()
        jd_layout = QVBoxLayout(jd)
        jd_layout.setContentsMargins(8, 8, 8, 8)
        self.jd_text = QPlainTextEdit()
        self.jd_text.setReadOnly(True)
        jd_layout.addWidget(self.jd_text, 1)
        jd_layout.addWidget(
            CopyButton(self.jd_text.toPlainText, "Copy job description"), 0, Qt.AlignmentFlag.AlignRight
        )
        self.tabs.addTab(jd, "Job Description")

        raw = QWidget()
        raw_layout = QVBoxLayout(raw)
        raw_layout.setContentsMargins(8, 8, 8, 8)
        self.json_text = QPlainTextEdit()
        self.json_text.setReadOnly(True)
        self.json_text.setObjectName("Mono")
        self.json_text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        raw_layout.addWidget(self.json_text, 1)
        raw_layout.addWidget(CopyButton(self.json_text.toPlainText, "Copy JSON"), 0, Qt.AlignmentFlag.AlignRight)
        self.tabs.addTab(raw, "Resume JSON")

        self._animation = QPropertyAnimation(self, b"pos", self)
        self._animation.setDuration(180)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)

    # -- geometry --------------------------------------------------------------------

    def _width(self) -> int:
        return max(460, min(680, int(self.page_.width() * 0.56)))

    def reposition(self) -> None:
        width = self._width()
        self.resize(width, self.page_.height())
        if self.isVisible():
            self.move(self.page_.width() - width, 0)

    def open(self, row: ResumeRow) -> None:
        self.row = row
        self.title.setText(row.title)
        self.generated.setText(f"Generated {format_datetime(row.created_at)} · {row.candidate_name}")
        self.update_status(row.status)
        self.chat_field.setText(row.chat_url or "")
        self.chat_field.setCursorPosition(0)
        self.jd_field.setText(row.jd_url)
        self.jd_field.setCursorPosition(0)
        self.jd_text.setPlainText(row.jd_text or "")
        self.json_text.setPlainText(pretty_json(row.resume_json))
        self.pdf_document.close()
        self.preview_note.setText("Rendering the resume…")
        self.tabs.setCurrentIndex(0)

        width = self._width()
        self.resize(width, self.page_.height())
        was_visible = self.isVisible()
        self.show()
        self.raise_()
        if not was_visible:
            self._animation.stop()
            self._animation.setStartValue(QPoint(self.page_.width(), 0))
            self._animation.setEndValue(QPoint(self.page_.width() - width, 0))
            self._animation.start()
        else:
            self.move(self.page_.width() - width, 0)
        self.setFocus()
        spawn(self._load(row))

    async def _load(self, row: ResumeRow) -> None:
        full = await self.page_._full(row)
        if not shiboken6.isValid(self) or self.row is None or self.row.id != row.id:
            return  # another record was opened meanwhile, or the page is gone
        if full is not None:
            self.row = full
            self.jd_text.setPlainText(full.jd_text or "")
            self.json_text.setPlainText(pretty_json(full.resume_json))
        document = await self.page_.render(full or row, "pdf")
        if not shiboken6.isValid(self) or self.row is None or self.row.id != row.id:
            return
        if document is None:
            self.preview_note.setText("The preview is unavailable: the web app could not be reached.")
            return
        self.pdf_document.load(str(document.path))
        pages = self.pdf_document.pageCount()
        self.preview_note.setText(f"{pages} page{'s' if pages != 1 else ''} · {document.filename}")

    def close_drawer(self) -> None:
        self.hide()
        self.row = None
        self.pdf_document.close()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            self.close_drawer()
            return
        super().keyPressEvent(event)

    # -- actions ---------------------------------------------------------------------

    def update_status(self, status: str) -> None:
        if self.status_combo is not None:
            self.status_combo.set_status(status)
        if self.status_chip is not None:
            self.status_chip.set_status(status)

    def _status_changed(self, status: str) -> None:
        if self.row is not None:
            self.page_.set_status(self.row.id, status)

    def _zoom(self, factor: float) -> None:
        current = self.pdf_view.zoomFactor()
        if self.pdf_view.zoomMode() != QPdfView.ZoomMode.Custom:
            # Start from what "fit width" is showing, so the first click is not a jump.
            page_width = self.pdf_document.pagePointSize(0).width() if self.pdf_document.pageCount() else 612
            current = max(0.2, (self.pdf_view.viewport().width() - 24) / max(1.0, page_width * 96 / 72))
            self.pdf_view.setZoomMode(QPdfView.ZoomMode.Custom)
        self.pdf_view.setZoomFactor(max(0.25, min(4.0, current * factor)))

    def _open(self, url: str) -> None:
        if url.startswith(("https://", "http://")):
            self.page_.open_url.emit(url)

    def _download(self, fmt: Format) -> None:
        if self.row is not None:
            widget = self.pdf_button if fmt == "pdf" else self.docx_button
            spawn(self.page_.download(self.row, fmt, widget))


def period_filter(
    statuses: tuple[str, ...], date_field: str, start_local: datetime, end_local: datetime
) -> ResumeFilter:
    """The filter behind a dashboard card: the same rows the card counted."""
    return ResumeFilter(
        statuses=statuses,
        date_field=date_field,  # type: ignore[arg-type]
        date_from=local_to_utc(start_local, None),
        date_to=local_to_utc(end_local, None),
    )


def describe_period(start_local: datetime, end_local: datetime) -> str:
    last = end_local - timedelta(days=1)
    if last.date() == start_local.date():
        return f"{start_local.strftime('%b')} {start_local.day}"
    return f"{start_local.strftime('%b')} {start_local.day} – {last.strftime('%b')} {last.day}"
