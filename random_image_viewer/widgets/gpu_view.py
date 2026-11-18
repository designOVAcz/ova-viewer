"""🎮 The canvas drawn on the graphics card while the view moves.

Zooming or panning, the picture and each layer's own image (see
RandomImageViewer._layer_canvas) are drawn here through OpenGL: the images
go to the graphics card once (again only when they change) and every step
of a zoom or pan is just drawing them at the new size - a fraction of a
millisecond, whole, with nothing to wait for, as in a paint program. The
plain canvas takes over again once the view rests or a stroke starts.

It is a window of its own, frameless, laid exactly over the canvas and
transparent to input - the mouse, wheel and pen go straight through to the
canvas under it. (As a widget inside the main window, its OpenGL made Qt
draw the whole window through OpenGL, and every dab of a stroke cost a
full-window composition: painting ran at half speed.) The tool panels lying
on the canvas are drawn over it as they look (grabbed once per zoom).
"""

from PySide6.QtCore import QPoint, QRect, QRectF, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtOpenGLWidgets import QOpenGLWidget


class GpuView(QOpenGLWidget):
    passes_input = True     # the canvas under it gets the mouse and pen (see ImageLabel)

    def __init__(self, window, canvas):
        # No Qt parent: an OpenGL widget made with the main window as parent
        # left every stroke after it about twice as slow, even once hidden
        # or deleted. It belongs to the main window through Windows only
        # (owner, see _own): above it, minimised with it.
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowTransparentForInput
                         | Qt.WindowDoesNotAcceptFocus | Qt.NoDropShadowWindowHint)
        self._owner = window
        self._owned = False
        self._canvas = canvas
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setFocusPolicy(Qt.NoFocus)
        self._background = QColor(0, 0, 0)
        self._items = []        # (image, transform, opacity, composition mode or None, fill)
        self._overlay = None    # callable(painter): lines drawn over it all (guides)
        self._panels = []       # (pixmap, QPoint): tool panels on the canvas, as they look
        self._painted = set()   # cacheKeys of the images drawn last: on the graphics card already
        self.failed = False
        self.active = False     # showing (opaque) - else there but see-through, preloading

    def ensure_shown(self):
        """There, see-through until used: a window shown only when a zoom
        began was black while the images went to the graphics card."""
        if not self.isVisible():
            if not self.active:
                self.setWindowOpacity(0.0)
            self._own()
            self.place()
            self.show()

    def _own(self):
        """The main window as this window's owner, window to window only
        (a transient parent, not a widget parent): above it, minimised
        with it."""
        if self._owned:
            return
        try:
            self.winId()
            mine, theirs = self.windowHandle(), self._owner.windowHandle()
            if mine is not None and theirs is not None:
                mine.setTransientParent(theirs)
                self._owned = True
        except Exception as e:
            print(f"GPU view owner not set: {e}")

    def ready_for(self, items):
        """Every image in *items* is on the graphics card already (drawn
        before): showing it now shows them at once, not black."""
        for image, *_rest in items:
            if image is not None and image.cacheKey() not in self._painted:
                return False
        return True

    def activate(self):
        """Seen from now on - drawn with this view first: made opaque with
        an update still waiting, it showed what it drew last (the view
        before, for a frame - a blink)."""
        if not self.active:
            self.active = True
            self._keep()
            self.repaint()
            self.setWindowOpacity(1.0)

    def put_away_soon(self, ms=50):
        """Away once the canvas under it is on screen: hidden at once, the
        canvas from before the zoom showed for a frame - a blink. A zoom
        again meanwhile keeps it (see set_scene)."""
        from PySide6.QtCore import QTimer
        timer = getattr(self, '_away_timer', None)
        if timer is None:
            timer = self._away_timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(self.put_away)
        timer.start(ms)

    def _keep(self):
        timer = getattr(self, '_away_timer', None)
        if timer is not None:
            timer.stop()

    def put_away(self):
        """Gone until the next zoom - hidden, not just see-through: a window
        kept over the canvas slowed every dab of painting. Shown again, it
        draws once see-through before it is used (see ready_for)."""
        self.active = False
        self._panels = []
        self._painted = set()
        if self.isVisible():
            self.setWindowOpacity(0.0)
            self.hide()

    def place(self):
        """Exactly over the canvas, on screen."""
        top_left = self._canvas.mapToGlobal(QPoint(0, 0))
        rect = QRect(top_left, self._canvas.size())
        if self.geometry() != rect:
            self.setGeometry(rect)

    def grab_panels(self, panels):
        """The tool panels lying on the canvas, as pictures to draw on top."""
        self._panels = []
        for panel in panels:
            try:
                if panel.isVisible():
                    self._panels.append((panel.grab(), panel.mapTo(self._canvas, QPoint(0, 0))))
            except RuntimeError:
                pass

    def set_scene(self, background, items, overlay=None):
        """*items*: (QImage | QPixmap | None, QTransform, opacity, mode, fill)
        drawn in order - an image through its transform, or (image None)
        fill = (QRectF, QColor) through it; *overlay* draws on top,
        untransformed (canvas coordinates)."""
        self._keep()
        self._background = QColor(background)
        self._items = list(items)
        self._overlay = overlay
        self.place()
        self.update()

    def paintGL(self):
        p = QPainter(self)
        try:
            p.fillRect(self.rect(), self._background)
            p.setRenderHint(QPainter.SmoothPixmapTransform, True)
            self._painted = {image.cacheKey() for image, *_r in self._items if image is not None}
            for image, transform, opacity, mode, fill in self._items:
                p.save()
                p.setTransform(transform)
                p.setOpacity(opacity)
                if mode is not None:
                    p.setCompositionMode(mode)
                if image is None:
                    if fill is not None:
                        rect, colour = fill
                        p.fillRect(QRectF(rect), colour)
                elif hasattr(image, 'toImage'):
                    p.drawPixmap(0, 0, image)
                else:
                    p.drawImage(0, 0, image)
                p.restore()
            if self._overlay is not None:
                self._overlay(p)
            if self.active:
                for pixmap, at in self._panels:
                    p.drawPixmap(at, pixmap)
        except Exception as e:          # a driver that will not: the plain path from now on
            print(f"🎮 GPU view failed, drawing on the CPU: {e}")
            self.failed = True
        finally:
            p.end()
