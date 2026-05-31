"""Comic-book archive (``.cbr`` / ``.cbz``) support.

Mirrors :class:`random_image_viewer.pdf_utils.PdfDocument` so the viewer can
treat a comic archive like a paginated document: on-demand extraction of one
page at a time, a small LRU cache, background prefetch, and optional 2/3-page
side-by-side spreads sharing the existing PDF spread UI.

``.cbz`` files are handled with the stdlib :mod:`zipfile` module. ``.cbr``
files are RAR archives and require the optional :mod:`rarfile` package plus a
working ``unrar`` executable on ``PATH`` (or bundled next to the EXE). The
import is deferred so the rest of the app stays usable when ``rarfile`` isn't
installed.
"""

import os
import tempfile
import threading
import zipfile
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

from random_image_viewer.constants import IMAGE_EXTENSIONS
from random_image_viewer.image_utils import natural_sort_key
from random_image_viewer.pdf_utils import _temp_dirs, _MAX_RENDER_LONG_EDGE


CBR_EXTENSIONS = {'.cbr', '.cbz'}


def is_cbr_file(file_path):
    """Return True if *file_path* points to an existing ``.cbr``/``.cbz``."""
    if not file_path:
        return False
    if not os.path.isfile(file_path):
        return False
    return os.path.splitext(file_path)[1].lower() in CBR_EXTENSIONS


def _is_image_entry(name):
    """Filter helper: is *name* an image entry we should treat as a page?"""
    if not name or name.endswith('/') or name.endswith('\\'):
        return False
    base = os.path.basename(name)
    if not base or base.startswith('.') or base.startswith('__MACOSX'):
        return False
    ext = os.path.splitext(base)[1].lower()
    return ext in IMAGE_EXTENSIONS


def _looks_like_zip(file_path):
    """Magic-byte sniff: True if *file_path* starts with a ZIP signature.

    Many ``.cbr`` files in the wild are actually ZIP archives that were
    just renamed; checking the first 4 bytes lets us route them to the
    stdlib zipfile backend without needing an unrar tool at all.
    """
    try:
        with open(file_path, 'rb') as f:
            head = f.read(4)
    except OSError:
        return False
    # ZIP local file header / empty archive / spanned archive signatures.
    return head in (b'PK\x03\x04', b'PK\x05\x06', b'PK\x07\x08')


class _ZipBackend:
    """Tiny adapter so CBR and CBZ share one extraction code path."""

    def __init__(self, path):
        self._zf = zipfile.ZipFile(path, 'r')

    def names(self):
        return [
            zi.filename for zi in self._zf.infolist() if not zi.is_dir()
        ]

    def read(self, name):
        return self._zf.read(name)

    def close(self):
        try:
            self._zf.close()
        except Exception:
            pass


class _RarBackend:
    """Adapter around :mod:`rarfile` matching :class:`_ZipBackend`."""

    # Common Windows install locations for an unrar-compatible extractor.
    # Used to auto-configure ``rarfile.UNRAR_TOOL`` when none is on PATH so
    # the user doesn't have to set up environment variables manually.
    _CANDIDATE_TOOLS = (
        r"C:\Program Files\WinRAR\UnRAR.exe",
        r"C:\Program Files (x86)\WinRAR\UnRAR.exe",
        r"C:\Program Files\WinRAR\Rar.exe",
        r"C:\Program Files (x86)\WinRAR\Rar.exe",
        r"C:\Program Files\7-Zip\7z.exe",
        r"C:\Program Files (x86)\7-Zip\7z.exe",
    )

    @classmethod
    def _ensure_tool_configured(cls):
        """Point ``rarfile`` at a working extractor, searching common paths."""
        import shutil
        import rarfile

        # If the default tool already resolves on PATH, leave it alone.
        if shutil.which(rarfile.UNRAR_TOOL):
            return
        for candidate in cls._CANDIDATE_TOOLS:
            if os.path.isfile(candidate):
                # 7-Zip needs a different argument set; rarfile supports it
                # via ALT_TOOL but the simpler path is to keep UNRAR_TOOL
                # pointing at unrar/Rar when available and only fall back
                # to 7z when no WinRAR install exists.
                if candidate.lower().endswith("7z.exe"):
                    rarfile.UNRAR_TOOL = candidate
                    # 7z uses different command-line syntax than unrar; let
                    # rarfile know via ALT_TOOL when it supports it.
                    if hasattr(rarfile, "ALT_TOOL"):
                        rarfile.ALT_TOOL = candidate
                else:
                    rarfile.UNRAR_TOOL = candidate
                return

    def __init__(self, path):
        import rarfile  # lazy: only required for .cbr
        self._ensure_tool_configured()
        # Verify the extractor is actually usable before opening the archive
        # so missing-tool errors surface in _load_cbr (where they're handled
        # cleanly) instead of later during page rendering.
        try:
            rarfile.tool_setup()
        except rarfile.RarCannotExec as e:
            raise RuntimeError(
                "CBR support needs 'unrar.exe' (or WinRAR/7-Zip) available. "
                "Install WinRAR, or place 'unrar.exe' on PATH. "
                f"Original error: {e}") from e
        self._rf = rarfile.RarFile(path)

    def names(self):
        return [
            ri.filename for ri in self._rf.infolist() if not ri.isdir()
        ]

    def read(self, name):
        return self._rf.read(name)

    def close(self):
        try:
            self._rf.close()
        except Exception:
            pass


class CbrDocument:
    """A comic archive opened for on-demand page extraction.

    Public surface mirrors :class:`PdfDocument` so :mod:`main_window` can drive
    it through the same toolbar / spread / prefetch plumbing.
    """

    CACHE_SIZE = 16

    def __init__(self, archive_path):
        self.archive_path = archive_path

        ext = os.path.splitext(archive_path)[1].lower()
        if ext == '.cbz':
            self._backend = _ZipBackend(archive_path)
        elif ext == '.cbr':
            # Many .cbr files in the wild are actually ZIP archives with the
            # wrong extension. Try ZIP first when the file's magic bytes
            # match, so we don't even need an unrar tool for those.
            if _looks_like_zip(archive_path):
                self._backend = _ZipBackend(archive_path)
            else:
                try:
                    self._backend = _RarBackend(archive_path)
                except ImportError as e:
                    raise RuntimeError(
                        "CBR support requires the 'rarfile' package. "
                        "Install it with: pip install rarfile") from e
                except Exception as e:
                    # rarfile raises various subclasses (NotRarFile,
                    # NeedFirstVolume, RarCannotExec, ...). If the archive
                    # isn't actually RAR, transparently fall back to ZIP.
                    msg = str(e) or e.__class__.__name__
                    if 'not a rar' in msg.lower() or 'notrar' in e.__class__.__name__.lower():
                        try:
                            self._backend = _ZipBackend(archive_path)
                        except Exception:
                            raise RuntimeError(
                                "File has .cbr extension but is neither a "
                                "RAR nor a ZIP archive.") from e
                    elif 'unrar' in msg.lower() or 'cannot exec' in msg.lower():
                        raise RuntimeError(
                            "CBR support needs 'unrar.exe' on PATH (or "
                            "bundled next to the app). Original error: "
                            + msg) from e
                    else:
                        raise
        else:
            raise ValueError(f"Unsupported archive extension: {ext}")

        # Natural-sort image entries by full path so chapter folders stay
        # in order while filenames within a folder sort numerically.
        all_names = self._backend.names()
        self._entries = sorted(
            (n for n in all_names if _is_image_entry(n)),
            key=lambda p: (
                natural_sort_key(os.path.dirname(p)),
                natural_sort_key(os.path.basename(p)),
            ),
        )
        self.page_count = len(self._entries)
        if self.page_count == 0:
            self._backend.close()
            raise ValueError(
                "Archive contains no images: " + os.path.basename(archive_path))

        # LRU cache: page_num (int) or ("spread", start, count) → file path
        self._cache = OrderedDict()

        base_name = os.path.splitext(os.path.basename(archive_path))[0]
        self._temp_dir = tempfile.mkdtemp(prefix=f"riv_cbr_{base_name}_")
        _temp_dirs.append(self._temp_dir)

        self._lock = threading.Lock()
        # zipfile/rarfile objects are not safe for concurrent reads; serialize.
        self._archive_lock = threading.Lock()
        self._prefetch_pool = ThreadPoolExecutor(max_workers=1)
        self._prefetch_pending = set()

    # ── public API ──────────────────────────────────────────────

    def render_page(self, page_num):
        """Return the file path for *page_num* (0-based), extracting if needed."""
        if page_num < 0 or page_num >= self.page_count:
            return None

        with self._lock:
            if page_num in self._cache:
                self._cache.move_to_end(page_num)
                return self._cache[page_num]

        path = self._do_extract(page_num)

        with self._lock:
            self._cache[page_num] = path
            self._cache.move_to_end(page_num)
            self._evict()

        return path

    def prefetch_around(self, page_num, radius=3):
        """Schedule background extraction of pages near *page_num*."""
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

        Identical composition strategy to
        :meth:`PdfDocument.render_spread` — pages are painted side-by-side
        with a 6px black gutter, capped to ``_MAX_RENDER_LONG_EDGE`` on the
        tallest dimension. The composite is itself LRU-cached.
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

        from PySide6.QtGui import QPixmap, QPainter
        from PySide6.QtCore import Qt

        pixmaps = [QPixmap(p) for p in page_paths]
        pixmaps = [pm for pm in pixmaps if not pm.isNull()]
        if not pixmaps:
            return None

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
        """Warm the page cache for the next/previous spread(s)."""
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
        try:
            self._prefetch_pool.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
        with self._archive_lock:
            self._backend.close()

    # ── internal ────────────────────────────────────────────────

    def _do_extract(self, page_num):
        entry = self._entries[page_num]
        with self._archive_lock:
            data = self._backend.read(entry)

        # Preserve the original extension so downstream code (QImageReader)
        # can pick the right decoder. Fall back to .jpg when unknown.
        ext = os.path.splitext(entry)[1].lower()
        if ext not in IMAGE_EXTENSIONS:
            ext = '.jpg'

        out_path = os.path.join(
            self._temp_dir, f"page_{page_num + 1:04d}{ext}")
        with open(out_path, 'wb') as f:
            f.write(data)
        return out_path

    def _prefetch_one(self, page_num):
        try:
            path = self._do_extract(page_num)
            with self._lock:
                self._prefetch_pending.discard(page_num)
                if page_num not in self._cache:
                    self._cache[page_num] = path
                    self._cache.move_to_end(page_num)
                    self._evict()
        except Exception as e:
            with self._lock:
                self._prefetch_pending.discard(page_num)
            print(f"CBR prefetch error page {page_num}: {e}")

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
