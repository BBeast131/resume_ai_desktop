"""Login and Register: a centred card on a soft blue gradient.

These are web app accounts (Supabase Auth). The password is typed here by the
user, sent once over HTTPS to Supabase, and never stored or logged; only the
refresh token is kept, in the operating system's credential store.
"""

from __future__ import annotations

import re

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.config import AppConfig
from app.services.auth import AuthError, AuthService
from app.ui.context import spawn
from app.ui.widgets.common import PasswordField, button, label, repolish, shadow

EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")
MIN_PASSWORD = 8


def _field(caption: str, widget: QLineEdit, layout: QVBoxLayout) -> QLabel:
    layout.addWidget(label(caption, "FieldLabel"))
    layout.addWidget(widget)
    error = label("", "ErrorText", wrap=True)
    error.hide()
    layout.addWidget(error)
    layout.addSpacing(4)
    return error


class _Form(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.layout_ = QVBoxLayout(self)
        self.layout_.setContentsMargins(0, 0, 0, 0)
        self.layout_.setSpacing(5)
        self.errors: dict[QLineEdit, QLabel] = {}
        self.message = label("", "ErrorText", wrap=True)
        self.message.hide()

    def add(self, caption: str, widget: QLineEdit) -> None:
        self.errors[widget] = _field(caption, widget, self.layout_)
        widget.textEdited.connect(lambda _text, w=widget: self.set_error(w, ""))

    def set_error(self, widget: QLineEdit, text: str) -> None:
        error = self.errors[widget]
        error.setText(text)
        error.setVisible(bool(text))
        widget.setProperty("invalid", bool(text))
        repolish(widget)

    def clear_errors(self) -> None:
        for widget in self.errors:
            self.set_error(widget, "")
        self.set_message("")

    def set_message(self, text: str, ok: bool = False) -> None:
        self.message.setText(text)
        self.message.setVisible(bool(text))
        self.message.setStyleSheet("color: #166534;" if ok else "")


class AuthWindow(QWidget):
    #: Emitted after a successful sign-in; the session is on the AuthService.
    signed_in = Signal()

    def __init__(self, config: AppConfig, auth: AuthService) -> None:
        super().__init__()
        self.config = config
        self.auth = auth
        self.busy = False
        self.setObjectName("AuthBackground")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setWindowTitle("Resume AI")
        self.resize(980, 700)
        self.setMinimumSize(520, 620)

        outer = QVBoxLayout(self)
        outer.setAlignment(Qt.AlignmentFlag.AlignCenter)

        card = QFrame()
        card.setObjectName("AuthCard")
        card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        card.setFixedWidth(400)
        shadow(card, blur=48, alpha=40, offset=12)
        outer.addWidget(card)

        body = QVBoxLayout(card)
        body.setContentsMargins(34, 30, 34, 28)
        body.setSpacing(4)

        brand = label("RESUME AI", "AuthBrand")
        brand.setAlignment(Qt.AlignmentFlag.AlignCenter)
        body.addWidget(brand)
        sub = label("Automated Resume Builder", "AuthSub")
        sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        body.addWidget(sub)
        body.addSpacing(18)

        # Both forms live in the card; only one is shown, so the card hugs the visible one.
        self.pages = [self._build_login(), self._build_register()]
        for page in self.pages:
            body.addWidget(page)
        self.current_page = 0
        self.show_page(0)

        if not config.auth_configured:
            self.login_form.set_message(
                "Setup needed: copy .env.example to .env and fill in SUPABASE_URL and SUPABASE_PUBLISHABLE_KEY "
                "(the same public values the web app uses), then restart."
            )
            self.sign_in_button.setEnabled(False)
            self.create_button.setEnabled(False)

    # -- Login ---------------------------------------------------------------------

    def _build_login(self) -> QWidget:
        form = _Form()
        self.login_form = form
        self.login_email = QLineEdit()
        self.login_email.setPlaceholderText("you@example.com")
        self.login_password = PasswordField("Enter your password")
        form.add("Email", self.login_email)
        form.add("Password", self.login_password)
        form.layout_.addWidget(form.message)

        self.sign_in_button = button("Sign In", "primary", size="large")
        self.sign_in_button.clicked.connect(self.submit_login)
        self.login_password.returnPressed.connect(self.submit_login)
        self.login_email.returnPressed.connect(self.login_password.setFocus)
        form.layout_.addSpacing(6)
        form.layout_.addWidget(self.sign_in_button)
        form.layout_.addSpacing(10)
        form.layout_.addWidget(self._switch_row("Don't have an account?", "Create account", 1))
        return form

    def _build_register(self) -> QWidget:
        form = _Form()
        self.register_form = form
        self.register_name = QLineEdit()
        self.register_name.setPlaceholderText("John Doe")
        self.register_email = QLineEdit()
        self.register_email.setPlaceholderText("you@example.com")
        self.register_password = PasswordField("Enter your password")
        self.register_confirm = PasswordField("Confirm your password")
        form.add("Full Name", self.register_name)
        form.add("Email", self.register_email)
        form.add("Password", self.register_password)
        form.add("Confirm Password", self.register_confirm)
        form.layout_.addWidget(form.message)

        self.create_button = button("Create Account", "primary", size="large")
        self.create_button.clicked.connect(self.submit_register)
        self.register_confirm.returnPressed.connect(self.submit_register)
        form.layout_.addSpacing(6)
        form.layout_.addWidget(self.create_button)
        form.layout_.addSpacing(10)
        form.layout_.addWidget(self._switch_row("Already have an account?", "Sign in", 0))
        return form

    def _switch_row(self, text: str, link: str, target: int) -> QWidget:
        from PySide6.QtWidgets import QHBoxLayout

        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addStretch(1)
        layout.addWidget(label(text, "Muted"))
        action = QPushButton(link)
        action.setObjectName("Link")
        action.setCursor(Qt.CursorShape.PointingHandCursor)
        action.clicked.connect(lambda _checked=False: self.show_page(target))
        layout.addWidget(action)
        layout.addStretch(1)
        if target == 1:
            self.to_register = action
        else:
            self.to_login = action
        return row

    def show_page(self, index: int) -> None:
        self.current_page = index
        for position, page in enumerate(self.pages):
            page.setVisible(position == index)
        (self.login_email if index == 0 else self.register_name).setFocus()

    def set_busy(self, busy: bool) -> None:
        self.busy = busy
        self.sign_in_button.setEnabled(not busy and self.config.auth_configured)
        self.create_button.setEnabled(not busy and self.config.auth_configured)
        self.sign_in_button.setText("Signing in…" if busy and self.current_page == 0 else "Sign In")
        self.create_button.setText("Creating account…" if busy and self.current_page == 1 else "Create Account")

    # -- Validation ------------------------------------------------------------------

    def validate_login(self) -> bool:
        form = self.login_form
        form.clear_errors()
        ok = True
        if not EMAIL_PATTERN.match(self.login_email.text().strip()):
            form.set_error(self.login_email, "Enter a valid e-mail address.")
            ok = False
        if not self.login_password.text():
            form.set_error(self.login_password, "Enter your password.")
            ok = False
        return ok

    def validate_register(self) -> bool:
        form = self.register_form
        form.clear_errors()
        ok = True
        name = self.register_name.text().strip()
        if len(name) < 2:
            form.set_error(self.register_name, "Enter your full name (at least 2 characters).")
            ok = False
        elif len(name) > 120:
            form.set_error(self.register_name, "Full name must be 120 characters or fewer.")
            ok = False
        if not EMAIL_PATTERN.match(self.register_email.text().strip()):
            form.set_error(self.register_email, "Enter a valid e-mail address.")
            ok = False
        password = self.register_password.text()
        if len(password) < MIN_PASSWORD:
            form.set_error(self.register_password, f"Use at least {MIN_PASSWORD} characters.")
            ok = False
        if self.register_confirm.text() != password:
            form.set_error(self.register_confirm, "The two passwords do not match.")
            ok = False
        return ok

    # -- Submit ----------------------------------------------------------------------

    def submit_login(self) -> None:
        if self.busy or not self.validate_login():
            return
        spawn(self._login())

    async def _login(self) -> None:
        self.set_busy(True)
        try:
            await self.auth.sign_in(self.login_email.text(), self.login_password.text())
        except AuthError as error:
            self.login_form.set_message(error.message)
            return
        finally:
            self.set_busy(False)
        self.login_password.clear()
        self.signed_in.emit()

    def submit_register(self) -> None:
        if self.busy or not self.validate_register():
            return
        spawn(self._register())

    async def _register(self) -> None:
        self.set_busy(True)
        try:
            session = await self.auth.sign_up(
                self.register_name.text(), self.register_email.text(), self.register_password.text()
            )
        except AuthError as error:
            self.register_form.set_message(error.message)
            return
        finally:
            self.set_busy(False)

        self.register_password.clear()
        self.register_confirm.clear()
        if session is None:
            # The project requires e-mail confirmation before the first sign-in.
            self.login_email.setText(self.register_email.text().strip())
            self.show_page(0)
            self.login_form.set_message("Check your inbox to confirm your e-mail, then sign in.", ok=True)
            return
        self.signed_in.emit()
