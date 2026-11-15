"""🟥 The current brush colour as a big floating square.

Frameless, always on top, it never takes focus: drag it anywhere, resize it
from the lower right corner (as big as you like - to judge a colour on its
own, away from the painting), ✕ to put it away. It follows the brush colour
whatever sets it: the picker, palette, eyedropper, ColorPenguin, F8.

Signals: closed() when ✕ is clicked; moved_or_resized() to keep its place.
"""

from PySide6.QtCore import QRect, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QSizeGrip, QToolButton, QWidget


class ColourSwatchWindow(QWidget):
    closed = Signal()
    moved_or_resized = Signal()

    MIN_SIDE = 48
    DEFAULT_SIDE = 160

    def __init__(self, parent=None):
        super().__init__(parent, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setWindowTitle("Colour")
        self.setMinimumSize(self.MIN_SIDE, self.MIN_SIDE)
        self.resize(self.DEFAULT_SIDE, self.DEFAULT_SIDE)
        self.setCursor(Qt.SizeAllCursor)
        self.setToolTip("The brush colour - drag to move, the lower right corner to resize")
        self._colour = QColor(255, 255, 255)
        self._drag = None
        self._close = QToolButton(self)
        self._close.setText("✕")
        self._close.setFixedSize(18, 18)
        self._close.setCursor(Qt.PointingHandCursor)
        self._close.setToolTip("Put the colour swatch away")
        self._close.setStyleSheet(
            "QToolButton { color: #e0e0e0; background: rgba(0,0,0,110); border: none;"
            " border-radius: 3px; font-weight: bold; }"
            "QToolButton:hover { background: #c54040; }")
        self._close.clicked.connect(self._on_close)
        self._grip = QSizeGrip(self)
        self._grip.setFixedSize(16, 16)
        self._place_controls()

    def set_colour(self, colour):
        colour = QColor(colour)
        colour.setAlpha(255)
        if colour.rgb() != self._colour.rgb():
            self._colour = colour
            self.update()

    def _place_controls(self):
        self._close.move(self.width() - self._close.width() - 4, 4)
        self._grip.move(self.width() - self._grip.width(), self.height() - self._grip.height())

    def resizeEvent(self, event):
        self._place_controls()
        super().resizeEvent(event)
        self.moved_or_resized.emit()

    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), self._colour)
        p.setPen(QPen(QColor(0, 0, 0, 160), 1))
        p.drawRect(self.rect().adjusted(0, 0, -1, -1))
        p.setPen(QPen(QColor(255, 255, 255, 90), 1))
        p.drawRect(self.rect().adjusted(1, 1, -2, -2))
        # Its hex at the bottom, in black or white - whichever reads on it.
        if self.height() >= 64 and self.width() >= 64:
            f = QFont(self.font())
            f.setPixelSize(max(9, min(16, self.height() // 12)))
            f.setBold(True)
            p.setFont(f)
            light = self._colour.lightnessF() > 0.55
            p.setPen(QColor(0, 0, 0, 170) if light else QColor(255, 255, 255, 190))
            band = QRect(6, self.height() - 6 - f.pixelSize() * 2, self.width() - 28, f.pixelSize() * 2)
            p.drawText(band, Qt.AlignLeft | Qt.AlignVCenter, self._colour.name().upper())
        p.end()

    def _on_close(self):
        self.hide()
        self.closed.emit()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if self._drag is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag)
            event.accept()

    def mouseReleaseEvent(self, event):
        if self._drag is not None:
            self._drag = None
            self.moved_or_resized.emit()
