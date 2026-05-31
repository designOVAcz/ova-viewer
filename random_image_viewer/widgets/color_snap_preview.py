"""Floating color preview swatch used by the Color Snap eyedropper tool.

A small frameless widget (≈64×80) that follows the cursor while the snap tool
is active. Shows the sampled (5×5-averaged) color as a swatch with a hex label
underneath. Transparent for input so it never steals mouse clicks.
"""

from PySide6.QtWidgets import QWidget
from PySide6.QtCore import Qt, QRect
from PySide6.QtGui import QPainter, QColor, QPen, QFont


class ColorSnapPreview(QWidget):
    SWATCH_SIZE = 50
    LABEL_HEIGHT = 16
    MARGIN = 4

    def __init__(self, parent=None):
        # Tooltip-style flags: stays on top, no taskbar, no focus stealing
        super().__init__(
            parent,
            Qt.ToolTip | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint,
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)

        self._color = QColor(0, 0, 0)
        self._hex = "#000000"

        w = self.SWATCH_SIZE + 2 * self.MARGIN
        h = self.SWATCH_SIZE + self.LABEL_HEIGHT + 2 * self.MARGIN
        self.setFixedSize(w, h)

    def set_color(self, color: QColor):
        if not isinstance(color, QColor):
            color = QColor(color)
        self._color = QColor(color.red(), color.green(), color.blue())
        self._hex = self._color.name().upper()
        self.update()

    def color(self) -> QColor:
        return QColor(self._color)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, False)

        # Background panel (semi-transparent dark)
        p.setPen(QPen(QColor(0, 0, 0, 200), 1))
        p.setBrush(QColor(30, 30, 30, 220))
        p.drawRect(0, 0, self.width() - 1, self.height() - 1)

        # Swatch
        swatch_rect = QRect(self.MARGIN, self.MARGIN,
                            self.SWATCH_SIZE, self.SWATCH_SIZE)
        p.setPen(QPen(QColor(255, 255, 255, 220), 1))
        p.setBrush(self._color)
        p.drawRect(swatch_rect)
        # Inner contrast border
        inner = swatch_rect.adjusted(1, 1, -1, -1)
        p.setPen(QPen(QColor(0, 0, 0, 200), 1))
        p.setBrush(Qt.NoBrush)
        p.drawRect(inner)

        # Hex label
        label_rect = QRect(0, self.MARGIN + self.SWATCH_SIZE,
                           self.width(), self.LABEL_HEIGHT)
        f = QFont()
        f.setPointSize(8)
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor(230, 230, 230))
        p.drawText(label_rect, Qt.AlignCenter, self._hex)
        p.end()
