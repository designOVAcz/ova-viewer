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
from PySide6.QtGui import QColor, QPainter, QImage, QPen, QLinearGradient, QPainterPath
from PySide6.QtCore import Qt, Signal, QRect, QRectF, QPoint, QPointF, QEvent

from random_image_viewer.drawing import okhsl


_HUE0 = 0.6        # the hue (OKLab, 0..1) at the field's left edge: cyan-blue,
                   # so violet, red, yellow, green follow across it


def _rgb_image(rgb):
    """(h, w, 3) sRGB 0..1 -> a QImage (copied, owns its pixels)."""
    import numpy as np
    a = np.ascontiguousarray((np.clip(rgb, 0, 1) * 255 + 0.5).astype(np.uint8))
    h, w = a.shape[:2]
    return QImage(a.data, w, h, w * 3, QImage.Format_RGB888).copy()


def _rounded(rect, radius):
    path = QPainterPath()
    path.addRoundedRect(QRectF(rect), radius, radius)
    return path


class _HueLightField(QWidget):
    """Every hue across, lightness down (white top, black bottom), at one
    saturation (_SatBar) - in OKLab (drawing/okhsl.py), so a row is one
    lightness to the eye and every colour shown can be painted."""

    changed = Signal(float, float)   # hue, lightness - both 0..1

    SCALE = 2           # worked out at half size, scaled up smooth (it is soft anyway)
    _cmax = {}          # (w, h) -> the most chroma per cell (the costly part, kept)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(204, 172)
        self.setCursor(Qt.CrossCursor)
        self._hue, self._sat, self._light = 0.08, 0.6, 0.6
        self._colour = QColor(Qt.white)
        self._cache = None
        self._cache_sat = None

    def set_state(self, hue, sat, light, colour):
        if self._cache_sat is None or abs(sat - self._cache_sat) > 1e-4:
            self._cache = None
        self._hue, self._sat, self._light = hue, sat, light
        self._colour = QColor(colour)
        self.update()

    def _grid(self):
        import numpy as np
        w, h = max(2, self.width() // self.SCALE), max(2, self.height() // self.SCALE)
        hue = ((_HUE0 + np.arange(w) / (w - 1)) % 1.0)[None, :]
        light = (1.0 - np.arange(h) / (h - 1))[:, None]
        key = (w, h)
        if key not in self._cmax:
            self._cmax[key] = okhsl.max_chroma(np.broadcast_to(light, (h, w)),
                                               np.broadcast_to(hue * 2 * np.pi, (h, w)))
        return hue, light, self._cmax[key]

    def _render(self):
        hue, light, cmax = self._grid()
        small = _rgb_image(okhsl.to_rgb(hue, self._sat, light, cmax))
        self._cache = small.scaled(self.width(), self.height(), Qt.IgnoreAspectRatio,
                                   Qt.SmoothTransformation)
        self._cache_sat = self._sat

    def _xy(self):
        x = ((self._hue - _HUE0) % 1.0) * (self.width() - 1)
        y = (1.0 - self._light) * (self.height() - 1)
        return QPointF(x, y)

    def paintEvent(self, event):
        if self._cache is None:
            self._render()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setClipPath(_rounded(self.rect(), 6))
        p.drawImage(0, 0, self._cache)
        p.setClipping(False)
        # the colour in a ring, where it is
        at = self._xy()
        p.setBrush(self._colour)
        p.setPen(QPen(QColor(0, 0, 0, 120), 3.5))
        p.drawEllipse(at, 10, 10)
        p.setPen(QPen(QColor(235, 235, 235), 2))
        p.drawEllipse(at, 10, 10)
        p.end()

    def _pick(self, pos):
        x = min(max(pos.x(), 0.0), self.width() - 1.0)
        y = min(max(pos.y(), 0.0), self.height() - 1.0)
        self._hue = (_HUE0 + x / max(1, self.width() - 1)) % 1.0
        self._light = 1.0 - y / max(1, self.height() - 1)
        self.update()
        self.changed.emit(self._hue, self._light)

    def mousePressEvent(self, event):
        self._pick(event.position())

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.LeftButton:
            self._pick(event.position())


class _SatBar(QWidget):
    """Saturation, under the field: from the grey of the colour's lightness
    to the most intense the screen holds at its hue and lightness."""

    changed = Signal(float)          # saturation 0..1

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(204, 22)
        self.setCursor(Qt.CrossCursor)
        self._hue, self._sat, self._light = 0.08, 0.6, 0.6

    def set_state(self, hue, sat, light):
        self._hue, self._sat, self._light = hue, sat, light
        self.update()

    def paintEvent(self, event):
        import numpy as np
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        s = np.linspace(0.0, 1.0, 17)
        rgb = okhsl.to_rgb(np.full(17, self._hue), s, np.full(17, self._light))
        grad = QLinearGradient(0, 0, self.width(), 0)
        for t, c in zip(s, rgb):
            grad.setColorAt(float(t), QColor.fromRgbF(*[float(v) for v in np.clip(c, 0, 1)]))
        p.fillPath(_rounded(self.rect(), 5), grad)
        # the handle: a slim upright pill
        x = 4 + self._sat * (self.width() - 9)
        pill = QRectF(x - 3.5, 2, 7, self.height() - 4)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(0, 0, 0, 140), 3))
        p.drawRoundedRect(pill, 3.5, 3.5)
        p.setPen(QPen(QColor(235, 235, 235), 1.6))
        p.drawRoundedRect(pill, 3.5, 3.5)
        p.end()

    def _pick(self, pos):
        s = (min(max(pos.x(), 4.0), self.width() - 5.0) - 4.0) / max(1, self.width() - 9)
        self._sat = s
        self.update()
        self.changed.emit(s)

    def mousePressEvent(self, event):
        self._pick(event.position())

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.LeftButton:
            self._pick(event.position())


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
        if self._moved:
            self._moved = False
            done = getattr(self._window, 'drag_finished', None)
            if done is not None:
                done.emit()
        event.accept()


class ColorPickerPopup(QWidget):
    color_picked = Signal(QColor)
    pinned_changed = Signal(bool)
    closed = Signal()
    drag_finished = Signal()        # moved by hand (it leaves / rejoins the dock)
    # the second colour (the gradient's other end), when shown - see enable_second
    second_requested = Signal()     # its swatch clicked: pick it
    swap_requested = Signal()       # ⇄
    second_picked = Signal(QColor)  # Alt+click on a palette swatch

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

        # hue across, lightness down, saturation on the bar below (OKLab)
        self._hsl = (0.08, 0.6, 0.6)
        self._field = _HueLightField()
        self._field.setToolTip("Hue across, light to dark down - every colour at the same "
                               "lightness looks equally light")
        self._bar = _SatBar()
        self._bar.setToolTip("Saturation: from grey to the most intense colour the screen shows")
        inner.addWidget(self._field)
        inner.addSpacing(-2)
        inner.addWidget(self._bar)

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
        # The second colour - as a paint program's background colour: the
        # \U0001F308 gradient's other end. ⇄ (X) swaps the two.
        self._swap_btn = QToolButton()
        self._swap_btn.setText("\u21c4")
        self._swap_btn.setFixedSize(20, 22)
        self._swap_btn.setCursor(Qt.PointingHandCursor)
        self._swap_btn.setToolTip("Swap the pen colour and the second colour (X)")
        self._swap_btn.setStyleSheet(
            "QToolButton { color: #c8c8c8; background: transparent; border: none; font-size: 11pt; }"
            "QToolButton:hover { background: #3c3f44; border-radius: 3px; }")
        self._swap_btn.clicked.connect(self.swap_requested)
        row.addWidget(self._swap_btn)
        self._second_btn = QToolButton()
        self._second_btn.setFixedSize(26, 22)
        self._second_btn.setCursor(Qt.PointingHandCursor)
        self._second_btn.setToolTip("The second colour - the \U0001F308 gradient's other end. Click to "
                                    "pick it; Alt+click a palette swatch or the eyedropper sets it too")
        self._second_btn.clicked.connect(self.second_requested)
        row.addWidget(self._second_btn)
        self._second_on = False
        self._swap_btn.hide()
        self._second_btn.hide()
        inner.addLayout(row)

        self._swatch_label = QLabel("Picked from images")
        inner.addWidget(self._swatch_label)
        self._swatches = _Swatches()
        self._swatches.picked.connect(self._on_swatch)
        inner.addWidget(self._swatches)

        self._field.changed.connect(self._on_field)
        self._bar.changed.connect(self._on_bar)

    # ── public API ──
    def enable_second(self, on=True):
        """Show the second colour (and ⇄) beside the current one."""
        self._second_on = bool(on)
        self._swap_btn.setVisible(self._second_on)
        self._second_btn.setVisible(self._second_on)

    def set_second_color(self, color):
        c = QColor(color)
        self._second_btn.setStyleSheet(
            f"QToolButton {{ background: {c.name()}; border: 1px solid #6a6d70; border-radius: 3px; }}"
            "QToolButton:hover { border: 1px solid #ffcc00; }")

    def set_title(self, text):
        self._title.setText(f"\U0001f3a8 {text}")

    def set_color(self, color):
        """Show *color* without emitting (used when opening / syncing)."""
        self._updating = True
        new = QColor(color)
        if new.name() != self._color.name() or not getattr(self, '_shown_once', False):
            # (our own colour coming back - the app echoes what we sent - is
            # left where it is: rounding to 8 bits would nudge the ring)
            self._shown_once = True
            h, s, l = okhsl.from_rgb(new.redF(), new.greenF(), new.blueF())
            if h is None:
                h = self._hsl[0]        # a grey: keep the hue it had
            self._show_hsl(h, s, l, new)
        self._color = new
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

    def _show_hsl(self, h, s, l, colour):
        self._hsl = (h, s, l)
        self._field.set_state(h, s, l, colour)
        self._bar.set_state(h, s, l)

    def _set_hsl(self, h, s, l):
        r, g, b = (float(v) for v in okhsl.to_rgb(h, s, l))
        colour = QColor.fromRgbF(r, g, b)
        self._color = colour
        self._show_hsl(h, s, l, colour)
        self._emit()

    def _on_field(self, hue, light):
        self._set_hsl(hue, self._hsl[1], light)

    def _on_bar(self, sat):
        self._set_hsl(self._hsl[0], sat, self._hsl[2])

    def _on_swatch(self, color):
        if self._second_on and QApplication.keyboardModifiers() & Qt.AltModifier:
            self.second_picked.emit(QColor(color))      # Alt: the second colour
            return
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
