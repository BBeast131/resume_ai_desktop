"""Admin: the user list, and "View Profile" to see one user's data read-only.

Being an admin is decided by the web app (`role = 'admin'` on the profile) and
checked again by the server on every admin request. Hiding this tab from
other users is a convenience, not the protection.
"""

from __future__ import annotations

from typing import Any

import shiboken6
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHeaderView, QStackedWidget, QVBoxLayout, QWidget

from app.data.types import from_iso
from app.sync.api_client import ApiError
from app.ui.context import AppContext, spawn
from app.ui.pages.saved_jobs import cell, fit_columns, make_table, text_item
from app.ui.widgets.common import EmptyState, PageHeader, SearchBox, ToastHost, button, format_datetime, label

COLUMNS = ["#", "Full Name", "Email", "Joined At", "Generated", "Action"]
#: 100 users a page: up to 2,000 users are listed.
MAX_PAGES = 20


class AdminPage(QWidget):
    #: (user id, full name, e-mail)
    view_profile = Signal(str, str, str)

    def __init__(self, ctx: AppContext, toasts: ToastHost) -> None:
        super().__init__()
        self.ctx = ctx
        self.toasts = toasts
        self.users: list[dict[str, Any]] = []
        self._loaded = False
        self._request = 0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(14)
        refresh = button("Refresh", icon_name="refresh")
        refresh.clicked.connect(lambda _checked=False: self.reload())
        layout.addWidget(PageHeader("Admin - User Management", "Manage all users", refresh))

        self.search = SearchBox("Search users…")
        self.search.search.connect(lambda _text: self.reload())
        layout.addWidget(self.search)

        self.stack = QStackedWidget()
        layout.addWidget(self.stack, 1)
        self.table = make_table(COLUMNS)
        self.table.setSelectionMode(self.table.SelectionMode.NoSelection)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        self.stack.addWidget(self.table)
        self.message = EmptyState("users", "Loading users…", "")
        self.stack.addWidget(self.message)
        self.count = label("", "Muted")
        layout.addWidget(self.count)
        self.stack.setCurrentWidget(self.message)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if not self._loaded:
            self.reload()

    def reload(self) -> None:
        spawn(self.load())

    async def load(self) -> None:
        self._loaded = True
        self._request += 1
        request = self._request
        term = self.search.text().strip()
        try:
            data = await self.ctx.api.admin_users(term)
            items = list(data.get("items") or [])
            page = 1
            while data.get("hasNext") and page < MAX_PAGES and request == self._request:
                page += 1
                data = await self.ctx.api.admin_users(term, page=page)
                items += list(data.get("items") or [])
            data = {**data, "items": items}
        except ApiError as error:
            if request != self._request or not shiboken6.isValid(self):
                return
            self.users = []
            self.message.title.setText("The user list could not be loaded")
            self.message.text.setText(error.message)
            self.stack.setCurrentWidget(self.message)
            self.count.setText("")
            return
        if request != self._request or not shiboken6.isValid(self):
            return  # a newer search has been started
        self.users = [item for item in data.get("items") or [] if isinstance(item, dict)]
        total = int(data.get("total") or len(self.users))
        self.count.setText(f"{total} user{'s' if total != 1 else ''}")
        if not self.users:
            self.message.title.setText("No users found")
            self.message.text.setText("Nobody matches that search.")
            self.stack.setCurrentWidget(self.message)
            return
        self.stack.setCurrentWidget(self.table)
        self._fill()

    def _fill(self) -> None:
        table = self.table
        table.setRowCount(0)
        table.setRowCount(len(self.users))
        for index, user in enumerate(self.users):
            name = str(user.get("fullName") or "")
            email = str(user.get("email") or "")
            try:
                joined = format_datetime(from_iso(str(user.get("createdAt"))))
            except ValueError:
                joined = "—"
            table.setItem(index, 0, text_item(str(index + 1)))
            table.setItem(index, 1, text_item(name + ("  (admin)" if user.get("role") == "admin" else "")))
            table.setItem(index, 2, text_item(email))
            table.setItem(index, 3, text_item(joined))
            generated = text_item(str(user.get("applicationCount") or 0))
            generated.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            table.setItem(index, 4, generated)
            view = button("View Profile", "soft", "eye", icon_px=13)
            user_id = str(user.get("id") or "")
            view.clicked.connect(lambda _checked=False, uid=user_id, n=name, e=email: self.view_profile.emit(uid, n, e))
            table.setCellWidget(index, 5, cell(view))
        fit_columns(table, (1, 2))

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        fit_columns(self.table, (1, 2))
