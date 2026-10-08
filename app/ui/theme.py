"""The look of the app, defined once.

`Tokens` holds every colour and size; `build_stylesheet` turns a set of tokens
into the application's QSS. The light theme below matches the design. A dark
theme is a second `Tokens` value away: nothing else hard-codes a colour.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Tokens:
    # Sidebar
    sidebar: str = "#0F172A"
    sidebar_hover: str = "#1E293B"
    sidebar_text: str = "#E2E8F0"
    sidebar_muted: str = "#94A3B8"
    sidebar_divider: str = "#1E293B"
    # Brand
    primary: str = "#2563EB"
    primary_hover: str = "#1D4ED8"
    primary_pressed: str = "#1E40AF"
    primary_soft: str = "#EFF6FF"
    primary_soft_border: str = "#BFDBFE"
    # Surfaces
    workspace: str = "#F5F7FB"
    card: str = "#FFFFFF"
    border: str = "#E4E7EC"
    border_strong: str = "#D0D5DD"
    field: str = "#FFFFFF"
    subtle: str = "#F8FAFC"
    hover: str = "#F1F5F9"
    # Text
    text: str = "#0F172A"
    text_secondary: str = "#334155"
    muted: str = "#64748B"
    placeholder: str = "#94A3B8"
    # Feedback
    danger: str = "#DC2626"
    danger_hover: str = "#B91C1C"
    danger_soft: str = "#FEF2F2"
    danger_border: str = "#FECACA"
    success: str = "#16A34A"
    success_soft: str = "#DCFCE7"
    warning: str = "#D97706"
    warning_soft: str = "#FEF3C7"
    warning_border: str = "#FDE68A"
    # Charts
    chart_generated: str = "#2563EB"
    chart_applied: str = "#22C55E"
    chart_shortlisted: str = "#F59E0B"
    chart_rejected: str = "#EF4444"
    # Shape
    radius: int = 8
    radius_small: int = 6
    font_family: str = '"Segoe UI", "Inter", "Helvetica Neue", Arial, sans-serif'
    font_size: int = 13
    mono_family: str = '"Cascadia Mono", "Consolas", "DejaVu Sans Mono", monospace'


LIGHT = Tokens()

#: Status → (text colour, background, border). Generated green, Applied blue,
#: Shortlisted amber, Rejected red.
STATUS_COLORS: dict[str, tuple[str, str, str]] = {
    "generated": ("#166534", "#DCFCE7", "#BBF7D0"),
    "applied": ("#1D4ED8", "#DBEAFE", "#BFDBFE"),
    "shortlisted": ("#B45309", "#FEF3C7", "#FDE68A"),
    "rejected": ("#B91C1C", "#FEE2E2", "#FECACA"),
}
STATUS_LABELS: dict[str, str] = {
    "generated": "Generated",
    "applied": "Applied",
    "shortlisted": "Shortlisted",
    "rejected": "Rejected",
}

SIDEBAR_EXPANDED = 236
SIDEBAR_COLLAPSED = 48

_current = LIGHT


def tokens() -> Tokens:
    return _current


def build_stylesheet(t: Tokens = LIGHT) -> str:
    return f"""
* {{
    font-family: {t.font_family};
    font-size: {t.font_size}px;
    color: {t.text};
    outline: none;
}}
QMainWindow, QDialog, #Workspace {{ background: {t.workspace}; }}
QToolTip {{
    background: {t.sidebar}; color: #FFFFFF; border: none; padding: 5px 8px; border-radius: 4px;
}}

/* ---- Sidebar ---------------------------------------------------------- */
#Sidebar {{ background: {t.sidebar}; }}
#Sidebar QLabel {{ color: {t.sidebar_text}; background: transparent; }}
#Sidebar #Brand {{ font-size: 15px; font-weight: 700; color: #FFFFFF; letter-spacing: 0.5px; }}
#Sidebar #BrandSub {{ font-size: 10px; color: {t.sidebar_muted}; }}
#Hamburger {{ background: transparent; border: none; border-radius: {t.radius_small}px; padding: 6px; }}
#Hamburger:hover {{ background: {t.sidebar_hover}; }}
#NavItem {{
    background: transparent; color: {t.sidebar_text}; border: none; border-radius: {t.radius}px;
    padding: 9px 12px; text-align: left; font-size: 13px;
}}
#NavItem:hover {{ background: {t.sidebar_hover}; }}
#NavItem:checked {{ background: {t.primary}; color: #FFFFFF; font-weight: 600; }}
#Sidebar #NavBadge {{
    background: {t.sidebar_hover}; color: {t.sidebar_text}; border-radius: 9px; padding: 1px 7px;
    font-size: 11px; font-weight: 600;
}}
#Sidebar #NavBadge[active="true"] {{ background: rgba(255,255,255,0.22); color: #FFFFFF; }}
#Sidebar #NavRunning {{ background: #22C55E; border-radius: 4px; }}
#SidebarDivider {{ background: {t.sidebar_divider}; max-height: 1px; min-height: 1px; }}
#SyncChip {{
    background: {t.sidebar_hover}; color: {t.sidebar_text}; border: none; border-radius: {t.radius}px;
    padding: 7px 10px; text-align: left; font-size: 12px;
}}
#SyncChip:hover {{ background: #273449; }}
#Sidebar #Avatar {{
    background: {t.primary}; color: #FFFFFF; border-radius: 16px; font-weight: 700; font-size: 12px;
}}
#Sidebar #UserName {{ font-weight: 600; font-size: 13px; color: #FFFFFF; }}
#Sidebar #UserEmail {{ font-size: 11px; color: {t.sidebar_muted}; }}
#SidebarButton {{
    background: transparent; color: {t.sidebar_text}; border: none; border-radius: {t.radius_small}px;
    padding: 7px 10px; text-align: left;
}}
#SidebarButton:hover {{ background: {t.sidebar_hover}; }}
#SidebarIconButton {{ background: transparent; border: none; border-radius: {t.radius_small}px; padding: 5px; }}
#SidebarIconButton:hover {{ background: {t.sidebar_hover}; }}

/* ---- Headings ----------------------------------------------------------- */
#PageTitle {{ font-size: 21px; font-weight: 700; color: {t.text}; }}
#PageSubtitle {{ font-size: 13px; color: {t.muted}; }}
#SectionTitle {{ font-size: 14px; font-weight: 700; }}
#Muted {{ color: {t.muted}; }}
#Small {{ color: {t.muted}; font-size: 12px; }}
#FieldLabel {{ font-weight: 600; color: {t.text_secondary}; }}
#ErrorText {{ color: {t.danger}; font-size: 12px; }}
#Link {{ color: {t.primary}; background: transparent; border: none; font-weight: 600; padding: 0; }}
#Link:hover {{ color: {t.primary_hover}; text-decoration: underline; }}

/* ---- Cards -------------------------------------------------------------- */
#Card {{ background: {t.card}; border: 1px solid {t.border}; border-radius: {t.radius}px; }}
#Card QLabel {{ background: transparent; }}
#StatCard {{ background: {t.card}; border: 1px solid {t.border}; border-radius: {t.radius}px; }}
#StatCard:hover {{ border-color: {t.primary_soft_border}; }}
#StatValue {{ font-size: 28px; font-weight: 700; }}
#StatLabel {{ font-size: 13px; font-weight: 600; color: {t.text_secondary}; }}
#Placeholder {{ background: #EEF1F6; border: 1px solid {t.border}; border-radius: {t.radius}px; }}

/* ---- Buttons ------------------------------------------------------------ */
QPushButton {{
    background: {t.card}; color: {t.text}; border: 1px solid {t.border_strong};
    border-radius: {t.radius_small}px; padding: 7px 14px; font-weight: 600;
}}
QPushButton:hover {{ background: {t.hover}; }}
QPushButton:pressed {{ background: {t.border}; }}
QPushButton:disabled {{ color: {t.placeholder}; background: {t.subtle}; border-color: {t.border}; }}
QPushButton[variant="primary"] {{ background: {t.primary}; color: #FFFFFF; border: 1px solid {t.primary}; }}
QPushButton[variant="primary"]:hover {{ background: {t.primary_hover}; border-color: {t.primary_hover}; }}
QPushButton[variant="primary"]:pressed {{ background: {t.primary_pressed}; }}
QPushButton[variant="primary"]:disabled {{ background: #93B4F5; border-color: #93B4F5; color: #FFFFFF; }}
QPushButton[variant="danger"] {{ background: {t.danger}; color: #FFFFFF; border: 1px solid {t.danger}; }}
QPushButton[variant="danger"]:hover {{ background: {t.danger_hover}; border-color: {t.danger_hover}; }}
QPushButton[variant="danger"]:disabled {{ background: #F3A9A9; border-color: #F3A9A9; color: #FFFFFF; }}
QPushButton[variant="soft"] {{
    background: {t.primary_soft}; color: {t.primary}; border: 1px solid {t.primary_soft_border};
    padding: 4px 10px; font-size: 12px;
}}
QPushButton[variant="soft"]:hover {{ background: #DBEAFE; }}
QPushButton[variant="soft"]:disabled {{ color: {t.placeholder}; background: {t.subtle}; border-color: {t.border}; }}
QPushButton[variant="ghost"] {{ background: transparent; border: none; padding: 5px; }}
QPushButton[variant="ghost"]:hover {{ background: {t.hover}; }}
QPushButton[variant="dangerGhost"] {{ background: transparent; border: none; padding: 5px; }}
QPushButton[variant="dangerGhost"]:hover {{ background: {t.danger_soft}; }}
QPushButton[size="large"] {{ padding: 10px 18px; font-size: 14px; }}

/* ---- Segmented control ---------------------------------------------------- */
#Segmented {{ background: {t.card}; border: 1px solid {t.border}; border-radius: {t.radius}px; }}
#Segment {{
    background: transparent; border: none; border-radius: {t.radius_small}px; padding: 7px 18px;
    font-weight: 600; color: {t.text_secondary};
}}
#Segment:hover {{ background: {t.hover}; }}
#Segment:checked {{ background: {t.primary}; color: #FFFFFF; }}

/* ---- Fields ---------------------------------------------------------------- */
QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox {{
    background: {t.field}; border: 1px solid {t.border_strong}; border-radius: {t.radius_small}px;
    padding: 7px 10px; selection-background-color: {t.primary}; selection-color: #FFFFFF;
}}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QSpinBox:focus {{ border: 1px solid {t.primary}; }}
QLineEdit:read-only {{ background: {t.subtle}; color: {t.text_secondary}; }}
QLineEdit[invalid="true"], QPlainTextEdit[invalid="true"] {{ border: 1px solid {t.danger}; }}
QLineEdit#AddressBar {{ background: {t.subtle}; border: 1px solid {t.border}; border-radius: 14px; padding: 5px 12px; color: {t.text_secondary}; }}
#Mono {{ font-family: {t.mono_family}; font-size: 12px; }}
QComboBox {{
    background: {t.field}; border: 1px solid {t.border_strong}; border-radius: {t.radius_small}px;
    padding: 6px 28px 6px 10px; min-height: 18px;
}}
QComboBox:hover {{ border-color: {t.placeholder}; }}
QComboBox:focus {{ border-color: {t.primary}; }}
QComboBox::drop-down {{ border: none; width: 24px; }}
QComboBox::down-arrow {{ image: url(ICON_CHEVRON_DOWN); width: 12px; height: 12px; }}
QComboBox QAbstractItemView {{
    background: {t.card}; border: 1px solid {t.border}; selection-background-color: {t.primary_soft};
    selection-color: {t.text}; outline: none; padding: 4px;
}}
QCheckBox {{ spacing: 8px; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border: 1px solid {t.border_strong}; border-radius: 4px; background: {t.card}; }}
QCheckBox::indicator:checked {{ background: {t.primary}; border-color: {t.primary}; image: url(ICON_CHECK_WHITE); }}

/* ---- Tables ---------------------------------------------------------------- */
QTableWidget, QTableView {{
    background: {t.card}; border: 1px solid {t.border}; border-radius: {t.radius}px;
    gridline-color: transparent; selection-background-color: {t.primary_soft}; selection-color: {t.text};
    alternate-background-color: {t.card};
}}
QTableWidget::item, QTableView::item {{ border-bottom: 1px solid {t.border}; padding: 4px 6px; }}
QTableWidget::item:hover, QTableView::item:hover {{ background: {t.subtle}; }}
QTableWidget::item:selected, QTableView::item:selected {{ background: {t.primary_soft}; color: {t.text}; }}
QHeaderView {{ background: transparent; }}
QHeaderView::section {{
    background: {t.subtle}; color: {t.text_secondary}; font-weight: 700; border: none;
    border-bottom: 1px solid {t.border}; padding: 9px 10px;
}}
QTableCornerButton::section {{ background: {t.subtle}; border: none; }}

/* ---- Lists (job queue) -------------------------------------------------------- */
QListWidget {{ background: {t.card}; border: none; outline: none; }}
QListWidget::item {{ border-bottom: 1px solid {t.border}; padding: 0; }}
QListWidget::item:selected {{ background: {t.primary_soft}; color: {t.text}; }}

/* ---- Tabs ---------------------------------------------------------------------- */
QTabWidget::pane {{ border: 1px solid {t.border}; border-radius: {t.radius}px; background: {t.card}; top: -1px; }}
QTabBar {{ background: transparent; }}
QTabBar::tab {{
    background: transparent; color: {t.muted}; padding: 8px 14px; border: none;
    border-bottom: 2px solid transparent; font-weight: 600;
}}
QTabBar::tab:hover {{ color: {t.text}; }}
QTabBar::tab:selected {{ color: {t.primary}; border-bottom: 2px solid {t.primary}; }}
#BrowserTabs::pane {{ border: none; border-top: 1px solid {t.border}; border-radius: 0; }}
#BrowserTabs QTabBar::tab {{
    background: {t.subtle}; color: {t.text_secondary}; border: 1px solid {t.border}; border-bottom: none;
    border-top-left-radius: 6px; border-top-right-radius: 6px; padding: 6px 10px; margin-right: 2px;
    min-width: 90px; max-width: 200px; font-weight: 500;
}}
#BrowserTabs QTabBar::tab:selected {{ background: {t.card}; color: {t.text}; border-bottom: 2px solid {t.primary}; }}
#BrowserTabs QTabBar::close-button {{ image: url(ICON_X_MUTED); subcontrol-position: right; }}
#BrowserTabs QTabBar::close-button:hover {{ background: {t.border}; border-radius: 3px; }}
#BrowserBar {{ background: {t.card}; border-bottom: 1px solid {t.border}; }}

/* ---- Progress ------------------------------------------------------------------ */
QProgressBar {{ background: {t.border}; border: none; border-radius: 3px; max-height: 6px; min-height: 6px; text-align: center; }}
QProgressBar::chunk {{ background: {t.primary}; border-radius: 3px; }}

/* ---- Scrollbars ---------------------------------------------------------------- */
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: #CBD5E1; border-radius: 3px; min-height: 28px; }}
QScrollBar::handle:vertical:hover {{ background: #94A3B8; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: #CBD5E1; border-radius: 3px; min-width: 28px; }}
QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {{ background: none; border: none; width: 0; height: 0; }}
QScrollArea {{ background: transparent; border: none; }}
QSplitter::handle {{ background: {t.border}; }}
QSplitter::handle:horizontal {{ width: 1px; }}

/* ---- Chips, banners, overlays --------------------------------------------------- */
#Chip {{ background: {t.subtle}; border: 1px solid {t.border}; border-radius: 10px; padding: 2px 9px; font-size: 12px; color: {t.text_secondary}; }}
#SharedChip {{ background: {t.primary_soft}; border: 1px solid {t.primary_soft_border}; border-radius: 10px; padding: 2px 9px; font-size: 12px; color: {t.primary}; font-weight: 600; }}
#AttentionBadge {{ background: {t.warning_soft}; border: 1px solid {t.warning_border}; color: #92400E; border-radius: 10px; padding: 2px 9px; font-size: 12px; font-weight: 600; }}
#ViewAsBanner {{ background: {t.danger_soft}; border-bottom: 1px solid {t.danger_border}; }}
#ViewAsBanner QLabel {{ color: {t.danger_hover}; font-weight: 600; background: transparent; }}
#OfflineBanner {{ background: {t.warning_soft}; border-bottom: 1px solid {t.warning_border}; }}
#OfflineBanner QLabel {{ color: #92400E; background: transparent; }}
#Toast {{ background: {t.sidebar}; border-radius: {t.radius}px; }}
#Toast QLabel {{ color: #FFFFFF; background: transparent; }}
#Toast QPushButton {{ background: transparent; color: #93C5FD; border: none; padding: 2px 4px; font-weight: 600; }}
#Toast QPushButton:hover {{ color: #FFFFFF; }}
#FloatCard {{ background: {t.card}; border: 1px solid {t.border_strong}; border-radius: 10px; }}
#FloatCard QLabel {{ background: transparent; }}
#AlertCard {{ background: #FFFBEB; border: 1px solid {t.warning_border}; border-radius: 10px; }}
#AlertCard QLabel {{ background: transparent; }}
#ModalCard {{ background: {t.card}; border-radius: 12px; }}
#ModalCard QLabel {{ background: transparent; }}
#ModalTitle {{ font-size: 17px; font-weight: 700; }}
#Scrim {{ background: rgba(15, 23, 42, 0.45); }}
#Drawer {{ background: {t.card}; border-left: 1px solid {t.border}; }}
#Drawer QLabel {{ background: transparent; }}
#DrawerTitle {{ font-size: 17px; font-weight: 700; }}
#ModeCard {{ background: {t.card}; border: 1px solid {t.border}; border-radius: {t.radius}px; }}
#ModeCard[selected="true"] {{ background: {t.primary_soft}; border: 1px solid {t.primary}; }}
#ModeCard QLabel {{ background: transparent; }}
#StepDot {{ background: {t.border}; color: {t.text_secondary}; border-radius: 12px; font-weight: 700; font-size: 12px; }}
#StepDot[state="active"] {{ background: {t.primary}; color: #FFFFFF; }}
#StepDot[state="done"] {{ background: {t.primary}; color: #FFFFFF; }}
#StepLabel {{ color: {t.muted}; font-weight: 600; }}
#StepLabel[state="active"] {{ color: {t.text}; }}
#StepLine {{ background: {t.border}; max-height: 2px; min-height: 2px; }}
#QueueTitle {{ font-weight: 700; font-size: 14px; padding: 12px 14px; border-bottom: 1px solid {t.border}; }}
#RunLog {{ background: {t.subtle}; border: 1px solid {t.border}; border-radius: {t.radius_small}px; font-family: {t.mono_family}; font-size: 11px; color: {t.text_secondary}; }}
#AuthBackground {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #EAF2FF, stop:0.5 #F5F8FF, stop:1 #DCE9FF); }}
#AuthCard {{ background: {t.card}; border: 1px solid {t.border}; border-radius: 14px; }}
#AuthCard QLabel {{ background: transparent; }}
#AuthBrand {{ font-size: 26px; font-weight: 800; color: {t.primary}; letter-spacing: 0.5px; }}
#AuthSub {{ font-size: 13px; color: {t.text_secondary}; font-weight: 600; }}
QMenu {{ background: {t.card}; border: 1px solid {t.border}; border-radius: 6px; padding: 4px; }}
QMenu::item {{ padding: 6px 18px; border-radius: 4px; }}
QMenu::item:selected {{ background: {t.primary_soft}; }}
"""
