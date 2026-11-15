"""✎ The layer just switched to, as a badge at the top centre of the canvas.

In ◧ Studio, when the layer changes, it blinks up for about two seconds and
fades away: its colour dot (the same one as in 📚 Layers), its name, where
it sits in the stack, ⛰ / its blend mode when it has them - struck through
while the layer is hidden. Clicks go through it to the canvas.
"""

from PySide6.QtCore import QEvent, QPropertyAnimation, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QGraphicsOpacityEffect, QLabel


class LayerBadge(QLabel):
    TOP = 10

    def __init__(self, canvas):
        super().__init__(canvas)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setTextFormat(Qt.RichText)
        self._canvas = canvas
        canvas.installEventFilter(self)
        self._glow = QGraphicsOpacityEffect(self)
        self._glow.setOpacity(0.92)
        self.setGraphicsEffect(self._glow)
        # A blink when the layer changes, about two seconds in view, then it
        # fades away (shown all the time it was in the way).
        self._pulse = QPropertyAnimation(self._glow, b"opacity", self)
        self._pulse.setDuration(2600)
        self._pulse.setKeyValueAt(0.0, 1.0)
        self._pulse.setKeyValueAt(0.06, 0.45)
        self._pulse.setKeyValueAt(0.12, 1.0)
        self._pulse.setKeyValueAt(0.77, 1.0)
        self._pulse.setKeyValueAt(1.0, 0.0)
        self._pulse.finished.connect(self.hide)
        self._colour = QColor("#8ab4f8")
        self._style(self._colour, False)
        self.hide()

    def _style(self, colour, bright):
        border = colour.name()
        bg = "rgba(32, 33, 36, 225)" if not bright else "rgba(48, 50, 54, 240)"
        width = 3 if bright else 2
        self.setStyleSheet(
            f"QLabel {{ color: #f1f3f4; background: {bg}; font-size: 13px; font-weight: 600;"
            f" padding: 4px 12px; border: {width}px solid {border}; border-radius: 12px; }}")

    def show_layer(self, name, colour, position, total, hidden=False, extras="", flash=False):
        dot = f"<span style='color:{QColor(colour).name()}; font-size:15px;'>●</span>"
        text = f"{dot}&nbsp; ✎ {name}"
        if total > 1:
            text += f"&nbsp;&nbsp;<span style='color:#9aa0a6; font-weight:400;'>{position} of {total}</span>"
        if extras:
            text += f"&nbsp;&nbsp;<span style='color:#c8ccd0; font-weight:400;'>{extras}</span>"
        if hidden:
            text = f"<s>{text}</s>&nbsp; <span style='color:#f28b82;'>(hidden)</span>"
        if not flash and not self.isVisible():
            return                  # only a layer switch brings it up
        self.setText(text)
        self._colour = QColor(colour)
        self._style(self._colour, True)
        self.adjustSize()
        self._place()
        if flash:
            self.show()
            self.raise_()
            self._pulse.stop()
            self._pulse.start()

    def dismiss(self):
        self._pulse.stop()
        self.hide()

    def _place(self):
        self.move(max(0, (self._canvas.width() - self.width()) // 2), self.TOP)

    def eventFilter(self, obj, event):
        if obj is self._canvas and event.type() == QEvent.Resize and self.isVisible():
            self._place()
        return False
