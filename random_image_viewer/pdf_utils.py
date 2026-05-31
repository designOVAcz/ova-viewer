import os
import tempfile
import atexit
import shutil
import time
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed

from PySide6.QtCore import QThread, Signal


# Keep track of temp dirs so they can be cleaned up on exit
_temp_dirs = []


def _cleanup_temp_dirs():
    for d in _temp_dirs:
        try:
            shutil.rmtree(d, ignore_errors=True)
        except Exception:
            pass


atexit.register(_cleanup_temp_dirs)


def is_pdf_file(file_path):
    """Check whether *file_path* points to an existing PDF."""
    if not file_path:
        return False
    return os.path.isfile(file_path) and file_path.lower().endswith('.pdf')


def _get_adaptive_dpi(pdf_path, page_count):
    """Choose DPI based on file size and page count for speed/quality balance."""
    try:
        size_mb = os.path.getsize(pdf_path) / (1024 * 1024)
    except OSError:
        size_mb = 0

    if size_mb > 200 or page_count > 200:
        return 150
    if size_mb > 100 or page_count > 100:
        return 175
    return 200


# Cap rendered output to roughly this many pixels on the longest side.
# The viewer downscales to ~2048 anyway, so going much higher just wastes
# CPU and memory (e.g. comic-book PDFs whose native pages are already huge).
_MAX_RENDER_LONG_EDGE = 2600


class PdfDocument:
    """Manages a PDF opened for on-demand page rendering with a small LRU cache.

    Pages are rendered only when requested.  A background thread pre-fetches
    nearby pages so forward/backward navigation feels instant.
    """

    # Cache holds rendered single pages AND composite spread images.
    # 16 leaves room for ~10 single pages plus several 2/3-page composites
    # without thrashing.
    CACHE_SIZE = 16

    def __init__(self, pdf_path):
        import fitz
        self.pdf_path = pdf_path
        self._doc = fitz.open(pdf_path)
        self.page_count = self._doc.page_count
        self.dpi = _get_adaptive_dpi(pdf_path, self.page_count)
        self._zoom = self.dpi / 72.0

        # LRU cache: page_num → JPEG file path
        self._cache = OrderedDict()

        # Temp dir for rendered page files
        base_name = os.path.splitext(os.path.basename(pdf_path))[0]
        self._temp_dir = tempfile.mkdtemp(prefix=f"riv_pdf_{base_name}_")
        _temp_dirs.append(self._temp_dir)

        self._lock = threading.Lock()
        # PyMuPDF documents are NOT thread-safe; serialize all access
        # to self._doc (rendering, page loads, etc.) with this lock.
        self._fitz_lock = threading.Lock()
        self._prefetch_pool = ThreadPoolExecutor(max_workers=1)
        self._prefetch_pending = set()

    # ── public API ──────────────────────────────────────────────

    def render_page(self, page_num):
        """Return the file path for *page_num* (0-based), rendering if needed."""
        if page_num < 0 or page_num >= self.page_count:
            return None

        with self._lock:
            if page_num in self._cache:
                self._cache.move_to_end(page_num)
                return self._cache[page_num]

        # Render synchronously (first display of this page)
        path = self._do_render(page_num)

        with self._lock:
            self._cache[page_num] = path
            self._cache.move_to_end(page_num)
            self._evict()

        return path

    def prefetch_around(self, page_num, radius=3):
        """Schedule background rendering of pages near *page_num*."""
        for offset in range(1, radius + 1):
            for pn in (page_num + offset, page_num - offset):
                if 0 <= pn < self.page_count:
                    with self._lock:
                        if pn in self._cache or pn in self._prefetch_pending:
                            continue
                        self._prefetch_pending.add(pn)
                    self._prefetch_pool.submit(self._prefetch_one, pn)

    def render_spread(self, start_page, count):
        """Render *count* pages starting at *start_page* into one composite JPEG.

        Returns the file path to the composite. For ``count == 1`` this is just
        :meth:`render_page`. For 2 or 3, individual pages are rendered (re-using
        the existing cache) and painted side-by-side with a small black gap on
        a single ``QPixmap`` that is saved into the same temp dir. The composite
        is itself LRU-cached so re-displaying the same spread is instant.
        """
        if count <= 1:
            return self.render_page(start_page)
        if start_page < 0 or start_page >= self.page_count:
            return None

        cache_key = ("spread", start_page, count)
        with self._lock:
            if cache_key in self._cache:
                self._cache.move_to_end(cache_key)
                return self._cache[cache_key]

        # Render the individual pages first (each call is itself cached + locked).
        page_paths = []
        for offset in range(count):
            pn = start_page + offset
            if pn >= self.page_count:
                break
            p = self.render_page(pn)
            if p:
                page_paths.append(p)

        if not page_paths:
            return None

        # Compose with QPainter. Imported lazily so this module stays usable
        # in environments where Qt isn't loaded yet at import time.
        from PySide6.QtGui import QPixmap, QPainter
        from PySide6.QtCore import Qt

        pixmaps = [QPixmap(p) for p in page_paths]
        pixmaps = [pm for pm in pixmaps if not pm.isNull()]
        if not pixmaps:
            return None

        # Common target height = the tallest input, capped at the same long-edge
        # limit used for single-page renders so the composite stays a reasonable
        # size for the display pipeline.
        target_height = min(
            max(pm.height() for pm in pixmaps), _MAX_RENDER_LONG_EDGE)

        scaled = [
            pm.scaledToHeight(target_height, Qt.SmoothTransformation)
            for pm in pixmaps
        ]

        gap = 6  # px of black gutter between pages
        total_width = sum(pm.width() for pm in scaled) + gap * (len(scaled) - 1)

        composite = QPixmap(total_width, target_height)
        composite.fill(Qt.black)

        painter = QPainter(composite)
        try:
            x = 0
            for pm in scaled:
                # Vertically center each page in case heights differ slightly
                # after scaling rounding.
                y = (target_height - pm.height()) // 2
                painter.drawPixmap(x, y, pm)
                x += pm.width() + gap
        finally:
            painter.end()

        out_path = os.path.join(
            self._temp_dir,
            f"spread_{start_page + 1:04d}_x{count}.jpg")
        composite.save(out_path, "JPG", 85)

        with self._lock:
            self._cache[cache_key] = out_path
            self._cache.move_to_end(cache_key)
            self._evict()

        return out_path

    def prefetch_spread_around(self, start_page, count, radius=1):
        """Warm the individual page cache for the next/previous spread(s).

        Composing the spread on the main thread is cheap (a couple of
        ``drawPixmap`` calls) once the underlying page JPEGs are rendered,
        so we only need to prefetch the raw pages here.
        """
        if count <= 1:
            self.prefetch_around(start_page, radius=3)
            return
        for offset in range(1, radius + 1):
            for anchor in (start_page + offset * count,
                           start_page - offset * count):
                for k in range(count):
                    pn = anchor + k
                    if 0 <= pn < self.page_count:
                        with self._lock:
                            if (pn in self._cache or
                                    pn in self._prefetch_pending):
                                continue
                            self._prefetch_pending.add(pn)
                        self._prefetch_pool.submit(self._prefetch_one, pn)

    def close(self):
        """Release all resources."""
        self._prefetch_pool.shutdown(wait=True, cancel_futures=True)
        try:
            with self._fitz_lock:
                self._doc.close()
        except Exception:
            pass

    # ── internal ────────────────────────────────────────────────

    def _do_render(self, page_num):
        import fitz
        # PyMuPDF is not thread-safe — serialize document access so that
        # the main thread (synchronous renders) and the prefetch worker
        # never touch self._doc concurrently.
        with self._fitz_lock:
            page = self._doc.load_page(page_num)
            rect = page.rect
            page_long_edge_pt = max(rect.width, rect.height)
            # Start with the file-level adaptive zoom, then clamp so the
            # rendered pixmap never exceeds _MAX_RENDER_LONG_EDGE on its
            # longest side. This keeps comic-book / scanned PDFs (whose
            # native pages are already thousands of points wide) from
            # producing 100+ megapixel pixmaps that hang the UI.
            zoom = self._zoom
            if page_long_edge_pt > 0:
                max_zoom = _MAX_RENDER_LONG_EDGE / page_long_edge_pt
                if max_zoom < zoom:
                    zoom = max_zoom
            mat = fitz.Matrix(zoom, zoom)
            pix = page.get_pixmap(matrix=mat, alpha=False)
            out_path = os.path.join(
                self._temp_dir, f"page_{page_num + 1:04d}.jpg")
            pix.save(out_path, jpg_quality=85)
        return out_path

    def _prefetch_one(self, page_num):
        try:
            path = self._do_render(page_num)
            with self._lock:
                self._prefetch_pending.discard(page_num)
                if page_num not in self._cache:
                    self._cache[page_num] = path
                    self._cache.move_to_end(page_num)
                    self._evict()
        except Exception as e:
            with self._lock:
                self._prefetch_pending.discard(page_num)
            print(f"Prefetch error page {page_num}: {e}")

    def _evict(self):
        """Remove oldest entries beyond CACHE_SIZE (caller holds _lock)."""
        while len(self._cache) > self.CACHE_SIZE:
            _, old_path = self._cache.popitem(last=False)
            if not old_path:
                continue
            try:
                os.remove(old_path)
            except OSError:
                pass
