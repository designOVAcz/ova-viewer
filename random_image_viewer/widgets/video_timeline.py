"""The video timeline: click to jump, drag to scrub, hover to preview a frame.

Three pieces, kept out of the main window:

* :class:`SeekSlider` — a :class:`ClickableSlider` that also reports where the
  pointer is over the track, so a preview can follow it.
* :class:`ThumbnailWorker` — decodes one frame at a time off the GUI thread,
  always the most recently asked-for position, and remembers what it decoded.
* :class:`ThumbnailPopup` — the small frame-and-time card that follows the
  pointer along the timeline.
"""

from PySide6.QtCore import Qt, QThread, QMutex, QWaitCondition, Signal, QRectF
from PySide6.QtGui import QImage, QPixmap, QPainter, QColor, QPen, QFontMetrics
from PySide6.QtWidgets import QWidget

from random_image_viewer.widgets.clickable_slider import ClickableSlider


class SeekSlider(ClickableSlider):
    """A timeline you can click, drag, and hover over for a preview."""

    hover_time = Signal(int, int)   # value under the pointer, x in slider coords
    hover_left = Signal()

    def __init__(self, orientation=Qt.Horizontal, parent=None):
        super().__init__(orientation, parent)
        self.setMouseTracking(True)     # hover events without a button held

    def _emit_hover(self, pos):
        if self.maximum() > self.minimum():
            self.hover_time.emit(int(self.value_at(pos)), int(pos.x()))

    def mousePressEvent(self, event):
        super().mousePressEvent(event)
        if event.button() == Qt.LeftButton:
            self._emit_hover(event.position())

    def mouseMoveEvent(self, event):
        super().mouseMoveEvent(event)
        # Also while scrubbing: the preview is most useful exactly then.
        self._emit_hover(event.position())

    def leaveEvent(self, event):
        self.hover_left.emit()
        super().leaveEvent(event)


class ThumbnailWorker(QThread):
    """Decodes preview frames off the GUI thread, newest request first.

    Hovering asks for far more positions than can be decoded, so only the
    latest pending one is kept; everything else is dropped. Frames already
    decoded are remembered per second of video, which makes a second pass
    along the timeline instant.
    """

    ready = Signal(str, int, QImage)     # path, position in ms, frame

    WIDTH = 168          # preview width in pixels; height follows the video
    CACHE_LIMIT = 240    # roughly four minutes of one-second steps

    def __init__(self, parent=None):
        super().__init__(parent)
        self._mutex = QMutex()
        self._wait = QWaitCondition()
        self._pending = None
        self._stop = False
        self._cache = {}
        self._capture = None
        self._capture_path = None

    # ── called from the GUI thread ──
    def request(self, path, ms):
        """Ask for the frame at *ms*; served from memory when already decoded."""
        if not path:
            return
        cached = self._cache.get((path, max(0, ms) // 1000))
        if cached is not None:
            self.ready.emit(path, ms, cached)
            return
        self._mutex.lock()
        self._pending = (path, ms)
        self._mutex.unlock()
        self._wait.wakeAll()
        if not self.isRunning():
            self.start()

    def stop(self):
        self._mutex.lock()
        self._stop = True
        self._pending = None
        self._mutex.unlock()
        self._wait.wakeAll()
        self.wait(2000)

    # ── worker thread ──
    def run(self):
        try:
            import cv2
        except ImportError:
            return          # no previews without OpenCV; the timeline still works
        while True:
            self._mutex.lock()
            while self._pending is None and not self._stop:
                self._wait.wait(self._mutex)
            if self._stop:
                self._mutex.unlock()
                break
            path, ms = self._pending
            self._pending = None
            self._mutex.unlock()

            image = self._grab(cv2, path, ms)
            if image is not None and not image.isNull():
                if len(self._cache) > self.CACHE_LIMIT:
                    self._cache.clear()
                self._cache[(path, max(0, ms) // 1000)] = image
                self.ready.emit(path, ms, image)
        if self._capture is not None:
            self._capture.release()
            self._capture = None

    def _grab(self, cv2, path, ms):
        try:
            import numpy as np
            if self._capture is None or self._capture_path != path:
                if self._capture is not None:
                    self._capture.release()
                self._capture = cv2.VideoCapture(path)
                self._capture_path = path
            if not self._capture.isOpened():
                return None
            self._capture.set(cv2.CAP_PROP_POS_MSEC, max(0, ms))
            ok, frame = self._capture.read()
            if not ok or frame is None:
                return None
            h, w = frame.shape[:2]
            if w <= 0 or h <= 0:
                return None
            height = max(1, round(self.WIDTH * h / w))
            frame = cv2.resize(frame, (self.WIDTH, height), interpolation=cv2.INTER_AREA)
            frame = np.ascontiguousarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            image = QImage(frame.data, frame.shape[1], frame.shape[0],
                           frame.strides[0], QImage.Format_RGB888)
            return image.copy()          # own the pixels; frame is about to go
        except Exception as e:
            print(f"thumbnail decode failed: {e}")
            return None


class ThumbnailPopup(QWidget):
    """The preview card that follows the pointer along the timeline."""

    PAD = 6
    RADIUS = 6

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._pixmap = None
        self._text = ""
        self.hide()

    def set_frame(self, image):
        self._pixmap = QPixmap.fromImage(image) if image is not None else None
        self._resize()

    def set_text(self, text):
        self._text = text or ""
        self._resize()

    def clear_frame(self):
        self._pixmap = None
        self._resize()

    def _resize(self):
        fm = QFontMetrics(self.font())
        text_w = fm.horizontalAdvance(self._text) if self._text else 0
        text_h = fm.height() if self._text else 0
        frame_w = self._pixmap.width() if self._pixmap else 0
        frame_h = self._pixmap.height() if self._pixmap else 0
        width = max(frame_w, text_w) + 2 * self.PAD
        height = frame_h + text_h + 2 * self.PAD + (2 if frame_h and text_h else 0)
        self.resize(max(width, 40), max(height, 20))
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(QPen(QColor(255, 255, 255, 45), 1))
        painter.setBrush(QColor(18, 18, 18, 235))
        painter.drawRoundedRect(QRectF(0.5, 0.5, self.width() - 1, self.height() - 1),
                                self.RADIUS, self.RADIUS)
        y = self.PAD
        if self._pixmap is not None:
            x = (self.width() - self._pixmap.width()) // 2
            painter.drawPixmap(x, y, self._pixmap)
            y += self._pixmap.height() + 2
        if self._text:
            painter.setPen(QColor(235, 235, 235))
            painter.drawText(0, y, self.width(), QFontMetrics(self.font()).height(),
                             Qt.AlignHCenter | Qt.AlignVCenter, self._text)
        painter.end()
