"""Splash screen with dynamic status text shown during startup.

It continues the launcher's boot line (see the spec and tools/make_splash.py)
without a jump: the line is the globe's equator, the same width and grays, so
the splash first shows just that line where the boot line was, then fades the
rest of the globe in around it, and fades out once the main window is up.
"""

import os
import sys
import time

from PySide6.QtWidgets import QWidget, QApplication
from PySide6.QtGui import (QPainter, QColor, QFont, QFontMetrics, QPixmap, QGuiApplication,
                           QLinearGradient)
from PySide6.QtCore import Qt, QRect, QRectF, QSize, QVariantAnimation

# The globe's equator in ova_viewer.png, as fractions of the image (measured
# by tools/make_splash.py, which also draws the boot line from the same row -
# rerun it if the picture changes): the seam's bright edge and its grays.
EQUATOR_Y = 0.51264
EQUATOR_X = (0.07161, 0.92188)
EQUATOR_STOPS = (
    (0.0, (24, 25, 25)), (0.067, (38, 39, 39)), (0.133, (55, 56, 57)),
    (0.2, (80, 81, 82)), (0.267, (74, 75, 76)), (0.333, (73, 74, 75)),
    (0.4, (77, 78, 79)), (0.467, (76, 77, 78)), (0.533, (70, 71, 72)),
    (0.6, (63, 64, 64)), (0.667, (45, 46, 47)), (0.733, (32, 33, 34)),
    (0.8, (38, 38, 39)), (0.867, (6, 6, 6)), (0.933, (0, 0, 0)),
    (1.0, (0, 0, 0)),
)
FADE_IN_S = 0.22        # line -> globe
FADE_OUT_MS = 260       # globe -> gone, over the main window


def _find_splash_image():
    """Locate ova_viewer.png in the project root or PyInstaller bundle."""
    if getattr(sys, 'frozen', False):
        base = sys._MEIPASS
    else:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(base, "ova_viewer.png")
    if os.path.exists(path):
        return path
    return None


def _png_size(path):
    """Width and height from a PNG's IHDR chunk (bytes 16-23), or an invalid
    QSize."""
    try:
        with open(path, 'rb') as fh:
            head = fh.read(24)
        if head[:8] == b'\x89PNG\r\n\x1a\n' and head[12:16] == b'IHDR':
            return QSize(int.from_bytes(head[16:20], 'big'), int.from_bytes(head[20:24], 'big'))
    except OSError:
        pass
    return QSize()


class DynamicSplash(QWidget):
    """A splash screen that displays module loading progress.

    A plain frameless QWidget rather than QSplashScreen: on Windows
    QSplashScreen.show() blocks until the window reports itself exposed and
    routinely sits out its full 1 s timeout, adding a second to every launch.
    """

    WIDTH = 420
    HEIGHT = 300

    def __init__(self):
        # Only the picture's size here, so the line can show at once; the
        # globe is loaded in reveal(). Read from the PNG header by hand:
        # QImageReader would set up Qt's PNG support first (~0.4 s), with
        # nothing on screen meanwhile.
        self._img_path = _find_splash_image()
        size = _png_size(self._img_path) if self._img_path else QSize()
        if size.isValid():
            size = size.scaled(self.WIDTH, self.HEIGHT, Qt.KeepAspectRatio)
            self.WIDTH, self.HEIGHT = size.width(), size.height()
        else:
            self._img_path = None

        super().__init__(None, Qt.SplashScreen | Qt.FramelessWindowHint
                         | Qt.WindowStaysOnTopHint)
        self._pixmap = None
        self._globe = 0.0 if self._img_path else 1.0      # 0 = just the line
        self._opacity = 1.0                                 # fade-out
        self._fade_out = None
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedSize(self.WIDTH, self.HEIGHT)
        screen = QGuiApplication.primaryScreen()
        if screen is not None:
            # The equator's middle on the whole screen's centre: exactly where
            # the boot line was (the launcher centres it on the whole screen;
            # centred above the taskbar it sat a few px off).
            c = screen.geometry().center()
            ex = (EQUATOR_X[0] + EQUATOR_X[1]) / 2 * self.WIDTH
            ey = EQUATOR_Y * self.HEIGHT
            self.move(round(c.x() - ex), round(c.y() - ey))
        self._status = "Starting Ova Viewer…"

    def reveal(self):
        """Grow the globe around the line: load it, then cross-fade (about
        0.2 s, drawn frame by frame - the startup that follows blocks the
        event loop, so an animation would only advance in jumps)."""
        if self._img_path is None or self._pixmap is not None:
            return
        src = QPixmap(self._img_path)
        if src.isNull():
            self._globe = 1.0
            self._img_path = None
            self.repaint()
            return
        self._pixmap = src.scaled(self.WIDTH, self.HEIGHT, Qt.KeepAspectRatio,
                                  Qt.SmoothTransformation)
        # The first text drawn loads the font database (~0.5 s): do it now,
        # while the line is up, not in the middle of the fade.
        QFontMetrics(self._status_font()).horizontalAdvance(self._status)
        start = time.perf_counter()
        while True:
            t = min(1.0, (time.perf_counter() - start) / FADE_IN_S)
            self._globe = t * t * (3 - 2 * t)                # smoothstep
            self.repaint()
            QApplication.processEvents()
            if t >= 1.0:
                break
            time.sleep(0.012)

    def fade_out(self):
        """Fade away over the main window, then close (runs in the event loop)."""
        anim = QVariantAnimation(self)
        anim.setStartValue(1.0)
        anim.setEndValue(0.0)
        anim.setDuration(FADE_OUT_MS)
        anim.valueChanged.connect(self._set_opacity)
        anim.finished.connect(self.close)
        self._fade_out = anim
        anim.start()

    def _set_opacity(self, value):
        self._opacity = float(value)
        self.update()

    def set_status(self, text: str):
        """Update the status line and repaint immediately."""
        self._status = text
        self.repaint()
        QApplication.processEvents()

    def mousePressEvent(self, event):
        # Same as QSplashScreen: a click dismisses the splash.
        self.hide()

    def paintEvent(self, event):
        painter = QPainter(self)
        if self._globe > 0.0:
            painter.setOpacity(self._globe * self._opacity)
            if self._pixmap is not None:
                painter.drawPixmap(0, 0, self._pixmap)
            self.drawContents(painter)
        if self._img_path is not None and self._globe < 1.0:
            # The boot line, carried on: the globe's equator in its own grays.
            painter.setOpacity((1.0 - self._globe) * self._opacity)
            x0, x1 = EQUATOR_X[0] * self.WIDTH, EQUATOR_X[1] * self.WIDTH
            grad = QLinearGradient(x0, 0, x1, 0)
            for t, (r, g, b) in EQUATOR_STOPS:
                grad.setColorAt(t, QColor(r, g, b))
            painter.fillRect(QRectF(x0, EQUATOR_Y * self.HEIGHT, x1 - x0, 1.0), grad)
        painter.end()

    @staticmethod
    def _status_font():
        return QFont("Segoe UI", 9)

    # ------------------------------------------------------------------
    def drawContents(self, painter: QPainter):
        """Custom paint: simple status text near the bottom of the splash."""
        painter.setRenderHint(QPainter.Antialiasing)

        if self._img_path is None:
            painter.fillRect(self.rect(), QColor(45, 45, 45))

        # Simple, single-line status centered on the image — no outlines, no shadows.
        painter.setFont(self._status_font())
        painter.setPen(QColor(255, 255, 255))
        painter.drawText(QRect(0, 0, self.WIDTH, self.HEIGHT),
                         Qt.AlignHCenter | Qt.AlignVCenter, self._status)


def show_splash(app: QApplication) -> DynamicSplash:
    """Create and show the splash - at first just the line (see reveal())."""
    splash = DynamicSplash()
    splash.show()
    splash.repaint()
    app.processEvents()
    return splash


def close_splash(splash: DynamicSplash | None = None):
    """Fade the Qt splash out (it closes itself when done)."""
    if splash is not None:
        splash.fade_out()
