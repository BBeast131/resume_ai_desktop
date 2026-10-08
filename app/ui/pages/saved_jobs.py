"""Saved Jobs: the queue the generator works through, oldest first."""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QHBoxLayout,
    QHeaderView,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.browser.browser import plain_tip
from app.services.provider import DataProvider, JobRow
from app.ui.context import AppContext
from app.ui.widgets.common import (
    EmptyState,
    PageHeader,
    SearchBox,
    ToastHost,
    button,
    confirm,
    format_datetime,
    label,
    labelled_combo,
)

SOURCE_LABELS = {"jobright": "JobRight", "hiring_cafe": "HiringCafe", "other": "Other"}
COLUMNS = ["#", "Company", "Role", "Source", "Attention", "Saved At", "Job URL", "Action"]


def cell(*widgets: QWidget, align: Qt.AlignmentFlag = Qt.AlignmentFlag.AlignLeft) -> QWidget:
    holder = QWidget()
    layout = QHBoxLayout(holder)
    layout.setContentsMargins(4, 2, 2, 2)
    layout.setSpacing(6)
    if align == Qt.AlignmentFlag.AlignCenter:
        layout.addStretch(1)
    for widget in widgets:
        layout.addWidget(widget)
    layout.addStretch(1)
    return holder


def text_item(text: str, tooltip: str = "") -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
    item.setToolTip(plain_tip(tooltip or text))
    return item


def make_table(columns: list[str], row_height: int = 44) -> QTableWidget:
    table = QTableWidget(0, len(columns))
    table.setHorizontalHeaderLabels(columns)
    table.verticalHeader().setVisible(False)
    table.verticalHeader().setDefaultSectionSize(row_height)
    table.setShowGrid(False)
    table.setAlternatingRowColors(False)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    table.setMouseTracking(True)
    table.setWordWrap(False)
    table.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
    table.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
    header = table.horizontalHeader()
    header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    header.setHighlightSections(False)
    header.setSectionsClickable(False)
    return table


def fit_columns(table: QTableWidget, stretch: tuple[int, ...], min_stretch: int = 112) -> None:
    """Give every column the width its content needs, and share what is left between the `stretch` columns.

    Measured from the widgets themselves, so it holds for any font or display scale. When the window is too
    narrow for everything, the table scrolls sideways instead of clipping buttons.
    """
    header = table.horizontalHeader()
    bold = header.font()
    bold.setBold(True)
    from PySide6.QtGui import QFontMetrics

    header_metrics = QFontMetrics(bold)
    metrics = table.fontMetrics()
    used = 0
    for column in range(table.columnCount()):
        if table.isColumnHidden(column) or column in stretch:
            continue
        title = table.horizontalHeaderItem(column)
        width = header_metrics.horizontalAdvance(title.text() if title else "") + 24
        for row in range(table.rowCount()):
            widget = table.cellWidget(row, column)
            if widget is not None:
                widget.ensurePolished()
                for child in widget.findChildren(QWidget):
                    child.ensurePolished()
                widget.layout().invalidate()
                # The stylesheet pads every cell by 6 px on each side, cell widgets included.
                width = max(width, widget.sizeHint().width() + 14)
            else:
                item = table.item(row, column)
                if item is not None and item.text():
                    width = max(width, metrics.horizontalAdvance(item.text()) + 24)
        table.setColumnWidth(column, width)
        used += width
    visible = [column for column in stretch if not table.isColumnHidden(column)]
    if visible:
        share = max(min_stretch, (table.viewport().width() - used) // len(visible))
        for column in visible:
            table.setColumnWidth(column, share)


class SavedJobsPage(QWidget):
    #: Ask the main window to open a URL in the Job Search browser.
    open_in_job_search = Signal(str)

    def __init__(self, ctx: AppContext, provider: DataProvider, toasts: ToastHost) -> None:
        super().__init__()
        self.ctx = ctx
        self.provider = provider
        self.toasts = toasts
        self.rows: list[JobRow] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(14)
        self.run_active = False
        self.check_duplicates = button("Check duplicates", "soft", "copy")
        self.check_duplicates.clicked.connect(self._check_duplicates)
        self.check_duplicates.setVisible(not provider.read_only)
        layout.addWidget(
            PageHeader("Saved Jobs", "Jobs saved from JobRight.ai and HiringCafe.com", self.check_duplicates)
        )

        toolbar = QHBoxLayout()
        toolbar.setSpacing(10)
        self.search = SearchBox("Search saved jobs…")
        self.search.search.connect(lambda _text: self.refresh())
        toolbar.addWidget(self.search, 1)
        self.attention_only = QCheckBox("Needs attention")
        self.attention_only.setToolTip("Only jobs that were set aside because their page needed you")
        self.attention_only.toggled.connect(lambda _on: self.refresh())
        toolbar.addWidget(self.attention_only)
        sort_holder, self.sort = labelled_combo("Sort:", [("oldest", "Oldest first"), ("newest", "Newest first")])
        self.sort.currentIndexChanged.connect(lambda _index: self.refresh())
        toolbar.addWidget(sort_holder)
        self.count = label("", "Muted")
        toolbar.addWidget(self.count)
        self.remove_selected = button("Remove selected", "danger", "trash")
        self.remove_selected.clicked.connect(self._remove_selected)
        self.remove_selected.hide()
        toolbar.addWidget(self.remove_selected)
        layout.addLayout(toolbar)

        self.stack = QStackedWidget()
        layout.addWidget(self.stack, 1)

        self.table = make_table(COLUMNS)
        self.table.setSelectionMode(
            QAbstractItemView.SelectionMode.NoSelection
            if provider.read_only
            else QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        if provider.read_only:
            self.table.setColumnHidden(7, True)
        self.stack.addWidget(self.table)

        self.empty = EmptyState(
            "bookmark",
            "No saved jobs yet",
            "Open Job Search, browse JobRight.ai or HiringCafe.com, and click a job. "
            "Every job you open is saved here automatically."
            if not provider.read_only
            else "This user has no saved jobs in their last sync.",
        )
        self.stack.addWidget(self.empty)
        self._sync_duplicate_button()
        self.refresh()

    # -- data ------------------------------------------------------------------------

    def refresh(self) -> None:
        self.rows = self.provider.saved_jobs(
            search=self.search.text().strip(),
            newest_first=self.sort.currentData() == "newest",
            attention_only=self.attention_only.isChecked(),
        )
        filtered = bool(self.search.text().strip()) or self.attention_only.isChecked()
        self.count.setText(f"{len(self.rows)} job{'s' if len(self.rows) != 1 else ''}")
        if not self.rows and not filtered:
            self.stack.setCurrentWidget(self.empty)
            return
        self.stack.setCurrentWidget(self.table)
        self._fill()

    def _fill(self) -> None:
        table = self.table
        table.setRowCount(0)
        table.setRowCount(len(self.rows))
        for index, job in enumerate(self.rows):
            number = text_item(str(index + 1))
            number.setData(Qt.ItemDataRole.UserRole, job.id)
            table.setItem(index, 0, number)
            table.setItem(index, 1, text_item(job.company or "—"))
            table.setItem(index, 2, text_item(job.role or "—"))

            source = label(SOURCE_LABELS.get(job.source_site, "Other"), "Chip")
            if job.shared:
                shared = label("Shared", "SharedChip")
                shared.setToolTip("Found by another profile. Your list keeps its own progress on it.")
                table.setCellWidget(index, 3, cell(source, shared))
            else:
                table.setCellWidget(index, 3, cell(source))

            if job.attention_reason:
                reason = job.attention_reason if len(job.attention_reason) <= 34 else job.attention_reason[:33] + "…"
                badge = label(f"Needs attention: {reason}", "AttentionBadge")
                badge.setToolTip(
                    f"This job was set aside: {plain_tip(job.attention_reason)}. Run the generator in Human "
                    "Check mode to handle it, or tick “Include jobs needing attention” when you start."
                )
                table.setCellWidget(index, 4, cell(badge))
            else:
                table.setItem(index, 4, text_item(""))

            table.setItem(index, 5, text_item(format_datetime(job.created_at)))

            copy = button("Copy", "soft", "copy", tooltip=plain_tip(job.url), icon_px=13)
            copy.clicked.connect(lambda _checked=False, url=job.url, widget=copy: self._copy(url, widget))
            widgets: list[QWidget] = [copy]
            if not self.provider.read_only:
                open_button = button("", "ghost", "external", tooltip="Open in Job Search browser")
                open_button.clicked.connect(lambda _checked=False, url=job.url: self.open_in_job_search.emit(url))
                widgets.append(open_button)
            table.setCellWidget(index, 6, cell(*widgets))

            if not self.provider.read_only:
                remove = button("", "dangerGhost", "trash", tooltip="Remove")
                remove.clicked.connect(lambda _checked=False, item=job: self._remove([item]))
                table.setCellWidget(index, 7, cell(remove, align=Qt.AlignmentFlag.AlignCenter))
        self._selection_changed()
        fit_columns(self.table, (1, 2))

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        QTimer.singleShot(0, self, lambda: fit_columns(self.table, (1, 2)))

    # -- actions ---------------------------------------------------------------------

    def set_run_active(self, active: bool) -> None:
        """A generation run is working through this list: the duplicate check waits for it."""
        self.run_active = active
        self._sync_duplicate_button()

    def _sync_duplicate_button(self) -> None:
        self.check_duplicates.setEnabled(not self.run_active)
        self.check_duplicates.setToolTip(
            "Available when the generation run has finished"
            if self.run_active
            else "Remove saved jobs that are the same job as an older saved one, or that already have a resume"
        )

    def _check_duplicates(self) -> None:
        if self.provider.read_only or self.run_active:
            return
        report = self.ctx.store.remove_duplicate_saved_jobs()
        if report.removed == 0:
            self.toasts.show("No duplicates found", "info")
            return
        parts = []
        if report.same_as_saved:
            parts.append(f"{report.same_as_saved} already saved")
        if report.already_generated:
            parts.append(f"{report.already_generated} already generated")
        plural = "s" if report.removed != 1 else ""
        self.toasts.show(f"{report.removed} duplicate job{plural} removed ({', '.join(parts)})", "success")
        self.ctx.changed("jobs")
        self.refresh()

    def _copy(self, url: str, widget: QWidget) -> None:
        QGuiApplication.clipboard().setText(url)
        self.toasts.show("Job URL copied", "success")

    def selected_ids(self) -> list[str]:
        ids = []
        for index in sorted({item.row() for item in self.table.selectedItems()}):
            item = self.table.item(index, 0)
            if item is not None:
                ids.append(str(item.data(Qt.ItemDataRole.UserRole)))
        return ids

    def _selection_changed(self) -> None:
        count = len(self.selected_ids()) if not self.provider.read_only else 0
        self.remove_selected.setVisible(count > 1)
        self.remove_selected.setText(f"Remove selected ({count})")

    def _remove_selected(self) -> None:
        ids = set(self.selected_ids())
        self._remove([job for job in self.rows if job.id in ids])

    def _remove(self, jobs: list[JobRow]) -> None:
        if not jobs or self.provider.read_only:
            return
        if len(jobs) == 1:
            title = "Remove this job?"
            text = f"{jobs[0].role or 'This job'} at {jobs[0].company or 'unknown company'} will leave the queue."
        else:
            title = f"Remove {len(jobs)} jobs?"
            text = "They will leave the queue. You can save them again from Job Search."
        if not confirm(self, title, text, "Remove", danger=True):
            return
        removed = self.ctx.store.remove_saved_jobs([job.id for job in jobs])
        self.toasts.show(f"Removed {removed} job{'s' if removed != 1 else ''}", "success")
        self.ctx.changed("jobs")
        self.refresh()
