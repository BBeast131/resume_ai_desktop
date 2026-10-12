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


def shift_held() -> bool:
    return bool(QGuiApplication.keyboardModifiers() & Qt.KeyboardModifier.ShiftModifier)


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
        layout.addLayout(toolbar)

        # Above the table: tick jobs in the first column, then remove them in one go.
        self.checked: set[str] = set()
        self._anchor_row: int | None = None
        self._filling = False
        self.selection_bar = QWidget()
        bar = QHBoxLayout(self.selection_bar)
        bar.setContentsMargins(0, 0, 0, 0)
        bar.setSpacing(10)
        self.select_all = QCheckBox("Select all")
        self.select_all.setToolTip("Tick every job shown in the table")
        self.select_all.clicked.connect(self._toggle_all)
        bar.addWidget(self.select_all)
        self.selected_count = label("", "Muted")
        bar.addWidget(self.selected_count)
        bar.addStretch(1)
        self.clear_selection = button("Clear", "ghost")
        self.clear_selection.setToolTip("Untick every job")
        self.clear_selection.clicked.connect(lambda: self._set_checked(set()))
        bar.addWidget(self.clear_selection)
        self.remove_selected = button("Remove selected", "danger", "trash")
        self.remove_selected.setToolTip("Remove every ticked job from Saved Jobs")
        self.remove_selected.clicked.connect(self._remove_selected)
        bar.addWidget(self.remove_selected)
        self.selection_bar.setVisible(not provider.read_only)
        layout.addWidget(self.selection_bar)

        self.stack = QStackedWidget()
        layout.addWidget(self.stack, 1)

        self.table = make_table(COLUMNS)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        self.table.itemChanged.connect(self._item_changed)
        self.table.cellClicked.connect(self._cell_clicked)
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
        # Only jobs still on screen stay ticked, so nothing hidden by a search is removed unseen.
        self.checked &= {job.id for job in self.rows}
        self._anchor_row = None
        if not self.rows and not filtered:
            self.stack.setCurrentWidget(self.empty)
            self._update_selection_bar()
            return
        self.stack.setCurrentWidget(self.table)
        self._fill()

    def _fill(self) -> None:
        table = self.table
        self._filling = True
        table.setRowCount(0)
        table.setRowCount(len(self.rows))
        for index, job in enumerate(self.rows):
            number = text_item(str(index + 1))
            number.setData(Qt.ItemDataRole.UserRole, job.id)
            if not self.provider.read_only:
                number.setFlags(number.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                number.setCheckState(Qt.CheckState.Checked if job.id in self.checked else Qt.CheckState.Unchecked)
                number.setToolTip("Tick to select. Shift-click ticks a range.")
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
        self._filling = False
        self._update_selection_bar()
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

    # -- ticking jobs ---------------------------------------------------------------

    def selected_ids(self) -> list[str]:
        """The ticked jobs, in table order."""
        return [job.id for job in self.rows if job.id in self.checked]

    def _row_item(self, row: int) -> QTableWidgetItem | None:
        return self.table.item(row, 0)

    def _item_changed(self, item: QTableWidgetItem) -> None:
        if self._filling or item.column() != 0 or self.provider.read_only:
            return
        row = item.row()
        on = item.checkState() == Qt.CheckState.Checked
        shift = shift_held()
        rows = [row]
        if shift and self._anchor_row is not None and self._anchor_row != row:
            low, high = sorted((self._anchor_row, row))
            rows = list(range(low, high + 1))
        for index in rows:
            if 0 <= index < len(self.rows):
                (self.checked.add if on else self.checked.discard)(self.rows[index].id)
        self._anchor_row = row
        if len(rows) > 1:
            self._apply_checks()
        self._update_selection_bar()

    def _cell_clicked(self, row: int, column: int) -> None:
        """A click on the row's text (not on its buttons) ticks or unticks it too."""
        if self.provider.read_only or column == 0:
            return
        item = self._row_item(row)
        if item is None:
            return
        item.setCheckState(
            Qt.CheckState.Unchecked if item.checkState() == Qt.CheckState.Checked else Qt.CheckState.Checked
        )

    def _apply_checks(self) -> None:
        self._filling = True
        for index, job in enumerate(self.rows):
            item = self._row_item(index)
            if item is not None:
                item.setCheckState(Qt.CheckState.Checked if job.id in self.checked else Qt.CheckState.Unchecked)
        self._filling = False

    def _set_checked(self, ids: set[str]) -> None:
        self.checked = set(ids)
        self._anchor_row = None
        self._apply_checks()
        self._update_selection_bar()

    def _toggle_all(self) -> None:
        everyone = {job.id for job in self.rows}
        self._set_checked(set() if everyone and everyone <= self.checked else everyone)

    def _update_selection_bar(self) -> None:
        count = len(self.selected_ids())
        total = len(self.rows)
        self.select_all.setEnabled(total > 0)
        self.select_all.setCheckState(
            Qt.CheckState.Unchecked
            if count == 0
            else Qt.CheckState.Checked
            if count == total
            else Qt.CheckState.PartiallyChecked
        )
        self.selected_count.setText(f"{count} selected" if count else "Tick jobs to remove several at once")
        self.clear_selection.setVisible(count > 0)
        self.remove_selected.setEnabled(count > 0)
        self.remove_selected.setText(f"Remove selected ({count})" if count else "Remove selected")

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
        self.checked -= {job.id for job in jobs}
        self.toasts.show(f"Removed {removed} job{'s' if removed != 1 else ''}", "success")
        self.ctx.changed("jobs")
        self.refresh()
