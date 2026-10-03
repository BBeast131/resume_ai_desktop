"""Applying the theme to the application."""

from __future__ import annotations

from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication

from app.ui import icons
from app.ui.theme import LIGHT, Tokens, build_stylesheet


def apply_theme(app: QApplication, tokens: Tokens = LIGHT) -> None:
    font = QFont("Segoe UI")
    font.setStyleHint(QFont.StyleHint.SansSerif)
    font.setPixelSize(tokens.font_size)
    app.setFont(font)
    sheet = build_stylesheet(tokens)
    # QSS needs image files for the few glyphs it draws itself.
    sheet = sheet.replace("ICON_CHEVRON_DOWN", icons.icon_file("chevron-down", tokens.muted, 12))
    sheet = sheet.replace("ICON_CHECK_WHITE", icons.icon_file("check", "#FFFFFF", 12))
    sheet = sheet.replace("ICON_X_MUTED", icons.icon_file("x", tokens.muted, 12))
    app.setStyleSheet(sheet)
