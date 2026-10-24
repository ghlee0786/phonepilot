"""Dark theme (PRD 18): design tokens, the application style sheet and the line icons used across the GUI.

Every colour in the GUI comes from COLORS so widgets, badges and painted overlays stay consistent.
Icons are 24x24 stroke drawings rendered from inline SVG and tinted per use; no emoji glyphs (their look
depends on the installed font and they do not scale with the theme).
"""

from __future__ import annotations

from functools import lru_cache

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPalette, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication

COLORS = {
    "bg": "#0b1120",  # window, toolbar, status bar
    "surface": "#111a2e",  # cards, lists, panels
    "surface_2": "#18233a",  # buttons, inputs, headers
    "surface_3": "#22304b",  # hover
    "border": "#26324a",
    "border_strong": "#3a4a68",
    "text": "#e2e8f0",
    "text_muted": "#94a3b8",
    "text_subtle": "#64748b",
    "primary": "#3b82f6",
    "primary_hover": "#2f6fe0",
    "success": "#22c55e",
    "warning": "#f59e0b",
    "danger": "#ef4444",
    "danger_hover": "#dc2626",
    "purple": "#a855f7",
}

# status pill: (tint background, text) — tints keep text contrast >= 4.5:1 on the dark bar
PILL = {
    "gray": ("#1e293b", "#cbd5e1"),
    "blue": ("#13284d", "#93c5fd"),
    "yellow": ("#3a2c0b", "#fcd34d"),
    "orange": ("#3d2010", "#fdba74"),
    "red": ("#3f1518", "#fca5a5"),
    "green": ("#0f3020", "#86efac"),
}

FONT_FAMILIES = ["Segoe UI", "Malgun Gothic"]
MONO_FAMILIES = ["Cascadia Mono", "Consolas", "Malgun Gothic"]


def pill_style(color: str, blink_off: bool = False) -> str:
    bg, fg = PILL[color]
    border = fg if blink_off else bg
    if blink_off:
        bg = "transparent"
    return f"QLabel{{background:{bg};color:{fg};border:1px solid {border};border-radius:10px;padding:2px 10px;font-weight:600;}}"


# ---------------------------------------------------------------- icons
_SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{c}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">{body}</svg>'

ICONS = {
    "back": '<path d="M15 18l-6-6 6-6"/>',
    "home": '<path d="M3 10.5 12 3l9 7.5"/><path d="M5 9.5V21h14V9.5"/><path d="M10 21v-6h4v6"/>',
    "recents": '<rect x="7" y="3" width="14" height="14" rx="2"/><path d="M3 7v12a2 2 0 0 0 2 2h12"/>',
    "rotate": '<path d="M21 12a9 9 0 1 1-3-6.7"/><path d="M21 3v6h-6"/>',
    "screen_off": '<rect x="6" y="2" width="12" height="20" rx="2"/><path d="M3 3l18 18"/>',
    "camera": '<path d="M4 8h3l2-3h6l2 3h3a1 1 0 0 1 1 1v10a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V9a1 1 0 0 1 1-1z"/><circle cx="12" cy="13.5" r="3.5"/>',
    "fullscreen": '<path d="M4 9V4h5"/><path d="M15 4h5v5"/><path d="M20 15v5h-5"/><path d="M9 20H4v-5"/>',
    "pin": '<path d="M12 17v5"/><path d="M9 3h6l-1 6 3 3v2H7v-2l3-3z"/>',
    "panel": '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M15 4v16"/>',
    "pause": '<path d="M9 5v14"/><path d="M15 5v14"/>',
    "play": '<path d="M7 4l13 8-13 8z"/>',
    "stop": '<rect x="6" y="6" width="12" height="12" rx="2"/>',
    "shield_check": '<path d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z"/><path d="M9 12l2 2 4-4"/>',
    "user": '<circle cx="12" cy="8" r="4"/><path d="M4 21c0-4 4-6 8-6s8 2 8 6"/>',
    "system": '<circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9l2.1 2.1M17 17l2.1 2.1M4.9 19.1 7 17M17 7l2.1-2.1"/>',
    "bot": '<rect x="4" y="8" width="16" height="12" rx="3"/><path d="M12 4v4"/><circle cx="12" cy="3" r="1"/><path d="M9 14h.01M15 14h.01"/>',
    "workflow": '<path d="M17 2l4 4-4 4"/><path d="M3 11V9a3 3 0 0 1 3-3h15"/><path d="M7 22l-4-4 4-4"/><path d="M21 13v2a3 3 0 0 1-3 3H3"/>',
    "lock": '<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/>',
    "alert": '<path d="M12 3l10 18H2z"/><path d="M12 10v4"/><path d="M12 17.5v.5"/>',
    "hand": '<path d="M7 11V5a1.5 1.5 0 0 1 3 0v5"/><path d="M10 10V3.5a1.5 1.5 0 0 1 3 0V10"/><path d="M13 10V5a1.5 1.5 0 0 1 3 0v6"/><path d="M16 11V8a1.5 1.5 0 0 1 3 0v6a7 7 0 0 1-7 7h-1a6 6 0 0 1-5-2.7L3.5 14a1.5 1.5 0 0 1 2.5-1.7L7 14"/>',
    "refresh": '<path d="M21 12a9 9 0 0 1-15.5 6.2L3 16"/><path d="M3 21v-5h5"/><path d="M3 12a9 9 0 0 1 15.5-6.2L21 8"/><path d="M21 3v5h-5"/>',
    "copy": '<rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h10a1 1 0 0 1 1 1v1"/>',
    "qr": '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><path d="M14 14h3v3M21 14v.01M14 21h.01M17 21h4v-4"/>',
    "wifi": '<path d="M5 12.5a10 10 0 0 1 14 0"/><path d="M8.5 16a5 5 0 0 1 7 0"/><path d="M2 9a15 15 0 0 1 20 0"/><path d="M12 19.5h.01"/>',
    "keypad": '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 9h.01M12 9h.01M17 9h.01M7 13h.01M12 13h.01M17 13h.01M9 17h6"/>',
    "check": '<path d="M5 12l5 5L20 7"/>',
    "record": '<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="3.5"/>',
    "chart": '<path d="M4 20V4"/><path d="M4 20h16"/><path d="M8 16v-5M12 16V8M16 16v-3"/>',
    "undo": '<path d="M9 14 4 9l5-5"/><path d="M4 9h11a5 5 0 0 1 0 10h-3"/>',
    "trash": '<path d="M4 7h16"/><path d="M10 11v6M14 11v6"/><path d="M6 7l1 13h10l1-13"/><path d="M9 7V4h6v3"/>',
    "open": '<path d="M14 4h6v6"/><path d="M20 4l-9 9"/><path d="M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/>',
}


def _pixmap(name: str, color: str, size: int) -> QPixmap:
    renderer = QSvgRenderer(QByteArray(_SVG.format(c=color, body=ICONS[name]).encode()))
    scale = 2  # render at 2x so icons stay crisp at 125-200 % display scaling
    pix = QPixmap(size * scale, size * scale)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(painter, QRectF(0, 0, size * scale, size * scale))
    painter.end()
    pix.setDevicePixelRatio(scale)
    return pix


@lru_cache(maxsize=256)
def icon(name: str, color: str | None = None, size: int = 18) -> QIcon:
    """Line icon tinted with `color` (default: body text); disabled state uses the subtle text colour."""
    ic = QIcon()
    ic.addPixmap(_pixmap(name, color or COLORS["text"], size), QIcon.Mode.Normal)
    ic.addPixmap(_pixmap(name, COLORS["text_subtle"], size), QIcon.Mode.Disabled)
    return ic


# ---------------------------------------------------------- style sheet
def build_qss() -> str:
    c = COLORS
    return f"""
QWidget {{ color: {c['text']}; font-size: 13px; }}
QMainWindow, QDialog {{ background: {c['bg']}; }}
QToolTip {{ background: {c['surface_2']}; color: {c['text']}; border: 1px solid {c['border_strong']}; padding: 6px 8px; border-radius: 6px; }}

/* toolbar */
QToolBar#mainToolbar {{ background: {c['bg']}; border: none; border-bottom: 1px solid {c['border']}; padding: 6px 8px; spacing: 2px; }}
QToolBar#mainToolbar::separator {{ background: {c['border']}; width: 1px; margin: 6px 8px; }}
QToolBar QToolButton {{ background: transparent; border: 1px solid transparent; border-radius: 8px; padding: 6px; color: {c['text']}; }}
QToolBar QToolButton:hover {{ background: {c['surface_2']}; border-color: {c['border']}; }}
QToolBar QToolButton:pressed {{ background: {c['surface_3']}; }}
QToolBar QToolButton:checked {{ background: #13284d; border-color: #1e3a6b; color: #bfdbfe; }}
QToolBar QToolButton:disabled {{ color: {c['text_subtle']}; }}
QToolButton#stopButton {{ background: {c['danger']}; color: white; font-weight: 600; padding: 6px 14px; }}
QToolButton#stopButton:hover {{ background: {c['danger_hover']}; border-color: {c['danger_hover']}; }}
QToolButton#allowButton {{ background: #15803d; color: white; font-weight: 600; padding: 6px 12px; }}
QToolButton#allowButton:hover {{ background: #166534; }}
QToolButton#pauseButton {{ padding: 6px 12px; border-color: {c['border']}; }}
QToolButton#pauseButton:checked {{ background: #3a2c0b; border-color: #78530f; color: #fde68a; }}

/* status bar */
QStatusBar {{ background: {c['bg']}; border-top: 1px solid {c['border']}; color: {c['text_muted']}; }}
QStatusBar::item {{ border: none; }}
QStatusBar QLabel {{ color: {c['text_muted']}; padding: 0 4px; }}

/* dock */
QDockWidget {{ color: {c['text_muted']}; }}
QDockWidget::title {{ background: {c['bg']}; padding: 8px 12px; border-left: 1px solid {c['border']}; text-align: left; }}
QDockWidget > QWidget {{ background: {c['bg']}; border-left: 1px solid {c['border']}; }}

/* tabs: underline style */
QTabWidget::pane {{ border: none; border-top: 1px solid {c['border']}; background: {c['bg']}; top: -1px; }}
QTabBar {{ qproperty-drawBase: 0; }}
QTabBar::tab {{ background: transparent; color: {c['text_muted']}; padding: 8px 14px; border: none; border-bottom: 2px solid transparent; margin-right: 2px; font-weight: 600; }}
QTabBar::tab:hover {{ color: {c['text']}; }}
QTabBar::tab:selected {{ color: {c['text']}; border-bottom: 2px solid {c['primary']}; }}

/* cards */
QGroupBox {{ background: {c['surface']}; border: 1px solid {c['border']}; border-radius: 10px; margin-top: 22px; padding: 12px 10px 10px 10px; }}
QGroupBox::title {{ subcontrol-origin: margin; subcontrol-position: top left; left: 4px; top: 0px; padding: 0 2px; color: {c['text_muted']}; font-size: 12px; font-weight: 700; }}
QLabel#sectionLabel {{ color: {c['text_muted']}; font-size: 12px; font-weight: 700; padding: 6px 2px 2px 2px; }}
QLabel#helpText, QLabel#metaText {{ color: {c['text_muted']}; }}
QLabel#fieldKey {{ color: {c['text_muted']}; }}
QLabel#monoValue {{ font-family: {', '.join(f'"{f}"' for f in MONO_FAMILIES)}; }}

/* buttons */
QPushButton {{ background: {c['surface_2']}; color: {c['text']}; border: 1px solid {c['border']}; border-radius: 8px; padding: 7px 14px; font-weight: 600; }}
QPushButton:hover {{ background: {c['surface_3']}; border-color: {c['border_strong']}; }}
QPushButton:pressed {{ background: {c['border']}; }}
QPushButton:focus {{ border-color: {c['primary']}; }}
QPushButton:disabled {{ background: {c['surface']}; color: {c['text_subtle']}; border-color: {c['border']}; }}
QPushButton#primaryButton {{ background: {c['primary']}; border-color: {c['primary']}; color: white; }}
QPushButton#primaryButton:hover {{ background: {c['primary_hover']}; }}
QPushButton#dangerButton {{ background: {c['danger']}; border-color: {c['danger']}; color: white; }}
QPushButton#dangerButton:hover {{ background: {c['danger_hover']}; }}
QPushButton#primaryButton:disabled, QPushButton#dangerButton:disabled {{ background: {c['surface_2']}; border-color: {c['border']}; color: {c['text_subtle']}; }}
QPushButton#ghostButton {{ background: transparent; border-color: transparent; color: {c['text_muted']}; }}
QPushButton#ghostButton:hover {{ background: {c['surface_2']}; color: {c['text']}; }}

/* inputs */
QLineEdit, QPlainTextEdit {{ background: {c['bg']}; border: 1px solid {c['border_strong']}; border-radius: 8px; padding: 7px 10px; selection-background-color: {c['primary']}; }}
QLineEdit:focus, QPlainTextEdit:focus {{ border-color: {c['primary']}; }}
QTextBrowser {{ background: {c['surface']}; border: 1px solid {c['border']}; border-radius: 8px; padding: 8px; }}
QProgressBar {{ background: {c['surface_2']}; border: none; border-radius: 4px; height: 8px; text-align: center; color: transparent; }}
QProgressBar::chunk {{ background: {c['primary']}; border-radius: 4px; }}
QCheckBox {{ spacing: 8px; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 4px; border: 1px solid {c['border_strong']}; background: {c['bg']}; }}
QCheckBox::indicator:checked {{ background: {c['danger']}; border-color: {c['danger']}; }}

/* lists, trees, tables */
QListWidget, QTreeWidget, QTableWidget {{ background: {c['surface']}; border: 1px solid {c['border']}; border-radius: 10px; outline: none; alternate-background-color: {c['surface_2']}; }}
QListWidget::item {{ padding: 7px 8px; border-bottom: 1px solid {c['border']}; }}
QTreeWidget::item, QTableWidget::item {{ padding: 5px 4px; }}
QListWidget::item:hover, QTreeWidget::item:hover {{ background: {c['surface_2']}; }}
QListWidget::item:selected, QTreeWidget::item:selected, QTableWidget::item:selected {{ background: #13284d; color: {c['text']}; }}
QHeaderView::section {{ background: {c['surface']}; color: {c['text_muted']}; border: none; border-bottom: 1px solid {c['border']}; padding: 6px 8px; font-weight: 700; font-size: 12px; }}
QTableCornerButton::section {{ background: {c['surface']}; border: none; }}

/* scroll */
QScrollArea {{ background: transparent; border: none; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle {{ background: {c['border_strong']}; border-radius: 3px; min-height: 28px; min-width: 28px; }}
QScrollBar::handle:hover {{ background: {c['text_subtle']}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

/* approval dialog */
QLabel#highBand {{ background: #3f1518; color: #fecaca; border: 1px solid #7f1d1d; border-radius: 8px; padding: 10px 12px; font-weight: 700; }}
QLabel#dialogTitle {{ font-size: 17px; font-weight: 700; }}
QLabel#amountLabel {{ color: #fca5a5; font-size: 15px; font-weight: 700; }}
QLabel#countdown {{ color: {c['text_subtle']}; }}
QLabel#previewImage {{ background: #000; border-radius: 8px; }}

/* human hand-off banner */
QFrame#humanBanner {{ background: #2a1a06; border: none; border-bottom: 2px solid {c['warning']}; }}
QFrame#humanBanner QLabel {{ color: #fde68a; font-weight: 600; }}

QMessageBox {{ background: {c['bg']}; }}
"""


def palette() -> QPalette:
    c = COLORS
    pal = QPalette()
    roles = {
        QPalette.ColorRole.Window: c["bg"],
        QPalette.ColorRole.WindowText: c["text"],
        QPalette.ColorRole.Base: c["surface"],
        QPalette.ColorRole.AlternateBase: c["surface_2"],
        QPalette.ColorRole.Text: c["text"],
        QPalette.ColorRole.Button: c["surface_2"],
        QPalette.ColorRole.ButtonText: c["text"],
        QPalette.ColorRole.Highlight: c["primary"],
        QPalette.ColorRole.HighlightedText: "#ffffff",
        QPalette.ColorRole.ToolTipBase: c["surface_2"],
        QPalette.ColorRole.ToolTipText: c["text"],
        QPalette.ColorRole.PlaceholderText: c["text_subtle"],
        QPalette.ColorRole.Link: "#93c5fd",
    }
    for role, value in roles.items():
        pal.setColor(role, QColor(value))
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText):
        pal.setColor(QPalette.ColorGroup.Disabled, role, QColor(c["text_subtle"]))
    return pal


def apply_theme(app: QApplication) -> None:
    app.setStyle("Fusion")  # native Windows style ignores parts of the style sheet (tabs, scroll bars)
    app.setPalette(palette())
    font = QFont()
    font.setFamilies(FONT_FAMILIES)
    font.setPointSizeF(9.75)
    app.setFont(font)
    app.setStyleSheet(build_qss())
