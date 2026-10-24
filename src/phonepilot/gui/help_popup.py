"""Small "?" buttons that open a balloon with the explanation, instead of paragraphs of help text in the panels."""

from __future__ import annotations

from PySide6.QtCore import QPoint, QRectF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QKeyEvent, QPainter, QPainterPath, QPaintEvent, QPen, QResizeEvent
from PySide6.QtWidgets import QFrame, QGroupBox, QLabel, QToolButton, QVBoxLayout, QWidget

from phonepilot.gui.theme import COLORS

ARROW = 8  # height of the balloon's pointer
RADIUS = 10
MAX_WIDTH = 320


class HelpBalloon(QFrame):
    """Popup balloon pointing at its anchor. Closes on a click outside, Esc, or another click on the anchor."""

    def __init__(self, text: str, anchor: QWidget) -> None:
        super().__init__(anchor, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint | Qt.WindowType.NoDropShadowWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setObjectName("helpBalloon")
        self._anchor = anchor
        self._arrow_x = 24
        self._below = True
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12 + ARROW, 14, 12)
        self.label = QLabel(text)
        self.label.setObjectName("helpBalloonText")
        self.label.setWordWrap(True)
        self.label.setTextFormat(Qt.TextFormat.PlainText)
        self.label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self.label)
        self.label.setFixedWidth(min(MAX_WIDTH, max(180, self.label.fontMetrics().horizontalAdvance(text) + 4)))

    def show_at_anchor(self) -> None:
        self.adjustSize()
        a = self._anchor
        tip = a.mapToGlobal(QPoint(a.width() // 2, a.height()))
        screen = (a.screen() or QGuiApplication.primaryScreen()).availableGeometry()
        win = a.window()
        win_mid = win.mapToGlobal(QPoint(win.width() // 2, 0)).x()
        # open towards the inside of the window: buttons on the right edge get a balloon that extends left
        x = tip.x() - 24 if tip.x() < win_mid else tip.x() - self.width() + 24
        x = min(max(x, screen.left() + 4), screen.right() - self.width() - 4)
        y = tip.y() + 2
        self._below = y + self.height() <= screen.bottom()
        if not self._below:  # no room below: open above the button, pointer at the bottom
            lay = self.layout()
            lay.setContentsMargins(14, 12, 14, 12 + ARROW)
            self.adjustSize()
            y = a.mapToGlobal(QPoint(0, 0)).y() - self.height() - 2
        self._arrow_x = min(max(tip.x() - x, RADIUS + ARROW), self.width() - RADIUS - ARROW)
        self.move(x, y)
        self.show()

    def keyPressEvent(self, e: QKeyEvent) -> None:
        if e.key() == Qt.Key.Key_Escape:
            self.close()
            return
        super().keyPressEvent(e)

    def paintEvent(self, _e: QPaintEvent) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width() - 1, self.height() - 1
        top, bottom = (ARROW, h) if self._below else (0, h - ARROW)
        path = QPainterPath()
        path.addRoundedRect(QRectF(0.5, top + 0.5, w - 1, bottom - top - 1), RADIUS, RADIUS)
        ax = self._arrow_x
        tri = QPainterPath()
        if self._below:
            tri.moveTo(ax - ARROW, top + 1)
            tri.lineTo(ax, 0.5)
            tri.lineTo(ax + ARROW, top + 1)
        else:
            tri.moveTo(ax - ARROW, bottom - 1)
            tri.lineTo(ax, h - 0.5)
            tri.lineTo(ax + ARROW, bottom - 1)
        tri.closeSubpath()
        path = path.united(tri)
        p.setPen(QPen(QColor(COLORS["border_strong"]), 1))
        p.setBrush(QColor(COLORS["surface_2"]))
        p.drawPath(path)
        p.end()


class HelpButton(QToolButton):
    """Round "?" that opens the explanation in a balloon."""

    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.help_text = text
        self.setText("?")
        self.setObjectName("helpButton")
        self.setToolTip("도움말")
        self.setAccessibleName("도움말")
        self.setAccessibleDescription(text)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(20, 20)
        self.balloon: HelpBalloon | None = None
        self.clicked.connect(self.toggle)

    def toggle(self) -> None:
        if self.balloon is not None and self.balloon.isVisible():
            self.balloon.close()
            return
        self.balloon = HelpBalloon(self.help_text, self)
        self.balloon.destroyed.connect(lambda: setattr(self, "balloon", None))
        self.balloon.show_at_anchor()


class HelpGroupBox(QGroupBox):
    """QGroupBox with a "?" at the right end of its title row."""

    def __init__(self, title: str, help_text: str, parent: QWidget | None = None) -> None:
        super().__init__(title, parent)
        self.help_button = HelpButton(help_text, self)

    def resizeEvent(self, e: QResizeEvent) -> None:
        super().resizeEvent(e)
        self.help_button.move(self.width() - self.help_button.width() - 4, 0)
        self.help_button.raise_()
