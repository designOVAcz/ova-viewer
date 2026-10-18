"""Thumbnail grid: the whole folder at a glance, PureRef-style.

Built so that a folder of tens of thousands of files opens at once and
stays responsive:

* **Two layouts.** *Justified rows* (default) fill each row edge to edge;
  that needs every picture's shape, read from file headers in order and
  remembered on disk, so a folder is only ever read once. Rows are only
  appended below, and a placed cell never moves. The plain *grid* gives every
  item the same square cell, so it needs nothing from the files at all.
* **A canvas with a camera.** The layout is computed once, in "canvas" pixels
  that fill the window width. On top sits a camera — a zoom factor and an
  x / y offset. Right-drag (or middle-drag) pans in any direction with a bit
  of inertia; right button + wheel (or Ctrl + wheel) zooms around the cursor.
  Zoom never goes below "fits the width", so no more cells are ever on screen
  — and decoded — than in the plain view.
* **Loads only what you see.** The cells on screen and a margin around them —
  capped at a few hundred — are the only things decoded. Every move replaces
  the queue, so cells you passed are dropped. A thin line along the bottom
  and the status bar show how much of the current view is loaded.
* **Virtualised.** No widget per thumbnail: the layout is a few flat lists
  and painting touches only the rows in view (binary search).
* **Cheap frames.** Each thumbnail is kept pre-scaled to its cell's exact
  device-pixel size, so drawing a cell is a plain copy. Panning and scrolling
  shift the pixels already on screen and paint only the strip that comes into
  view; an arriving thumbnail repaints only its own cell. While a zoom is in
  progress the thumbnails are drawn scaled on the fly, and re-cut sharp at
  the final size once it settles.
* **Sharp at any size.** Small cells use 384 px thumbnails; big or zoomed
  cells get 640 / 960 / 1440 / 2160 / 3200 px versions — only for what is
  on screen, and a cell bigger than the screen is drawn straight from its
  thumbnail rather than through a cell-sized copy.
* **Bounded memory.** Decoded pixmaps live in an LRU capped by bytes.
* **GPU drawing (optional).** The GPU toggle swaps the viewport for a
  ``QOpenGLWidget`` so compositing runs on the graphics card.
"""

import bisect
import json
import math
import os
import time
from collections import OrderedDict, deque

from PySide6.QtWidgets import (
    QAbstractScrollArea, QFrame, QWidget, QToolButton, QLabel, QHBoxLayout,
)
from PySide6.QtCore import Qt, QTimer, QRectF, QRect, QEvent, QPoint, Signal
from PySide6.QtGui import QPainter, QPixmap, QColor, QFont, QPen, QFontMetrics

from random_image_viewer.platform_utils import is_windows_dark_mode
from random_image_viewer.services.thumbnails import (
    ThumbLoader, item_kind, THUMB_EDGE, SHARP_EDGES, GUESS_ASPECT,
    load_aspect_index, save_aspect_index,
)
from random_image_viewer.widgets.clickable_slider import ClickableSlider
from random_image_viewer.widgets.floating_panel import FloatingPanel

EDGES = (THUMB_EDGE,) + SHARP_EDGES
PIXMAP_CACHE_BYTES = 320 * 1024 * 1024
WINDOW_MAX = 400            # most items decoded around the view at any time
MAX_PER_ROW = 12            # more tiny cells means hundreds of full-size decodes per screen
REFERENCE_ASPECT = 1.5      # justified 'per row' counts 3:2 pictures; fixed so rows never depend on load order
FIT_ALL_MIN_ROW = 40        # px — below this "fit all" gives up and scrolls
FIT_ALL_MAX_ITEMS = 3000    # past this, fitting everything is pointless
MAX_ZOOM = 40.0
ZOOM_STEP = 1.15            # per wheel notch
ZOOM_SETTLE_MS = 160        # after the last zoom step, re-cut thumbnails sharp
PAN_FRICTION = 4.5          # inertia decay per second (higher stops sooner)
SETTINGS_KEY = "thumb_grid"


def symbol_font():
    """Segoe UI lacks ▦ / ⛶ / ▤; naming the font that has them skips Qt's
    slow scan of every installed font for a fallback."""
    return QFont("Segoe UI Symbol", 11)


class _Thumb:
    """A decoded thumbnail plus its copy scaled to the cell it sits in."""
    __slots__ = ('pix', 'scaled', 'edge', 'full')

    def __init__(self, pix, edge):
        self.pix = pix
        self.scaled = None
        self.edge = edge
        # Smaller than asked for: the source itself is that small, so no
        # larger size would be any sharper.
        self.full = max(pix.width(), pix.height()) < edge * 0.98

    def sharp_enough(self, edge):
        return self.full or self.edge >= edge

    def nbytes(self):
        n = self.pix.width() * self.pix.height() * 4
        if self.scaled is not None:
            n += self.scaled.width() * self.scaled.height() * 4
        return n


class _ProgressLine(QWidget):
    """A thin loading line along the bottom of the grid."""

    def __init__(self, parent, color):
        super().__init__(parent)
        self._color = color
        self._fraction = 0.0
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.hide()

    def set_fraction(self, f):
        self._fraction = max(0.0, min(1.0, f))
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(0, 0, 0, 90))
        p.fillRect(QRect(0, 0, round(self.width() * self._fraction), self.height()), self._color)


class ThumbGrid(QAbstractScrollArea):
    """Scrollable, zoomable, pannable grid of every item in the playlist."""

    activated = Signal(str)          # open this path in the viewer
    close_requested = Signal()       # Esc / G / back button
    open_folder_requested = Signal()
    file_types_requested = Signal()
    progress = Signal(int, int)      # thumbnails loaded / wanted around the view

    # Keys the grid handles itself even though the main window has global
    # QShortcuts on them (accepting ShortcutOverride routes them here).
    _OWN_KEYS = {
        Qt.Key_Left, Qt.Key_Right, Qt.Key_Up, Qt.Key_Down, Qt.Key_Home,
        Qt.Key_End, Qt.Key_PageUp, Qt.Key_PageDown, Qt.Key_Return,
        Qt.Key_Enter, Qt.Key_Space, Qt.Key_Escape, Qt.Key_G, Qt.Key_Delete,
    }
    _ZOOM_KEYS = {Qt.Key_Plus, Qt.Key_Equal, Qt.Key_Minus, Qt.Key_0}

    def __init__(self, parent=None, settings=None):
        super().__init__(parent)
        self._settings = settings
        self.setFrameShape(QFrame.NoFrame)
        self.setFocusPolicy(Qt.StrongFocus)
        # The horizontal scrollbar stays hidden but still holds the x offset.
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)   # no width flip while rows append

        dark = is_windows_dark_mode()
        self._bg = QColor('#1b1d1f' if dark else '#e9eaec')
        self._placeholder = QColor('#26292c' if dark else '#d9dbde')
        self._glyph = QColor('#6c7177' if dark else '#8a8f95')
        self._accent = QColor('#4a90d9')

        # Items and per-item state
        self._items = []
        self._index = {}
        self._aspect = load_aspect_index()   # path -> width / height
        self._aspect_dirty = False
        self._thumbs = OrderedDict()         # path -> _Thumb, LRU order
        self._bytes = 0
        self._failed = set()
        self._current = None
        self._selected = -1
        self._hover = -1
        self._probe_gen = 0
        self._probing = False
        self._center_on = -1                 # scroll here once it has been placed
        self._window = set()                 # paths wanted around the view
        self._last_progress = None

        # Layout, in canvas pixels (= screen pixels at zoom 1). Flat lists;
        # index = row or item. Items [0, _placed) have cells.
        self._row_y, self._row_h, self._row_start = [], [], []
        self._cell_x, self._cell_w = [], []
        self._placed = 0
        self._next_y = 0.0
        self._target = 100.0
        self._content_w = 0
        self._content_h = 0

        # Camera: canvas -> screen is (canvas * zoom) - (scrollbar offsets).
        self._zoom = 1.0
        self._zooming = False
        self._zoom_settle = QTimer(self, singleShot=True, interval=ZOOM_SETTLE_MS)
        self._zoom_settle.timeout.connect(self._on_zoom_settled)

        # Right / middle drag panning, with inertia after release.
        self._pan = None                     # active drag state
        self._right_wheeled = False          # wheel turned while right is held
        self._velocity = (0.0, 0.0)
        self._inertia_t = 0.0
        self._inertia = QTimer(self, interval=16)
        self._inertia.timeout.connect(self._inertia_step)

        # Click-to-centre: the camera glides to a target offset.
        self._glide_target = (0.0, 0.0)
        self._glide = QTimer(self, interval=16)
        self._glide.timeout.connect(self._glide_step)

        # Options (persisted)
        self._per_row = 6
        self._gap = 6
        self._names = False
        self._fit_all = False
        self._gpu = False
        self._justified = True
        self._load_settings()

        self._loader = ThumbLoader(self)
        self._loader.ready.connect(self._on_thumb)
        self._loader.aspects.connect(self._on_aspects)
        self._loader.probe_done.connect(self._on_probe_done)

        self._request_timer = QTimer(self, singleShot=True, interval=30)
        self._request_timer.timeout.connect(self._update_requests)

        # Smooth wheel scrolling: the wheel moves a target, a 60 fps timer eases
        # the scrollbar towards it.
        self._scroll_target = 0.0
        self._scroll_timer = QTimer(self, interval=16)
        self._scroll_timer.timeout.connect(self._scroll_step)
        self.verticalScrollBar().valueChanged.connect(self._on_scrolled)
        self.horizontalScrollBar().valueChanged.connect(self._on_scrolled)

        self._progress_line = _ProgressLine(self, self._accent)
        self._gpu_viewport_set = False
        self._apply_viewport()
        self._build_panel()

    # ── public API ───────────────────────────────────────────────
    def set_items(self, paths, current=None):
        """Show *paths*. Shapes and thumbnails already known are kept."""
        self._items = list(paths)
        self._index = {p: i for i, p in enumerate(self._items)}
        self._hover = -1
        self._current = current
        self._selected = self._index.get(current, 0 if self._items else -1)
        self._center_on = self._selected
        self._zoom = 1.0
        self._set_offset(0, 0)
        self._start_probe_if_needed()
        self._reset_layout()
        self._update_status()

    def set_current(self, path):
        self._current = path
        self.viewport().update()

    def suspend(self):
        """Grid hidden: stop decoding and motion, save what was measured."""
        self._loader.request([])
        self._scroll_timer.stop()
        self._inertia.stop()
        self._save_aspects()

    def shutdown(self):
        self._loader.stop()
        self._save_aspects()

    def track(self, widget):
        """Keep this grid laid exactly over *widget* (the image canvas)."""
        self._tracked = widget
        widget.installEventFilter(self)
        self.setGeometry(widget.geometry())

    # ── settings ─────────────────────────────────────────────────
    def _load_settings(self):
        if self._settings is None:
            return
        try:
            st = json.loads(self._settings.value(SETTINGS_KEY, "") or "{}")
        except Exception:
            st = {}
        self._per_row = max(1, min(MAX_PER_ROW, int(st.get("per_row", self._per_row))))
        self._gap = int(st.get("gap", self._gap))
        self._names = bool(st.get("names", self._names))
        self._fit_all = bool(st.get("fit_all", self._fit_all))
        # GPU is not restored: every session starts on the CPU, which opens
        # instantly (creating the OpenGL surface costs about half a second).
        # The panel is not restored either: it opens collapsed, bottom-right.
        self._justified = st.get("layout", "justified") == "justified"

    def _save_settings(self):
        if self._settings is None:
            return
        st = {"per_row": self._per_row, "gap": self._gap, "names": self._names,
              "fit_all": self._fit_all,
              "layout": "justified" if self._justified else "grid"}
        self._settings.setValue(SETTINGS_KEY, json.dumps(st))

    def _save_aspects(self):
        if self._aspect_dirty:
            self._aspect_dirty = False
            save_aspect_index(self._aspect)

    # ── floating panel ───────────────────────────────────────────
    def _tool(self, text, tip, checkable=False, checked=False):
        b = QToolButton()
        b.setText(text)
        b.setToolTip(tip)
        b.setCheckable(checkable)
        b.setChecked(checked)
        b.setFixedSize(24, 24)
        b.setFocusPolicy(Qt.NoFocus)
        if len(text) == 1 and ord(text) < 0x2B00:
            b.setFont(symbol_font())
        return b

    def _slider_box(self, glyph, tip, lo, hi, value, width):
        box = QWidget()
        lay = QHBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(3)
        icon = QLabel(glyph)
        icon.setFont(symbol_font())
        slider = ClickableSlider(Qt.Horizontal)
        slider.setRange(lo, hi)
        slider.setValue(value)
        slider.setFixedWidth(width)
        slider.setFocusPolicy(Qt.NoFocus)
        value_label = QLabel(str(value))
        value_label.setFixedWidth(16)
        slider.valueChanged.connect(lambda v: value_label.setText(str(v)))
        for w in (icon, slider, value_label):
            lay.addWidget(w)
        box.setToolTip(tip)
        box.setFixedHeight(24)
        return box, slider

    def _build_panel(self):
        p = FloatingPanel("GRID", self)
        back = self._tool("🖼", "Back to the viewer (Esc or G)")
        back.clicked.connect(self.close_requested)
        open_btn = self._tool("📂", "Open folder")
        open_btn.clicked.connect(self.open_folder_requested)
        types_btn = self._tool("🗂", "File Types: show only the types you check")
        types_btn.clicked.connect(self.file_types_requested)

        row_box, self._row_slider = self._slider_box(
            "▦", "Pictures per row (lays the grid out again).\n"
                 "To enlarge without re-flowing: hold the right button and turn the wheel.",
            1, MAX_PER_ROW, self._per_row, 90)
        self._row_slider.valueChanged.connect(self._on_per_row)
        gap_box, gap_slider = self._slider_box("↔", "Gap between pictures", 0, 24, self._gap, 60)
        gap_slider.valueChanged.connect(self._on_gap)

        self._justify_btn = self._tool(
            "▤", "Justified rows: fill each row edge to edge, no empty space.\n"
                 "Reads every picture's size first (once per folder). Off: equal "
                 "square cells, instant even for very large folders.", True, self._justified)
        self._justify_btn.toggled.connect(self._on_justified)
        self._fit_btn = self._tool("⛶", "Fit all: size the cells so everything fills the screen "
                                   f"(up to {FIT_ALL_MAX_ITEMS} items)", True, self._fit_all)
        self._fit_btn.toggled.connect(self._on_fit_all)
        names_btn = self._tool("Aa", "Show file names", True, self._names)
        names_btn.toggled.connect(self._on_names)
        self._gpu_btn = self._tool("GPU", "Draw the grid on the graphics card (OpenGL).\n"
                                          "Off at every start: the CPU path opens instantly.", True, self._gpu)
        self._gpu_btn.setFixedWidth(34)
        self._gpu_btn.toggled.connect(self._on_gpu)

        self._count_label = QLabel("")
        self._count_label.setFixedHeight(24)
        self._count_label.setMinimumWidth(170)
        self._count_label.setToolTip("Right-drag: pan · right button + wheel: zoom · Ctrl+0: fit width")

        for w in (back, open_btn, types_btn):
            p.add_tool(w)
        p.add_separator()
        for w in (row_box, gap_box, self._justify_btn, self._fit_btn, names_btn, self._gpu_btn):
            p.add_tool(w)
        p.add_separator()
        p.add_tool(self._count_label)
        p.finalize()
        p.moved_by_user.connect(p.clamp_into_parent)
        self._panel = p
        self._dock_panel()

    def _dock_panel(self):
        """Collapsed, in the bottom-right corner (clear of the scrollbar and
        the loading line) — how the panel looks every time the grid opens."""
        self._panel.set_collapsed(True, emit=False)
        self._panel.dock('right', 'bottom', self.verticalScrollBar().width() + 8, 10)
        self._panel.raise_()

    def _on_per_row(self, v):
        self._per_row = v
        if self._fit_all:
            self._fit_btn.setChecked(False)     # picking a size ends "fit all"
        self._relayout_keeping_view(reset_zoom=True)
        self._save_settings()

    def _on_gap(self, v):
        self._gap = v
        self._relayout_keeping_view(reset_zoom=True)
        self._save_settings()

    def _on_justified(self, on):
        self._justified = on
        self._start_probe_if_needed()
        self._relayout_keeping_view(reset_zoom=True)
        self._save_settings()

    def _on_fit_all(self, on):
        self._fit_all = on
        self._relayout_keeping_view(reset_zoom=True)
        self._save_settings()

    def _on_names(self, on):
        self._names = on
        self.viewport().update()
        self._save_settings()

    def _on_gpu(self, on):
        self._gpu = on
        self._apply_viewport()
        self._save_settings()

    # ── progress ─────────────────────────────────────────────────
    def _update_status(self):
        """Panel text, loading line and the progress signal."""
        n = len(self._items)
        total = len(self._window)
        done = sum(1 for p in self._window if p in self._thumbs or p in self._failed)
        zoom = f" · {round(self._zoom * 100)}%" if self._zoom > 1.001 else ""
        if self._probing:
            text = f"{n} items · reading sizes {self._placed * 100 // max(1, n)}%"
            fraction = self._placed / max(1, n)
        elif done < total:
            text = f"{n} items · loading {done * 100 // total}%{zoom}"
            fraction = done / total
        else:
            text, fraction = f"{n} items{zoom}", 1.0
        self._count_label.setText(text)
        busy = fraction < 1.0 and self.isVisible()
        self._progress_line.setVisible(busy)
        if busy:
            self._progress_line.set_fraction(fraction)
            self._progress_line.raise_()
        state = (done, total)
        if state != self._last_progress:
            self._last_progress = state
            self.progress.emit(done, total)

    def _place_progress_line(self):
        vp = self.viewport().geometry()
        self._progress_line.setGeometry(vp.left(), vp.bottom() - 2, vp.width(), 3)

    # ── GPU / raster viewport ────────────────────────────────────
    def _apply_viewport(self):
        vp = None
        if self._gpu:
            try:
                from PySide6.QtOpenGLWidgets import QOpenGLWidget
                vp = QOpenGLWidget()
            except Exception as e:
                print(f"OpenGL viewport unavailable, using CPU drawing: {e}")
                self._gpu = False
                if hasattr(self, '_gpu_btn'):
                    self._gpu_btn.blockSignals(True)
                    self._gpu_btn.setChecked(False)
                    self._gpu_btn.setEnabled(False)
                    self._gpu_btn.blockSignals(False)
        if vp is None:
            if self._gpu_viewport_set:
                vp = QWidget()
            else:
                # Keep the built-in viewport: swapping viewports costs ~0.5 s
                # inside the main window.
                vp = self.viewport()
            vp.setAttribute(Qt.WA_OpaquePaintEvent, True)
        vp.setMouseTracking(True)
        if vp is not self.viewport():
            self.setViewport(vp)
        self._gpu_viewport_set = self._gpu
        if hasattr(self, '_panel'):
            self._panel.raise_()
        self._progress_line.raise_()
        vp.update()

    # ── layout (canvas pixels) ───────────────────────────────────
    def _start_probe_if_needed(self):
        """Justified rows need every shape; read the unknown ones in order."""
        unknown = [p for p in self._items if p not in self._aspect] if self._justified else []
        self._probing = bool(unknown)
        if unknown:
            self._probe_gen = self._loader.probe(unknown)
        else:
            self._loader.cancel_probe()

    def _avail_width(self):
        return max(50, self.viewport().width() - 2 * self._gap)

    def _layout_height(self, target, W, aspects):
        """Justified content height for row target *target* (no storage)."""
        gap = self._gap
        y, count, total_a = gap, 0, 0.0
        for a in aspects:
            count += 1
            total_a += a
            if total_a * target + gap * (count - 1) >= W:
                y += (W - gap * (count - 1)) / total_a + gap
                count, total_a = 0, 0.0
        if count:
            y += target + gap
        return y

    def _fit_all_height(self, W):
        """Largest justified row height at which every item fits on one screen."""
        vh = self.viewport().height()
        aspects = [self._aspect.get(p, GUESS_ASPECT[item_kind(p)]) for p in self._items]
        if len(aspects) > FIT_ALL_MAX_ITEMS or \
                self._layout_height(FIT_ALL_MIN_ROW, W, aspects) > vh:
            return FIT_ALL_MIN_ROW
        lo, hi = FIT_ALL_MIN_ROW, float(max(vh, FIT_ALL_MIN_ROW))
        for _ in range(18):
            mid = (lo + hi) / 2
            if self._layout_height(mid, W, aspects) <= vh:
                lo = mid
            else:
                hi = mid
        return lo

    def _fit_all_columns(self, W):
        """Fewest grid columns at which every item fits on one screen."""
        n, gap, vh = len(self._items), self._gap, self.viewport().height()
        smallest = max(1, int((W + gap) / (FIT_ALL_MIN_ROW + gap)))
        if n > FIT_ALL_MAX_ITEMS:
            return smallest
        for cols in range(1, smallest + 1):
            cell = (W - gap * (cols - 1)) / cols
            if gap + math.ceil(n / cols) * (cell + gap) <= vh:
                return cols
        return smallest

    def _reset_layout(self):
        """Throw the layout away and lay out everything that can be placed."""
        n = len(self._items)
        self._row_y, self._row_h, self._row_start = [], [], []
        self._cell_x, self._cell_w = [0] * n, [0] * n
        self._placed = 0
        self._next_y = float(self._gap)
        self._content_w = self._avail_width() + 2 * self._gap
        self._drop_scaled()
        if self._justified:
            W = self._avail_width()
            if self._fit_all:
                # Fitting needs every shape; until the probe ends nothing is placed.
                self._target = self._fit_all_height(W) if not self._probing else 0
            else:
                n_row = max(1, self._per_row)
                self._target = max(24.0, (W - self._gap * (n_row - 1)) / (n_row * REFERENCE_ASPECT))
            self._extend_layout()
        else:
            self._layout_uniform()
        self.viewport().update()

    def _drop_scaled(self):
        """Forget every cell-sized copy (cell sizes on screen changed)."""
        for t in self._thumbs.values():
            if t.scaled is not None:
                self._bytes -= t.scaled.width() * t.scaled.height() * 4
                t.scaled = None

    def _layout_uniform(self):
        """Equal square cells: needs nothing from the files, so it is instant."""
        n, gap = len(self._items), self._gap
        W = self._avail_width()
        cols = self._fit_all_columns(W) if self._fit_all else max(1, self._per_row)
        cell = max(16.0, (W - gap * (cols - 1)) / cols)
        xs = [round(gap + c * (cell + gap)) for c in range(cols)]
        ws = [round(gap + c * (cell + gap) + cell) - xs[c] for c in range(cols)]
        rows = math.ceil(n / cols)
        self._row_y = [round(gap + r * (cell + gap)) for r in range(rows)]
        self._row_h = [round(cell)] * rows
        self._row_start = list(range(0, n, cols))
        self._cell_x = [xs[i % cols] for i in range(n)]
        self._cell_w = [ws[i % cols] for i in range(n)]
        self._placed = n
        self._next_y = gap + rows * (cell + gap)
        self._target = cell
        self._layout_grew()

    def _extend_layout(self):
        """Justified: append rows for newly known items. Placed cells never change."""
        if self._target <= 0:
            return
        items, aspect = self._items, self._aspect
        n = len(items)
        gap = self._gap
        W = self._avail_width()
        target = self._target
        start = self._placed
        total_a = 0.0
        i = start
        while i < n:
            a = aspect.get(items[i])
            if a is None:
                break
            total_a += a
            count = i - start + 1
            if total_a * target + gap * (count - 1) >= W:
                self._place_row(start, i + 1, (W - gap * (count - 1)) / total_a, True)
                start, total_a = i + 1, 0.0
            i += 1
        # The last, short row waits until every shape is in, so it is placed once.
        if i == n and start < n and not self._probing:
            self._place_row(start, n, target, False)
            start = n
        if start == self._placed:
            return
        self._placed = start
        self._layout_grew()

    def _layout_grew(self):
        self._content_h = int(self._next_y)
        self._update_ranges()
        if 0 <= self._center_on < self._placed:
            self.ensure_visible(self._center_on, center=True)
            self._center_on = -1
        self._schedule_requests()

    def _update_ranges(self):
        """Scroll ranges for the zoomed content (the offsets are the camera)."""
        z = self._zoom
        vw, vh = self.viewport().width(), self.viewport().height()
        hsb, vsb = self.horizontalScrollBar(), self.verticalScrollBar()
        hsb.setRange(0, max(0, round(self._content_w * z) - vw))
        vsb.setRange(0, max(0, round(self._content_h * z) - vh))
        hsb.setPageStep(vw)
        vsb.setPageStep(vh)
        vsb.setSingleStep(max(20, int(self._target * z / 3)))

    def _place_row(self, first, last, h, stretch):
        gap = self._gap
        right = gap + self._avail_width()
        x = float(gap)
        for i in range(first, last):
            end = right if (stretch and i == last - 1) else x + self._aspect[self._items[i]] * h
            self._cell_x[i] = round(x)
            self._cell_w[i] = max(1, round(end) - round(x))
            x = end + gap
        self._row_y.append(round(self._next_y))
        self._row_h.append(max(1, round(h)))
        self._row_start.append(first)
        self._next_y += h + gap
        # A new row inside the view needs drawing (scrolling does not cover it).
        if round(self._row_y[-1] * self._zoom) < self._oy() + self.viewport().height():
            self.viewport().update()

    def _relayout_keeping_view(self, reset_zoom=False):
        """Full relayout holding one item still on screen: the selected one if
        visible, otherwise the one at the view centre."""
        anchor = None
        if self._row_y:
            vw, vh = self.viewport().width(), self.viewport().height()
            sel_r = self._row_of(self._selected)
            if sel_r is not None and self._screen_rect(self._selected, sel_r).intersects(
                    QRect(0, 0, vw, vh)):
                anchor = self._selected
            else:
                anchor = self._item_near(QPoint(vw // 2, vh // 2))
            if anchor >= 0:
                before = self._screen_rect(anchor).center()
            else:
                anchor = None
        if reset_zoom:
            self._zoom = 1.0
        self._reset_layout()
        self._zoom = min(self._zoom, self._max_zoom())
        self._update_ranges()
        if anchor is not None and anchor < self._placed:
            after = self._screen_rect(anchor).center()
            self._set_offset(self._ox() + after.x() - before.x(),
                             self._oy() + after.y() - before.y())
        elif anchor is not None:
            self._center_on = anchor

    # ── camera ───────────────────────────────────────────────────
    def _ox(self):
        return self.horizontalScrollBar().value()

    def _oy(self):
        return self.verticalScrollBar().value()

    def _set_offset(self, x, y):
        hsb, vsb = self.horizontalScrollBar(), self.verticalScrollBar()
        self._scroll_timer.stop()
        hsb.setValue(max(hsb.minimum(), min(hsb.maximum(), int(round(x)))))
        vsb.setValue(max(vsb.minimum(), min(vsb.maximum(), int(round(y)))))
        self._scroll_target = vsb.value()

    def _set_scroll(self, value):
        self._set_offset(self._ox(), value)

    def _max_zoom(self):
        """Close-up detail while still on the grid, but only as far as the
        sharpest thumbnail holds up: a typical cell may reach about 1.25x the
        largest size (beyond that it is just blur). At least 3x."""
        target = max(1.0, self._target)
        dpr = self.viewport().devicePixelRatioF()
        sharp_limit = EDGES[-1] * 1.25 / (target * REFERENCE_ASPECT * dpr)
        rows_limit = self.viewport().height() * 4 / target
        return max(1.0, min(MAX_ZOOM, max(3.0, min(sharp_limit, rows_limit))))

    def zoom_at(self, factor, pos=None):
        """Zoom by *factor*, keeping the canvas point under *pos* still."""
        if pos is None:
            pos = QPoint(self.viewport().width() // 2, self.viewport().height() // 2)
        old = self._zoom
        new = max(1.0, min(self._max_zoom(), old * factor))
        if abs(new - old) < 1e-4:
            return
        cx = (pos.x() + self._ox()) / old
        cy = (pos.y() + self._oy()) / old
        self._zoom = new
        self._update_ranges()
        # Thumbnails are drawn scaled on the fly until the zoom settles.
        self._zooming = True
        self._zoom_settle.start()
        self._set_offset(cx * new - pos.x(), cy * new - pos.y())
        self.viewport().update()
        self._schedule_requests()
        self._update_status()

    def reset_zoom(self):
        self.zoom_at(1.0 / self._zoom)

    def _on_zoom_settled(self):
        self._zooming = False
        self._drop_scaled()
        self.viewport().update()

    # ── geometry helpers ─────────────────────────────────────────
    def _row_of(self, idx):
        if idx < 0 or idx >= self._placed or not self._row_start:
            return None
        return bisect.bisect_right(self._row_start, idx) - 1

    def _row_end(self, r):
        return self._row_start[r + 1] if r + 1 < len(self._row_start) else self._placed

    def _visible_rows(self, top, bottom):
        """Rows overlapping canvas y range [top, bottom)."""
        if not self._row_y:
            return range(0)
        first = max(0, bisect.bisect_right(self._row_y, top) - 1)
        last = bisect.bisect_left(self._row_y, bottom)
        return range(first, min(last, len(self._row_y)))

    def _screen_rect(self, i, r=None):
        """Item *i*'s cell on screen, after zoom and pan (integer edges shared
        by neighbours, so gaps stay even at any zoom)."""
        if r is None:
            r = self._row_of(i)
        z, ox, oy = self._zoom, self._ox(), self._oy()
        x0 = round(self._cell_x[i] * z) - ox
        x1 = round((self._cell_x[i] + self._cell_w[i]) * z) - ox
        y0 = round(self._row_y[r] * z) - oy
        y1 = round((self._row_y[r] + self._row_h[r]) * z) - oy
        return QRect(x0, y0, max(1, x1 - x0), max(1, y1 - y0))

    def _view_canvas(self, margin_x=0.0, margin_up=0.0, margin_down=0.0):
        """The visible canvas area (optionally widened by fractions of the view)."""
        z = self._zoom
        vw, vh = self.viewport().width() / z, self.viewport().height() / z
        left, top = self._ox() / z, self._oy() / z
        return (left - vw * margin_x, top - vh * margin_up,
                left + vw * (1 + margin_x), top + vh * (1 + margin_down))

    def _picture_rect(self, i, cell):
        """Where the picture itself sits: the whole cell in justified rows,
        the fitted, centred part of the square in the grid."""
        if self._justified:
            return cell
        a = self._aspect.get(self._items[i])
        if not a:
            return cell
        w, h = cell.width(), cell.height()
        if a >= w / h:
            ph = max(1, round(w / a))
            return QRect(cell.left(), cell.top() + (h - ph) // 2, w, ph)
        pw = max(1, round(h * a))
        return QRect(cell.left() + (w - pw) // 2, cell.top(), pw, h)

    def _item_at(self, pos):
        z = self._zoom
        cx, cy = (pos.x() + self._ox()) / z, (pos.y() + self._oy()) / z
        if not self._row_y:
            return -1
        r = bisect.bisect_right(self._row_y, cy) - 1
        if r < 0 or cy > self._row_y[r] + self._row_h[r]:
            return -1
        for i in range(self._row_start[r], self._row_end(r)):
            if self._cell_x[i] <= cx < self._cell_x[i] + self._cell_w[i]:
                return i
        return -1

    def _item_near(self, pos):
        """The item under *pos*, or the closest one in that row."""
        i = self._item_at(pos)
        if i >= 0 or not self._row_y:
            return i
        z = self._zoom
        cx, cy = (pos.x() + self._ox()) / z, (pos.y() + self._oy()) / z
        r = max(0, min(len(self._row_y) - 1, bisect.bisect_right(self._row_y, cy) - 1))
        cells = range(self._row_start[r], self._row_end(r))
        return min(cells, key=lambda k: abs(self._cell_x[k] + self._cell_w[k] / 2 - cx)) if cells else -1

    def _update_cell(self, i):
        r = self._row_of(i)
        if r is not None:
            self.viewport().update(self._screen_rect(i, r))

    # ── loading ──────────────────────────────────────────────────
    def _on_aspects(self, batch, gen):
        if gen != self._probe_gen:
            return
        self._aspect.update(batch)
        self._aspect_dirty = True
        self._extend_layout()
        self._update_status()

    def _on_probe_done(self, gen):
        if gen != self._probe_gen:
            return
        self._probing = False
        if self._fit_all and self._justified:
            self._reset_layout()
        else:
            self._extend_layout()
        self._save_aspects()
        self._update_status()

    def _schedule_requests(self):
        if not self._request_timer.isActive():
            self._request_timer.start()

    def _edge_for(self, i, r, dpr):
        """Smallest thumbnail size that stays sharp in item *i*'s cell on screen."""
        need = max(self._cell_w[i], self._row_h[r]) * self._zoom * dpr
        for edge in EDGES:
            if edge * 1.15 >= need:
                return edge
        return EDGES[-1]

    def _update_requests(self):
        """Ask for the cells on screen, then a margin around them (more in the
        scroll direction) — never more than WINDOW_MAX items in all."""
        if not self.isVisible() or not self._items:
            return
        dpr = self.viewport().devicePixelRatioF()
        jobs, window = [], set()
        areas = (self._view_canvas(),                            # on screen: sharp
                 self._view_canvas(0.5, 0.5, 1.0))               # margin around it
        for n_area, (left, top, right, bottom) in enumerate(areas):
            quick, sharp = [], []
            for r in self._visible_rows(top, bottom):
                if n_area and len(window) >= WINDOW_MAX:
                    break           # the screen itself is never cut short
                for i in range(self._row_start[r], self._row_end(r)):
                    x = self._cell_x[i]
                    if x + self._cell_w[i] < left or x > right:
                        continue
                    p = self._items[i]
                    if p in window or p in self._failed:
                        continue
                    window.add(p)
                    edge = self._edge_for(i, r, dpr) if n_area == 0 else THUMB_EDGE
                    t = self._thumbs.get(p)
                    if t is not None:
                        self._thumbs.move_to_end(p)
                        if not t.sharp_enough(edge):
                            sharp.append((p, edge))
                    else:
                        # Standard size first (fast, often on disk), then sharper.
                        quick.append((p, THUMB_EDGE))
                        if edge > THUMB_EDGE:
                            sharp.append((p, edge))
            jobs += quick + sharp
        self._window = window
        self._loader.request(jobs)
        self._update_status()

    def _on_thumb(self, path, image, edge):
        i = self._index.get(path)
        if i is None:
            return
        if image.isNull():
            if edge == THUMB_EDGE:
                self._failed.add(path)
                self._update_cell(i)
                self._update_status()
            return
        a = image.width() / max(1, image.height())
        known = self._aspect.get(path)
        if known is None or abs(known - a) / a > 0.02:
            # Grid cells fit to this; justified rows pick it up next time.
            self._aspect[path] = a
            self._aspect_dirty = True
        old = self._thumbs.get(path)
        if old is not None and old.sharp_enough(edge) and old.edge != edge:
            return                  # already holding a sharper one
        if old is not None:
            del self._thumbs[path]
            self._bytes -= old.nbytes()
        t = _Thumb(QPixmap.fromImage(image), edge)
        self._thumbs[path] = t
        self._bytes += t.nbytes()
        while self._bytes > PIXMAP_CACHE_BYTES and len(self._thumbs) > 1:
            _p, evicted = self._thumbs.popitem(last=False)
            self._bytes -= evicted.nbytes()
        self._update_cell(i)
        if path in self._window:
            self._update_status()

    @staticmethod
    def _source_rect(pix, tw, th):
        """The part of *pix* with the target's shape (trims a sliver, never stretches)."""
        pw, ph = pix.width(), pix.height()
        cell_a, pix_a = tw / th, pw / ph
        if pix_a > cell_a:
            w = round(ph * cell_a)
            return QRect((pw - w) // 2, 0, w, ph)
        h = round(pw / cell_a)
        return QRect(0, (ph - h) // 2, pw, h)

    def _scaled_for(self, t, rect, dpr):
        """The thumbnail scaled to *rect* in device pixels (cached on *t*)."""
        tw, th = max(1, round(rect.width() * dpr)), max(1, round(rect.height() * dpr))
        s = t.scaled
        if s is not None and s.width() == tw and s.height() == th:
            return s
        src = self._source_rect(t.pix, tw, th)
        s = t.pix.copy(src).scaled(tw, th, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        s.setDevicePixelRatio(dpr)
        if t.scaled is not None:
            self._bytes -= t.scaled.width() * t.scaled.height() * 4
        t.scaled = s
        self._bytes += tw * th * 4
        return s

    # ── painting ─────────────────────────────────────────────────
    def paintEvent(self, event):
        vp = self.viewport()
        p = QPainter(vp)
        clip = event.rect()
        p.fillRect(clip, self._bg)
        if not self._items:
            p.setPen(self._glyph)
            p.drawText(vp.rect(), Qt.AlignCenter, "Open a folder to see its thumbnails")
            return
        if not self._row_y and self._probing:
            p.setPen(self._glyph)
            p.drawText(vp.rect(), Qt.AlignCenter, "Reading picture sizes…")
            return
        dpr = vp.devicePixelRatioF()
        z = self._zoom
        ox, oy = self._ox(), self._oy()
        name_font = QFont(self.font())
        name_font.setPointSize(8)
        glyph_font = QFont(self.font())
        glyph_font.setPointSize(18)
        fm = QFontMetrics(name_font)
        has_focus = self.hasFocus()
        if self._zooming:
            p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        c_left, c_right = (ox + clip.left()) / z, (ox + clip.right() + 1) / z

        vp_area = vp.width() * vp.height()
        for r in self._visible_rows((oy + clip.top()) / z, (oy + clip.bottom() + 1) / z):
            for i in range(self._row_start[r], self._row_end(r)):
                x = self._cell_x[i]
                if x + self._cell_w[i] < c_left or x > c_right:
                    continue
                cell = self._screen_rect(i, r)
                path = self._items[i]
                t = self._thumbs.get(path)
                if t is not None:
                    rect = self._picture_rect(i, cell)
                    if self._zooming or rect.width() * rect.height() > vp_area:
                        # Mid-zoom, or a cell bigger than the screen (a cached
                        # copy that size could run to a gigabyte): scale on the
                        # fly — Qt only rasterises the part inside the clip.
                        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
                        src = self._source_rect(t.pix, rect.width(), rect.height())
                        p.drawPixmap(QRectF(rect), t.pix, QRectF(src))
                    else:
                        p.drawPixmap(rect.topLeft(), self._scaled_for(t, rect, dpr))
                else:
                    rect = cell
                    self._paint_placeholder(p, cell, path, glyph_font)
                kind = item_kind(path)
                if kind != 'image' and t is not None:
                    self._paint_badge(p, rect, path, kind)
                if self._names or i == self._hover:
                    self._paint_name(p, rect, path, name_font, fm)
                if i == self._hover:
                    p.fillRect(rect, QColor(255, 255, 255, 28))
                if path == self._current:
                    self._paint_frame(p, rect, self._accent, 3)
                elif i == self._selected and has_focus:
                    self._paint_frame(p, rect, QColor(255, 255, 255, 200), 2)
        p.end()

    def _paint_placeholder(self, p, rect, path, font):
        p.fillRect(rect, self._placeholder)
        glyph = "⚠" if path in self._failed else {'video': "🎬", 'doc': "📄"}.get(item_kind(path), "")
        if glyph and rect.height() > 30:
            p.setFont(font)
            p.setPen(self._glyph)
            p.drawText(rect, Qt.AlignCenter, glyph)

    def _paint_badge(self, p, rect, path, kind):
        text = "▶" if kind == 'video' else os.path.splitext(path)[1][1:].upper()
        s = 18
        badge = QRect(rect.left() + 5, rect.top() + 5, s + (14 if kind == 'doc' else 0), s)
        if rect.width() < badge.width() + 10 or rect.height() < s + 10:
            return
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 150))
        p.drawRoundedRect(badge, 4, 4)
        p.setPen(QColor(255, 255, 255, 230))
        f = QFont(self.font())
        f.setPointSize(7 if kind == 'doc' else 8)
        f.setBold(True)
        p.setFont(f)
        p.drawText(badge, Qt.AlignCenter, text)
        p.setRenderHint(QPainter.Antialiasing, False)

    def _paint_name(self, p, rect, path, font, fm):
        if rect.height() < 28:
            return
        h = fm.height() + 6
        strip = QRect(rect.left(), rect.bottom() - h + 1, rect.width(), h)
        p.fillRect(strip, QColor(0, 0, 0, 150))
        p.setFont(font)
        p.setPen(QColor(235, 235, 235))
        name = fm.elidedText(os.path.basename(path), Qt.ElideMiddle, max(10, rect.width() - 8))
        p.drawText(strip.adjusted(4, 0, -4, 0), Qt.AlignVCenter | Qt.AlignLeft, name)

    @staticmethod
    def _paint_frame(p, rect, color, width):
        pen = QPen(color, width)
        pen.setJoinStyle(Qt.MiterJoin)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        half = width / 2
        p.drawRect(QRectF(rect).adjusted(half, half, -half, -half))

    # ── scrolling and panning ────────────────────────────────────
    def scrollContentsBy(self, dx, dy):
        # Shift what is already drawn; Qt then paints only the exposed strip.
        self.viewport().scroll(dx, dy)

    def _on_scrolled(self, _value):
        if not self._scroll_timer.isActive():
            self._scroll_target = self.verticalScrollBar().value()
        self._schedule_requests()

    def _scroll_step(self):
        sb = self.verticalScrollBar()
        cur = sb.value()
        diff = self._scroll_target - cur
        if abs(diff) < 1:
            sb.setValue(int(round(self._scroll_target)))
            self._scroll_timer.stop()
            return
        step = diff * 0.28
        step = max(1, step) if diff > 0 else min(-1, step)
        sb.setValue(int(round(cur + step)))

    def _start_pan(self, pos):
        self._stop_motion()
        now = time.monotonic()
        self._pan = {'last': pos, 'start': pos, 'moved': False,
                     'samples': deque([(now, pos.x(), pos.y())], maxlen=12)}
        self.viewport().setCursor(Qt.ClosedHandCursor)

    def _drag_pan(self, pos):
        pan = self._pan
        d = pos - pan['last']
        pan['last'] = pos
        if not pan['moved'] and (pos - pan['start']).manhattanLength() > 3:
            pan['moved'] = True
        self._set_offset(self._ox() - d.x(), self._oy() - d.y())
        pan['samples'].append((time.monotonic(), pos.x(), pos.y()))

    def _end_pan(self):
        """Release: keep gliding with the drag's last speed, slowing down."""
        pan, self._pan = self._pan, None
        self.viewport().setCursor(Qt.ArrowCursor)
        now = time.monotonic()
        recent = [s for s in pan['samples'] if now - s[0] < 0.08]
        if pan['moved'] and len(recent) >= 2 and now - recent[-1][0] < 0.05:
            dt = recent[-1][0] - recent[0][0]
            if dt > 0.005:
                vx = -(recent[-1][1] - recent[0][1]) / dt
                vy = -(recent[-1][2] - recent[0][2]) / dt
                if math.hypot(vx, vy) > 150:
                    self._velocity = (vx, vy)
                    self._inertia_t = now
                    self._inertia.start()

    def _inertia_step(self):
        now = time.monotonic()
        dt, self._inertia_t = now - self._inertia_t, now
        decay = math.exp(-PAN_FRICTION * dt)
        vx, vy = self._velocity[0] * decay, self._velocity[1] * decay
        self._velocity = (vx, vy)
        before = (self._ox(), self._oy())
        self._set_offset(self._ox() + vx * dt, self._oy() + vy * dt)
        if math.hypot(vx, vy) < 25 or (self._ox(), self._oy()) == before:
            self._inertia.stop()

    def _stop_motion(self):
        """Any new input takes over from gliding, inertia and smooth scrolling."""
        self._glide.stop()
        self._inertia.stop()
        self._scroll_timer.stop()

    def focus_item(self, idx):
        """Select *idx* and glide the camera to centre it, keeping the zoom."""
        r = self._row_of(idx)
        if r is None:
            return
        old, self._selected = self._selected, idx
        self._update_cell(old)
        self._update_cell(idx)
        rect = self._screen_rect(idx, r)
        vw, vh = self.viewport().width(), self.viewport().height()
        self._stop_motion()
        self._glide_target = (self._ox() + rect.center().x() - vw // 2,
                              self._oy() + rect.center().y() - vh // 2)
        self._glide.start()

    def _glide_step(self):
        hsb, vsb = self.horizontalScrollBar(), self.verticalScrollBar()
        tx = max(hsb.minimum(), min(hsb.maximum(), self._glide_target[0]))
        ty = max(vsb.minimum(), min(vsb.maximum(), self._glide_target[1]))
        dx, dy = tx - self._ox(), ty - self._oy()
        if abs(dx) < 1 and abs(dy) < 1:
            self._glide.stop()
            return
        # Ease out: cover a fixed share of the remaining way each frame.
        def step(d):
            s = d * 0.22
            return (max(1, s) if d > 0 else min(-1, s)) if abs(d) >= 1 else 0
        hsb.setValue(int(round(self._ox() + step(dx))))
        vsb.setValue(int(round(self._oy() + step(dy))))

    def ensure_visible(self, idx, center=False):
        r = self._row_of(idx)
        if r is None:
            return
        rect = self._screen_rect(idx, r)
        vw, vh = self.viewport().width(), self.viewport().height()
        ox, oy = self._ox(), self._oy()
        margin = round(self._gap * self._zoom)
        if center:
            ox += rect.center().x() - vw // 2
            oy += rect.center().y() - vh // 2
        else:
            if rect.left() < 0:
                ox += rect.left() - margin
            elif rect.right() >= vw:
                ox += rect.right() + 1 + margin - vw
            if rect.top() < 0:
                oy += rect.top() - margin
            elif rect.bottom() >= vh:
                oy += rect.bottom() + 1 + margin - vh
        self._set_offset(ox, oy)

    # ── events ───────────────────────────────────────────────────
    def eventFilter(self, obj, event):
        if obj is getattr(self, '_tracked', None) and event.type() in (QEvent.Resize, QEvent.Move):
            self.setGeometry(obj.geometry())
        return False

    def resizeEvent(self, event):
        old_w = event.oldSize().width()
        super().resizeEvent(event)
        self._place_progress_line()
        if self._items and (event.size().width() != old_w or self._fit_all):
            self._relayout_keeping_view()
        elif self._items:
            # Height only: rows stay, the scroll range follows.
            self._update_ranges()
            self._schedule_requests()
        if hasattr(self, '_panel'):
            self._panel.reposition_to_anchor()

    def showEvent(self, event):
        super().showEvent(event)
        self._dock_panel()
        self._place_progress_line()
        self._schedule_requests()

    def hideEvent(self, event):
        super().hideEvent(event)
        self._progress_line.hide()

    def event(self, event):
        if event.type() == QEvent.ShortcutOverride:
            mods = event.modifiers() & (Qt.ControlModifier | Qt.AltModifier)
            if (not mods and event.key() in self._OWN_KEYS) or \
                    (mods == Qt.ControlModifier and event.key() in self._ZOOM_KEYS):
                event.accept()
                return True
        return super().event(event)

    def wheelEvent(self, event):
        self._center_on = -1
        self._glide.stop()
        self._inertia.stop()
        dy = event.angleDelta().y()
        pos = event.position().toPoint()
        # Right button held (or Ctrl): zoom around the cursor.
        if event.buttons() & Qt.RightButton or event.modifiers() & Qt.ControlModifier:
            if event.buttons() & Qt.RightButton:
                self._right_wheeled = True
            if dy:
                self.zoom_at(ZOOM_STEP ** (dy / 120), pos)
            event.accept()
            return
        pixel = event.pixelDelta()
        sb = self.verticalScrollBar()
        if event.modifiers() & Qt.ShiftModifier:       # sideways
            step = dy if dy else pixel.x()
            self._set_offset(self._ox() - step, self._oy())
        elif pixel.y() or pixel.x():                    # precision touchpad: follow the fingers
            self._set_offset(self._ox() - pixel.x(), self._oy() - pixel.y())
        elif dy:
            step = max(60, self.viewport().height() * 0.18)
            base = self._scroll_target if self._scroll_timer.isActive() else sb.value()
            self._scroll_target = max(sb.minimum(), min(sb.maximum(), base - dy / 120 * step))
            self._scroll_timer.start()
        event.accept()

    def viewportEvent(self, event):
        if event.type() == QEvent.Leave and self._hover != -1:
            old, self._hover = self._hover, -1
            self._update_cell(old)
        return super().viewportEvent(event)

    def contextMenuEvent(self, event):
        event.accept()      # right button pans and zooms here; no menu

    def mousePressEvent(self, event):
        self.setFocus()
        pos = event.position().toPoint()
        if event.button() in (Qt.RightButton, Qt.MiddleButton):
            self._right_wheeled = False
            self._start_pan(pos)
            self._press_item = -1
        elif event.button() == Qt.LeftButton:
            self._inertia.stop()
            self._press_item = self._item_at(pos)

    def mouseDoubleClickEvent(self, event):
        """Double-click opens the picture in the viewer."""
        if event.button() != Qt.LeftButton:
            return
        i = self._item_at(event.position().toPoint())
        if i >= 0:
            self._glide.stop()
            self._selected = i
            self.activated.emit(self._items[i])

    def mouseMoveEvent(self, event):
        pos = event.position().toPoint()
        if self._pan is not None:
            self._drag_pan(pos)
            return
        i = self._item_at(pos)
        if i != self._hover:
            old, self._hover = self._hover, i
            self.viewport().setCursor(Qt.PointingHandCursor if i >= 0 else Qt.ArrowCursor)
            self._update_cell(old)
            self._update_cell(i)

    def mouseReleaseEvent(self, event):
        if event.button() in (Qt.RightButton, Qt.MiddleButton):
            if self._pan is not None:
                self._end_pan()
            return
        if event.button() != Qt.LeftButton:
            return
        # A single click frames the picture: select it and centre it at the
        # current zoom. Double-click (or Enter) opens it.
        i = self._item_at(event.position().toPoint())
        if i >= 0 and i == getattr(self, '_press_item', -1):
            self.focus_item(i)

    def keyPressEvent(self, event):
        self._center_on = -1
        self._glide.stop()
        key = event.key()
        if key in (Qt.Key_Escape, Qt.Key_G):
            self.close_requested.emit()
            return
        if key == Qt.Key_Delete:
            return          # never delete the viewer's file from inside the grid
        n = self._placed
        if not n:
            return
        if event.modifiers() & Qt.ControlModifier and key in self._ZOOM_KEYS:
            if key == Qt.Key_0:
                self.reset_zoom()
            else:
                self.zoom_at(ZOOM_STEP if key != Qt.Key_Minus else 1 / ZOOM_STEP)
            return
        sel = min(max(0, self._selected), n - 1)
        vh = self.viewport().height() / self._zoom
        if key in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            self.activated.emit(self._items[sel])
            return
        if key == Qt.Key_Left:
            sel -= 1
        elif key == Qt.Key_Right:
            sel += 1
        elif key == Qt.Key_Home:
            sel = 0
        elif key == Qt.Key_End:
            sel = n - 1
        elif key in (Qt.Key_Up, Qt.Key_Down, Qt.Key_PageUp, Qt.Key_PageDown):
            r = self._row_of(sel)
            cx = self._cell_x[sel] + self._cell_w[sel] / 2
            if key in (Qt.Key_Up, Qt.Key_Down):
                r += -1 if key == Qt.Key_Up else 1
            else:
                y = self._row_y[r] + (-vh if key == Qt.Key_PageUp else vh)
                r = max(0, bisect.bisect_right(self._row_y, y) - 1)
            r = max(0, min(len(self._row_y) - 1, r))
            cells = range(self._row_start[r], self._row_end(r))
            sel = min(cells, key=lambda i: abs(self._cell_x[i] + self._cell_w[i] / 2 - cx))
        else:
            super().keyPressEvent(event)
            return
        old, self._selected = self._selected, max(0, min(n - 1, sel))
        self._update_cell(old)
        self._update_cell(self._selected)
        self.ensure_visible(self._selected)
