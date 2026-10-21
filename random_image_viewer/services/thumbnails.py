"""Background thumbnail pipeline for the grid view.

Three parts, all off the GUI thread:

* **Decoders** — one per kind of item. Images go through ``QImageReader`` with
  a scaled size, so a JPEG is decoded straight at 1/2, 1/4 or 1/8 resolution
  instead of full size and shrunk afterwards (the single biggest speed-up).
  Videos give a frame 10 % in (the first frame is often black), documents
  their first page, comic archives their first picture.
* **Disk cache** — every thumbnail is written once to
  ``%LOCALAPPDATA%\\OvaViewer\\thumbs``. The file name is a hash of the path,
  modification time and size, so an edited file simply gets a new entry and
  the old one ages out when the cache passes its size cap.
* **Priority queue** — the grid hands over only what is on or near the
  screen, *in order*. Every hand-over replaces the queue, so work for cells
  scrolled away is dropped instead of piling up, and a folder of any size
  costs only what you actually look at.
* **Sizes** — the standard thumbnail is 384 px on the long edge. Big cells
  (few per row, zoomed in, high-DPI screens) ask for 640 up to 3200 px
  versions.

Workers are plain threads: Qt's image readers, OpenCV and Pillow all release
the GIL while they decode, so they run truly in parallel.
"""

import hashlib
import json
import os
import threading
import time
import zipfile
from collections import OrderedDict

from PySide6.QtCore import QObject, Signal, QSize, QBuffer, QByteArray, QIODevice
from PySide6.QtGui import QImage, QImageReader, QImageIOHandler

from random_image_viewer.constants import (
    IMAGE_EXTENSIONS, VIDEO_EXTENSIONS, DOCUMENT_EXTENSIONS,
)

THUMB_EDGE = 384                    # long edge of a standard cached thumbnail, px
SHARP_EDGES = (640, 960, 1440, 2160, 3200)       # larger tiers, made only for big cells near the view
# Ratio assumed when no header can tell (a document before its first thumbnail).
GUESS_ASPECT = {'image': 1.5, 'video': 16 / 9, 'doc': 0.72}
CACHE_CAP_BYTES = 1024 ** 3         # 1 GB on disk, oldest entries evicted
JPEG_QUALITY = 85

# PyMuPDF is not thread-safe, even across separate documents.
_FITZ_LOCK = threading.Lock()

# A page of a PDF / EPUB / comic archive as a grid item: "<file>::page::<n>"
# (0-based). Everything — cache, size reading, decoding — accepts it like a path.
PAGE_SEP = "::page::"


def page_item(path, n):
    return f"{path}{PAGE_SEP}{n}"


def split_page_item(item):
    """(document path, 0-based page) for a page item, else None."""
    if PAGE_SEP not in item:
        return None
    path, n = item.rsplit(PAGE_SEP, 1)
    return path, int(n)


def display_name(item):
    """File name, or "book.pdf · p. 12" for a page."""
    page = split_page_item(item)
    if page is None:
        return os.path.basename(item)
    return f"{os.path.basename(page[0])} · p. {page[1] + 1}"


def item_kind(path):
    """'image', 'video' or 'doc' — drives the decoder and the placeholder."""
    if PAGE_SEP in path:
        return 'image'          # a single page shows as a picture
    ext = os.path.splitext(path)[1].lower()
    if ext in VIDEO_EXTENSIONS:
        return 'video'
    if ext in DOCUMENT_EXTENSIONS:
        return 'doc'
    return 'image'


# ── disk cache ───────────────────────────────────────────────────

def cache_dir():
    base = os.environ.get('LOCALAPPDATA') or os.path.expanduser('~')
    return os.path.join(base, 'OvaViewer', 'thumbs')


def _cache_key(path, st, edge):
    raw = f"{os.path.normcase(os.path.abspath(path))}|{st.st_mtime_ns}|{st.st_size}|{edge}"
    return hashlib.sha1(raw.encode('utf-8', 'surrogatepass')).hexdigest()


def _cache_file(key, ext):
    # Two-character shards keep any one folder small.
    return os.path.join(cache_dir(), key[:2], key + ext)


def _find_cached(path, edge=THUMB_EDGE):
    """(key, cached file) for *path* at *edge*; the file is None if not cached.
    A page item is keyed by its document's file plus the page number."""
    page = split_page_item(path)
    try:
        key = _cache_key(path, os.stat(page[0] if page else path), edge)
    except OSError:
        return None, None
    for ext in ('.jpg', '.png'):
        f = _cache_file(key, ext)
        if os.path.isfile(f):
            return key, f
    return key, None


def _store_cached(key, image):
    if key is None or image.isNull():
        return
    # Transparency survives as PNG; everything else is a much smaller JPEG.
    ext, fmt, quality = ('.png', 'PNG', -1) if image.hasAlphaChannel() else ('.jpg', 'JPG', JPEG_QUALITY)
    target = _cache_file(key, ext)
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        # Write then rename, so a parallel reader never sees half a file.
        tmp = f"{target}.{threading.get_ident()}.tmp"
        if image.save(tmp, fmt, quality):
            os.replace(tmp, target)
    except OSError:
        pass


def prune_cache(cap=CACHE_CAP_BYTES):
    """Delete the least recently used thumbnails until the cache fits *cap*."""
    root = cache_dir()
    entries, total = [], 0
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            full = os.path.join(dirpath, name)
            try:
                st = os.stat(full)
            except OSError:
                continue
            if name.endswith('.tmp') and time.time() - st.st_mtime > 3600:
                _remove(full)       # left behind by a crash mid-write
                continue
            entries.append((st.st_mtime, st.st_size, full))
            total += st.st_size
    if total <= cap:
        return
    entries.sort()
    goal = cap * 0.8                # free a margin so this does not run every time
    for _mtime, size, full in entries:
        if total <= goal:
            break
        if _remove(full):
            total -= size


def _remove(path):
    try:
        os.remove(path)
        return True
    except OSError:
        return False


# ── decoders (worker threads) ────────────────────────────────────

def _fit(w, h, edge=THUMB_EDGE):
    if max(w, h) <= edge:
        return w, h
    s = edge / max(w, h)
    return max(1, round(w * s)), max(1, round(h * s))


def _read_scaled(reader, edge):
    """Decode through *reader* at thumbnail size, honouring EXIF rotation."""
    reader.setAutoTransform(True)
    if not reader.canRead():
        return QImage()
    size = reader.size()
    if size.isValid():
        w, h = _fit(size.width(), size.height(), edge)
        reader.setScaledSize(QSize(w, h))
    return reader.read()


def _decode_with_pillow(source, edge):
    """Fallback for formats Qt has no plugin for (e.g. JPEG 2000)."""
    try:
        from PIL import Image
        with Image.open(source) as im:
            im.draft('RGB', (edge, edge))   # cheap JPEG downscale
            im.thumbnail((edge, edge))
            im = im.convert('RGBA')
            data = im.tobytes('raw', 'RGBA')
            return QImage(data, im.width, im.height, QImage.Format_RGBA8888).copy()
    except Exception:
        return QImage()


def _decode_image(path, edge):
    image = _read_scaled(QImageReader(path), edge)
    return image if not image.isNull() else _decode_with_pillow(path, edge)


def _decode_video(path, edge):
    try:
        import cv2
    except ImportError:
        return QImage()
    cap = cv2.VideoCapture(path)
    try:
        if not cap.isOpened():
            return QImage()
        frames = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
        if frames > 10:
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(frames * 0.1))
        ok, frame = cap.read()
        if not ok or frame is None:
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, frame = cap.read()
        if not ok or frame is None:
            return QImage()
        h, w = frame.shape[:2]
        tw, th = _fit(w, h, edge)
        if (tw, th) != (w, h):
            frame = cv2.resize(frame, (tw, th), interpolation=cv2.INTER_AREA)
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        return QImage(rgb.data, tw, th, 3 * tw, QImage.Format_RGB888).copy()
    finally:
        cap.release()


# ── documents: pages of PDF / EPUB / comic archives ──────────────
#
# A page grid asks for many pages of one document, so the opened document
# (or archive) is kept for the next page instead of being re-opened — an
# EPUB lays out the whole book when it opens. A couple at most are kept.

_OPEN_DOCS_MAX = 2
_FITZ_DOCS = OrderedDict()          # path -> pymupdf document; use under _FITZ_LOCK
_ARCHIVES = OrderedDict()           # path -> (backend, page names, lock)
_ARCHIVES_LOCK = threading.Lock()


def _fitz_doc(path):
    """The open PyMuPDF document for *path*. Call with _FITZ_LOCK held."""
    import pymupdf
    doc = _FITZ_DOCS.pop(path, None)
    if doc is None:
        doc = pymupdf.open(path)
    _FITZ_DOCS[path] = doc
    while len(_FITZ_DOCS) > _OPEN_DOCS_MAX:
        _FITZ_DOCS.popitem(last=False)[1].close()
    return doc


def _archive(path):
    """(backend, page entry names in the viewer's order, lock) for a comic."""
    from random_image_viewer.cbr_utils import _is_image_entry, _ZipBackend, _RarBackend
    from random_image_viewer.image_utils import natural_sort_key
    with _ARCHIVES_LOCK:
        entry = _ARCHIVES.pop(path, None)
        if entry is None:
            backend = _ZipBackend(path) if zipfile.is_zipfile(path) else _RarBackend(path)
            pages = sorted((n for n in backend.names() if _is_image_entry(n)),
                           key=lambda p: (natural_sort_key(os.path.dirname(p)),
                                          natural_sort_key(os.path.basename(p))))
            entry = (backend, pages, threading.Lock())
        _ARCHIVES[path] = entry
        while len(_ARCHIVES) > _OPEN_DOCS_MAX:
            old_backend, _pages, old_lock = _ARCHIVES.popitem(last=False)[1]
            with old_lock:
                old_backend.close()
        return entry


def release_documents():
    """Close every kept document and archive (the grid was closed), so the
    files are not held open — Windows would refuse to move or delete them."""
    with _FITZ_LOCK:
        while _FITZ_DOCS:
            _FITZ_DOCS.popitem()[1].close()
    with _ARCHIVES_LOCK:
        while _ARCHIVES:
            backend, _pages, lock = _ARCHIVES.popitem()[1]
            with lock:
                backend.close()


def _decode_bytes(data, edge):
    buf = QBuffer()
    buf.setData(QByteArray(data))
    buf.open(QIODevice.ReadOnly)
    image = _read_scaled(QImageReader(buf), edge)
    buf.close()
    if image.isNull():
        import io
        image = _decode_with_pillow(io.BytesIO(data), edge)
    return image


def _decode_page(path, n, edge):
    """Page *n* of a PDF / EPUB / CBZ / CBR at thumbnail size."""
    if os.path.splitext(path)[1].lower() in ('.pdf', '.epub'):
        import pymupdf
        with _FITZ_LOCK:
            doc = _fitz_doc(path)
            if not 0 <= n < doc.page_count:
                return QImage()
            page = doc[n]
            r = page.rect
            zoom = edge / max(r.width, r.height, 1)
            pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
            return QImage(pix.samples, pix.width, pix.height, pix.stride,
                          QImage.Format_RGB888).copy()
    backend, pages, lock = _archive(path)
    if not 0 <= n < len(pages):
        return QImage()
    with lock:
        data = backend.read(pages[n])
    return _decode_bytes(data, edge) if data else QImage()


def _probe_page(path, n):
    """Shape of page *n* without rendering it, or None."""
    ext = os.path.splitext(path)[1].lower()
    if ext in ('.pdf', '.epub'):
        with _FITZ_LOCK:
            doc = _fitz_doc(path)
            if 0 <= n < doc.page_count:
                r = doc[n].rect
                return r.width / r.height if r.height else None
        return None
    backend, pages, lock = _archive(path)
    zf = getattr(backend, '_zf', None)
    if zf is None or not 0 <= n < len(pages):
        return None     # RAR: reading an entry means running unrar — too slow just for a size
    with lock, zf.open(pages[n]) as fh:
        head = fh.read(256 * 1024)          # image headers sit at the start
    buf = QBuffer()
    buf.setData(QByteArray(head))
    buf.open(QIODevice.ReadOnly)
    size = QImageReader(buf).size()
    return size.width() / size.height() if size.isValid() and size.height() > 0 else None


def make_thumbnail(path, edge=THUMB_EDGE):
    """Decode a fresh thumbnail for *path* (a file, or a page item) with long
    edge *edge* (or the original size if smaller). Returns a (possibly null)
    QImage. A document file on its own shows its first page."""
    try:
        page = split_page_item(path)
        if page is not None:
            return _decode_page(page[0], page[1], edge)
        ext = os.path.splitext(path)[1].lower()
        if ext in VIDEO_EXTENSIONS:
            return _decode_video(path, edge)
        if ext in DOCUMENT_EXTENSIONS:
            return _decode_page(path, 0, edge)
        return _decode_image(path, edge)
    except Exception as e:
        print(f"Thumbnail failed for {os.path.basename(path)}: {e}")
        return QImage()


def _probe_video(path):
    try:
        import cv2
    except ImportError:
        return None
    cap = cv2.VideoCapture(path)
    try:
        w = cap.get(cv2.CAP_PROP_FRAME_WIDTH)
        h = cap.get(cv2.CAP_PROP_FRAME_HEIGHT)
        return w / h if w > 0 and h > 0 else None
    finally:
        cap.release()


def probe_aspect(path):
    """Width / height of *path* from headers only — never decodes pixels.

    Always returns a ratio: when nothing cheap can tell (a document with no
    cached thumbnail yet) it returns a typical one for the kind, so the grid
    can place every cell once and never move it again.
    """
    page = split_page_item(path)
    if page is not None:
        try:
            a = _probe_page(*page)
        except Exception:
            a = None
        return a or GUESS_ASPECT['doc']
    kind = item_kind(path)
    if kind == 'image':
        reader = QImageReader(path)
        reader.setAutoTransform(True)
        size = reader.size()
        if size.isValid() and size.height() > 0:
            w, h = size.width(), size.height()
            try:
                if reader.transformation() & QImageIOHandler.Transformation.TransformationRotate90:
                    w, h = h, w
            except Exception:
                pass
            return w / h
        try:
            from PIL import Image
            with Image.open(path) as im:
                w, h = im.size
                if h:
                    return w / h
        except Exception:
            pass
    elif kind == 'video':
        a = _probe_video(path)
        if a:
            return a
    _key, cached = _find_cached(path)
    if cached:
        size = QImageReader(cached).size()
        if size.isValid() and size.height() > 0:
            return size.width() / size.height()
    return GUESS_ASPECT[kind]


# ── remembered picture sizes ─────────────────────────────────────

def _aspect_index_file():
    return os.path.join(cache_dir(), 'aspects.json')


def load_aspect_index():
    """path → width/height for every item a grid has measured before.

    Lets the justified layout of a folder seen before appear at once instead
    of opening every file again. Entries are checked against each thumbnail
    as it arrives, so an edited file corrects itself.
    """
    try:
        with open(_aspect_index_file(), encoding='utf-8') as fh:
            data = json.load(fh)
        return {p: float(a) for p, a in data.items()}
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


def save_aspect_index(aspects, limit=200_000):
    """Write *aspects* (newest last) back, keeping at most *limit* entries."""
    items = list(aspects.items())[-limit:]
    target = _aspect_index_file()
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        tmp = target + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump({p: round(a, 4) for p, a in items}, fh, separators=(',', ':'))
        os.replace(tmp, target)
    except OSError:
        pass


# ── loader ───────────────────────────────────────────────────────

class ThumbLoader(QObject):
    """Turns (path, edge) jobs into thumbnails on a pool of worker threads.

    The grid hands over only what is on or near the screen, most important
    first, and every :meth:`request` replaces the whole queue — work for
    cells scrolled past is dropped instead of piling up. Nothing else is ever
    decoded, so a folder of any size costs only what you look at.

    ``ready(path, image, edge)`` fires per finished job (null image = could
    not be decoded). ``aspects(dict, gen)`` streams header-probed ratios in
    playlist order and ``probe_done(gen)`` follows the last batch.
    """

    ready = Signal(str, QImage, int)
    aspects = Signal(dict, int)
    probe_done = Signal(int)

    def __init__(self, parent=None, threads=None):
        super().__init__(parent)
        self._cond = threading.Condition()
        self._queue = []            # reversed priority order: pop() = most urgent
        self._inflight = set()
        self._stop = False
        self._threads = []
        self._thread_count = threads or max(2, (os.cpu_count() or 4) - 1)
        self._probe_gen = 0
        self._pruned = False

    def request(self, jobs):
        """Replace the queue with *jobs* ((path, edge) pairs), most urgent first."""
        with self._cond:
            if self._stop:
                return
            inflight = self._inflight
            self._queue = [job for job in reversed(jobs) if job not in inflight]
            if self._queue and not self._threads:
                self._start_workers()
            self._cond.notify_all()
        if not self._pruned:
            self._pruned = True
            threading.Thread(target=prune_cache, daemon=True, name="thumb-prune").start()

    def probe(self, paths):
        """Header-probe *paths* in order on a background thread. Returns the
        generation number its signals carry; a newer call cancels this one."""
        self._probe_gen += 1
        gen = self._probe_gen
        threading.Thread(target=self._run_probe, args=(list(paths), gen),
                         daemon=True, name="thumb-probe").start()
        return gen

    def cancel_probe(self):
        self._probe_gen += 1

    def stop(self):
        with self._cond:
            self._stop = True
            self._queue = []
            self._probe_gen += 1
            self._cond.notify_all()
        for t in self._threads:
            t.join(timeout=1.0)

    # ── worker threads ──
    def _start_workers(self):
        for i in range(self._thread_count):
            t = threading.Thread(target=self._run, daemon=True, name=f"thumb-{i}")
            t.start()
            self._threads.append(t)

    def _run(self):
        while True:
            with self._cond:
                while not self._queue and not self._stop:
                    self._cond.wait()
                if self._stop:
                    return
                job = self._queue.pop()
                self._inflight.add(job)
            path, edge = job
            try:
                image = self._produce(path, edge)
            finally:
                with self._cond:
                    self._inflight.discard(job)
                    stopping = self._stop
            if stopping:
                return
            self.ready.emit(path, image, edge)

    @staticmethod
    def _produce(path, edge):
        key, cached = _find_cached(path, edge)
        if cached:
            image = QImageReader(cached).read()
            if not image.isNull():
                try:
                    os.utime(cached)        # mark as recently used for pruning
                except OSError:
                    pass
                return image
        image = make_thumbnail(path, edge)
        _store_cached(key, image)
        return image

    def _run_probe(self, paths, gen):
        batch, last = {}, time.monotonic()
        for path in paths:
            if gen != self._probe_gen:
                return
            try:
                batch[path] = probe_aspect(path)
            except Exception:
                batch[path] = GUESS_ASPECT[item_kind(path)]
            now = time.monotonic()
            if len(batch) >= 500 or now - last > 0.1:
                self.aspects.emit(batch, gen)
                batch, last = {}, now
        if gen == self._probe_gen:
            if batch:
                self.aspects.emit(batch, gen)
            self.probe_done.emit(gen)
