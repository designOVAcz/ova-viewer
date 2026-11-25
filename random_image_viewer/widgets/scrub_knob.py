"""Scrub knobs: a round button you drag sideways to change a value
(HeavyPaint's way for brush size and colour jitter).

    knob = ScrubKnob("Size", 0, 1000, rate=2.5)
    knob.scrubbed.connect(lambda value, done: ...)   # done: the drag ended

Hovered, small ‹ › arrows show it scrubs; while it is dragged, a preview
card (ScrubPreview) hangs under it - the brush's outline at its real size,
or a sample stroke - and goes when it is let go. The wheel steps it too.
"""

import math

from PySide6.QtCore import Qt, Signal, QPointF, QRectF, QRect, QSize, QPoint, QTimer
from PySide6.QtGui import (QColor, QConicalGradient, QCursor, QFont, QGuiApplication, QPainter,
                           QPen, QPixmap, QLinearGradient)
from PySide6.QtWidgets import QWidget, QToolButton

FACE = QColor(74, 77, 81)
FACE_HOT = QColor(92, 96, 101)
RIM = QColor(255, 255, 255, 28)
TITLE = QColor(185, 190, 195)
VALUE = QColor(240, 242, 244)
ARROW = QColor(200, 204, 208)


def _scale_font(font, factor, smallest=6.0):
    """*font* scaled by *factor* in place - in points, or in pixels when it
    is set in pixels (the app's stylesheet: "font-size: 11px"), where
    pointSizeF() is -1 and scaling it gave a negative size (Qt warned on
    every paint, and drew at a default size)."""
    if font.pointSizeF() > 0:
        font.setPointSizeF(max(smallest * 0.75, font.pointSizeF() * factor))
    elif font.pixelSize() > 0:
        font.setPixelSize(max(int(smallest), round(font.pixelSize() * factor)))


class ScrubKnob(QWidget):
    """A round knob: drag left / right to scrub its value."""

    scrubbed = Signal(float, bool)      # value, done (the drag / wheel step ended)
    started = Signal()                  # a drag began (show its preview)

    D = 34                              # the knob's diameter
    ARROWS = 11                         # room each side for ‹ ›

    def __init__(self, title, minimum, maximum, value=0.0, rate=1.0, step=1.0, parent=None):
        super().__init__(parent)
        self.title = title
        self.minimum, self.maximum = float(minimum), float(maximum)
        self._value = float(value)
        self.rate = float(rate)         # value per pixel dragged
        self.step = float(step)         # per wheel notch
        self._text = None               # shown instead of the value (see setText)
        self._press = None              # (x, value) where the drag started
        self._dragging = False
        self._hover = False
        # f() -> the value as the app has it now (changed elsewhere too):
        # read whenever it is not being dragged
        self.value_source = None
        self.setFixedSize(self.D + 2 * self.ARROWS, self.D + 2)
        self.setCursor(Qt.SizeHorCursor)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WA_Hover, True)

    # -- value -------------------------------------------------------------
    def value(self):
        return self._value

    def setValue(self, value):
        """Set it from code (no signal)."""
        self._value = max(self.minimum, min(self.maximum, float(value)))
        self.update()

    def setText(self, text):
        """What the face shows (e.g. the size the app works out); the
        app's labels were QLabels, set with "36px" - the unit is dropped."""
        text = str(text)
        self._text = text[:-2] if text.endswith('px') else text
        self.update()

    def display(self):
        return self._text if self._text is not None else f"{int(round(self._value))}"

    def is_scrubbing(self):
        return self._dragging

    # -- input -------------------------------------------------------------
    def enterEvent(self, event):
        self._hover = True
        self.update()

    def leaveEvent(self, event):
        self._hover = False
        self.update()

    def _sync(self):
        if self.value_source is not None and not self._dragging:
            try:
                self._value = max(self.minimum, min(self.maximum, float(self.value_source())))
            except Exception:
                pass

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._sync()
            self._press = (event.globalPosition().x(), self._value)
            self._dragging = False
            event.accept()

    def mouseMoveEvent(self, event):
        if self._press is None or not (event.buttons() & Qt.LeftButton):
            return
        x0, v0 = self._press
        dx = event.globalPosition().x() - x0
        if not self._dragging and abs(dx) < 2:
            return
        if not self._dragging:
            self._dragging = True
            self.started.emit()
        # Shift: finer
        rate = self.rate * (0.25 if event.modifiers() & Qt.ShiftModifier else 1.0)
        value = max(self.minimum, min(self.maximum, v0 + dx * rate))
        if value != self._value:
            self._value = value
            self.scrubbed.emit(value, False)
            self.update()
        event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self._press is not None:
            was = self._dragging
            self._press, self._dragging = None, False
            if was:
                self.scrubbed.emit(self._value, True)
            self.update()
            event.accept()

    def wheelEvent(self, event):
        notches = event.angleDelta().y() / 120.0
        if not notches:
            return
        self._sync()
        value = max(self.minimum, min(self.maximum, self._value + notches * self.step))
        if value != self._value:
            self._value = value
            self.scrubbed.emit(value, True)
            self.update()
        event.accept()

    # -- look --------------------------------------------------------------
    def knob_rect(self):
        return QRectF(self.ARROWS, 1, self.D, self.D)

    def paint_face(self, p, rect):
        """The knob's face: a grey disc, its title and value."""
        p.setPen(QPen(RIM, 1))
        p.setBrush(FACE_HOT if (self._hover or self._dragging) else FACE)
        p.drawEllipse(rect)
        f = QFont(self.font())
        _scale_font(f, 0.62, smallest=7.5)
        p.setFont(f)
        p.setPen(TITLE)
        p.drawText(rect.adjusted(0, 4, 0, -rect.height() / 2), Qt.AlignHCenter | Qt.AlignBottom, self.title)
        f = QFont(self.font())
        _scale_font(f, 0.95)
        f.setBold(True)
        p.setFont(f)
        p.setPen(VALUE)
        p.drawText(rect.adjusted(0, rect.height() / 2 - 1, 0, -2), Qt.AlignHCenter | Qt.AlignTop,
                   self.display())

    def paintEvent(self, event):
        self._sync()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        rect = self.knob_rect()
        self.paint_face(p, rect)
        if self._hover or self._dragging:
            # ‹ › : it scrubs sideways
            pen = QPen(ARROW, 1.4)
            pen.setCapStyle(Qt.RoundCap)
            pen.setJoinStyle(Qt.RoundJoin)
            p.setPen(pen)
            cy = rect.center().y()
            for x, d in ((self.ARROWS / 2.0 + 1, 1), (self.width() - self.ARROWS / 2.0 - 1, -1)):
                p.drawPolyline([QPointF(x + 2 * d, cy - 3.5), QPointF(x - 1.5 * d, cy),
                                QPointF(x + 2 * d, cy + 3.5)])
        p.end()

    def sizeHint(self):
        return QSize(self.D + 2 * self.ARROWS, self.D + 2)


class JitterKnob(ScrubKnob):
    """🎲 Colour jitter: the face a disc of the brush colour, the rainbow
    showing through it as much as the jitter is (none: just the colour)."""

    def __init__(self, parent=None):
        super().__init__("Jitter", 0, 100, rate=0.4, step=5, parent=parent)
        self.colour = QColor(200, 80, 110)
        self.colour_source = None       # f() -> the brush colour now
        self.setToolTip("🎲 Colour jitter: drag sideways (Shift: finer) - how far each hair / "
                        "speck strays from the colour in hue, saturation and value. The Brush "
                        "panel has each of them on its own")

    def set_colour(self, colour):
        self.colour = QColor(colour)
        self.update()

    def paint_face(self, p, rect):
        if self.colour_source is not None:
            try:
                self.colour = QColor(self.colour_source())
            except Exception:
                pass
        p.setPen(QPen(RIM, 1))
        p.setBrush(self.colour)
        p.drawEllipse(rect)
        amount = self._value / 100.0
        if amount > 0:
            g = QConicalGradient(rect.center(), 90)
            for i in range(13):
                g.setColorAt(i / 12.0, QColor.fromHsvF((i % 12) / 12.0, 0.85, 1.0))
            p.save()
            p.setOpacity(0.15 + 0.85 * amount if amount > 0.02 else amount * 8)
            p.setPen(Qt.NoPen)
            p.setBrush(g)
            p.drawEllipse(rect.adjusted(1, 1, -1, -1))
            p.restore()
        if self._hover or self._dragging:
            # the number on the disc while it is handled
            f = QFont(self.font())
            f.setBold(True)
            p.setFont(f)
            p.setPen(QColor(0, 0, 0, 150))
            p.drawText(rect.translated(0.8, 0.8), Qt.AlignCenter, self.display())
            p.setPen(VALUE)
            p.drawText(rect, Qt.AlignCenter, self.display())


class RoundToolButton(QToolButton):
    """A main tool as a round button the knobs' size: an image (the brush
    tip) or a symbol on a grey disc, a blue ring while it is the tool in
    hand. *sticky*: a click only turns it on (clicked again, it stays on -
    the caller decides what a second click does; see was_on)."""

    D = ScrubKnob.D
    ACCENT = QColor(88, 150, 235)

    def __init__(self, symbol="", sticky=False, parent=None, checkable=True):
        super().__init__(parent)
        self.symbol = symbol
        self.image = None           # a QPixmap / QImage shown instead of the symbol
        self.sticky = sticky
        self.was_on = False         # (sticky) on already when last clicked
        self.setCheckable(checkable)
        # (the app's stylesheet gives every QToolButton min-height / padding,
        # and the style's own size hint (31 x 27): the button came out
        # squeezed, its circle cut at the top - out of that rule here)
        self.setStyleSheet("QToolButton { border: none; background: transparent; padding: 0;"
                           " min-height: 0; min-width: 0; }")
        self.setFixedSize(self.D + 4, self.D + 2)
        self.setAttribute(Qt.WA_Hover, True)
        self.setAutoRaise(True)

    def sizeHint(self):
        return QSize(self.D + 4, self.D + 2)

    def minimumSizeHint(self):
        return self.sizeHint()

    # Held down (pen or mouse) this long, or right-clicked: long_pressed is
    # emitted instead of a click - a menu of choices, say (the 🖍 modes).
    long_pressed = Signal()
    HOLD_MS = 350
    hold_enabled = False        # set True where long_pressed is used

    def mousePressEvent(self, event):
        self._held = False
        if event.button() == Qt.RightButton and self.hold_enabled:
            self._held = True
            self.long_pressed.emit()
            event.accept()
            return
        if event.button() == Qt.LeftButton and self.hold_enabled:
            timer = getattr(self, '_hold_timer', None)
            if timer is None:
                timer = self._hold_timer = QTimer(self)
                timer.setSingleShot(True)
                timer.timeout.connect(self._on_hold)
            timer.start(self.HOLD_MS)
        super().mousePressEvent(event)

    def _on_hold(self):
        if not self.isDown():
            return
        self._held = True
        self.setDown(False)         # (no click when let go)
        self.long_pressed.emit()

    def mouseReleaseEvent(self, event):
        timer = getattr(self, '_hold_timer', None)
        if timer is not None:
            timer.stop()
        if getattr(self, '_held', False):
            self._held = False
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def set_image(self, image):
        self.image = image
        self.update()

    def setText(self, text):
        """(as QToolButton's: the symbol on the disc - the app sets the
        brush mode's emoji this way)"""
        self.symbol = str(text)
        self.update()

    def text(self):
        return self.symbol

    def nextCheckState(self):
        if self.sticky:
            self.was_on = self.isChecked()
            if not self.was_on:
                self.setChecked(True)
            return
        super().nextCheckState()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        rect = QRectF((self.width() - self.D) / 2.0, 1, self.D, self.D)
        on = self.isChecked()
        hot = self.underMouse() or self.isDown()
        p.setPen(QPen(self.ACCENT, 2) if on else QPen(RIM, 1))
        p.setBrush(FACE_HOT if (hot or on) else FACE)
        p.drawEllipse(rect.adjusted(1, 1, -1, -1) if on else rect)
        if self.image is not None:
            img = self.image
            dpr = img.devicePixelRatio() or 1.0
            w, h = img.width() / dpr, img.height() / dpr
            side = self.D - 10
            k = min(side / max(1.0, w), side / max(1.0, h))
            target = QRectF(rect.center().x() - w * k / 2, rect.center().y() - h * k / 2, w * k, h * k)
            p.save()
            from PySide6.QtGui import QPainterPath
            clip = QPainterPath()
            clip.addEllipse(rect.adjusted(3, 3, -3, -3))
            p.setClipPath(clip)
            if hasattr(img, 'toImage'):
                p.drawPixmap(target, img, QRectF(img.rect()))
            else:
                p.drawImage(target, img)
            p.restore()
        elif self.symbol:
            f = QFont(self.font())
            _scale_font(f, 1.35)
            p.setFont(f)
            p.setPen(VALUE)
            p.drawText(rect, Qt.AlignCenter, self.symbol)
        p.end()


class ScrubPreview(QWidget):
    """The card under a knob while it is scrubbed: an image (the brush's
    outline at its real size, or a sample stroke), optionally a title with
    the value and a rainbow bar for how much."""

    def __init__(self):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                         | Qt.WindowTransparentForInput | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self._pixmap = None
        self._title = None
        self._amount = None
        self._card = False

    def show_under(self, knob, pixmap, title=None, amount=None, card=False):
        """Show *pixmap* under *knob*: as it is (*card* False - a brush
        outline over the canvas), or on a dark card with *title* and a bar
        filled to *amount* (0..1)."""
        self._pixmap, self._title, self._amount, self._card = pixmap, title, amount, card
        dpr = pixmap.devicePixelRatio() if pixmap is not None else 1.0
        pw = int(math.ceil(pixmap.width() / dpr)) if pixmap is not None else 0
        ph = int(math.ceil(pixmap.height() / dpr)) if pixmap is not None else 0
        if card:
            w = max(220, pw + 24)
            h = ph + 24 + (26 if title else 0) + (14 if amount is not None else 0)
        else:
            w, h = pw + 8, ph + 8
        self.setFixedSize(w, h)
        # Beside the knob's panel - on its left, level with the knob - so it
        # covers no panel docked under it (🎨 Color sits there); on its right
        # when the panel is at the left edge; under it as the last resort.
        # (The panel is a child of the canvas, not a window: knob.window()
        # is the main window - see _panel_of.)
        panel = self._panel_of(knob)
        p_tl = panel.mapToGlobal(QPoint(0, 0))
        p_box = QRect(p_tl, panel.size())
        k_mid = knob.mapToGlobal(QPoint(knob.width() // 2, knob.height() // 2))
        screen = QGuiApplication.screenAt(k_mid) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry() if screen is not None else QRect(-10 ** 6, -10 ** 6, 2 * 10 ** 6, 2 * 10 ** 6)
        top_window = knob.window()
        if top_window is not None:
            # inside the app's window too (a window smaller than the screen)
            inside = QRect(top_window.mapToGlobal(QPoint(0, 0)), top_window.size()).intersected(area)
            if inside.width() > w + 8 and inside.height() > h + 8:
                area = inside
        gap = 10
        y = k_mid.y() - h // 2
        y = max(area.top() + 4, min(area.bottom() - h - 4, y))
        if p_box.left() - gap - w >= area.left() + 4:
            x = p_box.left() - gap - w                          # left of the panel
        elif p_box.right() + gap + w <= area.right() - 4:
            x = p_box.right() + gap                             # right of it
        else:
            # under the panel, its middle under the knob (or above, no room)
            x = max(area.left() + 4, min(area.right() - w - 4, k_mid.x() - w // 2))
            y = p_box.bottom() + gap
            if y + h > area.bottom() - 4:
                y = max(area.top() + 4, p_box.top() - h - gap)
        self.move(x, y)
        if not self.isVisible():
            self.show()
        self.update()

    @staticmethod
    def _panel_of(knob):
        """The floating panel holding *knob* (its nearest FloatingPanel), or
        the knob itself."""
        w = knob.parentWidget()
        while w is not None:
            if type(w).__name__ == 'FloatingPanel' or w.isWindow():
                return w if not w.isWindow() or w is not knob.window() else knob
            w = w.parentWidget()
        return knob

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        r = QRectF(self.rect())
        y = 4.0
        if self._card:
            p.setPen(QPen(QColor(255, 255, 255, 30), 1))
            p.setBrush(QColor(8, 8, 9, 235))
            p.drawRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), 9, 9)
            y = 10.0
            if self._title:
                f = QFont(self.font())
                _scale_font(f, 1.05)
                p.setFont(f)
                p.setPen(QColor(230, 232, 235))
                p.drawText(QRectF(0, y, r.width(), 22), Qt.AlignCenter, self._title)
                y += 26
        if self._pixmap is not None:
            dpr = self._pixmap.devicePixelRatio() or 1.0
            pw, ph = self._pixmap.width() / dpr, self._pixmap.height() / dpr
            p.drawPixmap(QPointF((r.width() - pw) / 2, y), self._pixmap)
            y += ph + 4
        if self._card and self._amount is not None:
            track = QRectF(14, r.height() - 14, r.width() - 28, 3)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(255, 255, 255, 40))
            p.drawRoundedRect(track, 1.5, 1.5)
            fill = QRectF(track.x(), track.y(), track.width() * max(0.0, min(1.0, self._amount)),
                          track.height())
            g = QLinearGradient(track.left(), 0, track.right(), 0)
            for i in range(7):
                g.setColorAt(i / 6.0, QColor.fromHsvF(i / 6.0 * 0.83, 0.85, 1.0))
            p.setBrush(g)
            p.drawRoundedRect(fill, 1.5, 1.5)
        p.end()
