import os
import tempfile
import atexit
import shutil
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

from random_image_viewer.pdf_utils import _temp_dirs


def is_epub_file(file_path):
    """Check whether *file_path* points to an existing EPUB."""
    if not file_path:
        return False
    return os.path.isfile(file_path) and file_path.lower().endswith('.epub')


def _get_adaptive_dpi(epub_path, page_count):
    """Choose DPI based on file size and page count for speed/quality balance."""
    try:
        size_mb = os.path.getsize(epub_path) / (1024 * 1024)
    except OSError:
        size_mb = 0

    if size_mb > 200 or page_count > 200:
        return 150
    if size_mb > 100 or page_count > 100:
        return 175
    return 200


class EpubDocument:
    """Manages an EPUB opened for on-demand page rendering with a small LRU cache.

    PyMuPDF (fitz) treats EPUB pages just like PDF pages, so this class
    mirrors PdfDocument closely.
    """

    CACHE_SIZE = 10

    def __init__(self, epub_path):
        import fitz
        self.epub_path = epub_path
        self._doc = fitz.open(epub_path)
        self.page_count = self._doc.page_count
        self.dpi = _get_adaptive_dpi(epub_path, self.page_count)
        self._zoom = self.dpi / 72.0

        # LRU cache: page_num → JPEG file path
        self._cache = OrderedDict()

        # Temp dir for rendered page files
        base_name = os.path.splitext(os.path.basename(epub_path))[0]
        self._temp_dir = tempfile.mkdtemp(prefix=f"riv_epub_{base_name}_")
        _temp_dirs.append(self._temp_dir)

        self._lock = threading.Lock()
        self._prefetch_pool = ThreadPoolExecutor(max_workers=2)
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

    def close(self):
        """Release all resources."""
        self._prefetch_pool.shutdown(wait=False, cancel_futures=True)
        try:
            self._doc.close()
        except Exception:
            pass

    # ── internal ────────────────────────────────────────────────

    def _do_render(self, page_num):
        import fitz
        page = self._doc.load_page(page_num)
        mat = fitz.Matrix(self._zoom, self._zoom)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        out_path = os.path.join(self._temp_dir, f"page_{page_num + 1:04d}.jpg")
        pix.save(out_path, jpg_quality=92)
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
            print(f"EPUB prefetch error page {page_num}: {e}")

    def _evict(self):
        """Remove oldest entries beyond CACHE_SIZE (caller holds _lock)."""
        while len(self._cache) > self.CACHE_SIZE:
            _, old_path = self._cache.popitem(last=False)
            try:
                os.remove(old_path)
            except OSError:
                pass
