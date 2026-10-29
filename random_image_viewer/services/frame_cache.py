"""Decode-ahead cache for playing a folder of pictures as a film.

At 24 fps a frame has about 40 ms to get from disk to screen, which a
full-size decode on the GUI thread cannot do. So:

* **Decode at screen size** — ``QImageReader`` with a scaled size decodes a
  JPEG straight at 1/2, 1/4 or 1/8 resolution, the same trick the grid's
  thumbnails use. A 24 MP photo shown on a 1080p screen costs a fraction of
  a full decode, and 8 MB of memory instead of 96.
* **Worker threads, in order** — the workers always take the lowest frame of
  the window ahead of the play head that is neither decoded nor in hand, so
  the next frame is ready first. Qt's image readers release the GIL, so the
  threads really decode in parallel.
* **A window, not the folder** — only the next second or two of frames are
  kept (bounded by a memory budget). Moving the play head drops what lies
  behind it; changing the sequence or the screen size starts a new
  generation, and frames still being decoded for the old one are discarded.

The GUI thread only asks ``frame(i)`` for a ready ``QImage`` and turns it into
a pixmap.
"""

import os
import threading

from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QImage, QImageReader, QImageIOHandler

from random_image_viewer.services.thumbnails import _decode_with_pillow

AHEAD_SECONDS = 2.0                     # how far ahead of the play head to decode
FRAME_BUDGET_BYTES = 768 * 1024 ** 2    # all decoded frames together
MIN_AHEAD = 4


def decode_frame(path, box):
    """*path* decoded to fit inside *box* (never enlarged), EXIF rotation applied.

    Returns a null ``QImage`` when the file cannot be read."""
    reader = QImageReader(path)
    reader.setAutoTransform(True)
    size = reader.size()
    if size.isValid():
        # The scaled size is applied before the EXIF rotation, so a picture
        # stored on its side has to fit a box turned the same way.
        fit_box = box
        if reader.transformation() & QImageIOHandler.Transformation.TransformationRotate90:
            fit_box = box.transposed()
        if size.width() > fit_box.width() or size.height() > fit_box.height():
            reader.setScaledSize(size.scaled(fit_box, Qt.KeepAspectRatio))
    image = reader.read()
    if image.isNull():
        image = _decode_with_pillow(path, max(box.width(), box.height()))
    if image.isNull():
        return image
    # Convert here, off the GUI thread, to the format a pixmap paints fastest.
    fmt = QImage.Format_ARGB32_Premultiplied if image.hasAlphaChannel() else QImage.Format_RGB32
    return image if image.format() == fmt else image.convertToFormat(fmt)


class FrameCache:
    """Decodes the frames just ahead of a play head on background threads."""

    def __init__(self, threads=None):
        self._cond = threading.Condition()
        self._paths = []
        self._box = QSize()
        self._loop = False
        self._pos = 0
        self._ahead = MIN_AHEAD
        self._gen = 0
        self._frames = {}       # index -> QImage (null when the file is unreadable)
        self._busy = set()      # indices a worker is decoding right now
        self._stopped = False
        self._threads = []
        self._thread_count = threads or max(2, min(6, (os.cpu_count() or 4) - 1))

    def reset(self, paths, box, loop=False):
        """Start over with a new sequence and/or frame size."""
        with self._cond:
            self._paths = list(paths)
            self._box = QSize(box)
            self._loop = loop
            self._pos = 0
            self._gen += 1
            self._frames.clear()
            self._cond.notify_all()
        if self._paths:
            self._start_workers()

    def set_loop(self, loop):
        with self._cond:
            if loop != self._loop:
                self._loop = loop
                self._trim()
                self._cond.notify_all()

    def seek(self, pos, fps):
        """Move the play head to *pos*: drop frames behind it, decode ahead."""
        with self._cond:
            self._pos = pos
            frame_bytes = max(1, self._box.width() * self._box.height() * 4)
            self._ahead = max(MIN_AHEAD, min(int(fps * AHEAD_SECONDS),
                                             FRAME_BUDGET_BYTES // frame_bytes))
            self._trim()
            self._cond.notify_all()

    def frame(self, index):
        """The decoded frame at *index*, or None while it is not ready yet."""
        with self._cond:
            return self._frames.get(index)

    def ahead(self):
        with self._cond:
            return self._ahead

    def clear(self):
        """Free every decoded frame; workers go idle."""
        self.reset([], QSize())

    def stop(self):
        with self._cond:
            self._stopped = True
            self._frames.clear()
            self._cond.notify_all()

    # ── internals (called with the lock held) ─────────────────────

    def _window(self):
        n = len(self._paths)
        count = min(self._ahead, n)
        if self._loop:
            return [(self._pos + k) % n for k in range(count)]
        return list(range(self._pos, min(n, self._pos + count)))

    def _trim(self):
        keep = set(self._window())
        for i in [i for i in self._frames if i not in keep]:
            del self._frames[i]

    def _next_job(self):
        for i in self._window():
            if i not in self._frames and i not in self._busy:
                return i
        return None

    def _start_workers(self):
        if self._threads:
            return
        for _ in range(self._thread_count):
            t = threading.Thread(target=self._run, name="frame-decoder", daemon=True)
            t.start()
            self._threads.append(t)

    def _run(self):
        while True:
            with self._cond:
                while True:
                    if self._stopped:
                        return
                    index = self._next_job()
                    if index is not None:
                        break
                    self._cond.wait()
                self._busy.add(index)
                gen, path, box = self._gen, self._paths[index], QSize(self._box)
            try:
                image = decode_frame(path, box)
            except Exception:
                image = QImage()
            with self._cond:
                self._busy.discard(index)
                if gen == self._gen and index in self._window():
                    self._frames[index] = image
                self._cond.notify_all()
