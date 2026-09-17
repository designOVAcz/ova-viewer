"""Compact inline colour picker.

A small window that drops under the colour button instead of the platform
colour dialog: a saturation/value square, a hue strip, a live preview with a
hex field, and the colours already snapped from images. Everything updates as
you drag, so picking a colour is one gesture rather than a modal round trip.

It opens as a transient picker that closes when you click elsewhere. Pin it
(📌), or drag it by its header, and it stays open on the canvas as a floating
panel — useful when you are switching colours constantly while painting.

Signals:
    color_picked(QColor)   — emitted continuously while dragging
    pinned_changed(bool)   — pin toggled
    closed()               — picker hidden
"""

from PySide6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout,
                               QLabel, QLineEdit, QSizePolicy, QToolButton)
from PySide6.QtGui import QColor, QPainter, QImage, QPen, QLinearGradient
from PySide6.QtCore import Qt, Signal, QRect, QPoint, QEvent


class _SquareField(QWidget):
    """Saturation (x) against value (y) for one hue."""

    changed = Signal(float, float)   # saturation, value - both 0..1

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(168, 128)
        self.setCursor(Qt.CrossCursor)
        self._hue = 0.0
        self._sat = 1.0
        self._val = 1.0
        self._cache = None
        self._cache_hue = None

    def set_hue(self, hue):
        if abs(hue - self._hue) > 1e-6:
            self._hue = hue
            self._cache = None
        self.update()

    def set_sv(self, sat, val):
        self._sat, self._val = sat, val
        self.update()

    def _render(self):
        """Build the gradient once per hue — it is the expensive part."""
        w, h = self.width(), self.height()
        img = QImage(w, h, QImage.Format_RGB32)
        for y in range(h):
            val = 1.0 - y / max(1, h - 1)
            for x in range(w):
                sat = x / max(1, w - 1)
                img.setPixelColor(x, y, QColor.fromHsvF(self._hue, sat, val))
        self._cache = img
        self._cache_hue = self._hue

    def paintEvent(self, event):
        if self._cache is None or self._cache_hue != self._hue:
            self._render()
        p = QPainter(self)
        p.drawImage(0, 0, self._cache)
        x = int(self._sat * (self.width() - 1))
        y = int((1.0 - self._val) * (self.height() - 1))
        ring = Qt.white if self._val < 0.6 else Qt.black
        p.setPen(QPen(ring, 1))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(QPoint(x, y), 5, 5)
        p.setPen(QPen(Qt.black if ring == Qt.white else Qt.white, 1))
        p.drawEllipse(QPoint(x, y), 6, 6)
        p.end()

    def _pick(self, pos):
        sat = min(max(pos.x(), 0), self.width() - 1) / max(1, self.width() - 1)
        val = 1.0 - min(max(pos.y(), 0), self.height() - 1) / max(1, self.height() - 1)
        self._sat, self._val = sat, val
        self.update()
        self.changed.emit(sat, val)

    def mousePressEvent(self, event):
        self._pick(event.position().toPoint())

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.LeftButton:
            self._pick(event.position().toPoint())


class _HueStrip(QWidget):
    """Vertical hue selector."""

    changed = Signal(float)          # hue 0..1

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(18, 128)
        self.setCursor(Qt.CrossCursor)
        self._hue = 0.0

    def set_hue(self, hue):
        self._hue = hue
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        grad = QLinearGradient(0, 0, 0, self.height())
        for i in range(7):
            grad.setColorAt(i / 6.0, QColor.fromHsvF(i / 6.0, 1.0, 1.0))
        p.fillRect(self.rect(), grad)
        y = int(self._hue * (self.height() - 1))
        p.setPen(QPen(Qt.white, 2))
        p.drawLine(0, y, self.width(), y)
        p.setPen(QPen(Qt.black, 1))
        p.drawLine(0, y, self.width(), y)
        p.end()

    def _pick(self, pos):
        hue = min(max(pos.y(), 0), self.height() - 1) / max(1, self.height() - 1)
        self._hue = hue
        self.update()
        self.changed.emit(hue)

    def mousePressEvent(self, event):
        self._pick(event.position().toPoint())

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.LeftButton:
            self._pick(event.position().toPoint())


class _Swatches(QWidget):
    """Clickable grid of previously used / snapped colours."""

    picked = Signal(QColor)

    CELL = 16
    GAP = 3

    def __init__(self, parent=None):
        super().__init__(parent)
        self._colors = []
        self._columns = 10
        self.setCursor(Qt.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

    def set_colors(self, colors):
        self._colors = [QColor(c) for c in colors][:30]
        rows = max(1, (len(self._colors) + self._columns - 1) // self._columns)
        self.setFixedSize(self._columns * (self.CELL + self.GAP),
                          rows * (self.CELL + self.GAP))
        self.update()

    def _rect_for(self, i):
        col = i % self._columns
        row = i // self._columns
        return QRect(col * (self.CELL + self.GAP), row * (self.CELL + self.GAP),
                     self.CELL, self.CELL)

    def paintEvent(self, event):
        p = QPainter(self)
        for i, c in enumerate(self._colors):
            r = self._rect_for(i)
            p.fillRect(r, c)
            p.setPen(QPen(QColor("#55595e"), 1))
            p.drawRect(r.adjusted(0, 0, -1, -1))
        p.end()

    def mousePressEvent(self, event):
        pos = event.position().toPoint()
        for i in range(len(self._colors)):
            if self._rect_for(i).contains(pos):
                self.picked.emit(self._colors[i])
                return


class _Header(QLabel):
    """Title strip that drags the picker; dragging it pins the picker."""

    dragged = Signal()

    def __init__(self, text, window, parent=None):
        super().__init__(text, parent)
        self._window = window
        self._offset = None
        self._moved = False
        self.setCursor(Qt.SizeAllCursor)
        self.setToolTip("Drag to move — dragging pins it open")

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._offset = (event.globalPosition().toPoint()
                            - self._window.frameGeometry().topLeft())
            self._moved = False
            event.accept()

    def mouseMoveEvent(self, event):
        if self._offset is not None and (event.buttons() & Qt.LeftButton):
            self._window.move(event.globalPosition().toPoint() - self._offset)
            if not self._moved:
                self._moved = True
                self.dragged.emit()
            event.accept()

    def mouseReleaseEvent(self, event):
        self._offset = None
        event.accept()


class ColorPickerPopup(QWidget):
    color_picked = Signal(QColor)
    pinned_changed = Signal(bool)
    closed = Signal()

    def __init__(self, parent=None):
        # A frameless tool window rather than Qt.Popup: a popup is closed by Qt
        # on any outside click, which makes pinning impossible. Click-away
        # dismissal is done here instead, and only while unpinned.
        super().__init__(parent, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self._color = QColor("#ffffff")
        self._updating = False
        self._pinned = False
        self._anchor = None          # widget whose clicks must not dismiss us
        self._filtering = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        bg = QWidget(self)
        bg.setObjectName("pickerBg")
        bg.setStyleSheet(
            "#pickerBg { background-color: #2b2d30; border: 1px solid #4a4d50;"
            " border-radius: 8px; }"
            "QLabel { color: #c8c8c8; font-size: 9pt; }"
            "QLineEdit { background: #34363a; color: #e0e0e0; border: 1px solid #4a4d50;"
            " border-radius: 3px; padding: 1px 4px; font-size: 9pt; }"
        )
        outer.addWidget(bg)

        inner = QVBoxLayout(bg)
        inner.setContentsMargins(10, 6, 10, 10)
        inner.setSpacing(8)

        # ── header: drag handle, pin, close ──
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(4)
        self._title = _Header("\U0001f3a8 Color", self)
        self._title.setStyleSheet(
            "QLabel { color: #c8c8c8; font-size: 10pt; font-weight: bold;"
            " padding: 2px 2px; background: transparent; }")
        self._title.dragged.connect(lambda: self.set_pinned(True))
        header.addWidget(self._title, 1)

        self._pin_btn = QToolButton()
        self._pin_btn.setText("\U0001f4cc")
        self._pin_btn.setCheckable(True)
        self._pin_btn.setFixedSize(20, 18)
        self._pin_btn.setCursor(Qt.PointingHandCursor)
        self._pin_btn.setToolTip("Pin: keep the picker open on the canvas")
        self._pin_btn.setStyleSheet(
            "QToolButton { background: transparent; border: none; font-size: 10pt;"
            " padding: 0px; color: #888; }"
            "QToolButton:hover { background: #3c3f44; border-radius: 3px; }"
            "QToolButton:checked { background: #4a5a7a; border-radius: 3px; }")
        self._pin_btn.toggled.connect(self.set_pinned)
        header.addWidget(self._pin_btn, 0)

        self._close_btn = QToolButton()
        self._close_btn.setText("✕")
        self._close_btn.setFixedSize(18, 18)
        self._close_btn.setCursor(Qt.PointingHandCursor)
        self._close_btn.setToolTip("Close")
        self._close_btn.setStyleSheet(
            "QToolButton { color: #c8c8c8; background: transparent; border: none;"
            " font-weight: bold; font-size: 10pt; padding: 0px; }"
            "QToolButton:hover { color: #fff; background: #c54040; border-radius: 3px; }")
        self._close_btn.clicked.connect(self.hide)
        header.addWidget(self._close_btn, 0)
        inner.addLayout(header)

        top = QHBoxLayout()
        top.setSpacing(8)
        self._field = _SquareField()
        self._strip = _HueStrip()
        top.addWidget(self._field)
        top.addWidget(self._strip)
        inner.addLayout(top)

        row = QHBoxLayout()
        row.setSpacing(6)
        self._preview = QLabel()
        self._preview.setFixedSize(34, 22)
        row.addWidget(self._preview)
        self._hex = QLineEdit()
        self._hex.setFixedWidth(80)
        self._hex.setToolTip("Hex value — type one and press Enter")
        self._hex.returnPressed.connect(self._on_hex_entered)
        row.addWidget(self._hex)
        row.addStretch(1)
        inner.addLayout(row)

        self._swatch_label = QLabel("Picked from images")
        inner.addWidget(self._swatch_label)
        self._swatches = _Swatches()
        self._swatches.picked.connect(self._on_swatch)
        inner.addWidget(self._swatches)

        self._field.changed.connect(self._on_field)
        self._strip.changed.connect(self._on_strip)

    # ── public API ──
    def set_color(self, color):
        """Show *color* without emitting (used when opening / syncing)."""
        self._updating = True
        self._color = QColor(color)
        h, s, v, _ = self._color.getHsvF()
        h = max(0.0, h)          # achromatic colours report hue -1
        self._field.set_hue(h)
        self._field.set_sv(s, v)
        self._strip.set_hue(h)
        self._sync_readout()
        self._updating = False

    def set_palette_colors(self, colors):
        """Offer previously snapped colours as one-click swatches."""
        self._swatches.set_colors(colors)
        has = bool(colors)
        self._swatches.setVisible(has)
        self._swatch_label.setVisible(has)
        self.adjustSize()

    def set_anchor(self, widget):
        """Clicks on *widget* (the button that opens us) never dismiss us."""
        self._anchor = widget

    def current_color(self):
        return QColor(self._color)

    def is_pinned(self):
        return self._pinned

    def set_pinned(self, pinned):
        pinned = bool(pinned)
        if pinned == self._pinned:
            return
        self._pinned = pinned
        self._pin_btn.blockSignals(True)
        self._pin_btn.setChecked(pinned)
        self._pin_btn.blockSignals(False)
        self._pin_btn.setToolTip(
            "Pinned: stays open — click to let it close on click-away" if pinned
            else "Pin: keep the picker open on the canvas")
        self._update_click_away()
        self.pinned_changed.emit(pinned)

    # ── click-away dismissal (unpinned only) ──
    def _update_click_away(self):
        want = self.isVisible() and not self._pinned
        app = QApplication.instance()
        if app is None or want == self._filtering:
            return
        if want:
            app.installEventFilter(self)
        else:
            app.removeEventFilter(self)
        self._filtering = want

    def eventFilter(self, obj, event):
        if (event.type() == QEvent.MouseButtonPress and self.isVisible()
                and not self._pinned):
            gp = event.globalPosition().toPoint()
            if not self.frameGeometry().contains(gp):
                anchor = self._anchor
                on_anchor = False
                if anchor is not None and anchor.isVisible():
                    top_left = anchor.mapToGlobal(QPoint(0, 0))
                    on_anchor = QRect(top_left, anchor.size()).contains(gp)
                if not on_anchor:
                    self.hide()
        return False

    def showEvent(self, event):
        super().showEvent(event)
        self._update_click_away()

    def hideEvent(self, event):
        super().hideEvent(event)
        self._update_click_away()
        self.closed.emit()

    # ── internals ──
    def _sync_readout(self):
        self._preview.setStyleSheet(
            f"background: {self._color.name()}; border: 1px solid #55595e;")
        if self._hex.text().lower() != self._color.name().lower():
            self._hex.setText(self._color.name())

    def _emit(self):
        self._sync_readout()
        if not self._updating:
            self.color_picked.emit(QColor(self._color))

    def _on_field(self, sat, val):
        h = max(0.0, self._color.hueF())
        if self._color.saturationF() == 0.0:
            h = self._strip._hue
        self._color = QColor.fromHsvF(h, sat, val)
        self._emit()

    def _on_strip(self, hue):
        _, s, v, _ = self._color.getHsvF()
        if s <= 0.0:
            s = 1.0
        self._field.set_hue(hue)
        self._color = QColor.fromHsvF(hue, s, v)
        self._emit()

    def _on_swatch(self, color):
        self.set_color(color)
        self.color_picked.emit(QColor(color))

    def _on_hex_entered(self):
        text = self._hex.text().strip()
        if not text.startswith("#"):
            text = "#" + text
        color = QColor(text)
        if color.isValid():
            self.set_color(color)
            self.color_picked.emit(QColor(color))
