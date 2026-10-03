"""Dashboard: what was generated, applied and shortlisted in the chosen period.

Every number comes from local rows, so it works offline. In the admin
"view as" mode the same page shows another user's rows.
"""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCharts import (
    QBarCategoryAxis,
    QBarSeries,
    QBarSet,
    QChart,
    QChartView,
    QPieSeries,
    QValueAxis,
)
from PySide6.QtCore import QMargins, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QCursor, QFont, QPainter
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QStackedWidget,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from app.data.store import ResumeFilter
from app.services.provider import DataProvider
from app.services.stats import PERIOD_LABELS, DashboardData, compute_dashboard
from app.services.stats import Card as StatCard
from app.ui import icons
from app.ui.context import AppContext
from app.ui.pages.generated import describe_period, period_filter
from app.ui.theme import STATUS_LABELS, tokens
from app.ui.widgets.common import Card, EmptyState, PageHeader, SegmentedControl, label, shadow

MIN_REFRESH_SECONDS = 30


class StatTile(QFrame):
    clicked = Signal()

    def __init__(self, title: str, caption: str, icon_name: str, color: str, soft: str) -> None:
        super().__init__()
        self.setObjectName("StatCard")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Show these resumes in Generated Resumes")
        shadow(self)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(14)

        badge = QLabel()
        badge.setFixedSize(46, 46)
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        badge.setPixmap(icons.pixmap(icon_name, color, 22))
        badge.setStyleSheet(f"background: {soft}; border-radius: 10px;")
        layout.addWidget(badge, 0, Qt.AlignmentFlag.AlignTop)

        text = QVBoxLayout()
        text.setSpacing(1)
        text.addWidget(label(title, "StatLabel"))
        self.value = label("0", "StatValue")
        text.addWidget(self.value)
        self.delta = label("", "Small")
        text.addWidget(self.delta)
        self.caption = label(caption, "Small", wrap=True)
        text.addWidget(self.caption)
        layout.addLayout(text, 1)

    def set_card(self, card: StatCard, period_word: str) -> None:
        self.value.setText(str(card.value))
        t = tokens()
        if card.delta == 0:
            self.delta.setText(f"same as previous {period_word}")
            self.delta.setStyleSheet(f"color: {t.muted};")
        else:
            arrow = "▲" if card.delta > 0 else "▼"
            percent = f" ({abs(card.delta_percent):.0f}%)" if card.delta_percent is not None else ""
            self.delta.setText(f"{arrow} {abs(card.delta)}{percent} vs previous {period_word}")
            self.delta.setStyleSheet(f"color: {t.success if card.delta > 0 else t.danger}; font-weight: 600;")

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class LegendRow(QPushButton):
    """A legend entry that is also a link into Generated Resumes."""

    def __init__(self, color: str, text: str) -> None:
        super().__init__()
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setProperty("variant", "ghost")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(8)
        dot = QLabel()
        dot.setFixedSize(10, 10)
        dot.setStyleSheet(f"background: {color}; border-radius: 5px;")
        layout.addWidget(dot)
        self.name = label(text)
        layout.addWidget(self.name, 1)
        self.amount = label("", "Muted")
        layout.addWidget(self.amount)
        self.setMinimumHeight(28)
        self.setMinimumWidth(176)

    def set_amount(self, text: str) -> None:
        self.amount.setText(text)


class DashboardPage(QWidget):
    #: (filter, description): open Generated Resumes showing what a card or legend entry counted.
    drill = Signal(object, str)
    open_job_search = Signal()

    def __init__(self, ctx: AppContext, provider: DataProvider, subtitle: str | None = None) -> None:
        super().__init__()
        self.ctx = ctx
        self.provider = provider
        self.data: DashboardData | None = None
        self.period_key = ctx.settings.dashboard_period
        t = tokens()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(14)

        self.selector = SegmentedControl([(key, text) for key, text in PERIOD_LABELS.items()], self.period_key)
        self.selector.changed.connect(self.set_period)
        self.header = PageHeader(
            "Dashboard", subtitle or "Overview of your resume generation and application activity", self.selector
        )
        layout.addWidget(self.header)
        self.period_title = label("", "Small")
        layout.addWidget(self.period_title)

        self.stack = QStackedWidget()
        layout.addWidget(self.stack, 1)

        content = QWidget()
        grid = QGridLayout(content)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(14)

        self.generated_tile = StatTile(
            "Generated",
            "Total resumes generated (includes applied, shortlisted, rejected)",
            "files",
            t.primary,
            "#DBEAFE",
        )
        self.applied_tile = StatTile("Applied", "Jobs applied", "send", "#16A34A", "#DCFCE7")
        self.shortlisted_tile = StatTile("Shortlisted", "Jobs shortlisted", "star", "#D97706", "#FEF3C7")
        self.generated_tile.clicked.connect(lambda: self._drill_card("generated"))
        self.applied_tile.clicked.connect(lambda: self._drill_card("applied"))
        self.shortlisted_tile.clicked.connect(lambda: self._drill_card("shortlisted"))
        grid.addWidget(self.generated_tile, 0, 0, 1, 4)
        grid.addWidget(self.applied_tile, 0, 4, 1, 4)
        grid.addWidget(self.shortlisted_tile, 0, 8, 1, 4)

        # ---- Activity Overview ----
        activity = Card()
        top = QHBoxLayout()
        top.addWidget(label("Activity Overview", "SectionTitle"))
        top.addStretch(1)
        for color, text in (
            (t.chart_generated, "Generated"),
            (t.chart_applied, "Applied"),
            (t.chart_shortlisted, "Shortlisted"),
        ):
            dot = QLabel()
            dot.setFixedSize(10, 10)
            dot.setStyleSheet(f"background: {color}; border-radius: 5px;")
            top.addWidget(dot)
            top.addWidget(label(text, "Small"))
            top.addSpacing(8)
        activity.body.addLayout(top)

        self.bar_chart = QChart()
        self.bar_chart.legend().hide()
        self.bar_chart.setBackgroundVisible(False)
        self.bar_chart.setMargins(QMargins(0, 0, 0, 0))
        self.bar_chart.layout().setContentsMargins(0, 0, 0, 0)
        self.bar_chart.setAnimationOptions(QChart.AnimationOption.NoAnimation)
        self.bar_view = QChartView(self.bar_chart)
        self.bar_view.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.bar_view.setStyleSheet("background: transparent; border: none;")
        self.bar_view.setMinimumHeight(220)
        activity.body.addWidget(self.bar_view, 1)
        grid.addWidget(activity, 1, 0, 1, 7)

        # ---- Status Breakdown ----
        breakdown = Card()
        breakdown.body.addWidget(label("Status Breakdown", "SectionTitle"))
        breakdown.body.addWidget(label("(of Generated Resumes)", "Small"))
        row = QHBoxLayout()
        row.setSpacing(6)

        donut_holder = QWidget()
        donut_holder.setMinimumSize(170, 170)
        donut_layout = QGridLayout(donut_holder)
        donut_layout.setContentsMargins(0, 0, 0, 0)
        self.pie_chart = QChart()
        self.pie_chart.legend().hide()
        self.pie_chart.setBackgroundVisible(False)
        self.pie_chart.setMargins(QMargins(0, 0, 0, 0))
        self.pie_chart.layout().setContentsMargins(0, 0, 0, 0)
        self.pie_view = QChartView(self.pie_chart)
        self.pie_view.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.pie_view.setStyleSheet("background: transparent; border: none;")
        donut_layout.addWidget(self.pie_view, 0, 0)
        centre = QWidget()
        centre.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        centre_layout = QVBoxLayout(centre)
        centre_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        centre_layout.setSpacing(0)
        self.total = label("0")
        self.total.setStyleSheet("font-size: 22px; font-weight: 700; background: transparent;")
        self.total.setAlignment(Qt.AlignmentFlag.AlignCenter)
        total_caption = label("Total", "Small")
        total_caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
        centre_layout.addWidget(self.total)
        centre_layout.addWidget(total_caption)
        donut_layout.addWidget(centre, 0, 0)
        row.addWidget(donut_holder, 1)

        legend = QVBoxLayout()
        legend.setSpacing(2)
        legend.addStretch(1)
        self.legend_rows: dict[str, LegendRow] = {}
        for status, color in self._status_colors().items():
            entry = LegendRow(color, STATUS_LABELS[status])
            entry.clicked.connect(lambda _checked=False, key=status: self._drill_status(key))
            self.legend_rows[status] = entry
            legend.addWidget(entry)
        legend.addStretch(1)
        row.addLayout(legend, 0)
        breakdown.body.addLayout(row, 1)
        grid.addWidget(breakdown, 1, 7, 1, 5)
        grid.setRowStretch(1, 1)
        for column in range(12):
            grid.setColumnStretch(column, 1)
        self.stack.addWidget(content)

        self.empty = EmptyState(
            "dashboard",
            "Nothing here yet",
            "Your numbers appear as soon as you generate your first resume."
            if not provider.read_only
            else "This user has no activity in their last sync.",
            "" if provider.read_only else "Start by searching jobs",
            self.open_job_search.emit,
        )
        self.stack.addWidget(self.empty)

        self._last_refresh: datetime | None = None
        self._timer = QTimer(self)
        self._timer.setInterval(MIN_REFRESH_SECONDS * 1000)
        self._timer.timeout.connect(self._tick)
        self.refresh()

    @staticmethod
    def _status_colors() -> dict[str, str]:
        t = tokens()
        return {
            "applied": t.chart_generated,
            "shortlisted": t.chart_shortlisted,
            "rejected": t.chart_rejected,
            "generated": t.chart_applied,
        }

    # -- refresh ---------------------------------------------------------------------

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self.refresh()
        self._timer.start()

    def hideEvent(self, event) -> None:  # noqa: N802
        super().hideEvent(event)
        self._timer.stop()

    def _tick(self) -> None:
        # Only so that "today" rolls over at midnight; never faster than every 30 seconds.
        if self.isVisible():
            self.refresh()

    def set_period(self, key: str) -> None:
        if key not in PERIOD_LABELS:
            return
        self.period_key = key
        self.selector.set_current(key)
        self.ctx.settings.dashboard_period = key
        self.refresh()

    def refresh(self, now: datetime | None = None) -> None:
        current = now or datetime.now().astimezone()
        rows = self.provider.resumes(ResumeFilter())
        self.data = compute_dashboard(rows, self.period_key, current)  # type: ignore[arg-type]
        self._last_refresh = current
        self._render(self.data, has_any=bool(rows))

    def _render(self, data: DashboardData, has_any: bool) -> None:
        word = self.period_key
        self.period_title.setText(f"{PERIOD_LABELS[self.period_key]}: {data.period.title}")
        self.generated_tile.set_card(data.generated, word)
        self.applied_tile.set_card(data.applied, word)
        self.shortlisted_tile.set_card(data.shortlisted, word)
        self.stack.setCurrentIndex(0 if has_any else 1)
        self._render_bars(data)
        self._render_donut(data)

    def _render_bars(self, data: DashboardData) -> None:
        t = tokens()
        chart = self.bar_chart
        chart.removeAllSeries()
        for axis in chart.axes():
            chart.removeAxis(axis)

        series = QBarSeries()
        series.setBarWidth(0.72)
        buckets = data.period.buckets
        for name, color, values in (
            ("Generated", t.chart_generated, data.series_generated),
            ("Applied", t.chart_applied, data.series_applied),
            ("Shortlisted", t.chart_shortlisted, data.series_shortlisted),
        ):
            bar = QBarSet(name)
            bar.setColor(QColor(color))
            bar.setBorderColor(QColor(color))
            bar.append([float(value) for value in values])
            bar.hovered.connect(
                lambda on, index, label_=name, numbers=values: self._bar_tip(on, index, label_, numbers)
            )
            series.append(bar)
        chart.addSeries(series)

        font = QFont()
        font.setPixelSize(11)
        # Hourly and monthly charts have many buckets: label every few so the axis stays readable.
        step = 1 if len(buckets) <= 8 else (3 if len(buckets) <= 24 else 5)
        categories = [
            bucket.label if index % step == 0 else " " * (index + 1)  # distinct blanks: categories must be unique
            for index, bucket in enumerate(buckets)
        ]
        x_axis = QBarCategoryAxis()
        x_axis.append(categories)
        x_axis.setLabelsFont(font)
        x_axis.setLabelsColor(QColor(t.muted))
        x_axis.setGridLineVisible(False)
        x_axis.setLineVisible(False)
        chart.addAxis(x_axis, Qt.AlignmentFlag.AlignBottom)
        series.attachAxis(x_axis)

        top = max([1, *data.series_generated, *data.series_applied, *data.series_shortlisted])
        ceiling = max(4, ((top + 3) // 4) * 4)
        y_axis = QValueAxis()
        y_axis.setRange(0, ceiling)
        y_axis.setTickCount(5)
        y_axis.setLabelFormat("%d")
        y_axis.setLabelsFont(font)
        y_axis.setLabelsColor(QColor(t.muted))
        y_axis.setGridLineColor(QColor(t.border))
        y_axis.setLineVisible(False)
        chart.addAxis(y_axis, Qt.AlignmentFlag.AlignLeft)
        series.attachAxis(y_axis)

    def _bar_tip(self, on: bool, index: int, name: str, values: tuple[int, ...]) -> None:
        if not on or self.data is None or not 0 <= index < len(values):
            QToolTip.hideText()
            return
        bucket = self.data.period.buckets[index]
        when = f"{bucket.start.strftime('%b')} {bucket.start.day}"
        if self.period_key == "day":
            when += f", {bucket.start.strftime('%H:00')}"
        QToolTip.showText(QCursor.pos(), f"{when}\n{name}: {values[index]}")

    def _render_donut(self, data: DashboardData) -> None:
        t = tokens()
        chart = self.pie_chart
        chart.removeAllSeries()
        total = data.breakdown_total
        self.total.setText(str(total))

        series = QPieSeries()
        series.setHoleSize(0.62)
        series.setPieSize(0.92)
        colors = self._status_colors()
        if total == 0:
            placeholder = series.append("", 1)
            placeholder.setColor(QColor(t.border))
            placeholder.setBorderColor(QColor(t.card))
        for status, color in colors.items():
            count = data.breakdown.get(status, 0)
            percent = f"{round(count / total * 100)}%" if total else "0%"
            self.legend_rows[status].set_amount(f"{count} ({percent})")
            if count:
                piece = series.append(STATUS_LABELS[status], count)
                piece.setColor(QColor(color))
                piece.setBorderColor(QColor(t.card))
                piece.setBorderWidth(2)
                piece.hovered.connect(
                    lambda on, text=f"{STATUS_LABELS[status]}: {count} ({percent})": (
                        QToolTip.showText(QCursor.pos(), text) if on else QToolTip.hideText()
                    )
                )
                piece.clicked.connect(lambda key=status: self._drill_status(key))
        chart.addSeries(series)

    # -- drill-down ------------------------------------------------------------------

    def _range_text(self) -> str:
        assert self.data is not None
        return describe_period(self.data.period.start, self.data.period.end)

    def _drill_card(self, card: str) -> None:
        if self.data is None:
            return
        period = self.data.period
        if card == "generated":
            filters = period_filter((), "created_at", period.start, period.end)
            text = f"Generated · {self._range_text()}"
        elif card == "applied":
            filters = period_filter(("applied", "shortlisted", "rejected"), "applied_at", period.start, period.end)
            text = f"Applied · {self._range_text()}"
        else:
            filters = period_filter(("shortlisted",), "shortlisted_at", period.start, period.end)
            text = f"Shortlisted · {self._range_text()}"
        self.drill.emit(filters, text)

    def _drill_status(self, status: str) -> None:
        if self.data is None:
            return
        period = self.data.period
        filters = period_filter((status,), "created_at", period.start, period.end)
        self.drill.emit(filters, f"{STATUS_LABELS[status]} · generated {self._range_text()}")
