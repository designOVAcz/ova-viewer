import os
import re
import time

from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QPixmap, QPainter, QFont, QIcon, QImageReader
from PySide6.QtCore import Qt, QSize, QObject, Signal

from random_image_viewer.constants import IMAGE_EXTENSIONS, VIDEO_EXTENSIONS, MEDIA_EXTENSIONS, PLAYLIST_EXTENSIONS


def is_animated_gif(file_path):
    """Check if a file is an animated GIF (has more than one frame)"""
    if not file_path or not file_path.lower().endswith('.gif'):
        return False
    try:
        reader = QImageReader(file_path)
        return reader.imageCount() > 1
    except Exception:
        return False


def is_video_file(file_path):
    """Check if a file is a supported video format"""
    if not file_path:
        return False
    return os.path.splitext(file_path)[1].lower() in VIDEO_EXTENSIONS


_SRT_TIME_RE = re.compile(
    r'(\d+):(\d{1,2}):(\d{1,2})[,.](\d{1,3})\s*-->\s*'
    r'(\d+):(\d{1,2}):(\d{1,2})[,.](\d{1,3})'
)
_SRT_TAG_RE = re.compile(r'<[^>]+>')


def find_subtitle_file(video_path):
    """Return the path to a sibling ``.srt`` with the same stem, or None.

    Matches the subtitle extension case-insensitively (``.srt`` / ``.SRT``).
    """
    if not video_path:
        return None
    base = os.path.splitext(video_path)[0]
    for ext in ('.srt', '.SRT'):
        candidate = base + ext
        if os.path.isfile(candidate):
            return candidate
    # Fall back to a case-insensitive directory scan on the stem
    try:
        folder = os.path.dirname(video_path) or '.'
        stem = os.path.basename(base).lower()
        for name in os.listdir(folder):
            n = name.lower()
            if n.endswith('.srt') and os.path.splitext(n)[0] == stem:
                return os.path.join(folder, name)
    except OSError:
        pass
    return None


def _srt_to_ms(h, m, s, frac):
    frac = (frac + '000')[:3]
    return ((int(h) * 60 + int(m)) * 60 + int(s)) * 1000 + int(frac)


def parse_srt(path):
    """Parse an SRT file into a sorted list of ``(start_ms, end_ms, text)``.

    Robust to common encodings, stray index lines and simple ``<tag>`` markup.
    Returns an empty list if the file cannot be read or has no cues.
    """
    try:
        with open(path, 'rb') as f:
            raw = f.read()
    except OSError:
        return []

    text = None
    for enc in ('utf-8-sig', 'utf-8', 'cp1252', 'latin-1'):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = raw.decode('utf-8', errors='replace')

    text = text.replace('\r\n', '\n').replace('\r', '\n')

    cues = []
    for block in re.split(r'\n{2,}', text.strip()):
        lines = block.split('\n')
        match = None
        t_idx = -1
        for i, line in enumerate(lines):
            match = _SRT_TIME_RE.search(line)
            if match:
                t_idx = i
                break
        if match is None or t_idx < 0:
            continue
        start = _srt_to_ms(match.group(1), match.group(2), match.group(3), match.group(4))
        end = _srt_to_ms(match.group(5), match.group(6), match.group(7), match.group(8))
        content = '\n'.join(lines[t_idx + 1:]).strip()
        content = _SRT_TAG_RE.sub('', content).strip()
        if content and end >= start:
            cues.append((start, end, content))

    cues.sort(key=lambda c: c[0])
    return cues


def get_image_file_size(file_path):
    """Get file size in MB for display purposes"""
    try:
        size_bytes = os.path.getsize(file_path)
        size_mb = size_bytes / (1024 * 1024)
        return size_mb
    except OSError:
        return 0


def smart_load_pixmap(file_path, max_dimension=2048):
    """Load pixmap with smart downscaling for better performance"""
    try:
        # Check file size first
        file_size_mb = get_image_file_size(file_path)

        # Use QImageReader for better control over loading
        reader = QImageReader(file_path)
        if not reader.canRead():
            return None, f"Cannot read image format"

        # Get original size without loading the full image
        original_size = reader.size()
        if not original_size.isValid():
            return None, f"Invalid image dimensions"

        # Calculate if we need to scale down for performance
        scale_factor = 1.0
        if original_size.width() > max_dimension or original_size.height() > max_dimension:
            scale_factor = max_dimension / max(original_size.width(), original_size.height())
            scaled_size = QSize(
                int(original_size.width() * scale_factor),
                int(original_size.height() * scale_factor)
            )
            reader.setScaledSize(scaled_size)

        # Load the image (potentially scaled)
        image = reader.read()
        if image.isNull():
            return None, f"Failed to load image"

        # Convert to pixmap
        pixmap = QPixmap.fromImage(image)

        # For very large files, warn but continue
        if file_size_mb > 100:
            print(f"Large image loaded with scaling ({file_size_mb:.1f} MB): {os.path.basename(file_path)}")

        return pixmap, None

    except Exception as e:
        error_msg = f"Error loading image: {str(e)}"
        print(f"Exception loading {os.path.basename(file_path)}: {error_msg}")
        return None, error_msg


def safe_load_pixmap(file_path):
    """Safely load a pixmap with error handling for large images"""
    # Use the smart loader for better performance
    return smart_load_pixmap(file_path)


def natural_sort_key(text):
    """
    Generate a key for natural sorting that handles numbers properly.
    This makes '1.jpg' come before '10.jpg' instead of after it.
    """
    def convert(text_part):
        return int(text_part) if text_part.isdigit() else text_part.lower()
    return [convert(c) for c in re.split(r'(\d+)', text)]


def get_images_in_folder(folder):
    image_paths = []
    for root, _, files in os.walk(folder):
        # Sort files in each directory using natural sorting for proper number ordering
        for f in sorted(files, key=natural_sort_key):
            if os.path.splitext(f)[1].lower() in MEDIA_EXTENSIONS:
                image_paths.append(os.path.join(root, f))
    # Final global sort using natural sorting so overall list is in proper order
    image_paths.sort(key=lambda p: natural_sort_key(os.path.basename(p)))
    return image_paths


def get_playlist_items_in_folder(folder):
    """Recursively collect playlist-eligible files (images, videos, GIFs, and
    documents — PDF/EPUB/CBR/CBZ) under *folder*.

    Mirrors :func:`get_images_in_folder` but with the wider extension set so
    a single mixed folder drop can produce a unified playlist.
    """
    items = []
    for root, _, files in os.walk(folder):
        for f in sorted(files, key=natural_sort_key):
            if os.path.splitext(f)[1].lower() in PLAYLIST_EXTENSIONS:
                items.append(os.path.join(root, f))
    items.sort(key=lambda p: natural_sort_key(os.path.basename(p)))
    return items


def emoji_icon(emoji="🎲", size=128):
    pix = QPixmap(size, size)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    f = QFont()
    f.setPointSize(int(size * 0.7))
    p.setFont(f)
    p.drawText(pix.rect(), Qt.AlignCenter, emoji)
    p.end()
    return QIcon(pix)


class PlaylistScanner(QObject):
    """Background worker that walks one or more paths and emits the resulting
    media list. Designed to be moved onto a ``QThread`` so the GUI thread stays
    responsive while scanning huge folder trees (10k+ files).

    Signals
    -------
    progress(int found, str current_dir)
        Emitted at most ~10 times per second with the running count of matched
        files and the directory currently being walked.
    finished(list items)
        Emitted once with the deduplicated, naturally sorted final list of
        absolute file paths.
    cancelled()
        Emitted if :meth:`cancel` was called before the walk completed.
    """

    progress = Signal(int, str)
    finished = Signal(list)
    cancelled = Signal()

    # Minimum interval between two progress emissions (seconds).
    _PROGRESS_INTERVAL = 0.1

    def __init__(self, paths, mode="playlist", parent=None):
        super().__init__(parent)
        self._paths = list(paths)
        self._mode = mode  # "playlist" or "images"
        self._cancel = False

    def cancel(self):
        """Request cooperative cancellation. The next directory boundary will
        stop the walk and emit :attr:`cancelled`."""
        self._cancel = True

    def run(self):
        """Entry point. Wire ``QThread.started`` to this slot."""
        if self._mode == "images":
            extensions = MEDIA_EXTENSIONS
        else:
            extensions = PLAYLIST_EXTENSIONS

        items = []
        last_emit = 0.0
        # Emit an initial 0-found progress so the UI updates immediately.
        self.progress.emit(0, "")

        for top in self._paths:
            if self._cancel:
                self.cancelled.emit()
                return
            if not top:
                continue
            if os.path.isdir(top):
                for root, _dirs, files in os.walk(top):
                    if self._cancel:
                        self.cancelled.emit()
                        return
                    for f in sorted(files, key=natural_sort_key):
                        if os.path.splitext(f)[1].lower() in extensions:
                            items.append(os.path.join(root, f))
                    now = time.monotonic()
                    if now - last_emit >= self._PROGRESS_INTERVAL:
                        last_emit = now
                        self.progress.emit(len(items), root)
            elif os.path.isfile(top):
                if os.path.splitext(top)[1].lower() in extensions:
                    items.append(top)

        # Deduplicate while preserving order, then natural-sort by basename.
        seen = set()
        deduped = []
        for it in items:
            if it not in seen:
                seen.add(it)
                deduped.append(it)
        deduped.sort(key=lambda p: natural_sort_key(os.path.basename(p)))

        if self._cancel:
            self.cancelled.emit()
            return

        self.finished.emit(deduped)
