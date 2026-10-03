"""Small building blocks used on every page."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime

from PySide6.QtCore import QEvent, QObject, QPoint, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QGuiApplication
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QDialog,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from app.ui import icons
from app.ui.theme import STATUS_COLORS, STATUS_LABELS, tokens


def repolish(widget: QWidget) -> None:
    """Re-apply the stylesheet after a dynamic property changed."""
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    widget.update()


def shadow(widget: QWidget, blur: int = 18, alpha: int = 22, offset: int = 3) -> None:
    effect = QGraphicsDropShadowEffect(widget)
    effect.setBlurRadius(blur)
    effect.setOffset(0, offset)
    effect.setColor(QColor(15, 23, 42, alpha))
    widget.setGraphicsEffect(effect)


def label(text: str = "", name: str = "", *, wrap: bool = False, selectable: bool = False) -> QLabel:
    """A label that always shows its text as plain text: page data is never treated as markup."""
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    if name:
        widget.setObjectName(name)
    widget.setWordWrap(wrap)
    if selectable:
        widget.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return widget


def button(
    text: str = "",
    variant: str = "",
    icon_name: str = "",
    *,
    icon_color: str | None = None,
    tooltip: str = "",
    size: str = "",
    icon_px: int = 16,
) -> QPushButton:
    widget = QPushButton(text)
    widget.setCursor(Qt.CursorShape.PointingHandCursor)
    if variant:
        widget.setProperty("variant", variant)
    if size:
        widget.setProperty("size", size)
    if icon_name:
        t = tokens()
        default = {
            "primary": "#FFFFFF",
            "danger": "#FFFFFF",
            "soft": t.primary,
            "dangerGhost": t.danger,
        }.get(variant, t.text_secondary)
        widget.setIcon(icons.icon(icon_name, icon_color or default, icon_px))
        widget.setIconSize(QSize(icon_px, icon_px))
    if tooltip:
        widget.setToolTip(tooltip)
    return widget


class Card(QFrame):
    def __init__(
        self, parent: QWidget | None = None, *, name: str = "Card", padding: int = 16, with_shadow: bool = True
    ) -> None:
        super().__init__(parent)
        self.setObjectName(name)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(padding, padding, padding, padding)
        self.body.setSpacing(10)
        if with_shadow:
            shadow(self)


class PageHeader(QWidget):
    """Title, grey subtitle under it, and an optional widget on the right."""

    def __init__(self, title: str, subtitle: str, right: QWidget | None = None) -> None:
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        text = QVBoxLayout()
        text.setSpacing(2)
        self.title = label(title, "PageTitle")
        self.subtitle = label(subtitle, "PageSubtitle")
        text.addWidget(self.title)
        text.addWidget(self.subtitle)
        layout.addLayout(text, 1)
        self.right = QHBoxLayout()
        self.right.setSpacing(8)
        layout.addLayout(self.right)
        if right is not None:
            self.right.addWidget(right)


class SegmentedControl(QFrame):
    changed = Signal(str)

    def __init__(self, options: Sequence[tuple[str, str]], current: str | None = None) -> None:
        super().__init__()
        self.setObjectName("Segmented")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(3, 3, 3, 3)
        layout.setSpacing(2)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._buttons: dict[str, QPushButton] = {}
        for key, text in options:
            item = QPushButton(text)
            item.setObjectName("Segment")
            item.setCheckable(True)
            item.setCursor(Qt.CursorShape.PointingHandCursor)
            item.clicked.connect(lambda _checked=False, value=key: self.changed.emit(value))
            self._group.addButton(item)
            self._buttons[key] = item
            layout.addWidget(item)
        self.set_current(current or options[0][0])
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def set_current(self, key: str) -> None:
        item = self._buttons.get(key)
        if item is not None:
            item.setChecked(True)

    def current(self) -> str:
        for key, item in self._buttons.items():
            if item.isChecked():
                return key
        return next(iter(self._buttons))


class SearchBox(QLineEdit):
    """A search field that reports changes after a short pause in typing."""

    search = Signal(str)

    def __init__(self, placeholder: str = "Search…") -> None:
        super().__init__()
        self.setPlaceholderText(placeholder)
        self.setClearButtonEnabled(True)
        self.addAction(icons.icon("search", tokens().placeholder, 16), QLineEdit.ActionPosition.LeadingPosition)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(220)
        self._timer.timeout.connect(lambda: self.search.emit(self.text().strip()))
        self.textChanged.connect(lambda _text: self._timer.start())
        self.setMinimumWidth(220)


class PasswordField(QLineEdit):
    """A password box with a show/hide eye."""

    def __init__(self, placeholder: str = "") -> None:
        super().__init__()
        self.setPlaceholderText(placeholder)
        self.setEchoMode(QLineEdit.EchoMode.Password)
        self._action = self.addAction(icons.icon("eye", tokens().muted, 16), QLineEdit.ActionPosition.TrailingPosition)
        self._action.setToolTip("Show password")
        self._action.triggered.connect(self.toggle)

    def toggle(self) -> None:
        hidden = self.echoMode() == QLineEdit.EchoMode.Password
        self.setEchoMode(QLineEdit.EchoMode.Normal if hidden else QLineEdit.EchoMode.Password)
        self._action.setIcon(icons.icon("eye-off" if hidden else "eye", tokens().muted, 16))
        self._action.setToolTip("Hide password" if hidden else "Show password")


def labelled_combo(
    caption: str, options: Sequence[tuple[str, str]], current: str | None = None
) -> tuple[QWidget, QComboBox]:
    """ "Sort: [Newest first ▾]" as one unit."""
    holder = QWidget()
    layout = QHBoxLayout(holder)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(6)
    layout.addWidget(label(caption, "Muted"))
    combo = QComboBox()
    for key, text in options:
        combo.addItem(text, key)
    if current is not None:
        index = combo.findData(current)
        if index >= 0:
            combo.setCurrentIndex(index)
    combo.setCursor(Qt.CursorShape.PointingHandCursor)
    layout.addWidget(combo)
    return holder, combo


class StatusChip(QLabel):
    """A coloured, read-only status."""

    def __init__(self, status: str) -> None:
        super().__init__()
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.set_status(status)

    def set_status(self, status: str) -> None:
        text, background, border = STATUS_COLORS.get(status, STATUS_COLORS["generated"])
        self.setText(STATUS_LABELS.get(status, status.title()))
        self.setStyleSheet(
            f"background: {background}; color: {text}; border: 1px solid {border}; border-radius: 10px;"
            " padding: 3px 10px; font-weight: 600; font-size: 12px;"
        )


class StatusCombo(QComboBox):
    """The status select: coloured like a chip, saved the moment it changes."""

    status_changed = Signal(str)

    def __init__(self, status: str) -> None:
        super().__init__()
        for key in ("generated", "applied", "shortlisted", "rejected"):
            self.addItem(STATUS_LABELS[key], key)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.set_status(status)
        self.currentIndexChanged.connect(self._on_change)
        widest = max(self.fontMetrics().horizontalAdvance(text) for text in STATUS_LABELS.values())
        self.setMinimumWidth(widest + 52)

    def wheelEvent(self, event) -> None:  # noqa: N802 - scrolling a table must not change a status
        event.ignore()

    def set_status(self, status: str) -> None:
        self.blockSignals(True)
        index = self.findData(status)
        self.setCurrentIndex(index if index >= 0 else 0)
        self.blockSignals(False)
        self._paint()

    def status(self) -> str:
        return str(self.currentData())

    def _paint(self) -> None:
        text, background, border = STATUS_COLORS.get(self.status(), STATUS_COLORS["generated"])
        self.setStyleSheet(
            f"QComboBox {{ background: {background}; color: {text}; border: 1px solid {border};"
            " border-radius: 6px; padding: 4px 24px 4px 10px; font-weight: 600; font-size: 12px; }"
        )

    def _on_change(self, _index: int) -> None:
        self._paint()
        self.status_changed.emit(self.status())


class EmptyState(QWidget):
    def __init__(
        self, icon_name: str, title: str, text: str, action: str = "", on_action: Callable[[], None] | None = None
    ) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.setSpacing(8)
        picture = QLabel()
        picture.setPixmap(icons.pixmap(icon_name, tokens().placeholder, 44))
        picture.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(picture)
        self.title = label(title, "SectionTitle")
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.title)
        self.text = label(text, "Muted", wrap=True)
        self.text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.text.setMaximumWidth(460)
        layout.addWidget(self.text, 0, Qt.AlignmentFlag.AlignCenter)
        self.action: QPushButton | None = None
        if action:
            self.action = button(action, "primary")
            if on_action is not None:
                self.action.clicked.connect(lambda _checked=False: on_action())
            layout.addSpacing(6)
            layout.addWidget(self.action, 0, Qt.AlignmentFlag.AlignCenter)


class CopyButton(QPushButton):
    """[Copy]: puts text on the clipboard and shows a "Copied" tick for a moment."""

    def __init__(
        self, provider: Callable[[], str], text: str = "Copy", tooltip: str = "", compact: bool = False
    ) -> None:
        super().__init__(text)
        self._provider = provider
        self._text = text
        self._compact = compact
        self.setProperty("variant", "soft")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        if not compact:
            self.setIcon(icons.icon("copy", tokens().primary, 13))
            self.setIconSize(QSize(13, 13))
        if tooltip:
            self.setToolTip(tooltip)
        self.clicked.connect(self.copy)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(1400)
        self._timer.timeout.connect(self._reset)

    def copy(self) -> None:
        value = self._provider()
        if not value:
            return
        QGuiApplication.clipboard().setText(value)
        self.setText("✓" if self._compact else "Copied")
        self.setToolTip("Copied") if self._compact else None
        if not self._compact:
            self.setIcon(icons.icon("check", tokens().success, 13))
        self._timer.start()

    def _reset(self) -> None:
        self.setText(self._text)
        if not self._compact:
            self.setIcon(icons.icon("copy", tokens().primary, 13))


def format_datetime(value: datetime | None) -> str:
    """ "Jun 3, 2026 · 14:05" in this PC's time zone: date and time in one cell."""
    if value is None:
        return "—"
    local = value.astimezone()
    return f"{local.strftime('%b')} {local.day}, {local.year} · {local.strftime('%H:%M')}"


def format_ago(value: datetime | None, now: datetime | None = None) -> str:
    if value is None:
        return "never"
    current = now or datetime.now(value.tzinfo)
    seconds = max(0, int((current - value).total_seconds()))
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} min ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} h ago"
    days = hours // 24
    return f"{days} day{'s' if days != 1 else ''} ago"


# ---------------------------------------------------------------------------
# Dialogs
# ---------------------------------------------------------------------------


class Modal(QDialog):
    """A dialog drawn as a white rounded card over a dimmed window."""

    def __init__(self, parent: QWidget | None, width: int = 440) -> None:
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setModal(True)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 18, 18, 18)
        self.card = QFrame()
        self.card.setObjectName("ModalCard")
        self.card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.card.setFixedWidth(width)
        shadow(self.card, blur=40, alpha=70, offset=10)
        outer.addWidget(self.card)
        self.body = QVBoxLayout(self.card)
        self.body.setContentsMargins(24, 22, 24, 22)
        self.body.setSpacing(12)
        self._scrim: QWidget | None = None
        # Dialogs are made per use; without this each one would stay parented to the window for
        # the whole session.
        self.finished.connect(lambda _code: self.deleteLater())

    def showEvent(self, event) -> None:  # noqa: N802
        parent = self.parentWidget()
        if parent is not None:
            window = parent.window()
            self._scrim = QWidget(window)
            self._scrim.setObjectName("Scrim")
            self._scrim.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
            self._scrim.setGeometry(window.rect())
            self._scrim.show()
            self.adjustSize()
            centre = window.mapToGlobal(window.rect().center())
            self.move(centre - QPoint(self.width() // 2, self.height() // 2))
        super().showEvent(event)

    def done(self, result: int) -> None:
        if self._scrim is not None:
            self._scrim.deleteLater()
            self._scrim = None
        super().done(result)

    def add_title(self, title: str, text: str = "") -> None:
        self.body.addWidget(label(title, "ModalTitle", wrap=True))
        if text:
            self.body.addWidget(label(text, "Muted", wrap=True))

    def add_icon(self, icon_name: str, color: str, background: str) -> None:
        circle = QLabel()
        circle.setFixedSize(56, 56)
        circle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        circle.setPixmap(icons.pixmap(icon_name, color, 26))
        circle.setStyleSheet(f"background: {background}; border-radius: 28px;")
        self.body.addWidget(circle, 0, Qt.AlignmentFlag.AlignHCenter)

    def add_buttons(self, *buttons: QPushButton) -> None:
        row = QHBoxLayout()
        row.setSpacing(10)
        for item in buttons:
            item.setProperty("size", "large")
            row.addWidget(item, 1)
        self.body.addSpacing(4)
        self.body.addLayout(row)


def confirm(
    parent: QWidget | None,
    title: str,
    text: str,
    confirm_text: str = "Confirm",
    *,
    danger: bool = False,
    cancel_text: str = "Cancel",
) -> bool:
    dialog = Modal(parent, 400)
    dialog.add_title(title, text)
    cancel = button(cancel_text)
    accept = button(confirm_text, "danger" if danger else "primary")
    cancel.clicked.connect(dialog.reject)
    accept.clicked.connect(dialog.accept)
    dialog.add_buttons(cancel, accept)
    accept.setDefault(True)
    return dialog.exec() == QDialog.DialogCode.Accepted


def inform(parent: QWidget | None, title: str, text: str, ok_text: str = "OK") -> None:
    dialog = Modal(parent, 420)
    dialog.add_title(title, text)
    ok = button(ok_text, "primary")
    ok.clicked.connect(dialog.accept)
    dialog.add_buttons(ok)
    dialog.exec()


# ---------------------------------------------------------------------------
# Toasts
# ---------------------------------------------------------------------------


class Toast(QFrame):
    def __init__(
        self, parent: QWidget, text: str, kind: str, action: str, on_action: Callable[[], None] | None
    ) -> None:
        super().__init__(parent)
        self.setObjectName("Toast")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(10)
        colors = {"success": "#4ADE80", "error": "#F87171", "warning": "#FBBF24", "info": "#93C5FD"}
        names = {"success": "check-circle", "error": "alert", "warning": "alert", "info": "info"}
        picture = QLabel()
        picture.setPixmap(icons.pixmap(names.get(kind, "info"), colors.get(kind, "#93C5FD"), 18))
        layout.addWidget(picture)
        message = label(text, wrap=True)
        message.setMaximumWidth(360)
        layout.addWidget(message, 1)
        if action and on_action is not None:
            act = QPushButton(action)
            act.setCursor(Qt.CursorShape.PointingHandCursor)
            act.clicked.connect(lambda _checked=False: on_action())
            layout.addWidget(act)
        shadow(self, blur=24, alpha=60, offset=6)
        self.text = text
        self.kind = kind


class ToastHost(QObject):
    """Non-blocking messages in the bottom-right corner; each hides itself after 4 seconds."""

    def __init__(self, window: QWidget, duration_ms: int = 4000) -> None:
        super().__init__(window)
        self.window = window
        self.duration_ms = duration_ms
        self.toasts: list[Toast] = []
        self.history: list[tuple[str, str]] = []
        window.installEventFilter(self)

    def show(
        self, text: str, kind: str = "info", action: str = "", on_action: Callable[[], None] | None = None
    ) -> Toast:
        toast = Toast(self.window, text, kind, action, on_action)
        self.toasts.append(toast)
        self.history.append((kind, text))
        del self.history[:-50]
        toast.show()
        toast.raise_()
        self._layout()
        # The timer belongs to the toast, so it dies with it (and with the window).
        timer = QTimer(toast)
        timer.setSingleShot(True)
        timer.timeout.connect(lambda: self._dismiss(toast))
        timer.start(self.duration_ms)
        return toast

    def _dismiss(self, toast: Toast) -> None:
        if toast in self.toasts:
            self.toasts.remove(toast)
            toast.deleteLater()
            self._layout()

    def _layout(self) -> None:
        bottom = self.window.height() - 18
        for toast in reversed(self.toasts):
            toast.adjustSize()
            toast.move(self.window.width() - toast.width() - 18, bottom - toast.height())
            bottom -= toast.height() + 8

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        if watched is self.window and event.type() == QEvent.Type.Resize:
            self._layout()
        return False
