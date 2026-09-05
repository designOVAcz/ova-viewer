import sys
import os
import random
import time
import subprocess
import gc
import struct

try:
    import ctypes
    import ctypes.wintypes
except ImportError:
    ctypes = None

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QLabel, QFileDialog, QVBoxLayout, QWidget,
    QListWidget, QListWidgetItem, QSplitter, QSpinBox, QCheckBox,
    QStatusBar, QToolBar, QToolButton, QSizePolicy, QSlider, QHBoxLayout,
    QStyle, QStyleOptionSlider, QGridLayout, QMenu, QColorDialog, QComboBox,
    QGraphicsOpacityEffect, QFrame
)
from PySide6.QtGui import (
    QPixmap, QPainter, QColor, QPen, QFont, QIcon, QColorTransform,
    QMouseEvent, QImageReader, QTransform, QAction, QShortcut, QImage, QTabletEvent, QCursor
)
from PySide6.QtCore import Qt, QTimer, QSize, QElapsedTimer, QRect, QEvent, QPropertyAnimation, QEasingCurve, QThread, QPoint, Signal
from PySide6.QtWidgets import QLayout, QWidgetAction

from random_image_viewer.constants import IMAGE_EXTENSIONS, MEDIA_EXTENSIONS, DOCUMENT_EXTENSIONS, PLAYLIST_EXTENSIONS
from random_image_viewer.platform_utils import (
    is_windows_dark_mode, enable_windows_dark_title_bar, setup_image_allocation_limit
)
from random_image_viewer.styles import get_adaptive_stylesheet
from random_image_viewer.image_utils import (
    get_image_file_size, smart_load_pixmap, safe_load_pixmap,
    natural_sort_key, get_images_in_folder, get_playlist_items_in_folder,
    emoji_icon, is_animated_gif, is_video_file, PlaylistScanner,
    find_subtitle_file, find_dub_audio_file, parse_srt, load_thumbnail_pixmap
)
from random_image_viewer.pdf_utils import is_pdf_file, PdfDocument
from random_image_viewer.epub_utils import is_epub_file, EpubDocument
from random_image_viewer.cbr_utils import is_cbr_file, CbrDocument
from random_image_viewer.widgets.clickable_slider import ClickableSlider
from random_image_viewer.widgets.circular_countdown import CircularCountdown
from random_image_viewer.widgets.image_label import ImageLabel
from random_image_viewer.widgets.enhancement_widget import ResponsiveEnhancementWidget
from random_image_viewer.widgets.color_snap_preview import ColorSnapPreview
from random_image_viewer.widgets.snapped_palette_window import SnappedPaletteWindow
from random_image_viewer.widgets.curves_window import CurvesWindow
from random_image_viewer.widgets.type_filter_window import TypeFilterWindow
from random_image_viewer.widgets.floating_panel import FloatingPanel
from random_image_viewer.processing.gpu_processor import GPULutProcessor


class PdfHiresWorker(QThread):
    """Render a single PDF page (or a 2/3-page spread) at high resolution off
    the GUI thread.

    Emits :attr:`ready` with ``(start_page, count, target, QImage)`` when done.
    A ``QImage`` is produced (not a ``QPixmap``) because only ``QImage`` may be
    built off the main thread; the receiver converts it to a ``QPixmap``.
    """

    ready = Signal(int, int, int, object)

    def __init__(self, pdf_doc, start_page, count, target, parent=None):
        super().__init__(parent)
        self._pdf_doc = pdf_doc
        self._start_page = start_page
        self._count = count
        self._target = target

    def run(self):
        try:
            if self._count > 1:
                img = self._pdf_doc.render_spread_qimage(
                    self._start_page, self._count, self._target)
            else:
                img = self._pdf_doc.render_page_qimage(
                    self._start_page, self._target)
        except Exception as e:
            print(f"PdfHiresWorker error: {e}")
            img = None
        if img is not None:
            self.ready.emit(self._start_page, self._count, self._target, img)


class PdfLoadingOverlay(QWidget):
    """Semi-transparent overlay with a spinning arc and progress text."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, False)
        self.setAutoFillBackground(False)
        self._angle = 0
        self._text = "Loading PDF…"
        self._progress_text = ""
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        if parent:
            parent.installEventFilter(self)
        self.hide()

    # ── public API ──────────────────────────────────────────────
    def start(self, text="Loading PDF…"):
        self._text = text
        self._progress_text = ""
        self._angle = 0
        self._reposition()
        self.show()
        self.raise_()
        self._timer.start(30)  # ~33 fps spinner

    def set_progress(self, done, total):
        pct = int(done / total * 100) if total else 0
        self._progress_text = f"{done} / {total} pages  ({pct}%)"
        self.update()

    def finish(self):
        self._timer.stop()
        self.hide()

    # ── internals ──────────────────────────────────────────────
    def _tick(self):
        self._angle = (self._angle + 5) % 360
        self.update()

    def _reposition(self):
        if self.parent():
            self.setGeometry(self.parent().rect())

    def eventFilter(self, obj, event):
        if obj is self.parent() and event.type() == QEvent.Resize:
            self._reposition()
        return False

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        # Dim background
        p.fillRect(self.rect(), QColor(0, 0, 0, 140))

        cx, cy = self.width() // 2, self.height() // 2

        # Spinning arc
        arc_size = min(self.width(), self.height()) // 5
        arc_rect = QRect(cx - arc_size // 2, cy - arc_size // 2 - 20,
                         arc_size, arc_size)
        pen = QPen(QColor(100, 180, 255), 4)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        p.drawArc(arc_rect, self._angle * 16, 270 * 16)

        # Title text
        p.setPen(QColor(255, 255, 255))
        p.setFont(QFont("Segoe UI", 13, QFont.Bold))
        p.drawText(self.rect().adjusted(0, arc_size // 2 + 10, 0, 0),
                   Qt.AlignHCenter | Qt.AlignTop, self._text)

        # Progress text
        if self._progress_text:
            p.setFont(QFont("Segoe UI", 10))
            p.setPen(QColor(180, 180, 180))
            p.drawText(self.rect().adjusted(0, arc_size // 2 + 40, 0, 0),
                       Qt.AlignHCenter | Qt.AlignTop, self._progress_text)

        p.end()


class VideoControlsOverlay(QWidget):
    """Floating translucent video controls panel that appears on mouse hover.

    Sits at the top of the image label (kept clear of bottom-anchored
    subtitles, which it would otherwise visually collide with), auto-hides
    after a few seconds of inactivity — classic video-player behaviour.
    """

    HIDE_DELAY_MS = 2500
    PANEL_HEIGHT = 56
    PANEL_MARGIN = 16

    def __init__(self, parent_label):
        super().__init__(parent_label)
        self._parent_label = parent_label
        self.setAutoFillBackground(False)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setCursor(Qt.ArrowCursor)

        self._opacity = QGraphicsOpacityEffect(self)
        self._opacity.setOpacity(0.0)
        self.setGraphicsEffect(self._opacity)

        self._fade_anim = QPropertyAnimation(self._opacity, b"opacity", self)
        self._fade_anim.setDuration(250)
        self._fade_anim.setEasingCurve(QEasingCurve.InOutQuad)

        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self._fade_out)

        self._visible_state = False
        self._duration_ms = 0
        self._position_ms = 0
        self._is_playing = False
        self._is_muted = False
        self._viewer = None

        self._build_ui()
        self.hide()
        parent_label.installEventFilter(self)

    # ── build UI ──────────────────────────────────────────────
    def _build_ui(self):
        self.setStyleSheet("VideoControlsOverlay { background: transparent; }")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        container = QWidget(self)
        container.setObjectName("vco_container")
        container.setStyleSheet("""
            #vco_container {
                background: rgba(20, 20, 20, 200);
                border-radius: 10px;
            }
            QLabel { color: #ddd; font-size: 11px; }
            QToolButton {
                color: #ddd; background: transparent;
                border: none; font-size: 14px;
            }
            QToolButton:hover {
                background: rgba(255,255,255,40);
                border-radius: 4px;
            }
            QToolButton:checked {
                background: rgba(100, 180, 255, 170);
                border-radius: 4px;
            }
            QToolButton:disabled { color: #777; }
            QSlider::groove:horizontal {
                height: 6px;
                background: rgba(255,255,255,50);
                border-radius: 3px;
            }
            QSlider::handle:horizontal {
                background: #fff;
                width: 14px; height: 14px;
                margin: -4px 0;
                border-radius: 7px;
            }
            QSlider::sub-page:horizontal {
                background: rgba(100, 180, 255, 200);
                border-radius: 3px;
            }
        """)

        c_layout = QVBoxLayout(container)
        c_layout.setContentsMargins(12, 6, 12, 8)
        c_layout.setSpacing(4)

        self._seek_slider = QSlider(Qt.Horizontal)
        self._seek_slider.setRange(0, 0)
        self._seek_slider.setFixedHeight(18)
        self._seek_slider.sliderMoved.connect(self._on_seek)
        c_layout.addWidget(self._seek_slider)

        ctrl_row = QHBoxLayout()
        ctrl_row.setSpacing(6)

        self._play_btn = QToolButton()
        self._play_btn.setText("\u25b6")
        self._play_btn.setFixedSize(28, 28)
        self._play_btn.clicked.connect(self._on_play_pause)
        ctrl_row.addWidget(self._play_btn)

        self._time_label = QLabel("0:00 / 0:00")
        self._time_label.setFixedHeight(22)
        ctrl_row.addWidget(self._time_label)

        ctrl_row.addStretch()

        # Autoplay next / dub audio also live on the main toolbar, but that is
        # hidden in minimal and panel layouts — mirror them here so they are
        # reachable whenever a video is on screen.
        self._autoplay_btn = QToolButton()
        self._autoplay_btn.setText("\u23ed")
        self._autoplay_btn.setCheckable(True)
        self._autoplay_btn.setToolTip(
            "Autoplay next: advance when this video or GIF finishes")
        self._autoplay_btn.setFixedSize(28, 28)
        self._autoplay_btn.clicked.connect(self._on_autoplay_toggle)
        ctrl_row.addWidget(self._autoplay_btn)

        self._subs_btn = QToolButton()
        self._subs_btn.setText("\U0001f4ac")
        self._subs_btn.setCheckable(True)
        self._subs_btn.setFixedSize(28, 28)
        self._subs_btn.clicked.connect(self._on_subs_toggle)
        ctrl_row.addWidget(self._subs_btn)

        self._dub_btn = QToolButton()
        self._dub_btn.setText("\U0001f3b5")
        self._dub_btn.setCheckable(True)
        self._dub_btn.setToolTip(
            "Dub audio: play a sibling audio file with the same name "
            "(clip.mp4 \u2192 clip.mp3) instead of the video's own track")
        self._dub_btn.setFixedSize(28, 28)
        self._dub_btn.clicked.connect(self._on_dub_toggle)
        ctrl_row.addWidget(self._dub_btn)

        self._mute_btn = QToolButton()
        self._mute_btn.setText("\U0001f50a")
        self._mute_btn.setFixedSize(28, 28)
        self._mute_btn.clicked.connect(self._on_mute_toggle)
        ctrl_row.addWidget(self._mute_btn)

        self._volume_slider = QSlider(Qt.Horizontal)
        self._volume_slider.setRange(0, 100)
        self._volume_slider.setValue(50)
        self._volume_slider.setFixedWidth(70)
        self._volume_slider.setFixedHeight(18)
        self._volume_slider.valueChanged.connect(self._on_volume)
        ctrl_row.addWidget(self._volume_slider)

        c_layout.addLayout(ctrl_row)
        layout.addWidget(container)

    # ── public API (called by MainWindow) ───────────────────────
    def set_viewer(self, viewer):
        self._viewer = viewer

    def activate(self, volume=50, muted=False):
        """Show the overlay and prepare for a new video."""
        self._duration_ms = 0
        self._position_ms = 0
        self._is_playing = True
        self._is_muted = muted
        self._volume_slider.blockSignals(True)
        self._volume_slider.setValue(volume)
        self._volume_slider.blockSignals(False)
        self._mute_btn.setText("\U0001f507" if muted else "\U0001f50a")
        if self._viewer is not None:
            self.update_autoplay_state(getattr(self._viewer, 'autoplay_next_enabled', False))
            self.update_dub_state(getattr(self._viewer, 'dub_audio_enabled', False))
            self.update_subtitle_state(
                bool(getattr(self._viewer, '_subtitle_cues', None)),
                getattr(self._viewer, '_subtitles_enabled', True),
                os.path.basename(getattr(self._viewer, '_subtitle_path', '') or '') or None)
        self._play_btn.setText("\u23f8")
        self._time_label.setText("0:00 / 0:00")
        self._seek_slider.setRange(0, 0)
        self._reposition()
        self.show()
        self.raise_()
        self._fade_in()

    def deactivate(self):
        self._hide_timer.stop()
        self._fade_anim.stop()
        self._opacity.setOpacity(0.0)
        self.hide()
        self._visible_state = False

    def update_duration(self, ms):
        self._duration_ms = ms
        self._seek_slider.setRange(0, max(ms, 0))
        self._update_time_text()

    def update_position(self, ms):
        self._position_ms = ms
        if not self._seek_slider.isSliderDown():
            self._seek_slider.setValue(ms)
        self._update_time_text()

    def update_play_state(self, playing):
        self._is_playing = playing
        self._play_btn.setText("\u23f8" if playing else "\u25b6")

    def update_mute_state(self, muted):
        self._is_muted = muted
        self._mute_btn.setText("\U0001f507" if muted else "\U0001f50a")

    def update_autoplay_state(self, enabled):
        """Mirror the toolbar's autoplay-next toggle."""
        self._autoplay_btn.blockSignals(True)
        self._autoplay_btn.setChecked(bool(enabled))
        self._autoplay_btn.blockSignals(False)

    def update_subtitle_state(self, available, enabled, name=None):
        """Mirror subtitle availability and on/off state.

        A video with no sibling .srt greys the button out rather than hiding
        it, so the control does not jump around between clips.
        """
        self._subs_btn.setEnabled(bool(available))
        self._subs_btn.blockSignals(True)
        self._subs_btn.setChecked(bool(available) and bool(enabled))
        self._subs_btn.blockSignals(False)
        if not available:
            self._subs_btn.setToolTip("Subtitles: no matching .srt beside this video")
        elif enabled:
            self._subs_btn.setToolTip(
                f"Subtitles: showing {name} (click to hide)" if name
                else "Subtitles: on (click to hide)")
        else:
            self._subs_btn.setToolTip(
                f"Subtitles: {name} hidden (click to show)" if name
                else "Subtitles: off (click to show)")

    def update_dub_state(self, enabled, detail=None):
        """Mirror the toolbar's dub-audio toggle; *detail* names the track."""
        self._dub_btn.blockSignals(True)
        self._dub_btn.setChecked(bool(enabled))
        self._dub_btn.blockSignals(False)
        if detail:
            self._dub_btn.setToolTip(
                f"Dub audio: playing {detail} (click to use the video's own track)")
        elif enabled:
            self._dub_btn.setToolTip(
                "Dub audio: on \u2014 no matching audio file beside this video")
        else:
            self._dub_btn.setToolTip(
                "Dub audio: play a sibling audio file with the same name "
                "(clip.mp4 \u2192 clip.mp3) instead of the video's own track")

    # ── internal ──────────────────────────────────────────────
    def _update_time_text(self):
        cur = self._fmt(self._position_ms)
        tot = self._fmt(self._duration_ms)
        self._time_label.setText(f"{cur} / {tot}")

    @staticmethod
    def _fmt(ms):
        s = max(0, ms) // 1000
        m, s = divmod(s, 60)
        h, m = divmod(m, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

    def _reposition(self):
        if self._parent_label:
            pw = self._parent_label.width()
            w = pw - 2 * self.PANEL_MARGIN
            if w < 200:
                w = pw
            x = (pw - w) // 2
            y = 10
            self.setGeometry(x, y, w, self.PANEL_HEIGHT)

    def _fade_in(self):
        self._fade_anim.stop()
        self._fade_anim.setStartValue(self._opacity.opacity())
        self._fade_anim.setEndValue(0.95)
        self._fade_anim.start()
        self._visible_state = True
        self._hide_timer.start(self.HIDE_DELAY_MS)

    def _fade_out(self):
        if self._seek_slider.isSliderDown() or self._volume_slider.isSliderDown():
            self._hide_timer.start(self.HIDE_DELAY_MS)
            return
        self._fade_anim.stop()
        self._fade_anim.setStartValue(self._opacity.opacity())
        self._fade_anim.setEndValue(0.0)
        self._fade_anim.start()
        self._visible_state = False

    def _on_seek(self, pos):
        if self._viewer:
            self._viewer.image_label.video_seek(pos)
        self._hide_timer.start(self.HIDE_DELAY_MS)

    def _on_play_pause(self):
        if self._viewer:
            self._viewer.image_label.video_toggle_play_pause()
        self._hide_timer.start(self.HIDE_DELAY_MS)

    def _on_mute_toggle(self):
        if self._viewer:
            self._viewer._toggle_video_mute()
        self._hide_timer.start(self.HIDE_DELAY_MS)

    def _on_subs_toggle(self, checked):
        if self._viewer:
            self._viewer.toggle_subtitles(checked)
        self._hide_timer.start(self.HIDE_DELAY_MS)

    def _on_autoplay_toggle(self, checked):
        if self._viewer:
            self._viewer.toggle_autoplay_next(checked)
        self._hide_timer.start(self.HIDE_DELAY_MS)

    def _on_dub_toggle(self, checked):
        if self._viewer:
            self._viewer.toggle_dub_audio(checked)
        self._hide_timer.start(self.HIDE_DELAY_MS)

    def _on_volume(self, val):
        if self._viewer:
            self._viewer._on_video_volume_changed(val)
        self._hide_timer.start(self.HIDE_DELAY_MS)

    # ── event filter: track mouse on parent label ─────────────
    def eventFilter(self, obj, event):
        if obj is self._parent_label:
            etype = event.type()
            if etype == QEvent.MouseMove:
                if self.isVisible() and self._viewer and self._viewer._video_playing:
                    self._reposition()
                    if not self._visible_state:
                        self._fade_in()
                    else:
                        self._hide_timer.start(self.HIDE_DELAY_MS)
            elif etype == QEvent.Resize:
                self._reposition()
            elif etype == QEvent.Enter:
                if self.isVisible() and self._viewer and self._viewer._video_playing:
                    self._fade_in()
            elif etype == QEvent.Leave:
                if self._visible_state:
                    self._hide_timer.start(800)
        return False

    def enterEvent(self, event):
        self._hide_timer.stop()
        if not self._visible_state:
            self._fade_in()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hide_timer.start(self.HIDE_DELAY_MS)
        super().leaveEvent(event)


class RandomImageViewer(QMainWindow):
    # ...existing code...
    def _create_overlay_icon(self, icon_type):
        """Create a QPainter-drawn icon for overlay tool buttons.
        icon_type: 'crosshair' or 'grid'
        """
        size = 18
        img = QImage(size, size, QImage.Format_ARGB32)
        img.fill(Qt.transparent)
        p = QPainter(img)
        p.setRenderHint(QPainter.Antialiasing, False)
        pen = QPen(QColor("#c8c8c8"), 1, Qt.SolidLine)
        p.setPen(pen)

        if icon_type == 'crosshair':
            cx, cy = size // 2, size // 2
            gap = 3  # gap around center
            # Vertical arm: top segment + bottom segment
            p.drawLine(cx, 0, cx, cy - gap)
            p.drawLine(cx, cy + gap, cx, size - 1)
            # Horizontal arm: left segment + right segment
            p.drawLine(0, cy, cx - gap, cy)
            p.drawLine(cx + gap, cy, size - 1, cy)
        elif icon_type == 'grid':
            # Outer border
            p.drawRect(0, 0, size - 1, size - 1)
            # Two vertical dividers at 1/3 and 2/3
            v1 = size // 3
            v2 = (size * 2) // 3
            p.drawLine(v1, 0, v1, size - 1)
            p.drawLine(v2, 0, v2, size - 1)
            # Two horizontal dividers at 1/3 and 2/3
            h1 = size // 3
            h2 = (size * 2) // 3
            p.drawLine(0, h1, size - 1, h1)
            p.drawLine(0, h2, size - 1, h2)

        p.end()
        return QIcon(QPixmap.fromImage(img))

    def _draw_fixed_overlays(self, painter, draw_x, draw_y, zoomed_width, zoomed_height):
        """Draw crosshair and/or 3x3 grid overlay onto an already-active QPainter.
        Coordinates are in display/pixmap space.
        """
        pen = QPen(self.line_color, self.line_thickness, Qt.SolidLine)
        painter.setPen(pen)

        if self.crosshair_overlay:
            cx = draw_x + zoomed_width // 2
            cy = draw_y + zoomed_height // 2
            # Full-length vertical and horizontal cross through image center
            painter.drawLine(cx, draw_y, cx, draw_y + zoomed_height)
            painter.drawLine(draw_x, cy, draw_x + zoomed_width, cy)

        if self.grid_overlay:
            # 3x3 grid: 2 vertical + 2 horizontal dividers
            for i in (1, 2):
                # Vertical line at i/3 of width
                x = draw_x + (zoomed_width * i) // 3
                painter.drawLine(x, draw_y, x, draw_y + zoomed_height)
                # Horizontal line at i/3 of height
                y = draw_y + (zoomed_height * i) // 3
                painter.drawLine(draw_x, y, draw_x + zoomed_width, y)

    def _apply_fixed_overlays_to_pixmap(self, pixmap):
        """Apply crosshair/grid overlay on top of pixmap (display space).
        Returns a copy with overlays painted, or the original if nothing to draw.
        """
        if not (self.crosshair_overlay or self.grid_overlay):
            return pixmap
        if not getattr(self, 'lines_visible', True):
            return pixmap
        if not pixmap or pixmap.isNull():
            return pixmap

        tx = self._compute_line_transform()
        if not tx:
            return pixmap

        result = pixmap.copy()
        painter = QPainter(result)
        painter.setRenderHint(QPainter.Antialiasing, False)
        self._draw_fixed_overlays(
            painter,
            tx['draw_x'], tx['draw_y'],
            tx['zoomed_width'], tx['zoomed_height']
        )
        painter.end()
        return result

    def _compute_line_transform(self):
        """Compute scaling and offset for mapping original image coords to current displayed pixmap.
        Returns dict with scale_x, scale_y, draw_x, draw_y. Now supports rotation and flips."""
        try:
            if not hasattr(self, 'original_pixmap') or not self.original_pixmap:
                return None
            if not self.image_label or not self.image_label.pixmap():
                return None
            
            original_size = self.original_pixmap.size()
            label_size = self.image_label.size()
            zoom_factor = getattr(self.image_label, 'zoom_factor', 1.0)
            
            # For rotated images (90° and 270°), the display dimensions are swapped
            if self.rotation_angle == 90 or self.rotation_angle == 270:
                # Use swapped dimensions as reference for scaling
                display_reference_size = QSize(original_size.height(), original_size.width())
            else:
                # 0° and 180° keep the same dimensions
                display_reference_size = original_size
            
            # Base scaled size at 100% (aspect fit) using the correct reference dimensions
            base_scaled = display_reference_size.scaled(label_size, Qt.KeepAspectRatio)
            zoomed_width = int(base_scaled.width() * zoom_factor)
            zoomed_height = int(base_scaled.height() * zoom_factor)
            draw_x = (label_size.width() - zoomed_width) // 2 + int(getattr(self.image_label, 'pan_offset_x', 0))
            draw_y = (label_size.height() - zoomed_height) // 2 + int(getattr(self.image_label, 'pan_offset_y', 0))
            scale_x = zoomed_width / original_size.width() if original_size.width() else 1.0
            scale_y = zoomed_height / original_size.height() if original_size.height() else 1.0
            return {
                'scale_x': scale_x,
                'scale_y': scale_y,
                'draw_x': draw_x,
                'draw_y': draw_y,
                'zoomed_width': zoomed_width,
                'zoomed_height': zoomed_height
            }
        except Exception as e:
            print(f"_compute_line_transform error: {e}")
            return None
    def _fast_line_update(self):
        """Fast path to redraw only lines over current displayed image using GPU if available.
        Falls back to full display_image if prerequisites missing.
        Assumes self.current_image is path to original image file."""
        try:
            if not self.current_image:
                return
            if not (self.drawn_lines or self.drawn_horizontal_lines or self.drawn_free_lines or self.drawn_free_strokes):
                return
            if not self.lines_visible:
                return
            # Semi-transparent lines cannot use the incremental overlay path:
            #   • the GPU kernel overwrites pixels (ignoring alpha), and
            #   • painting onto the already-drawn pixmap double-blends existing
            #     lines.
            # Render from the clean base image in a single pass so every line
            # (new and existing) reflects the current transparency level.
            if getattr(self, 'line_transparency', 255) < 255:
                self.display_image(self.current_image)
                return
            # 🧽 Eraser active: the fast overlay path paints onto the already-drawn
            # pixmap and cannot reveal the clean image. Use the full erase-aware path.
            if self.erase_strokes or self.current_erase_stroke:
                self.display_image(self.current_image)
                return
            # Get currently displayed pixmap (may already have LUT/enhancements applied)
            current_pixmap = self.image_label.pixmap()
            if (not current_pixmap) or current_pixmap.isNull():
                # Fallback: full redraw
                self.display_image(self.current_image)
                return
            # If rotation or flips are active we must avoid the GPU fast-path (it expects non-transformed pixmaps).
            # Allow the CPU fallback below to handle transformed images, so DO NOT call display_image()
            transforms_active = (self.rotation_angle != 0 or self.flipped_h or self.flipped_v)
            # Use GPU accelerated overlay when possible & image large enough
            # Only use GPU accelerated overlay when no transforms are active
            if (not transforms_active and hasattr(self, 'gpu_processor') and self.gpu_processor and 
                self.gpu_processor.is_available() and 
                current_pixmap.width() * current_pixmap.height() > 100000):
                # Convert to image in BGRA/rgba format
                image = current_pixmap.toImage()
                if image.format() != image.Format.Format_RGBA8888:
                    image = image.convertToFormat(image.Format.Format_RGBA8888)
                # Prepare scaled coordinates (since current_pixmap is already scaled/zoomed)
                # We assume original_pixmap exists to compute scale; if not, fallback
                if not hasattr(self, 'original_pixmap') or not self.original_pixmap:
                    self.display_image(self.current_image)
                    return
                tx = self._compute_line_transform()
                if not tx:
                    self.display_image(self.current_image)
                    return
                scale_x = tx['scale_x']; scale_y = tx['scale_y']
                draw_x = tx['draw_x']; draw_y = tx['draw_y']
                # If current pixmap already equals zoomed image (no letterbox), ignore draw offsets
                if current_pixmap.width() == tx['zoomed_width'] and current_pixmap.height() == tx['zoomed_height']:
                    offset_x = 0; offset_y = 0
                else:
                    offset_x = draw_x; offset_y = draw_y
                scaled_vertical = [int(x * scale_x) + offset_x for x in self.drawn_lines]
                scaled_horizontal = [int(y * scale_y) + offset_y for y in self.drawn_horizontal_lines]
                scaled_free = []
                for line in self.drawn_free_lines:
                    (sx, sy) = line['start']; (ex, ey) = line['end']
                    scaled_free.append({'start': (int(sx * scale_x) + offset_x, int(sy * scale_y) + offset_y),
                                        'end':   (int(ex * scale_x) + offset_x, int(ey * scale_y) + offset_y)})
                gpu_result = self.gpu_processor.draw_lines_gpu(
                    image,
                    scaled_vertical,
                    scaled_horizontal,
                    scaled_free,
                    self.line_color,
                    self.line_thickness
                )
                if gpu_result is not None:
                    # After GPU draw, verify free lines rendered; if none (possible off-screen), do CPU overlay pass
                    if self.drawn_free_lines:
                        overlay = gpu_result.copy()
                        painter = QPainter(overlay)
                        # Keep non-free lines non-antialiased for crispness and speed,
                        # but enable antialiasing for free-form lines for better visual quality.
                        painter.setPen(QPen(self.line_color, self.line_thickness, Qt.SolidLine))
                        painter.setRenderHint(QPainter.Antialiasing, True)
                        for line in self.drawn_free_lines:
                            (sx, sy) = line['start']; (ex, ey) = line['end']
                            painter.drawLine(int(sx * scale_x) + offset_x, int(sy * scale_y) + offset_y,
                                             int(ex * scale_x) + offset_x, int(ey * scale_y) + offset_y)
                        painter.end()
                        overlay = self._apply_fixed_overlays_to_pixmap(overlay)
                        self.image_label.setPixmap(overlay)
                    else:
                        self.image_label.setPixmap(self._apply_fixed_overlays_to_pixmap(gpu_result))
                    return
            # CPU fallback: manually paint over current_pixmap copy
            overlay = current_pixmap.copy()
            painter = QPainter(overlay)
            # Default to no antialiasing (vertical/horizontal lines remain crisp).
            painter.setRenderHint(QPainter.Antialiasing, False)
            painter.setPen(QPen(self.line_color, self.line_thickness, Qt.SolidLine))
            tx = self._compute_line_transform()
            if tx:
                scale_x = tx['scale_x']; scale_y = tx['scale_y']
                draw_x = tx['draw_x']; draw_y = tx['draw_y']
                if overlay.width() == tx['zoomed_width'] and overlay.height() == tx['zoomed_height']:
                    offset_x = 0; offset_y = 0
                else:
                    offset_x = draw_x; offset_y = draw_y
                
                # Get original size for transformation calculations
                original_size = self.original_pixmap.size() if hasattr(self, 'original_pixmap') and self.original_pixmap else QSize(1, 1)
                
                # Apply coordinate transformations based on rotation and flips
                if self.rotation_angle != 0 or self.flipped_h or self.flipped_v:
                    # Handle vertical lines with transformations
                    for x in self.drawn_lines:
                        # Apply flip transformations first
                        transformed_x = x
                        if self.flipped_h:
                            transformed_x = original_size.width() - x
                        
                        # Then apply rotation transformation
                        if self.rotation_angle == 90:
                            # Vertical line becomes horizontal
                            dy = int(transformed_x * scale_y) + offset_y
                            if 0 <= dy < overlay.height():
                                painter.drawLine(0, dy, overlay.width(), dy)
                        elif self.rotation_angle == 180:
                            # Vertical line stays vertical but position changes
                            final_x = original_size.width() - transformed_x
                            dx = int(final_x * scale_x) + offset_x
                            if 0 <= dx < overlay.width():
                                painter.drawLine(dx, 0, dx, overlay.height())
                        elif self.rotation_angle == 270:
                            # Vertical line becomes horizontal
                            final_y = original_size.height() - transformed_x
                            dy = int(final_y * scale_y) + offset_y
                            if 0 <= dy < overlay.height():
                                painter.drawLine(0, dy, overlay.width(), dy)
                        else:
                            # No rotation, just flips applied
                            dx = int(transformed_x * scale_x) + offset_x
                            if 0 <= dx < overlay.width():
                                painter.drawLine(dx, 0, dx, overlay.height())
                    
                    # Handle horizontal lines with transformations
                    for y in self.drawn_horizontal_lines:
                        # Apply flip transformations first
                        transformed_y = y
                        if self.flipped_v:
                            transformed_y = original_size.height() - y
                        
                        # Then apply rotation transformation
                        if self.rotation_angle == 90:
                            # Horizontal line becomes vertical
                            final_x = original_size.width() - transformed_y
                            dx = int(final_x * scale_x) + offset_x
                            if 0 <= dx < overlay.width():
                                painter.drawLine(dx, 0, dx, overlay.height())
                        elif self.rotation_angle == 180:
                            # Horizontal line stays horizontal but position changes
                            final_y = original_size.height() - transformed_y
                            dy = int(final_y * scale_y) + offset_y
                            if 0 <= dy < overlay.height():
                                painter.drawLine(0, dy, overlay.width(), dy)
                        elif self.rotation_angle == 270:
                            # Horizontal line becomes vertical
                            dx = int(transformed_y * scale_x) + offset_x
                            if 0 <= dx < overlay.width():
                                painter.drawLine(dx, 0, dx, overlay.height())
                        else:
                            # No rotation, just flips applied
                            dy = int(transformed_y * scale_y) + offset_y
                            if 0 <= dy < overlay.height():
                                painter.drawLine(0, dy, overlay.width(), dy)
                    
                    # Handle free lines with transformations
                    for line in self.drawn_free_lines:
                        start_x, start_y = line['start']
                        end_x, end_y = line['end']
                        
                        # Apply flip transformations first
                        flip_start_x = start_x if not self.flipped_h else original_size.width() - start_x
                        flip_start_y = start_y if not self.flipped_v else original_size.height() - start_y
                        flip_end_x = end_x if not self.flipped_h else original_size.width() - end_x
                        flip_end_y = end_y if not self.flipped_v else original_size.height() - end_y
                        
                        # Then apply rotation transformation
                        if self.rotation_angle == 90:
                            display_start_x = int((original_size.width() - flip_start_y) * scale_x) + offset_x
                            display_start_y = int(flip_start_x * scale_y) + offset_y
                            display_end_x = int((original_size.width() - flip_end_y) * scale_x) + offset_x
                            display_end_y = int(flip_end_x * scale_y) + offset_y
                        elif self.rotation_angle == 180:
                            display_start_x = int((original_size.width() - flip_start_x) * scale_x) + offset_x
                            display_start_y = int((original_size.height() - flip_start_y) * scale_y) + offset_y
                            display_end_x = int((original_size.width() - flip_end_x) * scale_x) + offset_x
                            display_end_y = int((original_size.height() - flip_end_y) * scale_y) + offset_y
                        elif self.rotation_angle == 270:
                            display_start_x = int(flip_start_y * scale_x) + offset_x
                            display_start_y = int((original_size.height() - flip_start_x) * scale_y) + offset_y
                            display_end_x = int(flip_end_y * scale_x) + offset_x
                            display_end_y = int((original_size.height() - flip_end_x) * scale_y) + offset_y
                        else:
                            # No rotation, just flips applied
                            display_start_x = int(flip_start_x * scale_x) + offset_x
                            display_start_y = int(flip_start_y * scale_y) + offset_y
                            display_end_x = int(flip_end_x * scale_x) + offset_x
                            display_end_y = int(flip_end_y * scale_y) + offset_y
                        
                        # Enable antialiasing for free-form lines for smoother appearance
                        painter.setRenderHint(QPainter.Antialiasing, True)
                        painter.drawLine(display_start_x, display_start_y, display_end_x, display_end_y)
                        painter.setRenderHint(QPainter.Antialiasing, False)
                    
                    # ⚡ DRAW FREE STROKES WITH TRANSFORMS: Handle free draw strokes with transformations
                    if self.drawn_free_strokes:
                        painter.setRenderHint(QPainter.Antialiasing, True)
                        for stroke in self.drawn_free_strokes:
                            if len(stroke) < 2:
                                continue
                            for i in range(len(stroke) - 1):
                                # 🎨 PEN PRESSURE: Handle both old 2-tuple and new 3-tuple formats
                                if len(stroke[i]) == 3:
                                    start_x, start_y, start_pressure = stroke[i]
                                else:
                                    start_x, start_y = stroke[i]
                                    start_pressure = 1.0
                                if len(stroke[i + 1]) == 3:
                                    end_x, end_y, end_pressure = stroke[i + 1]
                                else:
                                    end_x, end_y = stroke[i + 1]
                                    end_pressure = 1.0
                                
                                # Apply flip transformations first
                                flip_start_x = start_x if not self.flipped_h else original_size.width() - start_x
                                flip_start_y = start_y if not self.flipped_v else original_size.height() - start_y
                                flip_end_x = end_x if not self.flipped_h else original_size.width() - end_x
                                flip_end_y = end_y if not self.flipped_v else original_size.height() - end_y
                                
                                # Then apply rotation transformation
                                if self.rotation_angle == 90:
                                    display_start_x = int(flip_start_y * scale_x) + offset_x
                                    display_start_y = int((original_size.width() - flip_start_x) * scale_y) + offset_y
                                    display_end_x = int(flip_end_y * scale_x) + offset_x
                                    display_end_y = int((original_size.width() - flip_end_x) * scale_y) + offset_y
                                elif self.rotation_angle == 180:
                                    display_start_x = int((original_size.width() - flip_start_x) * scale_x) + offset_x
                                    display_start_y = int((original_size.height() - flip_start_y) * scale_y) + offset_y
                                    display_end_x = int((original_size.width() - flip_end_x) * scale_x) + offset_x
                                    display_end_y = int((original_size.height() - flip_end_y) * scale_y) + offset_y
                                elif self.rotation_angle == 270:
                                    display_start_x = int((original_size.height() - flip_start_y) * scale_x) + offset_x
                                    display_start_y = int(flip_start_x * scale_y) + offset_y
                                    display_end_x = int((original_size.height() - flip_end_y) * scale_x) + offset_x
                                    display_end_y = int(flip_end_x * scale_y) + offset_y
                                else:
                                    # No rotation, just flips applied
                                    display_start_x = int(flip_start_x * scale_x) + offset_x
                                    display_start_y = int(flip_start_y * scale_y) + offset_y
                                    display_end_x = int(flip_end_x * scale_x) + offset_x
                                    display_end_y = int(flip_end_y * scale_y) + offset_y
                                
                                # 🎨 PEN PRESSURE: Per-segment thickness matches the live preview
                                avg_pressure = (start_pressure + end_pressure) / 2.0
                                dynamic_thickness = self._pressure_to_thickness(avg_pressure)
                                painter.setPen(QPen(self.line_color, dynamic_thickness, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
                                painter.drawLine(display_start_x, display_start_y, display_end_x, display_end_y)
                        painter.setRenderHint(QPainter.Antialiasing, False)
                else:
                    # No transformations needed - simple case
                    for x in self.drawn_lines:
                        dx = int(x * scale_x) + offset_x
                        if 0 <= dx < overlay.width():
                            painter.drawLine(dx, 0, dx, overlay.height())
                    for y in self.drawn_horizontal_lines:
                        dy = int(y * scale_y) + offset_y
                        if 0 <= dy < overlay.height():
                            painter.drawLine(0, dy, overlay.width(), dy)
                    for line in self.drawn_free_lines:
                        (sx, sy) = line['start']; (ex, ey) = line['end']
                        painter.drawLine(int(sx * scale_x) + offset_x, int(sy * scale_y) + offset_y,
                                         int(ex * scale_x) + offset_x, int(ey * scale_y) + offset_y)
                    
                    # ⚡ DRAW FREE STROKES: Render completed free draw strokes  
                    if self.drawn_free_strokes:
                        painter.setRenderHint(QPainter.Antialiasing, True)
                        for stroke in self.drawn_free_strokes:
                            if len(stroke) < 2:
                                continue
                            for i in range(len(stroke) - 1):
                                # 🎨 PEN PRESSURE: Handle both old 2-tuple and new 3-tuple formats
                                if len(stroke[i]) == 3:
                                    start_x, start_y, start_pressure = stroke[i]
                                else:
                                    start_x, start_y = stroke[i]
                                    start_pressure = 1.0
                                if len(stroke[i + 1]) == 3:
                                    end_x, end_y, end_pressure = stroke[i + 1]
                                else:
                                    end_x, end_y = stroke[i + 1]
                                    end_pressure = 1.0
                                # 🎨 PEN PRESSURE: Per-segment thickness matches the live preview
                                avg_pressure = (start_pressure + end_pressure) / 2.0
                                dynamic_thickness = self._pressure_to_thickness(avg_pressure)
                                painter.setPen(QPen(self.line_color, dynamic_thickness, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
                                painter.drawLine(int(start_x * scale_x) + offset_x, int(start_y * scale_y) + offset_y,
                                               int(end_x * scale_x) + offset_x, int(end_y * scale_y) + offset_y)
                        painter.setRenderHint(QPainter.Antialiasing, False)
            else:
                # Fallback simple proportional scaling when transform computation fails
                if hasattr(self, 'original_pixmap') and self.original_pixmap:
                    orig_w = self.original_pixmap.width(); orig_h = self.original_pixmap.height()
                    disp_w = current_pixmap.width(); disp_h = current_pixmap.height()
                    scale_x = disp_w / orig_w if orig_w else 1.0
                    scale_y = disp_h / orig_h if orig_h else 1.0
                else:
                    scale_x = scale_y = 1.0
                # Simple drawing without coordinate transformations (fallback)
                for x in self.drawn_lines:
                    dx = int(x * scale_x)
                    if 0 <= dx < overlay.width():
                        painter.drawLine(dx, 0, dx, overlay.height())
                for y in self.drawn_horizontal_lines:
                    dy = int(y * scale_y)
                    if 0 <= dy < overlay.height():
                        painter.drawLine(0, dy, overlay.width(), dy)
                for line in self.drawn_free_lines:
                    (sx, sy) = line['start']; (ex, ey) = line['end']
                    painter.drawLine(int(sx * scale_x), int(sy * scale_y), int(ex * scale_x), int(ey * scale_y))
                
                # ⚡ DRAW FREE STROKES (FALLBACK): Render completed free draw strokes
                if self.drawn_free_strokes:
                    painter.setRenderHint(QPainter.Antialiasing, True)
                    for stroke in self.drawn_free_strokes:
                        if len(stroke) < 2:
                            continue
                        for i in range(len(stroke) - 1):
                            # 🎨 PEN PRESSURE: Handle both old 2-tuple and new 3-tuple formats
                            if len(stroke[i]) == 3:
                                start_x, start_y, start_pressure = stroke[i]
                            else:
                                start_x, start_y = stroke[i]
                                start_pressure = 1.0
                            if len(stroke[i + 1]) == 3:
                                end_x, end_y, end_pressure = stroke[i + 1]
                            else:
                                end_x, end_y = stroke[i + 1]
                                end_pressure = 1.0
                            # 🎨 PEN PRESSURE: Per-segment thickness matches the live preview
                            avg_pressure = (start_pressure + end_pressure) / 2.0
                            dynamic_thickness = self._pressure_to_thickness(avg_pressure)
                            painter.setPen(QPen(self.line_color, dynamic_thickness, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
                            painter.drawLine(int(start_x * scale_x), int(start_y * scale_y),
                                           int(end_x * scale_x), int(end_y * scale_y))
                    painter.setRenderHint(QPainter.Antialiasing, False)
            painter.end()
            overlay = self._apply_fixed_overlays_to_pixmap(overlay)
            self.image_label.setPixmap(overlay)
        except Exception as e:
            print(f"_fast_line_update failed: {e}; skipping line overlay to prevent loops")
            # DO NOT call display_image here as it can cause infinite loops with rotation + LUT
            # Instead, just skip the line overlay and let the current image display without lines
    # ...existing code...
    def __init__(self):
        super().__init__()
        self.setFocusPolicy(Qt.StrongFocus)
        self.setWindowTitle("Ova Viewer")
        self.setGeometry(100, 100, 1200, 760)
        # Accept touch events at the top-level window so Qt does not discard
        # them before they reach the ImageLabel child widget.
        self.setAttribute(Qt.WA_AcceptTouchEvents, True)
        
        # Set window icon
        self.set_window_icon()
        
        # Enable drag and drop for folders
        self.setAcceptDrops(True)
        
        self.folder = None
        self.images = []            # Browsing list AFTER the file-type filter
        self._all_items = []        # Every scanned item, before filtering
        self._hidden_types = set()  # Extensions hidden via the File Types panel
        self._type_filter_window = None
        self._type_filter_window_pos = None  # session-only memory of last drag pos
        self.history = []
        self.current_image = None
        self.current_index = -1
        self.history_index = -1  # NEW: For navigation

        # PDF on-demand state
        self._pdf_doc = None   # PdfDocument instance when viewing a PDF
        self._pdf_page = 0     # current 0-based page number
        self._pdf_name = ""

        # PDF high-resolution zoom state: when the user zooms into a PDF page we
        # re-render just that page (or the current 2/3-page spread) at a higher
        # resolution (on a background thread) so small text stays sharp instead
        # of upscaling a blurry raster. Images are untouched.
        self._pdf_hires_page = -1      # anchor page currently loaded at hi-res (-1 = none)
        self._pdf_hires_count = 1      # spread page-count of the current hi-res render
        self._pdf_hires_target = 0     # target size (long edge / composite height) px
        self._pdf_hires_thread = None  # active PdfHiresWorker, if any
        self._pdf_hires_timer = QTimer(self)
        self._pdf_hires_timer.setSingleShot(True)
        self._pdf_hires_timer.setInterval(180)
        self._pdf_hires_timer.timeout.connect(self._start_pdf_hires_render)

        # PDF spread (book) view: "single" | "2page" | "3page". Persisted via
        # QSettings so the user's preferred reading layout survives restarts.
        from PySide6.QtCore import QSettings
        self._settings = QSettings("OvaViewer", "RandomImageViewer")
        mode = self._settings.value("pdf_spread_mode", "single")
        if mode not in ("single", "2page", "3page"):
            mode = "single"
        self._pdf_spread_mode = mode

        # EPUB on-demand state
        self._epub_doc = None  # EpubDocument instance when viewing an EPUB
        self._epub_page = 0    # current 0-based page number
        self._epub_name = ""

        # CBR/CBZ on-demand state (comic-book archives)
        self._cbr_doc = None   # CbrDocument instance when viewing a comic archive
        self._cbr_page = 0     # current 0-based page number
        self._cbr_name = ""
        
        # Performance optimization: Cache management for large collections
        self.pixmap_cache = {}  # Cache for loaded pixmaps
        self.max_cache_size = 20  # Increased cache size for better performance with large collections
        self.scaled_cache = {}  # Cache for scaled versions
        self.last_size = None  # Track resize events
        self.resize_timer = QTimer()
        self.resize_timer.setSingleShot(True)
        self.resize_timer.timeout.connect(self._delayed_resize)
        self.resize_timer.setInterval(50)  # 50ms delay for resize debouncing
        
        # Line drawing functionality
        self.line_drawing_mode = False
        self.horizontal_line_drawing_mode = False
        self.free_line_drawing_mode = False  # New: Free line drawing mode
        self.free_draw_mode = False  # NEW: Free draw mode (continuous drawing)
        self.is_drawing_free_stroke = False  # Track if currently drawing a free stroke
        # 💉 Color Snap (eyedropper) tool state
        self.color_snap_mode = False
        self._color_snap_preview = None  # Lazy-created ColorSnapPreview widget
        self._color_snap_last_label_pos = None  # Sampled point in label coords (for overlay connector)
        self._color_snap_last_color = None  # Last sampled QColor (for overlay connector)
        # Snap palette: persisted swatches of colors picked via 💉 or 🪄 auto-extract.
        # Stored as ROWS — each 🪄 extract creates a new row; 💉 clicks append to
        # the most-recent row. Global dedup across all rows.
        self.snapped_rows = []  # list[list[str]]
        self.snapped_rows_max = 10  # FIFO cap on number of rows
        # Floating palette window (lazy-created), persistent until user hits ✕.
        self._snap_palette_window = None
        self._snap_palette_window_pos = None  # session-only memory of last drag pos
        # Floating Curves (RGB levels) window (lazy-created).
        self._curves_window = None
        self._curves_window_pos = None  # session-only memory of last drag pos
        # Cached source QImage for fast sampling (rebuilt on current_image change)
        self._color_snap_src_image = None
        self._color_snap_src_path = None
        # Debounce: only sample hover after cursor has been still for HOVER_MS
        self._color_snap_hover_ms = 350
        self._color_snap_hover_timer = QTimer(self)
        self._color_snap_hover_timer.setSingleShot(True)
        self._color_snap_hover_timer.timeout.connect(self._on_color_snap_hover_timeout)
        self._color_snap_pending_label_pos = None
        self._color_snap_pending_global_pos = None
        
        # ⚡ PERFORMANCE: Real-time drawing optimization
        self.drawing_cache = None  # Cache converted coordinates and display data
        self.temp_stroke_overlay = None  # Temporary overlay for real-time drawing
        self.last_draw_point = None  # Track last painted point for incremental drawing
        
        # ⚡ ULTRA-FAST: Timer-based updates for true real-time performance.
        # Free-draw paints onto a cached overlay (cheap, no per-event image reload),
        # so it can refresh almost immediately for low-latency pen feedback. The
        # eraser freeze was a separate issue (fixed via its own coord cache), so a
        # fast interval here is safe.
        self.stroke_update_timer = QTimer(self)
        self.stroke_update_timer.setSingleShot(True)
        self.stroke_update_timer.timeout.connect(self._update_display_with_overlay)
        self.stroke_update_timer.setInterval(8)  # ~120 FPS steady refresh during strokes; low latency, input-flood-safe
        self.drawn_lines = []  # List of x positions for vertical lines
        self.drawn_horizontal_lines = []  # List of y positions for horizontal lines
        self.drawn_free_lines = []  # List of free lines, each with start and end points
        self.drawn_free_strokes = []  # NEW: List of free draw strokes (continuous paths)
        # 🧽 Eraser tool: partial pixel-erase of the drawing/line layer (never the image).
        # Each stroke is a list of (x, y, radius) points in ORIGINAL image coords;
        # radius is stored in original-image px so erased holes stay anchored under zoom.
        self.eraser_mode = False
        self.eraser_size = 20  # Eraser diameter in screen px (range 1-30)
        self.erase_strokes = []  # List[List[(x, y, radius)]]
        # For each committed erase stroke, snapshot how many free strokes/lines existed
        # at that moment. Drawings added AFTER the most recent erase are kept visible
        # inside erase holes, so you can draw again over an erased area.
        self._erase_state_marks = []  # Parallel to erase_strokes: [{'free_strokes': n, 'free_lines': n}]
        self._undo_stack = []  # Chronological record: 'line'|'hline'|'free_line'|'free_stroke'|'erase'
        self.current_erase_stroke = None  # Stroke being drawn
        self.is_erasing = False  # Track if currently erasing
        self.eraser_cache = None  # Precomputed coord-mapping geometry during a stroke
        # ⚡ Coalesce high-frequency tablet/mouse erase points into ~60 FPS redraws
        # so a fast drag doesn't fire a full display_image per event and freeze.
        self.erase_update_timer = QTimer(self)
        self.erase_update_timer.setSingleShot(True)
        self.erase_update_timer.setInterval(16)
        self.erase_update_timer.timeout.connect(self._flush_erase_update)
        self.current_line_start = None  # Store first click point for free line
        self._line_preview_base = None  # Snapshot pixmap for free-line rubber-band preview
        self._line_preview_geom = None  # Geometry cache for the preview
        self._line_preview_pending_pos = None  # Latest pen pos awaiting render
        # ⚡ Throttle the free-line preview to ~60 FPS (tablet-event-rate safe)
        self.line_preview_timer = QTimer(self)
        self.line_preview_timer.setSingleShot(True)
        self.line_preview_timer.setInterval(16)
        self.line_preview_timer.timeout.connect(self._flush_line_preview)
        self.current_stroke = None  # NEW: Current stroke being drawn
        self.is_drawing = False  # NEW: Track if currently drawing a stroke
        self.lines_visible = True  # New: Toggle line visibility
        self.image_visible = True  # New: Toggle image visibility
        self.line_thickness = 5  # Default line thickness (increased for better pen pressure visibility)
        self.line_color = QColor("#FFFFFF")  # Default white color
        self.line_transparency = 255  # Line transparency (0=fully transparent, 255=fully opaque)
        self.line_color.setAlpha(self.line_transparency)  # Set initial transparency
        self.line_antialiasing = True  # Enable antialiasing for smoother lines
        self.performance_mode = True  # NEW: Performance mode toggle (True = fast, False = quality)
        self.pen_pressure_enabled = True  # 🎨 NEW: Pen pressure sensitivity toggle
        self._current_pressure = 1.0  # 🎨 NEW: Current pressure value for real-time painting
        self._tablet_pressure = 1.0  # 🎨 NEW: Stored tablet pressure for mouse event compatibility
        self._last_pressure = 1.0  # 🎨 NEW: Last pressure for smoothing interpolation
        # Fixed overlay tools (non-interactive, passive overlays over the image)
        self.crosshair_overlay = False  # Draw cross lines at image center
        self.grid_overlay = False       # Draw 3x3 grid over the image
        # Always on top functionality
        self.always_on_top = False

        # Fullscreen functionality
        self.is_fullscreen = False
        self.normal_geometry = None  # Store window geometry before fullscreen

        # Image enhancement parameters
        self.grayscale_value = 0  # 0 = color, 100 = full grayscale
        self.contrast_value = 50  # 50 = normal, -130 to 200 range
        self.gamma_value = 0     # 0 = normal, -200 to +500 range
        self.value_filter_enabled = False  # Posterize to N grayscale tones (value study)
        self.value_levels = 4              # Number of value levels when posterize is enabled (2-10)
        # Color Groups (palette quantization) - flat color fields from the image's own colors
        self.color_groups_enabled = False  # Reduce image to N dominant colors (color map)
        self.color_groups_count = 8        # Number of palette colors (2-32)
        self.color_groups_field = 0        # Field-size pre-smoothing to merge regions (0=off, 0-20)
        self._color_palette_cache = {}     # Cache computed palettes keyed by image+params
        # Object Groups (cryptomatte-style) - segment into regions, flatten each
        # to its own local colour (spatial separation, unlike Color Groups)
        self.object_groups_enabled = False   # Toggle the per-object flattening
        self.object_groups_detail = 45       # Region granularity (0-100, higher=more objects)
        self.object_groups_min_size = 12     # Minimum region size (0-100, higher=fewer specks)
        self.object_groups_mode = "local"    # local | id | local_edges
        # Edge detection (Canny "plane change" filter)
        self.edge_detection_enabled = False  # Toggle Canny edge detection
        self.edge_mode = "white_on_black"    # white_on_black | black_on_white | overlay
        self.edge_sensitivity = 50           # 0-100, drives Canny thresholds
        # Edge line color override. None = each mode's default (white on dark,
        # black on light, line color over image). Set by the line-color tools.
        self.edge_color = None
        # Curves (classical RGB levels) - per-channel black/white/midtone tone curve
        self.curves_enabled = False          # Toggle the curves effect
        self.curves_channel = "master"       # Active channel being edited (master/r/g/b)
        self.curves_black = {"master": 0, "r": 0, "g": 0, "b": 0}     # Black point (0-254)
        self.curves_white = {"master": 255, "r": 255, "g": 255, "b": 255}  # White point (1-255)
        self.curves_gamma = {"master": 0, "r": 0, "g": 0, "b": 0}     # Midtone slider (-100..100, 0=neutral)
        self.curves_opacity = 100            # Effect opacity/strength (0-100, 100=full)
        self.rotation_angle = 0   # Rotation angle in degrees
        self.flipped_h = False    # Horizontal flip state
        self.flipped_v = False    # Vertical flip state
        self.original_pixmap = None  # Cache original image for fast processing
        self.enhancement_cache = {}  # Cache enhanced versions
        
        # LUT (Look-Up Table) support
        self.current_lut = None   # Currently loaded LUT data
        self.current_lut_name = "None"  # Name of current LUT
        self.lut_folder = None    # Folder containing CUBE LUT files
        self.lut_files = []       # List of available LUT files
        self.lut_strength = 100   # LUT application strength (0-100%)
        self.lut_cache = {}       # Cache for loaded LUTs to avoid reloading
        # Separate enable flag so a selected LUT can be temporarily disabled without clearing selection
        self.lut_enabled = False
        # Track whether cached last processed image contained LUT so we don't reuse it when disabled
        self._last_processed_has_lut = False
        
        # GPU acceleration - initialized lazily on first use to avoid 30s startup delay
        self.gpu_processor = GPULutProcessor()

        # Autoplay-on-end: advance to the next item when a video or animated
        # GIF finishes, so clips play back to back. Off by default.
        self.autoplay_next_enabled = False
        self._autoplay_advancing = False   # re-entrancy guard while switching
        self._gif_prev_frame = -1          # last GIF frame seen (loop detection)
        self._gif_started_at = 0.0         # monotonic time the GIF pass began

        # Dub audio: play a sibling audio file (clip.mp4 -> clip.mp3) instead
        # of the video's own track. Off by default — it costs a sibling-file
        # lookup per video and a second media player while one is playing.
        self.dub_audio_enabled = False

        # Video playback state
        self._video_playing = False
        self._video_muted = True   # Videos start silent; unmuting sticks for the session
        self._video_volume = 50  # 0-100

        # Subtitle (.srt) state for video playback
        self._subtitles_enabled = True       # show subtitles when available
        self._subtitle_cues = []             # list of (start_ms, end_ms, text)
        self._subtitle_starts = []           # cached start times for bisect lookup
        self._subtitle_path = None           # loaded .srt path (or None)
        self._current_subtitle_text = ""     # cue text at the current position
        self._video_last_scaled = None       # last scaled frame (pre-subtitle) for redraw

        self.timer_interval = 5  # seconds
        self.timer_remaining = 0
        self._auto_advance_active = False
        self._timer_paused = False  # NEW: Timer pause state

        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        
        # Sorting mode for navigation
        self.random_mode = True  # Default to random mode
        
        # Store original window flags for UI toggle
        self.original_window_flags = None
        
        # Window dragging and resizing for frameless mode
        self.dragging = False
        self.drag_start_position = None
        self.resizing = False
        self.resize_edge = None
        self.resize_margin = 10  # Pixel margin for resize detection
        self.timer.timeout.connect(self._on_timer_tick)

        self.init_ui()
        # Set initial window geometry — a bit wider than before.
        try:
            self.resize(1200, 760)
        except Exception as _e:
            pass
        
        # Store original window flags for UI toggle functionality
        self.original_window_flags = self.windowFlags()
        
        # Force layout evaluation after initial resize
        QTimer.singleShot(0, self._delayed_resize)
        # Auto-set default LUT folder if present
        # NOTE: trailing backslash in a raw string would escape the quote -> syntax error
        self.default_lut_path = r"V:\LUTS"
        if os.path.isdir(self.default_lut_path):
            self.lut_folder = self.default_lut_path
            try:
                self.lut_files = self.scan_lut_folder(self.default_lut_path)
                if hasattr(self, 'lut_combo') and self.lut_files:
                    # Populate using the same display-name format as
                    # update_lut_combo (forward slashes, no .cube extension) so
                    # the combo text matches what apply_selected_lut expects.
                    self.update_lut_combo()
                    self.current_lut_name = "None"
                print(f"Default LUT folder loaded: {self.default_lut_path} ({len(self.lut_files)} files)")
            except Exception as e:
                print(f"Failed loading default LUT folder {self.default_lut_path}: {e}")

    def tabletEvent(self, event):
        """Forward tablet events to the ImageLabel for proper pressure handling"""
        if hasattr(self, 'image_label') and self.image_label:
            # Forward the tablet event to the ImageLabel
            self.image_label.tabletEvent(event)

    def set_window_icon(self):
        """Set the window icon from available icon files"""
        try:
            import os
            import sys
            
            # Determine the correct path for icon files
            if getattr(sys, 'frozen', False):
                # Running as PyInstaller executable
                base_path = sys._MEIPASS  # PyInstaller's temporary folder
                print(f"Running as executable, looking for icons in: {base_path}")
            else:
                # Running as Python script - icons are in project root
                base_path = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
                print(f"Running as script, looking for icons in: {base_path}")
            
            icon_loaded = False
            
            # Try different icon formats in order of preference
            icon_files = [
                ("icon.svg", "SVG"),
                ("icon.png", "PNG"), 
                ("icon.ico", "ICO")
            ]
            
            for filename, format_name in icon_files:
                icon_path = os.path.join(base_path, filename)
                print(f"Trying icon path: {icon_path}")
                if os.path.exists(icon_path):
                    icon = QIcon(icon_path)
                    if not icon.isNull():
                        self.setWindowIcon(icon)
                        print(f"✅ Window icon loaded from {format_name}: {filename}")
                        icon_loaded = True
                        break
                else:
                    print(f"❌ Icon not found: {icon_path}")
            
            if not icon_loaded:
                print("ℹ️ No icon file found (tried: icon.svg, icon.png, icon.ico)")
                
        except Exception as e:
            print(f"⚠️ Failed to load window icon: {e}")
            import traceback
            traceback.print_exc()

    def init_ui(self):
        # 🎨 Enable tablet tracking on main window for proper pressure support
        self.setTabletTracking(True)
        self.setAttribute(Qt.WA_TabletTracking, True)

        # Create main toolbar — Qt's native overflow ('»') button appears
        # automatically when items don't fit at the current window width,
        # giving access to clipped icons via a popup.
        self.main_toolbar = QToolBar("Main Toolbar")
        self.main_toolbar.setIconSize(QSize(20, 20))
        self.addToolBar(Qt.TopToolBarArea, self.main_toolbar)
        self.main_toolbar.setMovable(False)
        # Right padding keeps the last icon clear of the native overflow
        # ('»') button when the bar is narrow.
        self.main_toolbar.setStyleSheet("QToolBar { spacing: 4px; padding-right: 24px; }")

        # Force a toolbar break to ensure next toolbar goes on new line
        self.addToolBarBreak(Qt.TopToolBarArea)

        # Create secondary toolbar for sliders (initially hidden) - this will be BELOW main toolbar
        self.slider_toolbar = QToolBar("Slider Toolbar")
        self.slider_toolbar.setIconSize(QSize(20, 20))
        self.addToolBar(Qt.TopToolBarArea, self.slider_toolbar)
        self.slider_toolbar.setMovable(False)
        self.slider_toolbar.setStyleSheet("QToolBar { spacing: 4px; padding-right: 24px; background: #2a2d30; border-top: 1px solid #35383b; }")
        self.slider_toolbar.setMinimumHeight(32)
        self.slider_toolbar.setMaximumHeight(40)
        self.slider_toolbar.hide()

        # Track which mode we're in. Lowered threshold (was 1500) so the
        # second row appears earlier and fewer icons get hidden behind the
        # native '»' overflow popup at common window widths.
        # Track which mode we're in. Threshold raised (was 1100) so the
        # second row appears before any icon would overflow into the
        # native '»' popup, which is hard to keep open on hover-out.
        self.two_row_mode = False
        self.width_threshold = 1600

        # Setup main toolbar with all controls
        self._setup_main_toolbar()
        
        # Setup enhancement controls on BOTH toolbars permanently
        self._setup_enhancement_controls()

        # Keep the toolbar overflow ('»') popup open for 2 seconds after
        # the cursor leaves it, so the user has time to come back.
        self._install_popup_persistence()

        # Central widget and layout setup
        central_splitter = QSplitter(Qt.Horizontal)
        self.setCentralWidget(central_splitter)

        image_widget = QWidget()
        image_widget.setTabletTracking(True)
        image_widget.setAttribute(Qt.WA_TabletTracking, True)
        image_layout = QVBoxLayout(image_widget)
        image_layout.setContentsMargins(6, 6, 6, 6)

        self.image_label = ImageLabel("Open a folder to start")
        self.image_label.parent_viewer = self
        self.image_label.setAlignment(Qt.AlignCenter)
        self.image_label.setScaledContents(False)
        self.image_label.setMinimumSize(400, 400)
        self.image_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.image_label.setToolTip("")

        image_layout.addWidget(self.image_label)
        image_widget.setLayout(image_layout)
        central_splitter.addWidget(image_widget)

        # Semi-transparent overlay shown while a PDF is being rendered
        self._pdf_overlay = PdfLoadingOverlay(self.image_label)

        # Floating video controls overlay (appears on hover)
        self._video_overlay = VideoControlsOverlay(self.image_label)
        self._video_overlay.set_viewer(self)

        self.history_list = QListWidget()
        self.history_list.setMaximumWidth(180)
        self.history_list.itemClicked.connect(self.on_history_clicked)
        self.history_list.hide()
        central_splitter.addWidget(self.history_list)
        central_splitter.setSizes([900, 100])

        self.status = QStatusBar()
        self.setStatusBar(self.status)
        # The size grip reserves a bottom-right corner that nudges permanent
        # widgets upward; disabling it lets them sit vertically centered.
        self.status.setSizeGripEnabled(False)

        self.path_label = QLabel()

        self.statusBar().addPermanentWidget(self.path_label)
        self.path_label.linkActivated.connect(self.open_in_explorer)

        # Small reset-panels button pinned to the right corner of the status
        # bar. Snaps the floating tool panels back to their default right-side
        # vertical stack (see _reset_panel_layout). Kept compact so it does not
        # increase the status bar height.
        self.panel_reset_btn = QToolButton()
        self.panel_reset_btn.setText("⟲")
        self.panel_reset_btn.setToolTip(
            "Reset tool panels to default layout (Ctrl+Shift+R)")
        self.panel_reset_btn.setFixedSize(16, 16)
        self.panel_reset_btn.setCursor(Qt.PointingHandCursor)
        self.panel_reset_btn.setStyleSheet(
            "QToolButton { border: none; padding: 0px; margin: 0px;"
            " color: #888; font-size: 12px; }"
            "QToolButton:hover { color: #fff; }"
        )
        self.panel_reset_btn.clicked.connect(self._reset_panel_layout)
        # Wrap in a container that vertically centers the button so its glyph
        # lines up with the status-bar text instead of hugging the top edge.
        _reset_wrap = QWidget()
        _reset_wrap_layout = QHBoxLayout(_reset_wrap)
        _reset_wrap_layout.setContentsMargins(0, 0, 6, 0)
        _reset_wrap_layout.setSpacing(0)
        _reset_wrap_layout.addWidget(self.panel_reset_btn, 0, Qt.AlignVCenter)
        self.statusBar().addPermanentWidget(_reset_wrap)

        # Background folder-scan state (see _begin_scan / _on_scan_finished).
        # Drops/opens are routed through a QThread worker so the GUI stays
        # responsive while walking huge folder trees.
        self._scan_thread = None
        self._scan_worker = None
        self._scan_in_progress = False
        self._scan_source = ""
        self._scan_wait_cursor_active = False

        self.update_image_info()
        self._update_title()
        
        # Add global shortcuts that bypass normal event handling
        self.setup_global_shortcuts()
        
        # Initialize toggle button states
        self._update_enhancement_menu_states()
        if hasattr(self, 'grayscale_toggle_btn'):
            self.grayscale_toggle_btn.setChecked(self.grayscale_value > 0)
        if hasattr(self, 'contrast_toggle_btn'):
            self.contrast_toggle_btn.setChecked(self.contrast_value != 50)
        if hasattr(self, 'gamma_toggle_btn'):
            self.gamma_toggle_btn.setChecked(self.gamma_value != 0)

        # Replace the classic top toolbars with HeavyPaint-style floating panels
        self._build_floating_panels()

    def setup_global_shortcuts(self):
        """Setup global shortcuts that work even when focus is elsewhere"""
        print("Setting up global shortcuts...")
        
        # Emergency exit fullscreen shortcuts
        self.escape_shortcut = QShortcut("Esc", self)
        self.escape_shortcut.activated.connect(self.emergency_exit_fullscreen)
        
        self.f11_shortcut = QShortcut("F11", self)
        self.f11_shortcut.activated.connect(self.emergency_toggle_fullscreen)
        
        self.ctrl_esc_shortcut = QShortcut("Ctrl+Esc", self)
        self.ctrl_esc_shortcut.activated.connect(self.force_exit_fullscreen)
        
        # Alt+F4 as ultimate emergency exit
        self.alt_f4_shortcut = QShortcut("Alt+F4", self)
        self.alt_f4_shortcut.activated.connect(self.emergency_close)
        
        # Essential application shortcuts
        self.ctrl_o_shortcut = QShortcut("Ctrl+O", self)
        self.ctrl_o_shortcut.activated.connect(self.choose_folder)
        
        self.ctrl_r_shortcut = QShortcut("Ctrl+R", self)
        self.ctrl_r_shortcut.activated.connect(self.reset_enhancements)
        
        # Undo line shortcut
        self.ctrl_z_shortcut = QShortcut("Ctrl+Z", self)
        self.ctrl_z_shortcut.activated.connect(self.undo_last_line)
        
        # Toggle antialiasing shortcut
        self.ctrl_shift_a_shortcut = QShortcut("Ctrl+Shift+A", self)
        self.ctrl_shift_a_shortcut.activated.connect(lambda: self.toggle_line_antialiasing(not self.line_antialiasing))
        
        # Reset tool panels to the default right-side layout (works in fullscreen)
        self.ctrl_shift_r_shortcut = QShortcut("Ctrl+Shift+R", self)
        self.ctrl_shift_r_shortcut.activated.connect(self._reset_panel_layout)
        
        # 🎨 PEN PRESSURE: Test shortcut
        self.ctrl_shift_p_shortcut = QShortcut("Ctrl+Shift+P", self)
        self.ctrl_shift_p_shortcut.activated.connect(self.test_pen_pressure)
        
        # Zoom shortcuts
        self.ctrl_plus_shortcut = QShortcut("Ctrl++", self)
        self.ctrl_plus_shortcut.activated.connect(self.zoom_in)
        
        self.ctrl_minus_shortcut = QShortcut("Ctrl+-", self)
        self.ctrl_minus_shortcut.activated.connect(self.zoom_out)
        
        self.ctrl_0_shortcut = QShortcut("Ctrl+0", self)
        self.ctrl_0_shortcut.activated.connect(self.reset_zoom)

        self.ctrl_g_shortcut = QShortcut("Ctrl+G", self)
        self.ctrl_g_shortcut.activated.connect(self._go_to_image_or_page)

        # Paste image / file path from clipboard
        self.ctrl_v_shortcut = QShortcut("Ctrl+V", self)
        self.ctrl_v_shortcut.activated.connect(self.paste_from_clipboard)

        # Video playback shortcuts
        self.space_shortcut = QShortcut("Space", self)
        self.space_shortcut.activated.connect(self._video_shortcut_play_pause)

        self.m_shortcut = QShortcut("M", self)
        self.m_shortcut.activated.connect(self._video_shortcut_mute)

        self.left_shortcut = QShortcut("Left", self)
        self.left_shortcut.activated.connect(lambda: self._video_shortcut_seek(-5000))

        self.right_shortcut = QShortcut("Right", self)
        self.right_shortcut.activated.connect(lambda: self._video_shortcut_seek(5000))

        # Delete current file (move to Recycle Bin)
        self.delete_shortcut = QShortcut("Delete", self)
        self.delete_shortcut.activated.connect(self.delete_current_file)

        print("Global shortcuts set up successfully")

    def emergency_exit_fullscreen(self):
        """Emergency exit from fullscreen or minimal mode"""
        print("EMERGENCY: Escape shortcut activated")
        if self.is_fullscreen:
            self.force_exit_fullscreen()
        elif not self._ui_chrome_visible():
            print("EMERGENCY: Restoring UI from minimal mode")
            self.toggle_toolbar_visibility(True)  # Show UI

    def emergency_toggle_fullscreen(self):
        """Emergency toggle fullscreen"""
        print("EMERGENCY: F11 shortcut activated")
        if self.is_fullscreen:
            self.force_exit_fullscreen()
        else:
            self.toggle_fullscreen(True)

    def emergency_close(self):
        """Emergency close application"""
        print("EMERGENCY: Alt+F4 activated - closing application")
        self.close()

    def _setup_main_toolbar(self):
        toolbar = self.main_toolbar

        def add_spacer(width, tb=None):
            tb = tb or toolbar
            s = QWidget(); s.setFixedWidth(width); tb.addWidget(s)

        def add_section_divider(tb=None):
            """Visual group separator: small left pad + 1px vertical line + right pad."""
            tb = tb or toolbar
            add_spacer(6, tb)
            line = QFrame(); line.setFrameShape(QFrame.VLine); line.setFrameShadow(QFrame.Plain)
            line.setStyleSheet("color: #4a4d50; background-color: #4a4d50;")
            line.setFixedHeight(20); line.setFixedWidth(1)
            tb.addWidget(line)
            add_spacer(6, tb)

        # Expose for use by other helpers (slider toolbar, etc.)
        self._add_section_divider = add_section_divider

        # ── SECTION: File ──
        self.open_btn = open_btn = QToolButton(); open_btn.setText("📁"); open_btn.setToolTip("Open Folder"); open_btn.setFixedSize(24,24); open_btn.clicked.connect(self.choose_folder); toolbar.addWidget(open_btn)
        add_spacer(2)
        self.delete_file_btn = QToolButton(); self.delete_file_btn.setText("🗑"); self.delete_file_btn.setToolTip("Move current file to Recycle Bin (Delete)"); self.delete_file_btn.setFixedSize(24,24); self.delete_file_btn.clicked.connect(self.delete_current_file); toolbar.addWidget(self.delete_file_btn)
        add_spacer(2)
        self.save_btn = save_btn = QToolButton(); save_btn.setText("💾"); save_btn.setToolTip("Save current view to Downloads (includes LUT, enhancements and lines)"); save_btn.setFixedSize(24,24); save_btn.clicked.connect(self.save_current_view); toolbar.addWidget(save_btn)
        add_section_divider()

        # ── SECTION: Draw Tools ──
        self.line_tool_btn = QToolButton(); self.line_tool_btn.setText("📏"); self.line_tool_btn.setToolTip("Draw Vertical Lines"); self.line_tool_btn.setCheckable(True); self.line_tool_btn.setFixedSize(24,24); self.line_tool_btn.toggled.connect(self.toggle_line_drawing); toolbar.addWidget(self.line_tool_btn)
        add_spacer(2)
        self.hline_tool_btn = QToolButton(); self.hline_tool_btn.setText("━"); self.hline_tool_btn.setToolTip("Draw Horizontal Lines"); self.hline_tool_btn.setCheckable(True); self.hline_tool_btn.setFixedSize(24,24); self.hline_tool_btn.toggled.connect(self.toggle_hline_drawing); toolbar.addWidget(self.hline_tool_btn)
        add_spacer(2)
        self.free_line_tool_btn = QToolButton(); self.free_line_tool_btn.setText("╱"); self.free_line_tool_btn.setToolTip("Draw Free Lines (2 clicks per line)"); self.free_line_tool_btn.setCheckable(True); self.free_line_tool_btn.setFixedSize(24,24); self.free_line_tool_btn.toggled.connect(self.toggle_free_line_drawing); toolbar.addWidget(self.free_line_tool_btn)
        add_spacer(2)
        self.free_draw_tool_btn = QToolButton(); self.free_draw_tool_btn.setText("✏"); self.free_draw_tool_btn.setToolTip("Free Draw Tool (drag to draw)"); self.free_draw_tool_btn.setCheckable(True); self.free_draw_tool_btn.setFixedSize(24,24); self.free_draw_tool_btn.toggled.connect(self.toggle_free_draw); toolbar.addWidget(self.free_draw_tool_btn)
        add_spacer(2)
        self.eraser_tool_btn = QToolButton(); self.eraser_tool_btn.setText("⌫"); self.eraser_tool_btn.setToolTip("Eraser (erase parts of lines/drawings, not the image)"); self.eraser_tool_btn.setCheckable(True); self.eraser_tool_btn.setFixedSize(24,24); self.eraser_tool_btn.toggled.connect(self.toggle_eraser); toolbar.addWidget(self.eraser_tool_btn)
        add_spacer(2)
        self.eraser_size_spin = QSpinBox(); self.eraser_size_spin.setRange(1,30); self.eraser_size_spin.setValue(self.eraser_size); self.eraser_size_spin.setSuffix("px"); self.eraser_size_spin.setFixedHeight(24); self.eraser_size_spin.setFixedWidth(50); self.eraser_size_spin.setToolTip("Eraser Size"); self.eraser_size_spin.valueChanged.connect(self.update_eraser_size); toolbar.addWidget(self.eraser_size_spin)
        add_spacer(2)
        self.undo_line_btn = QToolButton(); self.undo_line_btn.setText("↶"); self.undo_line_btn.setToolTip("Undo Last Line"); self.undo_line_btn.setFixedSize(24,24); self.undo_line_btn.clicked.connect(self.undo_last_line); toolbar.addWidget(self.undo_line_btn)
        add_section_divider()

        # ── SECTION: Fixed Overlays ──
        self.crosshair_tool_btn = QToolButton()
        self.crosshair_tool_btn.setIcon(self._create_overlay_icon('crosshair'))
        self.crosshair_tool_btn.setToolTip("Crosshair: draw cross lines at image center")
        self.crosshair_tool_btn.setCheckable(True)
        self.crosshair_tool_btn.setFixedSize(24, 24)
        self.crosshair_tool_btn.toggled.connect(self.toggle_crosshair_overlay)
        toolbar.addWidget(self.crosshair_tool_btn)
        add_spacer(2)
        self.grid_tool_btn = QToolButton()
        self.grid_tool_btn.setIcon(self._create_overlay_icon('grid'))
        self.grid_tool_btn.setToolTip("3×3 Grid: divide image into 9 equal parts")
        self.grid_tool_btn.setCheckable(True)
        self.grid_tool_btn.setFixedSize(24, 24)
        self.grid_tool_btn.toggled.connect(self.toggle_grid_overlay)
        toolbar.addWidget(self.grid_tool_btn)
        add_section_divider()

        # ── SECTION: Line Style ──
        self.line_thickness_spin = QSpinBox(); self.line_thickness_spin.setRange(1,10); self.line_thickness_spin.setValue(self.line_thickness); self.line_thickness_spin.setSuffix("px"); self.line_thickness_spin.setFixedHeight(24); self.line_thickness_spin.setFixedWidth(50); self.line_thickness_spin.setToolTip("Line Thickness"); self.line_thickness_spin.valueChanged.connect(self.update_line_thickness); toolbar.addWidget(self.line_thickness_spin)
        add_spacer(4)
        trans_label = QLabel("T:")
        trans_label.setFixedWidth(12)
        trans_label.setStyleSheet("font-size: 9px; margin-right: 2px;")
        trans_label.setToolTip("Line Transparency")
        toolbar.addWidget(trans_label)
        self.line_transparency_slider = QSlider(Qt.Horizontal)
        self.line_transparency_slider.setRange(0, 255)
        self.line_transparency_slider.setValue(self.line_transparency)
        self.line_transparency_slider.setFixedWidth(60)
        self.line_transparency_slider.setFixedHeight(24)
        self.line_transparency_slider.setToolTip("Line Transparency: 0=Transparent, 255=Opaque")
        self.line_transparency_slider.valueChanged.connect(self.update_line_transparency)
        toolbar.addWidget(self.line_transparency_slider)
        add_spacer(4)
        self.antialiasing_btn = QToolButton(); self.antialiasing_btn.setText("✨"); self.antialiasing_btn.setToolTip("Toggle Line Antialiasing (Smoother Lines)"); self.antialiasing_btn.setCheckable(True); self.antialiasing_btn.setChecked(self.line_antialiasing); self.antialiasing_btn.setFixedSize(24,24); self.antialiasing_btn.toggled.connect(self.toggle_line_antialiasing); toolbar.addWidget(self.antialiasing_btn)
        add_spacer(2)
        self.pen_pressure_btn = QToolButton(); self.pen_pressure_btn.setText("🎨"); self.pen_pressure_btn.setToolTip("Toggle Pen Pressure Sensitivity (varies line thickness)"); self.pen_pressure_btn.setCheckable(True); self.pen_pressure_btn.setChecked(True); self.pen_pressure_btn.setFixedSize(24,24); self.pen_pressure_btn.toggled.connect(self.toggle_pen_pressure); toolbar.addWidget(self.pen_pressure_btn)
        add_section_divider()

        # ── SECTION: Color ──
        self.line_color_btn = QToolButton(); self.line_color_btn.setText("🎨"); self.line_color_btn.setToolTip("Choose Line Color"); self.line_color_btn.setFixedSize(24,24); self.line_color_btn.clicked.connect(self.choose_line_color); self.line_color_btn.setStyleSheet(f"QToolButton {{ background-color: {self.line_color.name()}; border:1px solid #666; }}"); toolbar.addWidget(self.line_color_btn)
        self.quick_color_btns = []
        for color_hex, color_name, emoji in [("#ffffff","White","⚪"),("#000000","Black","⚫"),("#808080","Grey","⚪")]:
            btn=QToolButton(); btn.setText(emoji); btn.setToolTip(f"Set Line Color to {color_name}"); btn.setFixedSize(18,24); btn.clicked.connect(lambda checked, c=color_hex: self.set_line_color(c)); btn.setStyleSheet("QToolButton { border:1px solid #444; margin:1px; }"); toolbar.addWidget(btn); self.quick_color_btns.append(btn)
        add_spacer(2)
        # 💉 Color Snap (eyedropper) — sample a color from the image as line color
        self.color_snap_btn = QToolButton(); self.color_snap_btn.setText("💉"); self.color_snap_btn.setToolTip("Color Snap: hover to preview (after ~350ms), click to pick (saves to palette →)"); self.color_snap_btn.setCheckable(True); self.color_snap_btn.setFixedSize(24,24); self.color_snap_btn.toggled.connect(self.toggle_color_snap); toolbar.addWidget(self.color_snap_btn)
        # 🪄 Auto-extract palette from current image
        self.palette_extract_btn = QToolButton(); self.palette_extract_btn.setText("🪄"); self.palette_extract_btn.setToolTip("Auto-extract dominant colors from current image into palette"); self.palette_extract_btn.setFixedSize(24,24); self.palette_extract_btn.clicked.connect(self.extract_palette_from_image); toolbar.addWidget(self.palette_extract_btn)
        # 🧽 Clear palette
        self.palette_clear_btn = QToolButton(); self.palette_clear_btn.setText("🧽"); self.palette_clear_btn.setToolTip("Clear saved color palette"); self.palette_clear_btn.setFixedSize(24,24); self.palette_clear_btn.clicked.connect(self.clear_snapped_palette); toolbar.addWidget(self.palette_clear_btn)
        add_section_divider()

        # ── SECTION: Visibility ──
        self.clear_lines_btn = clear_lines_btn = QToolButton(); clear_lines_btn.setText("🗑"); clear_lines_btn.setToolTip("Clear All Lines"); clear_lines_btn.setFixedSize(24,24); clear_lines_btn.clicked.connect(self.clear_lines); toolbar.addWidget(clear_lines_btn)
        add_spacer(2)
        self.toggle_lines_btn = QToolButton(); self.toggle_lines_btn.setText("👁"); self.toggle_lines_btn.setToolTip("Toggle Line Visibility On/Off"); self.toggle_lines_btn.setCheckable(True); self.toggle_lines_btn.setChecked(True); self.toggle_lines_btn.setFixedSize(24,24); self.toggle_lines_btn.toggled.connect(self.toggle_line_visibility); toolbar.addWidget(self.toggle_lines_btn)
        add_spacer(2)
        self.toggle_image_btn = QToolButton(); self.toggle_image_btn.setText("🖼"); self.toggle_image_btn.setToolTip("Toggle Image Visibility (keep lines visible)"); self.toggle_image_btn.setCheckable(True); self.toggle_image_btn.setChecked(True); self.toggle_image_btn.setFixedSize(24,24); self.toggle_image_btn.toggled.connect(self.toggle_image_visibility); toolbar.addWidget(self.toggle_image_btn)
        add_section_divider()

        # ── SECTION: Effect Toggles ──
        self.grayscale_toggle_btn = QToolButton(); self.grayscale_toggle_btn.setText("🌑"); self.grayscale_toggle_btn.setToolTip("Toggle Grayscale On/Off"); self.grayscale_toggle_btn.setCheckable(True); self.grayscale_toggle_btn.setFixedSize(24,24); self.grayscale_toggle_btn.toggled.connect(self.toggle_grayscale); toolbar.addWidget(self.grayscale_toggle_btn)
        add_spacer(2)
        self.contrast_toggle_btn = QToolButton(); self.contrast_toggle_btn.setText("🔆"); self.contrast_toggle_btn.setToolTip("Toggle Enhanced Contrast On/Off"); self.contrast_toggle_btn.setCheckable(True); self.contrast_toggle_btn.setFixedSize(24,24); self.contrast_toggle_btn.toggled.connect(self.toggle_contrast); toolbar.addWidget(self.contrast_toggle_btn)
        add_spacer(2)
        self.gamma_toggle_btn = QToolButton(); self.gamma_toggle_btn.setText("💡"); self.gamma_toggle_btn.setToolTip("Toggle Enhanced Brightness On/Off"); self.gamma_toggle_btn.setCheckable(True); self.gamma_toggle_btn.setFixedSize(24,24); self.gamma_toggle_btn.toggled.connect(self.toggle_gamma); toolbar.addWidget(self.gamma_toggle_btn)
        add_spacer(2)
        self.lut_toggle_btn = QToolButton(); self.lut_toggle_btn.setText("🎞"); self.lut_toggle_btn.setToolTip("Toggle LUT On/Off (preserves selection)"); self.lut_toggle_btn.setCheckable(True); self.lut_toggle_btn.setFixedSize(24,24); self.lut_toggle_btn.toggled.connect(self.toggle_lut_enabled); self.lut_toggle_btn.setChecked(False); toolbar.addWidget(self.lut_toggle_btn)
        add_spacer(2)
        self.value_filter_toggle_btn = QToolButton(); self.value_filter_toggle_btn.setText("◑"); self.value_filter_toggle_btn.setToolTip("Toggle Value Filter (posterize to N grayscale tones)"); self.value_filter_toggle_btn.setCheckable(True); self.value_filter_toggle_btn.setChecked(self.value_filter_enabled); self.value_filter_toggle_btn.setFixedSize(24,24); self.value_filter_toggle_btn.toggled.connect(self.toggle_value_filter); toolbar.addWidget(self.value_filter_toggle_btn)
        self.value_levels_spin = QSpinBox(); self.value_levels_spin.setRange(2, 10); self.value_levels_spin.setValue(self.value_levels); self.value_levels_spin.setFixedHeight(24); self.value_levels_spin.setFixedWidth(40); self.value_levels_spin.setToolTip("Number of value levels (2-10)"); self.value_levels_spin.valueChanged.connect(self.update_value_levels); toolbar.addWidget(self.value_levels_spin)
        add_spacer(2)
        # Color Groups (palette quantization) toggle + colors slider + field-size slider
        self.color_groups_toggle_btn = QToolButton(); self.color_groups_toggle_btn.setText("🎨"); self.color_groups_toggle_btn.setToolTip("Toggle Color Groups (reduce image to N flat colors sampled from the image)"); self.color_groups_toggle_btn.setCheckable(True); self.color_groups_toggle_btn.setChecked(self.color_groups_enabled); self.color_groups_toggle_btn.setFixedSize(24,24); self.color_groups_toggle_btn.toggled.connect(self.toggle_color_groups); toolbar.addWidget(self.color_groups_toggle_btn)
        self.color_groups_count_spin = QSpinBox(); self.color_groups_count_spin.setRange(2, 32); self.color_groups_count_spin.setValue(self.color_groups_count); self.color_groups_count_spin.setFixedHeight(24); self.color_groups_count_spin.setFixedWidth(44); self.color_groups_count_spin.setToolTip("Color Groups: number of colors (2-32)"); self.color_groups_count_spin.valueChanged.connect(self.update_color_groups_count); toolbar.addWidget(self.color_groups_count_spin)
        self.color_groups_field_spin = QSpinBox(); self.color_groups_field_spin.setRange(0, 20); self.color_groups_field_spin.setValue(self.color_groups_field); self.color_groups_field_spin.setFixedHeight(24); self.color_groups_field_spin.setFixedWidth(44); self.color_groups_field_spin.setToolTip("Color Groups: field size (0=off, higher=larger merged color fields)"); self.color_groups_field_spin.valueChanged.connect(self.update_color_groups_field); toolbar.addWidget(self.color_groups_field_spin)
        add_spacer(2)
        # Object Groups (cryptomatte-style): toggle + look menu + detail/min-size
        self.object_groups_toggle_btn = QToolButton(); self.object_groups_toggle_btn.setText("🧩"); self.object_groups_toggle_btn.setToolTip("Toggle Object Groups (cryptomatte-style: split into objects, flatten each to its own local color)"); self.object_groups_toggle_btn.setCheckable(True); self.object_groups_toggle_btn.setChecked(self.object_groups_enabled); self.object_groups_toggle_btn.setFixedSize(24,24); self.object_groups_toggle_btn.toggled.connect(self.toggle_object_groups); toolbar.addWidget(self.object_groups_toggle_btn)
        from PySide6.QtWidgets import QMenu as _QMenuObj
        from PySide6.QtGui import QAction as _QActionObj
        self.object_groups_mode_btn = QToolButton(); self.object_groups_mode_btn.setText("◧"); self.object_groups_mode_btn.setToolTip("Object Groups look: local object colors / cryptomatte ID colors / colors + outlines"); self.object_groups_mode_btn.setFixedSize(24,24); self.object_groups_mode_btn.setPopupMode(QToolButton.InstantPopup)
        obj_menu = _QMenuObj(self.object_groups_mode_btn)
        self._object_groups_mode_actions = {}
        for mode_key, label in (("local", "Local object colors"),
                                ("local_edges", "Local colors + outlines"),
                                ("id", "Cryptomatte ID colors")):
            act = _QActionObj(label, self)
            act.setCheckable(True)
            act.setChecked(self.object_groups_mode == mode_key)
            act.triggered.connect(lambda _checked=False, m=mode_key: self.set_object_groups_mode(m))
            obj_menu.addAction(act)
            self._object_groups_mode_actions[mode_key] = act
        self.object_groups_mode_btn.setMenu(obj_menu)
        toolbar.addWidget(self.object_groups_mode_btn)
        self.object_groups_detail_spin = QSpinBox(); self.object_groups_detail_spin.setRange(0, 100); self.object_groups_detail_spin.setValue(self.object_groups_detail); self.object_groups_detail_spin.setFixedHeight(24); self.object_groups_detail_spin.setFixedWidth(44); self.object_groups_detail_spin.setToolTip("Object Groups: detail (0-100, higher = more separate objects)"); self.object_groups_detail_spin.valueChanged.connect(self.update_object_groups_detail); toolbar.addWidget(self.object_groups_detail_spin)
        self.object_groups_min_spin = QSpinBox(); self.object_groups_min_spin.setRange(0, 100); self.object_groups_min_spin.setValue(self.object_groups_min_size); self.object_groups_min_spin.setFixedHeight(24); self.object_groups_min_spin.setFixedWidth(44); self.object_groups_min_spin.setToolTip("Object Groups: minimum object size (0 = keep specks, higher = merge small regions into bigger objects)"); self.object_groups_min_spin.valueChanged.connect(self.update_object_groups_min_size); toolbar.addWidget(self.object_groups_min_spin)
        add_spacer(2)
        self.edge_toggle_btn = QToolButton(); self.edge_toggle_btn.setText("📐"); self.edge_toggle_btn.setToolTip("Toggle Edge Detection (plane changes)"); self.edge_toggle_btn.setCheckable(True); self.edge_toggle_btn.setChecked(self.edge_detection_enabled); self.edge_toggle_btn.setFixedSize(24,24); self.edge_toggle_btn.toggled.connect(self.toggle_edge_detection); toolbar.addWidget(self.edge_toggle_btn)
        from PySide6.QtWidgets import QMenu as _QMenuEdge
        from PySide6.QtGui import QAction as _QActionEdge
        self.edge_mode_btn = QToolButton(); self.edge_mode_btn.setText("▦"); self.edge_mode_btn.setToolTip("Edge look: white-on-black / black-on-white / overlay on image"); self.edge_mode_btn.setFixedSize(24,24); self.edge_mode_btn.setPopupMode(QToolButton.InstantPopup)
        edge_menu = _QMenuEdge(self.edge_mode_btn)
        self._edge_mode_actions = {}
        for mode_key, label in (("white_on_black", "Edges on dark background"),
                                ("black_on_white", "Edges on white background"),
                                ("overlay", "Edges over image")):
            act = _QActionEdge(label, self)
            act.setCheckable(True)
            act.setChecked(self.edge_mode == mode_key)
            act.triggered.connect(lambda _checked=False, m=mode_key: self.set_edge_mode(m))
            edge_menu.addAction(act)
            self._edge_mode_actions[mode_key] = act
        self.edge_mode_btn.setMenu(edge_menu)
        toolbar.addWidget(self.edge_mode_btn)
        self.edge_sensitivity_spin = QSpinBox(); self.edge_sensitivity_spin.setRange(0, 100); self.edge_sensitivity_spin.setValue(self.edge_sensitivity); self.edge_sensitivity_spin.setFixedHeight(24); self.edge_sensitivity_spin.setFixedWidth(44); self.edge_sensitivity_spin.setToolTip("Edge sensitivity (0-100)"); self.edge_sensitivity_spin.valueChanged.connect(self.update_edge_sensitivity); toolbar.addWidget(self.edge_sensitivity_spin)
        add_spacer(2)
        # Curves (classical RGB levels): opens a dedicated floating panel
        self.curves_btn = QToolButton(); self.curves_btn.setText("📈"); self.curves_btn.setToolTip("Curves (RGB levels): open panel with Black/White/Midtone per channel"); self.curves_btn.setCheckable(True); self.curves_btn.setFixedSize(24,24); self.curves_btn.toggled.connect(self._toggle_curves_window); toolbar.addWidget(self.curves_btn)
        add_section_divider()

        # ── SECTION: Navigation & Timer ──
        self.prev_btn = prev_btn = QToolButton(); prev_btn.setText("⬅"); prev_btn.setToolTip("Previous Image (Go Back in History)"); prev_btn.setFixedSize(24,24); prev_btn.clicked.connect(self.show_previous_image); toolbar.addWidget(prev_btn)
        add_spacer(2)
        self.next_btn = next_btn = QToolButton(); next_btn.setText("🎲"); next_btn.setToolTip("Show Next Image"); next_btn.setFixedSize(24,24); next_btn.clicked.connect(self._manual_next_image); toolbar.addWidget(next_btn)
        add_spacer(2)
        self.sort_order_button = QToolButton(); self.sort_order_button.setCheckable(True); self.sort_order_button.setChecked(True); self.sort_order_button.setText("🔀"); self.sort_order_button.setToolTip("Toggle Random/Alphabetical Order"); self.sort_order_button.setFixedSize(24,24); self.sort_order_button.toggled.connect(self.toggle_sort_order); toolbar.addWidget(self.sort_order_button)
        add_spacer(2)
        # File Types palette: per-extension checkboxes that filter what you browse
        self.type_filter_btn = QToolButton(); self.type_filter_btn.setText("🗂"); self.type_filter_btn.setToolTip("File Types: show only the file types you check (e.g. only MP4 or GIF)"); self.type_filter_btn.setCheckable(True); self.type_filter_btn.setFixedSize(24,24); self.type_filter_btn.toggled.connect(self._toggle_type_filter_window); toolbar.addWidget(self.type_filter_btn)
        add_spacer(2)
        self.timer_button = QToolButton(); self.timer_button.setCheckable(True); self.timer_button.setText("⚡"); self.timer_button.setToolTip("Toggle Auto Advance"); self.timer_button.setFixedSize(24,24); self.timer_button.toggled.connect(self.toggle_timer); toolbar.addWidget(self.timer_button)
        add_spacer(2)
        self.timer_spin = QSpinBox(); self.timer_spin.setRange(1,3600); self.timer_spin.setValue(self.timer_interval); self.timer_spin.setSuffix(" s"); self.timer_spin.setFixedHeight(24); self.timer_spin.setFixedWidth(44); self.timer_spin.valueChanged.connect(self.update_timer_interval); toolbar.addWidget(self.timer_spin)
        add_spacer(2)
        self.circle_timer = CircularCountdown(self.timer_spin.value()); self.circle_timer.set_parent_viewer(self); toolbar.addWidget(self.circle_timer)
        add_spacer(2)
        # Autoplay next: when a video / animated GIF ends, move to the next item
        self.autoplay_next_btn = QToolButton(); self.autoplay_next_btn.setText("⏭"); self.autoplay_next_btn.setToolTip("Autoplay next: when a video or animated GIF finishes, advance to the next item"); self.autoplay_next_btn.setCheckable(True); self.autoplay_next_btn.setChecked(self.autoplay_next_enabled); self.autoplay_next_btn.setFixedSize(24,24); self.autoplay_next_btn.toggled.connect(self.toggle_autoplay_next); toolbar.addWidget(self.autoplay_next_btn)
        add_section_divider()

        # ── PDF page navigation (hidden until a PDF is loaded) ──
        self._pdf_nav_widget = QWidget()
        pdf_nav_layout = QHBoxLayout(self._pdf_nav_widget)
        pdf_nav_layout.setContentsMargins(0, 0, 0, 0)
        pdf_nav_layout.setSpacing(4)

        pdf_label = QLabel("Page")
        pdf_label.setFixedHeight(24)
        pdf_label.setStyleSheet("font-size: 11px;")
        pdf_nav_layout.addWidget(pdf_label)

        self._pdf_page_spin = QSpinBox()
        self._pdf_page_spin.setRange(1, 1)
        self._pdf_page_spin.setFixedHeight(24)
        self._pdf_page_spin.setFixedWidth(70)
        self._pdf_page_spin.setToolTip("Jump to PDF page")
        self._pdf_page_spin.setKeyboardTracking(False)  # only fire on Enter / arrows
        self._pdf_page_spin.valueChanged.connect(self._on_pdf_page_spin_changed)
        pdf_nav_layout.addWidget(self._pdf_page_spin)

        self._pdf_total_label = QLabel("/ ?")
        self._pdf_total_label.setFixedHeight(24)
        self._pdf_total_label.setStyleSheet("font-size: 11px;")
        pdf_nav_layout.addWidget(self._pdf_total_label)

        # Range label, e.g. "  [4-5]" — visible only in 2/3-page spread mode
        self._pdf_range_label = QLabel("")
        self._pdf_range_label.setFixedHeight(24)
        self._pdf_range_label.setStyleSheet(
            "font-size: 11px; color: #aaaaaa;")
        self._pdf_range_label.hide()
        pdf_nav_layout.addWidget(self._pdf_range_label)

        # 📖 Spread mode dropdown: Single / 2-Page / 3-Page
        from PySide6.QtWidgets import QMenu
        from PySide6.QtGui import QAction
        self._pdf_spread_btn = QToolButton()
        self._pdf_spread_btn.setText("📖")
        self._pdf_spread_btn.setToolTip(
            "View: single page, 2-page or 3-page book spread")
        self._pdf_spread_btn.setFixedSize(24, 24)
        self._pdf_spread_btn.setPopupMode(QToolButton.InstantPopup)
        spread_menu = QMenu(self._pdf_spread_btn)
        self._pdf_spread_actions = {}
        for mode_key, label in (("single", "\U0001F4C4  Single Page"),
                                ("2page", "\U0001F4D6  2-Page Spread"),
                                ("3page", "\U0001F4DA  3-Page Spread")):
            act = QAction(label, self)
            act.setCheckable(True)
            act.setChecked(self._pdf_spread_mode == mode_key)
            act.triggered.connect(
                lambda _checked=False, m=mode_key: self.set_pdf_spread_mode(m))
            spread_menu.addAction(act)
            self._pdf_spread_actions[mode_key] = act
        self._pdf_spread_btn.setMenu(spread_menu)
        pdf_nav_layout.addWidget(self._pdf_spread_btn)

        self._pdf_nav_widget.hide()
        toolbar.addWidget(self._pdf_nav_widget)

        # ── Video playback controls (hidden until a video is loaded) ──
        self._video_controls_widget = QWidget()
        vc_layout = QHBoxLayout(self._video_controls_widget)
        vc_layout.setContentsMargins(0, 0, 0, 0)
        vc_layout.setSpacing(4)

        self._video_play_btn = QToolButton()
        self._video_play_btn.setText("▶")
        self._video_play_btn.setToolTip("Play / Pause")
        self._video_play_btn.setFixedSize(24, 24)
        self._video_play_btn.clicked.connect(lambda: self.image_label.video_toggle_play_pause())
        vc_layout.addWidget(self._video_play_btn)

        self._video_time_label = QLabel("0:00")
        self._video_time_label.setFixedHeight(24)
        self._video_time_label.setStyleSheet("font-size: 11px; min-width: 36px;")
        vc_layout.addWidget(self._video_time_label)

        self._video_seek_slider = QSlider(Qt.Horizontal)
        self._video_seek_slider.setRange(0, 0)
        self._video_seek_slider.setFixedHeight(20)
        self._video_seek_slider.setMinimumWidth(100)
        self._video_seek_slider.sliderMoved.connect(self._on_video_seek_slider_moved)
        vc_layout.addWidget(self._video_seek_slider)

        self._video_total_label = QLabel("0:00")
        self._video_total_label.setFixedHeight(24)
        self._video_total_label.setStyleSheet("font-size: 11px; min-width: 36px;")
        vc_layout.addWidget(self._video_total_label)

        self._video_mute_btn = QToolButton()
        self._video_mute_btn.setText("🔇" if self._video_muted else "🔊")
        self._video_mute_btn.setToolTip("Unmute" if self._video_muted else "Mute")
        self._video_mute_btn.setFixedSize(24, 24)
        self._video_mute_btn.clicked.connect(self._toggle_video_mute)
        vc_layout.addWidget(self._video_mute_btn)

        self._video_volume_slider = QSlider(Qt.Horizontal)
        self._video_volume_slider.setRange(0, 100)
        self._video_volume_slider.setValue(self._video_volume)
        self._video_volume_slider.setFixedHeight(20)
        self._video_volume_slider.setFixedWidth(60)
        self._video_volume_slider.setToolTip("Volume")
        self._video_volume_slider.valueChanged.connect(self._on_video_volume_changed)
        vc_layout.addWidget(self._video_volume_slider)

        # Dub audio toggle: use a sibling audio file as the soundtrack
        self._dub_audio_btn = QToolButton()
        self._dub_audio_btn.setText("\U0001f3b5")
        self._dub_audio_btn.setToolTip(
            "Dub audio: play a sibling audio file with the same name "
            "(clip.mp4 \u2192 clip.mp3) instead of the video's own track")
        self._dub_audio_btn.setCheckable(True)
        self._dub_audio_btn.setChecked(self.dub_audio_enabled)
        self._dub_audio_btn.setFixedSize(24, 24)
        self._dub_audio_btn.toggled.connect(self.toggle_dub_audio)
        vc_layout.addWidget(self._dub_audio_btn)

        self._video_controls_widget.hide()
        toolbar.addWidget(self._video_controls_widget)

    def _setup_enhancement_controls(self):
        """Setup the enhancement controls - put them on main toolbar initially"""
        # Add a separator before enhancement controls for easy identification
        self.enhancement_separator = self.main_toolbar.addSeparator()
        
        # Create enhancement controls on the main toolbar initially
        self._create_enhancement_widgets_on_toolbar(self.main_toolbar)

        # Divider between enhancements/LUT and transform/view actions
        self._add_section_divider_to(self.main_toolbar)

        # Add action buttons to the main toolbar initially
        self._add_action_buttons_to_toolbar(self.main_toolbar)

        # Divider before History checkbox
        self._add_section_divider_to(self.main_toolbar)

        # History checkbox on main toolbar
        self.show_history_checkbox = QCheckBox("History")
        self.show_history_checkbox.setChecked(False)
        self.show_history_checkbox.setFixedHeight(24)
        self.show_history_checkbox.stateChanged.connect(self.toggle_history_panel)
        self.main_toolbar.addWidget(self.show_history_checkbox)

        # Add stretch to push everything to the left
        spacer_stretch = QWidget()
        spacer_stretch.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.main_toolbar.addWidget(spacer_stretch)

    def _build_floating_panels(self):
        """Replace the top toolbars with HeavyPaint-style floating panels.

        Existing tool widgets (already created and wired to their slots) are
        reparented into five draggable, semi-transparent panels overlaid on the
        image canvas. The original toolbars are hidden but kept alive because
        other code references ``self.main_toolbar``.
        """
        self._using_floating_panels = True

        # PDF / video context controls stay on a thin toolbar shown on demand.
        # Detach them from the main toolbar so hiding it does not hide them.
        # (They are re-shown via their own show()/hide() call sites.)

        # Group tool widgets into panels by attribute name. Only widgets that
        # exist are added, so this is resilient to future changes.
        groups = [
            ("FILE / NAV", [
                'open_btn', 'delete_file_btn', 'save_btn',
                'prev_btn', 'next_btn', 'sort_order_button', 'type_filter_btn',
                'timer_button', 'timer_spin', 'circle_timer', 'autoplay_next_btn',
            ]),
            ("DRAW", [
                'line_tool_btn', 'hline_tool_btn', 'free_line_tool_btn',
                'free_draw_tool_btn', 'eraser_tool_btn', 'eraser_size_spin',
                'undo_line_btn', 'crosshair_tool_btn', 'grid_tool_btn',
                'clear_lines_btn', 'toggle_lines_btn', 'toggle_image_btn',
            ]),
            ("LINE / COLOR", [
                'line_thickness_spin', 'line_transparency_slider',
                'antialiasing_btn', 'pen_pressure_btn', 'line_color_btn',
                '__quick_colors__', 'color_snap_btn', 'palette_extract_btn',
                'palette_clear_btn',
            ]),
            ("EFFECTS", [
                'grayscale_toggle_btn', 'contrast_toggle_btn', 'gamma_toggle_btn',
                'lut_toggle_btn', 'value_filter_toggle_btn', 'value_levels_spin',
                'color_groups_toggle_btn', 'color_groups_count_spin',
                'color_groups_field_spin',
                'object_groups_toggle_btn', 'object_groups_mode_btn',
                'object_groups_detail_spin', 'object_groups_min_spin',
                'edge_toggle_btn', 'edge_mode_btn',
                'edge_sensitivity_spin',
                'curves_btn',
                'grayscale_slider', 'contrast_slider', 'gamma_slider',
                'lut_btn', 'lut_combo', 'lut_strength_slider', 'enh_reset_btn',
            ]),
            ("TRANSFORM / VIEW", [
                'rotate_btn', 'flip_h_btn', 'flip_v_btn', 'reset_zoom_btn',
                'copy_btn', 'fullscreen_btn', 'always_on_top_btn',
                'show_history_checkbox',
            ]),
        ]

        self._floating_panels = []
        self._panel_by_key = {}
        for title, attrs in groups:
            panel = FloatingPanel(title, self.image_label)
            panel._persist_key = title
            for attr in attrs:
                if attr == '__quick_colors__':
                    for btn in getattr(self, 'quick_color_btns', []):
                        panel.add_tool(btn)
                    continue
                w = getattr(self, attr, None)
                if w is not None:
                    panel.add_tool(w)
            panel.finalize()
            panel.moved_by_user.connect(self._on_panel_moved)
            panel.changed.connect(self._save_panel_layout)
            self._floating_panels.append(panel)
            self._panel_by_key[title] = panel

        # Context panel (PDF page nav / video controls) — hidden until a PDF or
        # video is loaded. The child widgets keep their existing show()/hide()
        # call sites; an event filter refreshes this panel when they change.
        self._context_panel = FloatingPanel("PDF / VIDEO", self.image_label)
        self._context_panel._persist_key = "PDF / VIDEO"
        for attr in ('_pdf_nav_widget', '_video_controls_widget'):
            w = getattr(self, attr, None)
            if w is not None:
                self._context_panel.add_tool(w, show=False)
                w.installEventFilter(self)
        self._context_panel.hide()
        self._floating_panels.append(self._context_panel)

        # Hide the now-empty toolbars (kept alive; referenced elsewhere).
        self.main_toolbar.hide()
        if hasattr(self, 'slider_toolbar'):
            self.slider_toolbar.hide()

        # Reposition panels whenever the canvas resizes.
        self.image_label.installEventFilter(self)

        self._arrange_floating_panels()
        # Defer restoring saved layout until the canvas has its real size
        # (the window is not shown yet here, so image_label is still tiny —
        # restoring now would clamp moved panels into a small area and overlap
        # them). The first real resize triggers _restore_panel_layout() once.
        self._panel_layout_restored = False

    def _refresh_context_panel(self):
        """Show/size the context panel when a PDF/video control becomes visible."""
        panel = getattr(self, '_context_panel', None)
        if panel is None:
            return
        pdf_w = getattr(self, '_pdf_nav_widget', None)
        vid_w = getattr(self, '_video_controls_widget', None)
        visible = bool((pdf_w is not None and pdf_w.isVisible()) or
                       (vid_w is not None and vid_w.isVisible()))
        if visible:
            panel.relayout()
            panel.show()
            panel.raise_()
        else:
            panel.hide()
        self._arrange_floating_panels()

    def _on_panel_moved(self):
        """A panel was dragged by the user — keep all panels on-screen."""
        for p in getattr(self, '_floating_panels', []):
            p.clamp_into_parent()

    def _save_panel_layout(self):
        """Persist per-panel width / collapsed / position across restarts."""
        settings = getattr(self, '_settings', None)
        if settings is None:
            return
        try:
            import json
            data = {}
            for key, panel in getattr(self, '_panel_by_key', {}).items():
                data[key] = panel.state()
            ctx = getattr(self, '_context_panel', None)
            if ctx is not None:
                data[ctx._persist_key] = ctx.state()
            settings.setValue("floating_panels_layout", json.dumps(data))
        except Exception as e:
            print(f"Could not save panel layout: {e}")

    def _restore_panel_layout(self):
        """Restore per-panel layout saved by :meth:`_save_panel_layout`."""
        settings = getattr(self, '_settings', None)
        if settings is None:
            return
        raw = settings.value("floating_panels_layout", "")
        if not raw:
            return
        try:
            import json
            data = json.loads(raw)
        except Exception:
            return
        for key, panel in getattr(self, '_panel_by_key', {}).items():
            st = data.get(key)
            if st:
                panel.apply_state(st)
        ctx = getattr(self, '_context_panel', None)
        if ctx is not None and data.get(ctx._persist_key):
            # Only restore width/collapsed for context panel; visibility is
            # driven by whether a PDF/video is active.
            st = dict(data[ctx._persist_key])
            st['moved'] = False
            ctx.apply_state(st)
        # Re-pack any panels the user never moved so restored widths tile neatly.
        self._arrange_floating_panels()

    def _arrange_floating_panels(self):
        """Pack panels left-to-right along the top edge, wrapping as needed.

        Panels the user has dragged keep their position (only clamped on-screen).
        """
        panels = getattr(self, '_floating_panels', None)
        if not panels:
            return
        canvas = self.image_label
        avail_w = max(1, canvas.width())
        margin = 8
        gap = 8
        x = margin
        y = margin
        row_h = 0
        for p in panels:
            if p.isHidden():
                continue
            pw = p.width()
            ph = p.height()
            if p.user_moved:
                # Re-dock to the panel's corner anchor so right/bottom-docked
                # panels keep their relative position when the canvas resizes
                # (maximize / restore / fullscreen).
                p.reposition_to_anchor()
                p.raise_()
                continue
            if x > margin and x + pw > avail_w - margin:
                # wrap to next row
                x = margin
                y += row_h + gap
                row_h = 0
            p.move(x, y)
            p.raise_()
            x += pw + gap
            row_h = max(row_h, ph)

    def _reset_panel_layout(self):
        """Snap all tool panels into the default right-side vertical stack.

        Reproduces the screenshot arrangement: panels collapsed and stacked
        bottom-to-top against the bottom-right corner of the canvas (FILE / NAV
        on top, DRAW at the bottom). Each panel is corner-anchored so it stays
        docked when the window is resized, and the layout is persisted.
        """
        by_key = getattr(self, '_panel_by_key', None)
        if not by_key:
            return
        canvas = self.image_label
        margin = 8
        gap = 8
        # Top-to-bottom order matching the default right-hand stack.
        order = ["FILE / NAV", "TRANSFORM / VIEW", "LINE / COLOR", "EFFECTS", "DRAW"]
        # Collapse panels first so heights reflect the default (closed) look.
        panels = []
        for key in order:
            panel = by_key.get(key)
            if panel is None or panel.isHidden():
                continue
            if not panel.collapsed:
                panel.set_collapsed(True, emit=False)
            panels.append(panel)
        # Stack bottom-to-top against the bottom-right corner.
        y = canvas.height() - margin
        for panel in reversed(panels):
            y -= panel.height()
            x = max(margin, canvas.width() - margin - panel.width())
            panel.move(int(x), int(y))
            panel.mark_user_moved(True)
            panel.update_anchor()
            panel.raise_()
            y -= gap
        self._save_panel_layout()

    def _update_toolbar_layout(self, width):
        """Move sliders between main toolbar and second row based on window width"""
        # Floating-panel UI replaces the two-row responsive toolbar entirely.
        if getattr(self, '_using_floating_panels', False):
            return
        should_use_two_rows = width < self.width_threshold
        
        # Only switch if the mode actually needs to change
        if should_use_two_rows == self.two_row_mode:
            return
        
        if should_use_two_rows and not self.two_row_mode:
            print(f"Switching to two-row mode at width {width}")
            
            # Store current slider values and history checkbox state
            gray_val = getattr(self, 'grayscale_slider', None)
            gray_val = gray_val.value() if gray_val else self.grayscale_value
            contrast_val = getattr(self, 'contrast_slider', None)
            contrast_val = contrast_val.value() if contrast_val else self.contrast_value
            gamma_val = getattr(self, 'gamma_slider', None)
            gamma_val = gamma_val.value() if gamma_val else self.gamma_value
            history_checked = getattr(self, 'show_history_checkbox', None)
            history_checked = history_checked.isChecked() if history_checked else False
            
            # Store LUT settings before recreating controls
            lut_strength_val = getattr(self, 'lut_strength_slider', None)
            lut_strength_val = lut_strength_val.value() if lut_strength_val else self.lut_strength
            current_lut_selection = getattr(self, 'lut_combo', None)
            current_lut_selection = current_lut_selection.currentText() if current_lut_selection else "None"
            
            # Find and remove enhancement widgets AND action buttons from main toolbar
            actions_to_remove = []
            found_separator = False
            
            for action in self.main_toolbar.actions():
                if hasattr(self, 'enhancement_separator') and action == self.enhancement_separator:
                    found_separator = True
                    actions_to_remove.append(action)
                elif found_separator:
                    actions_to_remove.append(action)
            
            # Remove the actions
            for action in actions_to_remove:
                self.main_toolbar.removeAction(action)
            
            # Clear and setup slider toolbar with both sliders and action buttons
            self.slider_toolbar.clear()
            self._create_enhancement_widgets_on_toolbar(self.slider_toolbar)
            
            # Section divider between LUT/reset and transform/view groups
            self._add_section_divider_to(self.slider_toolbar)
            
            # Add action buttons to second toolbar
            self._add_action_buttons_to_toolbar(self.slider_toolbar)
            
            # Restore slider values
            if hasattr(self, 'grayscale_slider'):
                self.grayscale_slider.setValue(gray_val)
                self.contrast_slider.setValue(contrast_val)
                self.gamma_slider.setValue(gamma_val)
            if hasattr(self, 'lut_strength_slider'):
                self.lut_strength_slider.setValue(lut_strength_val)
            
            # Restore LUT selection after recreating combo box
            if hasattr(self, 'lut_combo'):
                # Always populate the combo box if we have a folder selected
                if self.lut_folder and self.lut_files:
                    self.update_lut_combo()
                    # Then restore the selection if there was one
                    if current_lut_selection != "None":
                        index = self.lut_combo.findText(current_lut_selection)
                        if index >= 0:
                            self.lut_combo.setCurrentIndex(index)
            
            # Add History checkbox to slider toolbar
            self._add_section_divider_to(self.slider_toolbar)
            
            self.show_history_checkbox = QCheckBox("History")
            self.show_history_checkbox.setChecked(history_checked)
            self.show_history_checkbox.setFixedHeight(24)
            self.show_history_checkbox.stateChanged.connect(self.toggle_history_panel)
            self.slider_toolbar.addWidget(self.show_history_checkbox)
            
            # Show second toolbar and update mode
            self.slider_toolbar.show()
            self.two_row_mode = True
            print("DEBUG: Second toolbar should now be visible")
            print(f"DEBUG: Slider toolbar visible: {self.slider_toolbar.isVisible()}")
            print(f"DEBUG: Slider toolbar widget count: {len([self.slider_toolbar.widgetForAction(a) for a in self.slider_toolbar.actions() if self.slider_toolbar.widgetForAction(a)])}")
            
            # Force update the UI
            self.slider_toolbar.update()
            self.repaint()
            
        elif not should_use_two_rows and self.two_row_mode:
            print(f"Switching to single-row mode at width {width}")
            
            # Store current values
            gray_val = getattr(self, 'grayscale_slider', None)
            gray_val = gray_val.value() if gray_val else self.grayscale_value
            contrast_val = getattr(self, 'contrast_slider', None)
            contrast_val = contrast_val.value() if contrast_val else self.contrast_value
            gamma_val = getattr(self, 'gamma_slider', None)
            gamma_val = gamma_val.value() if gamma_val else self.gamma_value
            history_checked = getattr(self, 'show_history_checkbox', None)
            history_checked = history_checked.isChecked() if history_checked else False
            
            # Store LUT settings before recreating controls
            lut_strength_val = getattr(self, 'lut_strength_slider', None)
            lut_strength_val = lut_strength_val.value() if lut_strength_val else self.lut_strength
            current_lut_selection = getattr(self, 'lut_combo', None)
            current_lut_selection = current_lut_selection.currentText() if current_lut_selection else "None"
            
            # Clear and hide slider toolbar
            self.slider_toolbar.clear()
            self.slider_toolbar.hide()
            
            # Re-add enhancement separator to main toolbar
            self.enhancement_separator = self.main_toolbar.addSeparator()
            
            # Add enhancement controls back to main toolbar
            self._create_enhancement_widgets_on_toolbar(self.main_toolbar)
            
            # Divider between enhancements/LUT and transform/view actions
            self._add_section_divider_to(self.main_toolbar)
            
            # Add action buttons back to main toolbar
            self._add_action_buttons_to_toolbar(self.main_toolbar)
            
            # Restore values
            if hasattr(self, 'grayscale_slider'):
                self.grayscale_slider.setValue(gray_val)
                self.contrast_slider.setValue(contrast_val)
                self.gamma_slider.setValue(gamma_val)
            
            # Restore LUT settings
            if hasattr(self, 'lut_strength_slider'):
                self.lut_strength_slider.setValue(lut_strength_val)
            
            # Restore LUT folder and selection
            if hasattr(self, 'lut_combo') and self.lut_folder:
                self.update_lut_combo()  # Repopulate combo box with current folder
                # Find and restore the previous selection
                combo_index = self.lut_combo.findText(current_lut_selection)
                if combo_index >= 0:
                    self.lut_combo.setCurrentIndex(combo_index)
                    # Ensure LUT is applied if one was selected
                    if current_lut_selection != "None":
                        self.apply_selected_lut(current_lut_selection)
            
            # Add History checkbox back to main toolbar
            self._add_section_divider_to(self.main_toolbar)
            self.show_history_checkbox = QCheckBox("History")
            self.show_history_checkbox.setChecked(history_checked)
            self.show_history_checkbox.setFixedHeight(24)
            self.show_history_checkbox.stateChanged.connect(self.toggle_history_panel)
            self.main_toolbar.addWidget(self.show_history_checkbox)
            
            # Add stretch to main toolbar
            spacer_stretch = QWidget()
            spacer_stretch.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
            self.main_toolbar.addWidget(spacer_stretch)
            
            self.two_row_mode = False
            print("DEBUG: Returned to single-row mode")

    def _add_section_divider_to(self, toolbar):
        """Add a section-divider QFrame to an arbitrary toolbar (used by second row)."""
        s1 = QWidget(); s1.setFixedWidth(6); toolbar.addWidget(s1)
        line = QFrame(); line.setFrameShape(QFrame.VLine); line.setFrameShadow(QFrame.Plain)
        line.setStyleSheet("color: #4a4d50; background-color: #4a4d50;")
        line.setFixedHeight(20); line.setFixedWidth(1)
        toolbar.addWidget(line)
        s2 = QWidget(); s2.setFixedWidth(6); toolbar.addWidget(s2)

    def _add_action_buttons_to_toolbar(self, toolbar):
        """Add action buttons (transforms, view, etc.) to the specified toolbar.

        Grouped (left → right):
          Transforms  ‖  View (zoom/copy/fullscreen/pin)
        """
        # ── SECTION: Transforms ──
        self.rotate_btn = QToolButton()
        self.rotate_btn.setText("↻")
        self.rotate_btn.setToolTip("Rotate Image 90° CW")
        self.rotate_btn.setFixedSize(24, 24)
        self.rotate_btn.clicked.connect(self.rotate_image_90)
        toolbar.addWidget(self.rotate_btn)

        spacer = QWidget(); spacer.setFixedWidth(2); toolbar.addWidget(spacer)

        self.flip_h_btn = QToolButton()
        self.flip_h_btn.setText("⟷")
        self.flip_h_btn.setToolTip("Flip Image Horizontally")
        self.flip_h_btn.setCheckable(True)
        self.flip_h_btn.setFixedSize(24, 24)
        self.flip_h_btn.clicked.connect(self.flip_horizontal)
        toolbar.addWidget(self.flip_h_btn)

        spacer = QWidget(); spacer.setFixedWidth(2); toolbar.addWidget(spacer)

        self.flip_v_btn = QToolButton()
        self.flip_v_btn.setText("↕")
        self.flip_v_btn.setToolTip("Flip Image Vertically")
        self.flip_v_btn.setCheckable(True)
        self.flip_v_btn.setFixedSize(24, 24)
        self.flip_v_btn.clicked.connect(self.flip_vertical)
        toolbar.addWidget(self.flip_v_btn)

        self._add_section_divider_to(toolbar)

        # ── SECTION: View ──
        # Reset Zoom — distinct text label ("1:1") so it can't be confused with rotate ↻
        self.reset_zoom_btn = QToolButton()
        self.reset_zoom_btn.setText("1:1")
        self.reset_zoom_btn.setToolTip("Reset Zoom to 100%")
        self.reset_zoom_btn.setFixedSize(32, 24)
        self.reset_zoom_btn.setStyleSheet("QToolButton { font-size: 10px; font-weight: bold; }")
        self.reset_zoom_btn.clicked.connect(self.reset_zoom)
        toolbar.addWidget(self.reset_zoom_btn)

        spacer = QWidget(); spacer.setFixedWidth(2); toolbar.addWidget(spacer)

        self.copy_btn = QToolButton()
        self.copy_btn.setText("📋")
        self.copy_btn.setToolTip("Copy Current Image to Clipboard (with lines and enhancements)")
        self.copy_btn.setFixedSize(24, 24)
        self.copy_btn.clicked.connect(self.copy_to_clipboard)
        toolbar.addWidget(self.copy_btn)

        spacer = QWidget(); spacer.setFixedWidth(2); toolbar.addWidget(spacer)

        self.fullscreen_btn = QToolButton()
        self.fullscreen_btn.setText("⛶")
        self.fullscreen_btn.setToolTip("Toggle Fullscreen (F11)")
        self.fullscreen_btn.setCheckable(True)
        self.fullscreen_btn.setFixedSize(24, 24)
        self.fullscreen_btn.toggled.connect(self.toggle_fullscreen)
        toolbar.addWidget(self.fullscreen_btn)

        spacer = QWidget(); spacer.setFixedWidth(2); toolbar.addWidget(spacer)

        self.always_on_top_btn = QToolButton()
        self.always_on_top_btn.setText("📌")
        self.always_on_top_btn.setToolTip("Always on Top")
        self.always_on_top_btn.setCheckable(True)
        self.always_on_top_btn.setFixedSize(24, 24)
        self.always_on_top_btn.toggled.connect(self.toggle_always_on_top)
        toolbar.addWidget(self.always_on_top_btn)

    def _create_enhancement_widgets_on_toolbar(self, toolbar):
        """Create enhancement widgets on the specified toolbar"""
        # Add some spacing before the enhancement controls
        spacer_start = QWidget()
        spacer_start.setFixedWidth(8)
        toolbar.addWidget(spacer_start)
        
        # Grayscale slider
        gray_label = QLabel("Gray:")
        gray_label.setFixedWidth(30)
        gray_label.setStyleSheet("font-size: 9px; margin-right: 2px;")
        toolbar.addWidget(gray_label)
        
        self.grayscale_slider = ClickableSlider(Qt.Horizontal)
        self.grayscale_slider.setRange(0, 100)
        self.grayscale_slider.setValue(self.grayscale_value)
        self.grayscale_slider.setFixedWidth(70)  # Increased width for easier clicking
        self.grayscale_slider.setFixedHeight(24)  # Increased height for easier clicking
        self.grayscale_slider.setStyleSheet("QSlider { margin: 2px 4px; }")  # Add margins around slider
        self.grayscale_slider.setToolTip("Grayscale: 0=Color, 100=B&W")
        self.grayscale_slider.valueChanged.connect(self.update_grayscale)
        toolbar.addWidget(self.grayscale_slider)

        # Small spacer between sliders
        spacer1 = QWidget()
        spacer1.setFixedWidth(4)
        toolbar.addWidget(spacer1)

        # Contrast slider
        contrast_label = QLabel("Con:")
        contrast_label.setFixedWidth(25)
        contrast_label.setStyleSheet("font-size: 9px; margin-right: 2px;")
        toolbar.addWidget(contrast_label)
        
        self.contrast_slider = ClickableSlider(Qt.Horizontal)
        self.contrast_slider.setRange(-130, 200)
        self.contrast_slider.setValue(self.contrast_value)
        self.contrast_slider.setFixedWidth(70)  # Increased width for easier clicking
        self.contrast_slider.setFixedHeight(24)  # Increased height for easier clicking
        self.contrast_slider.setStyleSheet("QSlider { margin: 2px 4px; }")  # Add margins around slider
        self.contrast_slider.setToolTip("Contrast: 50=Normal, -130=Grey, 200=Extreme")
        self.contrast_slider.valueChanged.connect(self.update_contrast)
        toolbar.addWidget(self.contrast_slider)

        # Small spacer between sliders
        spacer2 = QWidget()
        spacer2.setFixedWidth(4)
        toolbar.addWidget(spacer2)

        # Gamma slider
        gamma_label = QLabel("Gam:")
        gamma_label.setFixedWidth(25)
        gamma_label.setStyleSheet("font-size: 9px; margin-right: 2px;")
        toolbar.addWidget(gamma_label)
        
        self.gamma_slider = ClickableSlider(Qt.Horizontal)
        self.gamma_slider.setRange(-200, 500)  # New range: -200=very dark, 0=normal, 500=very bright
        self.gamma_slider.setValue(self.gamma_value)
        self.gamma_slider.setFixedWidth(70)  # Increased width for easier clicking
        self.gamma_slider.setFixedHeight(24)  # Increased height for easier clicking
        self.gamma_slider.setStyleSheet("QSlider { margin: 2px 4px; }")  # Add margins around slider
        self.gamma_slider.setToolTip("Gamma: -200=Very Dark, 0=Normal, 500=Very Bright")
        self.gamma_slider.valueChanged.connect(self.update_gamma)
        toolbar.addWidget(self.gamma_slider)

        # Small spacer between sliders
        spacer3 = QWidget()
        spacer3.setFixedWidth(4)
        toolbar.addWidget(spacer3)

        # ── SECTION DIVIDER: enhancement sliders | LUT ──
        self._add_section_divider_to(toolbar)

        # LUT controls
        lut_label = QLabel("LUT:")
        lut_label.setFixedWidth(25)
        lut_label.setStyleSheet("font-size: 9px; margin-right: 2px;")
        toolbar.addWidget(lut_label)
        
        # LUT selection button
        self.lut_btn = QToolButton()
        self.lut_btn.setText("📁")
        self.lut_btn.setToolTip("Select LUT Folder")
        self.lut_btn.setFixedSize(20, 24)
        self.lut_btn.clicked.connect(self.choose_lut_folder)
        toolbar.addWidget(self.lut_btn)
        
        # LUT dropdown (combo box)
        self.lut_combo = QComboBox()
        self.lut_combo.addItem("None")
        self.lut_combo.setFixedWidth(80)
        self.lut_combo.setFixedHeight(24)
        self.lut_combo.setToolTip("Select LUT")
        # Style the dropdown to be much wider when opened
        self.lut_combo.setStyleSheet("""
            QComboBox {
                min-width: 80px;
            }
            QComboBox QAbstractItemView {
                min-width: 400px;
                max-width: 400px;
            }
        """)
        self.lut_combo.currentTextChanged.connect(self.apply_selected_lut)
        toolbar.addWidget(self.lut_combo)
        
        # LUT strength slider
        self.lut_strength_slider = ClickableSlider(Qt.Horizontal)
        self.lut_strength_slider.setRange(0, 100)
        self.lut_strength_slider.setValue(self.lut_strength)
        self.lut_strength_slider.setFixedWidth(50)
        self.lut_strength_slider.setFixedHeight(24)
        self.lut_strength_slider.setStyleSheet("QSlider { margin: 2px 4px; }")
        self.lut_strength_slider.setToolTip("LUT Strength: 0=Off, 100=Full")
        self.lut_strength_slider.valueChanged.connect(self.update_lut_strength)
        toolbar.addWidget(self.lut_strength_slider)

        # Small spacer before reset button
        spacer4 = QWidget()
        toolbar.addWidget(spacer4)

        # Reset Enhancements button — 🧹 (broom) distinguishes it from rotate ↻ / reset-zoom
        self.enh_reset_btn = reset_btn = QToolButton()
        reset_btn.setText("\U0001F9F9")  # 🧹
        reset_btn.setToolTip("Reset All Enhancements (grayscale / contrast / gamma / LUT strength)")
        reset_btn.setFixedSize(24, 24)
        reset_btn.setStyleSheet("QToolButton { margin: 2px; }")
        reset_btn.clicked.connect(self.reset_enhancements)
        toolbar.addWidget(reset_btn)

    def _update_title(self):
        count = len(self.images)
        total = len(self._all_items)
        folder_name = os.path.basename(self.folder) if self.folder else ""
        if total and count != total:
            self.setWindowTitle(
                f"Ova Viewer - {folder_name} ({count} of {total} files shown)")
        else:
            self.setWindowTitle(f"Ova Viewer - {folder_name} ({count} images found)")

    def update_image_info(self, img_path=None):
        if img_path is None or not os.path.exists(img_path):
            self.status.showMessage("")
            return
        base = os.path.basename(img_path)
        
        # Use safe loading for image info
        pixmap, error = safe_load_pixmap(img_path)
        if error:
            info = f"{base} - {error}"
        elif not pixmap.isNull():
            file_size_mb = get_image_file_size(img_path)
            if file_size_mb > 10:  # Show file size for large files
                info = f"{base} – {pixmap.width()}x{pixmap.height()} ({file_size_mb:.1f} MB)"
            else:
                info = f"{base} – {pixmap.width()}x{pixmap.height()}"
        else:
            info = base
        self.status.showMessage(info)

    def show_random_image(self):
        try:
            if not self.images:
                return
            # Clear lines when showing a new random image
            self.drawn_lines.clear()
            self.drawn_horizontal_lines.clear()
            self.drawn_free_lines.clear()
            self.drawn_free_strokes.clear()  # Clear free draw strokes
            self.current_line_start = None
            self._clear_line_preview()
            # Reset rotation angle and flips for new image
            self.rotation_angle = 0
            self.flipped_h = False
            self.flipped_v = False
            # Reset button states safely
            if hasattr(self, 'flip_h_btn') and self.flip_h_btn is not None:
                self.flip_h_btn.setChecked(False)
            if hasattr(self, 'flip_v_btn') and self.flip_v_btn is not None:
                self.flip_v_btn.setChecked(False)
            available = [img for img in self.images if img not in self.history]
            if not available:
                self.history.clear()
                self.history_list.clear()
                available = self.images[:]
            img_path = random.choice(available)
            # Route through the playlist dispatcher so docs (PDF/EPUB/CBR)
            # are opened in their viewer instead of failing as images.
            self._load_playlist_item(img_path)
        except Exception as e:
            print(f"Error in show_random_image: {e}")
            # Don't let the error crash the app, just log it
            import traceback
            traceback.print_exc()

    def _manual_next_image(self):
        # PDF/EPUB/CBR mode delegates to show_next_image
        if (getattr(self, '_pdf_doc', None)
                or getattr(self, '_epub_doc', None)
                or getattr(self, '_cbr_doc', None)):
            self.show_next_image()
            return

        if not self.images:
            return

        if self.random_mode:
            self.show_random_image()
        else:
            # Sequential mode
            if self.current_image and self.current_image in self.images:
                try:
                    current_list_index = self.images.index(self.current_image)
                    next_index = (current_list_index + 1) % len(self.images)
                except ValueError:
                    next_index = 0
            else:
                next_index = 0
            
            img_path = self.images[next_index]
            self._display_image_with_lut_preview(img_path)
            self.add_to_history(img_path)
            self.current_image = img_path
            self.update_image_info(img_path)
            self.set_status_path(img_path)
            if self._auto_advance_active:
                self.timer_remaining = self.timer_spin.value()
                self._update_ring()

    def _stop_current_animation(self):
        """Stop any currently playing animated GIF"""
        if hasattr(self, 'image_label') and self.image_label:
            self.image_label.stop_animation()

    def _stop_current_video(self):
        """Stop any currently playing video"""
        if hasattr(self, 'image_label') and self.image_label:
            self.image_label.stop_video()
        self._video_playing = False
        # Clear subtitle state
        self._subtitle_cues = []
        self._subtitle_starts = []
        self._subtitle_path = None
        self._current_subtitle_text = ""
        self._video_last_scaled = None
        if hasattr(self, '_video_controls_widget'):
            self._video_controls_widget.hide()
        if hasattr(self, '_video_overlay'):
            self._video_overlay.deactivate()

    def _display_video(self, img_path):
        """Display a video with full enhancement/LUT/zoom support.

        QMediaPlayer + QVideoSink drive frame timing; each frame is
        processed through the normal enhancement pipeline before display.
        """
        self._stop_current_animation()
        self._stop_current_video()

        self.current_image = img_path
        self._video_playing = True

        # Load a sibling .srt subtitle file if one exists (same base name)
        self._load_subtitles_for(img_path)

        if self.image_label.start_video(img_path):
            self.update_image_info(img_path)
            self.set_status_path(img_path)
            # Wire up player signals to toolbar controls
            player = self.image_label._media_player
            if player and hasattr(self, '_video_controls_widget'):
                try:
                    player.durationChanged.connect(self._on_video_duration_changed)
                    player.positionChanged.connect(self._on_video_position_changed)
                    player.playbackStateChanged.connect(self._on_video_state_changed)
                    player.mediaStatusChanged.connect(self._on_video_media_status)
                except RuntimeError:
                    pass
                self._video_controls_widget.show()
                # Apply stored volume/mute
                self.image_label.video_set_volume(self._video_volume)
                self.image_label.video_set_muted(self._video_muted)
                # Activate the floating overlay
                if hasattr(self, '_video_overlay'):
                    self._video_overlay.activate(self._video_volume, self._video_muted)
            # Announce a dub track last: update_image_info posts its own status
            # message, which would otherwise bury this one.
            if self.image_label.has_dub_audio():
                self._on_dub_audio_started(self.image_label.dub_audio_path())
        else:
            self._video_playing = False

    def _on_video_frame_changed(self, video_frame):
        """Called by QVideoSink on every new video frame.

        Converts the frame to a pixmap and runs it through the same
        enhancement / LUT / rotation / zoom pipeline used for GIF frames.
        Implements frame throttling to avoid overloading the enhancement pipeline.
        """
        if video_frame is None or not video_frame.isValid():
            return

        # Frame throttling: skip frames if processing can't keep up
        import time as _time
        now = _time.monotonic()
        if hasattr(self.image_label, '_video_last_frame_time'):
            elapsed = now - self.image_label._video_last_frame_time
            if elapsed < 0.020:  # cap at ~50 fps for processing
                return
        self.image_label._video_last_frame_time = now

        image = video_frame.toImage()
        if image.isNull():
            return

        frame_pixmap = QPixmap.fromImage(image)
        if frame_pixmap.isNull():
            return

        # --- Apply LUT via fast pre-built table ---
        if self.lut_enabled and self.current_lut and self.lut_strength > 0:
            frame_pixmap = self._apply_lut_frame(frame_pixmap)

        # --- Apply enhancements (grayscale / contrast / gamma) ---
        if self.grayscale_value > 0 or self.contrast_value != 50 or self.gamma_value != 0:
            frame_pixmap = self.apply_fast_enhancements(frame_pixmap.copy())

        # --- Apply curves (classical RGB levels) ---
        if self.curves_enabled:
            frame_pixmap = self.apply_curves(frame_pixmap)

        # --- Apply value filter (posterize) ---
        if self.value_filter_enabled:
            frame_pixmap = self.apply_value_filter(frame_pixmap)

        # --- Apply color groups (palette quantization) ---
        if self.color_groups_enabled:
            frame_pixmap = self.apply_color_groups(frame_pixmap)

        # --- Apply object groups (cryptomatte-style per-object flattening) ---
        if self.object_groups_enabled:
            frame_pixmap = self.apply_object_groups(frame_pixmap)

        # --- Apply edge detection (plane changes) ---
        if self.edge_detection_enabled:
            frame_pixmap = self.apply_edge_detection(frame_pixmap)

        # --- Apply rotation / flips ---
        if self.rotation_angle != 0 or self.flipped_h or self.flipped_v:
            frame_pixmap = self._apply_cached_transforms(frame_pixmap)

        # Store as original_pixmap so zoom helpers work
        self.original_pixmap = frame_pixmap

        # --- Scale / zoom / pan ---
        final = self._scale_pixmap(frame_pixmap, self.current_image)
        final = self._apply_fixed_overlays_to_pixmap(final)
        # Keep a subtitle-free copy so toggling/seeking can redraw instantly
        self._video_last_scaled = final
        # Refresh the active cue from the player's exact position (frame-accurate)
        if self._subtitle_cues:
            player = getattr(self.image_label, '_media_player', None)
            if player is not None:
                self._current_subtitle_text = self._lookup_subtitle(player.position())
        self.image_label.setPixmap(self._draw_subtitle_on(final))

    # ── Video toolbar signal handlers ─────────────────────────────

    def _on_video_duration_changed(self, duration_ms):
        if hasattr(self, '_video_seek_slider'):
            self._video_seek_slider.setRange(0, max(duration_ms, 0))
            self._video_total_label.setText(self._format_video_time(duration_ms))
        if hasattr(self, '_video_overlay'):
            self._video_overlay.update_duration(duration_ms)

    def _on_video_position_changed(self, position_ms):
        if hasattr(self, '_video_seek_slider') and not self._video_seek_slider.isSliderDown():
            self._video_seek_slider.setValue(position_ms)
        if hasattr(self, '_video_time_label'):
            self._video_time_label.setText(self._format_video_time(position_ms))
        if hasattr(self, '_video_overlay'):
            self._video_overlay.update_position(position_ms)
        # Keep subtitles in sync while paused / seeking (playback redraws per frame)
        if self._subtitle_cues:
            new_text = self._lookup_subtitle(position_ms)
            if new_text != self._current_subtitle_text:
                self._current_subtitle_text = new_text
                if not self.image_label.is_video_playing():
                    self._redraw_video_subtitle()

    # ── Subtitles (.srt) ──────────────────────────────────────────

    def _load_subtitles_for(self, video_path):
        """Load a sibling ``.srt`` (same base name) for ``video_path`` if present."""
        self._subtitle_cues = []
        self._subtitle_starts = []
        self._subtitle_path = None
        self._current_subtitle_text = ""
        try:
            srt_path = find_subtitle_file(video_path)
            if srt_path:
                cues = parse_srt(srt_path)
                if cues:
                    self._subtitle_cues = cues
                    self._subtitle_starts = [c[0] for c in cues]
                    self._subtitle_path = srt_path
                    self.status.showMessage(
                        f"Subtitles loaded: {os.path.basename(srt_path)}", 3000)
        except Exception as e:
            print(f"subtitle load error: {e}")
        if hasattr(self, '_video_overlay'):
            self._video_overlay.update_subtitle_state(
                bool(self._subtitle_cues), self._subtitles_enabled,
                os.path.basename(self._subtitle_path or '') or None)

    def _lookup_subtitle(self, pos_ms):
        """Return the cue text active at ``pos_ms`` (empty string if none)."""
        cues = self._subtitle_cues
        if not cues:
            return ""
        import bisect
        i = bisect.bisect_right(self._subtitle_starts, pos_ms) - 1
        if 0 <= i < len(cues):
            start, end, text = cues[i]
            if start <= pos_ms <= end:
                return text
        return ""

    def _draw_subtitle_on(self, pixmap):
        """Return a copy of ``pixmap`` with the current subtitle drawn, or the
        original if subtitles are hidden / there is no active cue."""
        text = self._current_subtitle_text if self._subtitles_enabled else ""
        if not text or pixmap is None or pixmap.isNull():
            return pixmap
        from PySide6.QtCore import QRect
        result = pixmap.copy()
        painter = QPainter(result)
        painter.setRenderHint(QPainter.Antialiasing, True)
        w, h = result.width(), result.height()

        font = painter.font()
        px = max(14, int(h * 0.045))
        font.setPixelSize(px)
        font.setBold(True)
        painter.setFont(font)

        margin = max(8, int(h * 0.03))
        max_w = int(w * 0.9)
        flags = Qt.AlignHCenter | Qt.AlignBottom | Qt.TextWordWrap
        bounds = painter.boundingRect(0, 0, max_w, h, flags, text)
        tw, th = bounds.width(), bounds.height()
        x = (w - tw) // 2
        y = h - margin - th
        text_rect = QRect(x, y, tw, th)

        pad = max(4, int(px * 0.3))
        painter.fillRect(
            QRect(x - pad, y - pad, tw + 2 * pad, th + 2 * pad),
            QColor(0, 0, 0, 140))

        # Black outline for legibility over any background
        painter.setPen(QColor(0, 0, 0, 230))
        for dx, dy in ((-1, -1), (1, -1), (-1, 1), (1, 1), (0, -1), (0, 1), (-1, 0), (1, 0)):
            painter.drawText(text_rect.translated(dx, dy),
                             Qt.AlignHCenter | Qt.TextWordWrap, text)
        painter.setPen(QColor(255, 255, 255))
        painter.drawText(text_rect, Qt.AlignHCenter | Qt.TextWordWrap, text)
        painter.end()
        return result

    def _redraw_video_subtitle(self):
        """Redraw the last video frame with the current subtitle state applied."""
        if not self._video_playing:
            return
        base = getattr(self, '_video_last_scaled', None)
        if base is None or base.isNull():
            return
        self.image_label.setPixmap(self._draw_subtitle_on(base))

    def has_video_subtitles(self):
        """True if a subtitle track is loaded for the current video."""
        return self._video_playing and bool(self._subtitle_cues)

    def toggle_subtitles(self, checked=None):
        """Show/hide subtitles. With no argument, flips the current state."""
        if checked is None:
            self._subtitles_enabled = not self._subtitles_enabled
        else:
            self._subtitles_enabled = bool(checked)
        if hasattr(self, '_video_overlay'):
            self._video_overlay.update_subtitle_state(
                bool(self._subtitle_cues), self._subtitles_enabled,
                os.path.basename(self._subtitle_path or '') or None)
        self._redraw_video_subtitle()

    # ── Autoplay next (advance when a video / GIF ends) ────────────────

    # A very short looping GIF would otherwise flash past; keep it on screen at
    # least this long before its completed loop counts as "ended".
    _GIF_MIN_DWELL_S = 1.5

    def toggle_autoplay_next(self, checked):
        """Enable/disable advancing when a video or animated GIF ends."""
        self.autoplay_next_enabled = bool(checked)
        btn = getattr(self, "autoplay_next_btn", None)
        if btn is not None:
            btn.blockSignals(True)
            btn.setChecked(self.autoplay_next_enabled)
            btn.blockSignals(False)
        self._reset_gif_loop_tracking()
        if hasattr(self, '_video_overlay'):
            self._video_overlay.update_autoplay_state(self.autoplay_next_enabled)
        if self.autoplay_next_enabled:
            self.status.showMessage(
                "Autoplay next: on — videos and GIFs advance when they finish.")
        else:
            self.status.showMessage("Autoplay next: off.")

    def _reset_gif_loop_tracking(self):
        """Start GIF loop detection over for a freshly started animation."""
        self._gif_prev_frame = -1
        self._gif_started_at = time.monotonic()

    def _on_video_media_status(self, status):
        """Slot: QMediaPlayer status changed. EndOfMedia means the clip ended.

        EndOfMedia is used rather than a Stopped playback state because
        stopping happens on manual stop and on teardown too, which must not
        trigger an advance.
        """
        from PySide6.QtMultimedia import QMediaPlayer
        if status != QMediaPlayer.MediaStatus.EndOfMedia:
            return
        if not self.autoplay_next_enabled:
            return
        self._autoplay_advance()

    def _check_gif_loop_complete(self, movie, frame_number):
        """Advance when an animated GIF completes a full pass.

        Most GIFs declare an infinite loop count, so QMovie never emits
        finished(). A wrap of the frame counter back towards the start is the
        reliable end-of-pass signal for both finite and infinite loops.
        """
        previous = self._gif_prev_frame
        self._gif_prev_frame = frame_number
        if previous < 0 or frame_number >= previous:
            return
        # Single-frame files are not really animations; leave them alone.
        if movie.frameCount() == 1:
            return
        if time.monotonic() - self._gif_started_at < self._GIF_MIN_DWELL_S:
            # Too quick to be watchable — let it keep looping and re-check on
            # the next pass.
            self._gif_prev_frame = -1
            return
        self._autoplay_advance()

    def _autoplay_advance(self):
        """Move to the next playlist item because the current media ended."""
        if self._autoplay_advancing:
            return
        if not self.images:
            return
        self._autoplay_advancing = True
        try:
            self._reset_gif_loop_tracking()
            self._auto_advance_next()
        finally:
            self._autoplay_advancing = False

    def _media_holds_auto_advance(self):
        """True while playing media should hold the auto-advance countdown.

        With autoplay-on-end enabled, a running video or GIF decides when to
        move on, so the timer must not cut it off mid-clip.
        """
        if not self.autoplay_next_enabled:
            return False
        label = getattr(self, "image_label", None)
        if label is None:
            return False
        if label.is_animation_playing():
            return True
        if not self._video_playing:
            return False
        player = getattr(label, "_media_player", None)
        if player is None:
            return False
        try:
            from PySide6.QtMultimedia import QMediaPlayer
            return player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
        except Exception:
            return False

    def _on_video_state_changed(self, state):
        from PySide6.QtMultimedia import QMediaPlayer
        if hasattr(self, '_video_play_btn'):
            if state == QMediaPlayer.PlaybackState.PlayingState:
                self._video_play_btn.setText("⏸")
                self._video_play_btn.setToolTip("Pause")
            else:
                self._video_play_btn.setText("▶")
                self._video_play_btn.setToolTip("Play")
        if hasattr(self, '_video_overlay'):
            self._video_overlay.update_play_state(
                state == QMediaPlayer.PlaybackState.PlayingState)

    @staticmethod
    def _format_video_time(ms):
        s = max(0, ms) // 1000
        m, s = divmod(s, 60)
        h, m = divmod(m, 60)
        if h:
            return f"{h}:{m:02d}:{s:02d}"
        return f"{m}:{s:02d}"

    # ── Dub audio (sibling soundtrack) ────────────────────────────────

    def toggle_dub_audio(self, checked):
        """Enable/disable using a sibling audio file as the soundtrack.

        Takes effect on the next video, and immediately on the one playing —
        the clip is reloaded and resumed at the same position, so switching
        sounds like changing audio track in VLC rather than a setting that
        only applies later.
        """
        self.dub_audio_enabled = bool(checked)
        btn = getattr(self, '_dub_audio_btn', None)
        if btn is not None:
            btn.blockSignals(True)
            btn.setChecked(self.dub_audio_enabled)
            btn.blockSignals(False)
        if hasattr(self, '_video_overlay'):
            self._video_overlay.update_dub_state(self.dub_audio_enabled)

        if not (self._video_playing and self.current_image):
            self.status.showMessage(
                "Dub audio: on — videos will use a matching .mp3 if one sits beside them."
                if self.dub_audio_enabled else "Dub audio: off.")
            return

        # Only reload when it actually changes this video's soundtrack.
        had_dub = self.image_label.has_dub_audio()
        if not self.dub_audio_enabled and not had_dub:
            self.status.showMessage("Dub audio: off.")
            return
        if self.dub_audio_enabled and not find_dub_audio_file(self.current_image):
            self.status.showMessage(
                "Dub audio: on — no matching audio file next to "
                f"{os.path.basename(self.current_image)}.")
            return

        player = self.image_label._media_player
        position = player.position() if player is not None else 0
        was_paused = player is not None and not self.image_label.is_video_playing()
        self._display_video(self.current_image)
        if position > 0:
            # The fresh player needs a moment to load before it can seek.
            QTimer.singleShot(150, lambda p=position: self.image_label.video_seek(p))
        if was_paused:
            QTimer.singleShot(200, self.image_label.video_toggle_play_pause)
        if not self.dub_audio_enabled:
            self.status.showMessage("Dub audio: off — using the video's own track.")

    def _on_dub_audio_started(self, dub_path):
        """Called by the label when a dub track is attached to a video."""
        name = os.path.basename(dub_path)
        self.status.showMessage(f"Dub audio: playing {name} instead of the video's track")
        btn = getattr(self, '_dub_audio_btn', None)
        if btn is not None:
            btn.setToolTip(f"Dub audio: playing {name} (click to use the video's own track)")
        if hasattr(self, '_video_overlay'):
            self._video_overlay.update_dub_state(True, detail=name)

    def _on_video_seek_slider_moved(self, position_ms):
        self.image_label.video_seek(position_ms)

    def _on_video_volume_changed(self, value):
        self._video_volume = value
        self.image_label.video_set_volume(value)

    def _toggle_video_mute(self):
        self._video_muted = not self._video_muted
        self.image_label.video_set_muted(self._video_muted)
        if hasattr(self, '_video_mute_btn'):
            self._video_mute_btn.setText("🔇" if self._video_muted else "🔊")
            self._video_mute_btn.setToolTip("Unmute" if self._video_muted else "Mute")
        if hasattr(self, '_video_overlay'):
            self._video_overlay.update_mute_state(self._video_muted)

    def _video_shortcut_play_pause(self):
        if self._video_playing:
            self.image_label.video_toggle_play_pause()
        elif self.image_label.is_animation_active():
            self.image_label.gif_toggle_play_pause()

    def _video_shortcut_mute(self):
        if self._video_playing:
            self._toggle_video_mute()

    def _video_shortcut_seek(self, offset_ms):
        if self._video_playing and self.image_label._media_player is not None:
            pos = self.image_label._media_player.position()
            self.image_label.video_seek(max(0, pos + offset_ms))
        else:
            # No video playing — fall back to image navigation
            if offset_ms < 0:
                self.show_previous_image()
            else:
                self.show_next_image()

    def _display_animated_gif(self, img_path):
        """Display an animated GIF with full enhancement/LUT/zoom support.
        
        QMovie drives frame timing; each frame is processed through the
        normal enhancement pipeline before being displayed as a pixmap.
        """
        self._stop_current_animation()
        self._stop_current_video()
        label_size = self.image_label.size()
        from PySide6.QtGui import QImageReader
        reader = QImageReader(img_path)
        gif_size = reader.size()
        if gif_size.isValid():
            scaled = gif_size.scaled(label_size, Qt.KeepAspectRatio)
        else:
            scaled = label_size

        self.current_image = img_path

        if self.image_label.start_animation(img_path, scaled):
            self._reset_gif_loop_tracking()
            self.update_image_info(img_path)
            self.set_status_path(img_path)
        else:
            # Fallback: show first frame as a static image through normal path
            self._stop_current_animation()
            # temporarily bypass the animated-gif redirect in display_image
            self._gif_static_fallback = True
            self.display_image(img_path)
            self._gif_static_fallback = False

    def _redraw_paused_gif_frame(self):
        """Re-run the current (frozen) GIF frame through the display pipeline.

        While paused, QMovie no longer emits frameChanged, so enhancement,
        LUT, zoom, and resize changes need to be applied manually here to
        still take effect on screen.
        """
        movie = self.image_label._current_movie
        if movie is not None:
            self._on_gif_frame_changed(movie.currentFrameNumber())

    def _on_gif_frame_changed(self, _frame_number):
        """Called by QMovie on every frame change.
        
        Grabs the current frame pixmap and runs it through the same
        enhancement / LUT / rotation / zoom pipeline used for static images.
        Uses a pre-built fast lookup table for LUT to avoid per-frame overhead.
        """
        movie = self.image_label._current_movie
        if movie is None:
            return

        if self.autoplay_next_enabled:
            self._check_gif_loop_complete(movie, _frame_number)

        frame_pixmap = movie.currentPixmap()
        if frame_pixmap.isNull():
            return

        # --- Apply LUT via fast pre-built table ---
        if self.lut_enabled and self.current_lut and self.lut_strength > 0:
            frame_pixmap = self._apply_lut_frame(frame_pixmap)

        # --- Apply enhancements (grayscale / contrast / gamma) ---
        if self.grayscale_value > 0 or self.contrast_value != 50 or self.gamma_value != 0:
            frame_pixmap = self.apply_fast_enhancements(frame_pixmap.copy())

        # --- Apply curves (classical RGB levels) ---
        if self.curves_enabled:
            frame_pixmap = self.apply_curves(frame_pixmap)

        # --- Apply value filter (posterize) ---
        if self.value_filter_enabled:
            frame_pixmap = self.apply_value_filter(frame_pixmap)

        # --- Apply color groups (palette quantization) ---
        if self.color_groups_enabled:
            frame_pixmap = self.apply_color_groups(frame_pixmap)

        # --- Apply object groups (cryptomatte-style per-object flattening) ---
        if self.object_groups_enabled:
            frame_pixmap = self.apply_object_groups(frame_pixmap)

        # --- Apply edge detection (plane changes) ---
        if self.edge_detection_enabled:
            frame_pixmap = self.apply_edge_detection(frame_pixmap)

        # --- Apply rotation / flips ---
        if self.rotation_angle != 0 or self.flipped_h or self.flipped_v:
            frame_pixmap = self._apply_cached_transforms(frame_pixmap)

        # Store as original_pixmap so zoom helpers work
        self.original_pixmap = frame_pixmap

        # --- Scale / zoom / pan ---
        final = self._scale_pixmap(frame_pixmap, self.current_image)
        final = self._apply_fixed_overlays_to_pixmap(final)
        self.image_label.setPixmap(final)

    def _get_gif_lut_table_np(self):
        """Build a full 256³ → (R,G,B) numpy uint8 lookup table.

        Built once (~80ms with numpy) and cached until LUT or strength changes.
        Returns an ndarray of shape (256, 256, 256, 3) dtype uint8.
        """
        import numpy as np

        lut = self.current_lut
        strength = self.lut_strength
        cache_key = (id(lut), lut['file_path'], lut['size'], strength)

        if hasattr(self, '_gif_np_lut_key') and self._gif_np_lut_key == cache_key:
            return self._gif_np_lut

        lut_data = lut['data']
        lut_size = lut['size']
        sf = strength / 100.0

        # Build flat float32 LUT array (N, 3) from list of tuples
        lut_arr = np.array(lut_data, dtype=np.float32)  # (lut_size³, 3)

        # Generate all 256 levels per channel normalised to 0..1
        vals = np.linspace(0.0, 1.0, 256, dtype=np.float32)

        # Coordinates in LUT space
        scale = lut_size - 1
        coords = vals * scale  # (256,)

        low = np.clip(np.floor(coords).astype(np.int32), 0, lut_size - 1)
        high = np.clip(low + 1, 0, lut_size - 1)
        frac = coords - low  # fractional parts (256,)

        # Pre-index the flat LUT for all 8 corners of triplets (r, g, b)
        # Use meshgrid: axes order = (R, G, B) → output (256,256,256)
        r_low, g_low, b_low = np.meshgrid(low, low, low, indexing='ij')
        r_high = np.clip(r_low + 1, 0, lut_size - 1)
        g_high = np.clip(g_low + 1, 0, lut_size - 1)
        b_high = np.clip(b_low + 1, 0, lut_size - 1)

        rf, gf, bf = np.meshgrid(frac, frac, frac, indexing='ij')

        def _idx(ri, gi, bi):
            return ri + gi * lut_size + bi * lut_size * lut_size

        # Fetch 8 corners → shape (256,256,256,3)
        c000 = lut_arr[_idx(r_low,  g_low,  b_low)]
        c100 = lut_arr[_idx(r_high, g_low,  b_low)]
        c010 = lut_arr[_idx(r_low,  g_high, b_low)]
        c110 = lut_arr[_idx(r_high, g_high, b_low)]
        c001 = lut_arr[_idx(r_low,  g_low,  b_high)]
        c101 = lut_arr[_idx(r_high, g_low,  b_high)]
        c011 = lut_arr[_idx(r_low,  g_high, b_high)]
        c111 = lut_arr[_idx(r_high, g_high, b_high)]

        # Expand fracs to broadcast with (…,3)
        rf = rf[..., np.newaxis]
        gf = gf[..., np.newaxis]
        bf = bf[..., np.newaxis]

        # Trilinear interpolation (vectorised)
        c00 = c000 * (1 - rf) + c100 * rf
        c10 = c010 * (1 - rf) + c110 * rf
        c01 = c001 * (1 - rf) + c101 * rf
        c11 = c011 * (1 - rf) + c111 * rf
        c0 = c00 * (1 - gf) + c10 * gf
        c1 = c01 * (1 - gf) + c11 * gf
        result = c0 * (1 - bf) + c1 * bf  # (256,256,256,3) float32 0..1

        # Blend with identity by strength
        identity_r, identity_g, identity_b = np.meshgrid(vals, vals, vals, indexing='ij')
        identity = np.stack([identity_r, identity_g, identity_b], axis=-1)
        blended = identity * (1 - sf) + result * sf

        table = np.clip(blended * 255 + 0.5, 0, 255).astype(np.uint8)  # (256,256,256,3)

        self._gif_np_lut = table
        self._gif_np_lut_key = cache_key
        return table

    def _apply_lut_frame(self, pixmap):
        """Apply LUT to a single animated frame (GIF or video).

        Fast path 1: GPU (OpenCL) — instant, reuses existing infrastructure.
        Fast path 2: numpy vectorised 256³ lookup table — ~2-5ms per frame.
        """
        # --- GPU fast path (preferred) — uses quiet method, no per-frame prints ---
        if self.gpu_processor.is_available():
            image = pixmap.toImage()
            if image.format() != image.Format.Format_RGB32:
                image = image.convertToFormat(image.Format.Format_RGB32)
            lut = self.current_lut
            result = self.gpu_processor.apply_lut_gpu_quiet(
                image, lut['data'], lut['size'], self.lut_strength / 100.0)
            if result is not None:
                return QPixmap.fromImage(result)

        # --- numpy vectorised fallback ---
        try:
            import numpy as np
            table = self._get_gif_lut_table_np()  # (256,256,256,3) uint8

            image = pixmap.toImage()
            if image.isNull():
                return pixmap
            if image.format() != image.Format.Format_RGB32:
                image = image.convertToFormat(image.Format.Format_RGB32)

            w, h = image.width(), image.height()
            ptr = image.bits()
            arr = np.frombuffer(ptr, dtype=np.uint8).reshape((h, w, 4)).copy()

            # arr is BGRA — channels: B=0 G=1 R=2 A=3
            b_ch = arr[:, :, 0]
            g_ch = arr[:, :, 1]
            r_ch = arr[:, :, 2]

            mapped = table[r_ch, g_ch, b_ch]  # (H, W, 3) — R,G,B out

            arr[:, :, 2] = mapped[:, :, 0]  # R
            arr[:, :, 1] = mapped[:, :, 1]  # G
            arr[:, :, 0] = mapped[:, :, 2]  # B

            out = QImage(arr.data, w, h, w * 4, QImage.Format.Format_RGB32).copy()
            return QPixmap.fromImage(out)
        except ImportError:
            # numpy not available — return unprocessed frame
            return pixmap

    def display_image(self, img_path):
        # Video files: redirect to video display with full enhancement support
        if is_video_file(img_path):
            if self._video_playing and self.current_image == img_path:
                return
            self._display_video(img_path)
            return

        # Animated GIFs: redirect to animated display with full enhancement support
        if is_animated_gif(img_path) and not getattr(self, '_gif_static_fallback', False):
            # Already displaying this GIF (playing or paused) — don't restart it,
            # which would silently un-pause it.
            if self.image_label.is_animation_active() and self.current_image == img_path:
                # If playing, the next frame callback picks up enhancement/zoom
                # changes live. If paused, there is no next frame, so redraw the
                # frozen frame now so those changes still take effect.
                if self.image_label.is_animation_paused():
                    self._redraw_paused_gif_frame()
                return
            self._display_animated_gif(img_path)
            return

        # Stop any running animation/video before displaying a static image
        self._stop_current_animation()
        self._stop_current_video()

        # Clear LUT processing cache when changing images to prevent memory buildup
        if hasattr(self, 'current_image') and self.current_image != img_path:
            self.clear_lut_cache()
        
        # Create cache key including enhancement settings, rotation, flips, and line information
        lines_info = ""
        if self.lines_visible and (self.drawn_lines or self.drawn_horizontal_lines or self.drawn_free_lines or self.drawn_free_strokes):
            # Include line count and visibility in cache key since LUT processing differs with/without lines
            vlines = len(self.drawn_lines) if self.drawn_lines else 0
            hlines = len(self.drawn_horizontal_lines) if self.drawn_horizontal_lines else 0
            flines = len(self.drawn_free_lines) if self.drawn_free_lines else 0
            strokes = len(self.drawn_free_strokes) if self.drawn_free_strokes else 0
            lines_info = f"_lines_{vlines}_{hlines}_{flines}_{strokes}_{self.line_color.name()}_{self.line_thickness}"
        
        cache_key = f"{img_path}_{self.grayscale_value}_{self.contrast_value}_{self.gamma_value}_{self.rotation_angle}_{self.flipped_h}_{self.flipped_v}_{self.current_lut_name}_{self.lut_strength}_v{int(self.value_filter_enabled)}-{self.value_levels}_c{int(self.color_groups_enabled)}-{self.color_groups_count}-{self.color_groups_field}_o{int(self.object_groups_enabled)}-{self.object_groups_mode}-{self.object_groups_detail}-{self.object_groups_min_size}_e{int(self.edge_detection_enabled)}-{self.edge_mode}-{self.edge_sensitivity}-{self.edge_color.name() if self.edge_color else 'def'}_cv{self._curves_signature()}-{self.line_color.name()}{lines_info}"
        
        # Check enhanced cache first
        if cache_key in self.enhancement_cache:
            pixmap = self.enhancement_cache[cache_key]
        else:
            # Check base pixmap cache
            if img_path in self.pixmap_cache:
                base_pixmap = self.pixmap_cache[img_path]
            else:
                base_pixmap, error = safe_load_pixmap(img_path)
                if error:
                    self.image_label.setText(error)
                    self.status.showMessage(os.path.basename(img_path))
                    return
                
                # Cache the base pixmap
                self._manage_cache(self.pixmap_cache, img_path, base_pixmap)
            
            # SMART LUT PROCESSING: Use full quality when lines are present
            if self.lut_enabled and self.current_lut and self.lut_strength > 0:
                # Check if we have cached LUT result (with lines)
                lut_cache_key = self._get_lut_cache_key()
                if (hasattr(self, '_lut_process_cache') and 
                    lut_cache_key in self._lut_process_cache):
                    # Use cached full-quality LUT result
                    pixmap = self._lut_process_cache[lut_cache_key].copy()
                else:
                    # Need full LUT processing - use SAME processing path regardless of lines
                    # Apply LUT BEFORE enhancements for correct color processing
                    lut_pixmap = self.apply_lut_to_image(base_pixmap.copy(), self.current_lut, self.lut_strength)
                    pixmap = self.apply_fast_enhancements(lut_pixmap)
                    
                    # No async processing - use direct processing for consistent results
            elif self.grayscale_value > 0 or self.contrast_value != 50 or self.gamma_value != 0:
                # Only basic enhancements needed
                pixmap = self.apply_fast_enhancements(base_pixmap.copy())
            else:
                pixmap = base_pixmap

            # Apply curves (classical RGB levels) AFTER color enhancements / LUT
            if self.curves_enabled:
                pixmap = self.apply_curves(pixmap)

            # Apply value filter (posterize) AFTER color enhancements / LUT
            if self.value_filter_enabled:
                pixmap = self.apply_value_filter(pixmap)

            # Apply color groups (palette quantization) AFTER tonal processing
            if self.color_groups_enabled:
                pixmap = self.apply_color_groups(pixmap)

            # Apply object groups (per-object flattening) AFTER colour reduction
            if self.object_groups_enabled:
                pixmap = self.apply_object_groups(pixmap)

            # Apply edge detection (plane changes) AFTER all tonal processing
            if self.edge_detection_enabled:
                pixmap = self.apply_edge_detection(pixmap)

            # Apply rotation and flips if needed
            if self.rotation_angle != 0 or self.flipped_h or self.flipped_v:
                image = pixmap.toImage()
                
                # Apply flips first
                if self.flipped_h:
                    transform_h = QTransform().scale(-1, 1)
                    image = image.transformed(transform_h)
                if self.flipped_v:
                    transform_v = QTransform().scale(1, -1)
                    image = image.transformed(transform_v)
                
                # Apply rotation
                if self.rotation_angle != 0:
                    transform_rot = QTransform()
                    transform_rot.rotate(self.rotation_angle)
                    image = image.transformed(transform_rot)
                
                pixmap = QPixmap.fromImage(image)
            
            # Cache the enhanced and rotated version
            self._manage_cache(self.enhancement_cache, cache_key, pixmap)
        
        # Cache the original for line drawing reference
        self.original_pixmap = pixmap.copy()
        
        # Scale FIRST, then draw lines on the scaled version
        scaled_pixmap = self._scale_pixmap(pixmap, img_path)
        
        # Draw lines on the scaled pixmap if any exist AND lines are visible
        if self.lines_visible and (self.drawn_lines or self.drawn_horizontal_lines or self.drawn_free_lines or self.drawn_free_strokes):
            final_pixmap = scaled_pixmap.copy()
            painter = QPainter(final_pixmap)
            painter.setRenderHint(QPainter.Antialiasing, False)
            
            # Use user-selected color and thickness
            pen_color = self.line_color
            pen_thickness = self.line_thickness
            painter.setPen(QPen(pen_color, pen_thickness, Qt.SolidLine))
            
            # Get the transformation parameters for line drawing
            original_size = self.original_pixmap.size()
            label_size = self.image_label.size()
            zoom_factor = self.image_label.zoom_factor
            
            # UNIFIED coordinate calculation - use the same logic for ALL zoom levels
            # Calculate the base scaled size that would be used at 100% zoom
            base_scaled = pixmap.scaled(label_size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            
            # Apply zoom factor to get the actual displayed size
            zoomed_width = int(base_scaled.width() * zoom_factor)
            zoomed_height = int(base_scaled.height() * zoom_factor)
            
            # Calculate position within the final pixmap (including pan offset)
            draw_x = (label_size.width() - zoomed_width) // 2 + int(self.image_label.pan_offset_x)
            draw_y = (label_size.height() - zoomed_height) // 2 + int(self.image_label.pan_offset_y)
            
            # Scale factors from original to zoomed display (works for all zoom levels)
            scale_x = zoomed_width / original_size.width()
            scale_y = zoomed_height / original_size.height()
            
            # Handle transformations for line drawing (flips and rotation)
            if self.rotation_angle != 0 or self.flipped_h or self.flipped_v:
                # Get the original image dimensions for proper line transformation
                # We need to apply the same transformation sequence: flips first, then rotation
                
                # Draw vertical lines (adjusted for flips and rotation)
                for x in self.drawn_lines:
                    # Apply flip transformations first
                    transformed_x = x
                    if self.flipped_h:
                        transformed_x = original_size.width() - x
                    
                    # Then apply rotation transformation
                    if self.rotation_angle == 90:
                        # Vertical line becomes horizontal
                        transformed_y = transformed_x
                        display_y = int(transformed_y * scale_y) + draw_y
                        if 0 <= display_y < final_pixmap.height():
                            painter.drawLine(0, display_y, final_pixmap.width(), display_y)
                    elif self.rotation_angle == 180:
                        # Vertical line stays vertical but position changes
                        final_x = original_size.width() - transformed_x
                        display_x = int(final_x * scale_x) + draw_x
                        if 0 <= display_x < final_pixmap.width():
                            painter.drawLine(display_x, 0, display_x, final_pixmap.height())
                    elif self.rotation_angle == 270:
                        # Vertical line becomes horizontal
                        final_y = original_size.height() - transformed_x
                        display_y = int(final_y * scale_y) + draw_y
                        if 0 <= display_y < final_pixmap.height():
                            painter.drawLine(0, display_y, final_pixmap.width(), display_y)
                    else:
                        # No rotation, just flips applied
                        display_x = int(transformed_x * scale_x) + draw_x
                        if 0 <= display_x < final_pixmap.width():
                            painter.drawLine(display_x, 0, display_x, final_pixmap.height())
                
                # Draw horizontal lines (adjusted for flips and rotation)
                for y in self.drawn_horizontal_lines:
                    # Apply flip transformations first
                    transformed_y = y
                    if self.flipped_v:
                        transformed_y = original_size.height() - y
                    
                    # Then apply rotation transformation
                    if self.rotation_angle == 90:
                        # Horizontal line becomes vertical
                        final_x = original_size.width() - transformed_y
                        display_x = int(final_x * scale_x) + draw_x
                        if 0 <= display_x < final_pixmap.width():
                            painter.drawLine(display_x, 0, display_x, final_pixmap.height())
                    elif self.rotation_angle == 180:
                        # Horizontal line stays horizontal but position changes
                        final_y = original_size.height() - transformed_y
                        display_y = int(final_y * scale_y) + draw_y
                        if 0 <= display_y < final_pixmap.height():
                            painter.drawLine(0, display_y, final_pixmap.width(), display_y)
                    elif self.rotation_angle == 270:
                        # Horizontal line becomes vertical
                        display_x = int(transformed_y * scale_x) + draw_x
                        if 0 <= display_x < final_pixmap.width():
                            painter.drawLine(display_x, 0, display_x, final_pixmap.height())
                    else:
                        # No rotation, just flips applied
                        display_y = int(transformed_y * scale_y) + draw_y
                        if 0 <= display_y < final_pixmap.height():
                            painter.drawLine(0, display_y, final_pixmap.width(), display_y)
                
                # Draw free lines (adjusted for flips and rotation)
                for line in self.drawn_free_lines:
                    start_x, start_y = line['start']
                    end_x, end_y = line['end']
                    
                    # Apply flip transformations first
                    flip_start_x = start_x
                    flip_start_y = start_y
                    flip_end_x = end_x
                    flip_end_y = end_y
                    
                    if self.flipped_h:
                        flip_start_x = original_size.width() - start_x
                        flip_end_x = original_size.width() - end_x
                    if self.flipped_v:
                        flip_start_y = original_size.height() - start_y
                        flip_end_y = original_size.height() - end_y
                    
                    # Then apply rotation transformation
                    if self.rotation_angle == 90:
                        # 90° rotation transformations
                        display_start_x = int((original_size.width() - flip_start_y) * scale_x) + draw_x
                        display_start_y = int(flip_start_x * scale_y) + draw_y
                        display_end_x = int((original_size.width() - flip_end_y) * scale_x) + draw_x
                        display_end_y = int(flip_end_x * scale_y) + draw_y
                    elif self.rotation_angle == 180:
                        # 180° rotation: both coordinates are flipped
                        display_start_x = int((original_size.width() - flip_start_x) * scale_x) + draw_x
                        display_start_y = int((original_size.height() - flip_start_y) * scale_y) + draw_y
                        display_end_x = int((original_size.width() - flip_end_x) * scale_x) + draw_x
                        display_end_y = int((original_size.height() - flip_end_y) * scale_y) + draw_y
                    elif self.rotation_angle == 270:
                        # 270° rotation transformations
                        display_start_x = int(flip_start_y * scale_x) + draw_x
                        display_start_y = int((original_size.height() - flip_start_x) * scale_y) + draw_y
                        display_end_x = int(flip_end_y * scale_x) + draw_x
                        display_end_y = int((original_size.height() - flip_end_x) * scale_y) + draw_y
                    else:
                        # No rotation, just flips applied
                        display_start_x = int(flip_start_x * scale_x) + draw_x
                        display_start_y = int(flip_start_y * scale_y) + draw_y
                        display_end_x = int(flip_end_x * scale_x) + draw_x
                        display_end_y = int(flip_end_y * scale_y) + draw_y
                    
                    # Draw the line with more lenient bounds checking
                    tolerance = 10  # pixels
                    min_x = min(display_start_x, display_end_x)
                    max_x = max(display_start_x, display_end_x)
                    min_y = min(display_start_y, display_end_y)
                    max_y = max(display_start_y, display_end_y)
                    
                    if (max_x >= -tolerance and min_x <= final_pixmap.width() + tolerance and
                        max_y >= -tolerance and min_y <= final_pixmap.height() + tolerance):
                        painter.setRenderHint(QPainter.Antialiasing, True)
                        painter.drawLine(display_start_x, display_start_y, display_end_x, display_end_y)
                        painter.setRenderHint(QPainter.Antialiasing, False)
                
                # ⚡ OPTIMIZED FREE DRAW STROKES: Pre-calculate transformations for performance
                if self.drawn_free_strokes:
                    # Pre-calculate transformation parameters to avoid repeated calculations
                    has_transforms = self.rotation_angle != 0 or self.flipped_h or self.flipped_v
                    
                    if has_transforms:
                        # Pre-calculate transformation values for better performance
                        painter.setRenderHint(QPainter.Antialiasing, True)
                        
                        for stroke in self.drawn_free_strokes:
                            if len(stroke) < 2:
                                continue
                            
                            # Process stroke segments with optimized transformations
                            for i in range(len(stroke) - 1):
                                # 🎨 PEN PRESSURE: Handle 3-tuple format (x, y, pressure)
                                if len(stroke[i]) == 3 and self.pen_pressure_enabled:
                                    start_x, start_y, start_pressure = stroke[i]
                                    end_x, end_y, end_pressure = stroke[i + 1]
                                else:
                                    # Fallback for old format or when pressure is disabled
                                    if len(stroke[i]) == 3:
                                        start_x, start_y, _ = stroke[i]
                                        end_x, end_y, _ = stroke[i + 1]
                                    else:
                                        start_x, start_y = stroke[i]
                                        end_x, end_y = stroke[i + 1]
                                    start_pressure = end_pressure = 1.0
                                
                                # 🎨 PEN PRESSURE: Calculate dynamic thickness based on pressure
                                # Use shared helper so final width matches the live preview
                                if self.pen_pressure_enabled:
                                    avg_pressure = (start_pressure + end_pressure) / 2.0
                                    dynamic_thickness = self._pressure_to_thickness(avg_pressure)
                                else:
                                    dynamic_thickness = self.line_thickness
                                
                                # ⚡ FAST TRANSFORMATION: Apply flips first (simple arithmetic)
                                if self.flipped_h:
                                    start_x = original_size.width() - start_x
                                    end_x = original_size.width() - end_x
                                if self.flipped_v:
                                    start_y = original_size.height() - start_y
                                    end_y = original_size.height() - end_y
                                
                                # ⚡ FAST ROTATION: Use pre-calculated values
                                if self.rotation_angle == 90:
                                    # 90°: (x,y) -> (y, width-x)
                                    temp_start_x = start_y
                                    temp_start_y = original_size.width() - start_x
                                    temp_end_x = end_y
                                    temp_end_y = original_size.width() - end_x
                                elif self.rotation_angle == 180:
                                    # 180°: (x,y) -> (width-x, height-y)
                                    temp_start_x = original_size.width() - start_x
                                    temp_start_y = original_size.height() - start_y
                                    temp_end_x = original_size.width() - end_x
                                    temp_end_y = original_size.height() - end_y
                                elif self.rotation_angle == 270:
                                    # 270°: (x,y) -> (height-y, x)
                                    temp_start_x = original_size.height() - start_y
                                    temp_start_y = start_x
                                    temp_end_x = original_size.height() - end_y
                                    temp_end_y = end_x
                                else:
                                    # No rotation
                                    temp_start_x, temp_start_y = start_x, start_y
                                    temp_end_x, temp_end_y = end_x, end_y
                                
                                # Apply final scaling and positioning
                                display_start_x = int(temp_start_x * scale_x) + draw_x
                                display_start_y = int(temp_start_y * scale_y) + draw_y
                                display_end_x = int(temp_end_x * scale_x) + draw_x
                                display_end_y = int(temp_end_y * scale_y) + draw_y
                                
                                # 🎨 PEN PRESSURE: Use dynamic thickness for this segment
                                pen = QPen(self.line_color, dynamic_thickness, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
                                painter.setPen(pen)
                                painter.drawLine(display_start_x, display_start_y, display_end_x, display_end_y)
                        
                        painter.setRenderHint(QPainter.Antialiasing, False)
                    else:
                        # ⚡ ULTRA-FAST: No transformations needed - direct scaling only
                        painter.setRenderHint(QPainter.Antialiasing, True)
                        
                        for stroke in self.drawn_free_strokes:
                            if len(stroke) < 2:
                                continue
                            
                            for i in range(len(stroke) - 1):
                                # 🎨 PEN PRESSURE: Handle 3-tuple format (x, y, pressure)
                                if len(stroke[i]) == 3:
                                    start_x, start_y, start_pressure = stroke[i]
                                    end_x, end_y, end_pressure = stroke[i + 1]
                                else:
                                    # Fallback for old format (no pressure)
                                    start_x, start_y = stroke[i]
                                    end_x, end_y = stroke[i + 1]
                                    start_pressure = end_pressure = 1.0
                                
                                # 🎨 PEN PRESSURE: Calculate dynamic thickness based on pressure
                                # Use shared helper so final width matches the live preview
                                avg_pressure = (start_pressure + end_pressure) / 2.0
                                dynamic_thickness = self._pressure_to_thickness(avg_pressure)
                                
                                # Direct scaling without any transformations
                                display_start_x = int(start_x * scale_x) + draw_x
                                display_start_y = int(start_y * scale_y) + draw_y
                                display_end_x = int(end_x * scale_x) + draw_x
                                display_end_y = int(end_y * scale_y) + draw_y
                                
                                # 🎨 PEN PRESSURE: Use dynamic thickness for this segment
                                pen = QPen(self.line_color, dynamic_thickness, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
                                painter.setPen(pen)
                                painter.drawLine(display_start_x, display_start_y, display_end_x, display_end_y)
                        
                        painter.setRenderHint(QPainter.Antialiasing, False)
            else:
                # No rotation - original line drawing logic
                # Draw vertical lines
                for x in self.drawn_lines:
                    display_x = int(x * scale_x) + draw_x
                    if 0 <= display_x < final_pixmap.width():
                        painter.drawLine(display_x, 0, display_x, final_pixmap.height())
                
                # Draw horizontal lines
                for y in self.drawn_horizontal_lines:
                    display_y = int(y * scale_y) + draw_y
                    if 0 <= display_y < final_pixmap.height():
                        painter.drawLine(0, display_y, final_pixmap.width(), display_y)
                
                # Draw free lines (two-point lines)
                for line in self.drawn_free_lines:
                    start_x, start_y = line['start']
                    end_x, end_y = line['end']
                    
                    # No rotation - use original coordinates directly
                    display_start_x = int(start_x * scale_x) + draw_x
                    display_start_y = int(start_y * scale_y) + draw_y
                    display_end_x = int(end_x * scale_x) + draw_x
                    display_end_y = int(end_y * scale_y) + draw_y
                    
                    # Draw the line with more lenient bounds checking
                    # Allow lines to be drawn if any part might be visible (let QPainter handle clipping)
                    # Add some tolerance to prevent precision issues from hiding lines
                    tolerance = 10  # pixels
                    
                    # Check if the line potentially intersects the visible area
                    min_x = min(display_start_x, display_end_x)
                    max_x = max(display_start_x, display_end_x)
                    min_y = min(display_start_y, display_end_y)
                    max_y = max(display_start_y, display_end_y)
                    
                    # Draw if the line's bounding box intersects the pixmap (with tolerance)
                    if (max_x >= -tolerance and min_x <= final_pixmap.width() + tolerance and
                        max_y >= -tolerance and min_y <= final_pixmap.height() + tolerance):
                        painter.drawLine(display_start_x, display_start_y, display_end_x, display_end_y)
                
                # Draw free draw strokes (continuous paths)
                for stroke_idx, stroke in enumerate(self.drawn_free_strokes):
                    if len(stroke) < 2:
                        continue  # Need at least 2 points to draw

                    # Choose rendering method based on antialiasing setting
                    if self.line_antialiasing and self.line_thickness > 1:
                        # ✨ SMOOTH: Use QPainter with antialiasing for professional quality
                        painter.setRenderHint(QPainter.Antialiasing, True)
                        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)

                        # Draw smooth connected line segments with proper transformations
                        for i in range(len(stroke) - 1):
                            # 🎨 PEN PRESSURE: Handle 3-tuple format (x, y, pressure)
                            if len(stroke[i]) == 3 and self.pen_pressure_enabled:
                                start_x, start_y, start_pressure = stroke[i]
                                end_x, end_y, end_pressure = stroke[i + 1]
                            else:
                                # Fallback for old format or when pressure is disabled
                                if len(stroke[i]) == 3:
                                    start_x, start_y, _ = stroke[i]
                                    end_x, end_y, _ = stroke[i + 1]
                                else:
                                    start_x, start_y = stroke[i]
                                    end_x, end_y = stroke[i + 1]
                                start_pressure = end_pressure = 1.0

                            # 🎨 PEN PRESSURE: Use shared helper so the final width
                            # matches the live preview (1.5x at full pressure).
                            avg_pressure = (start_pressure + end_pressure) / 2.0
                            dynamic_thickness = self._pressure_to_thickness(avg_pressure)

                            # Create a pen with the correct thickness for this specific segment
                            pen = QPen(self.line_color, dynamic_thickness, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
                            painter.setPen(pen)

                            # Apply SAME coordinate transformation as real-time drawing
                            display_start_x = int(start_x * scale_x) + draw_x
                            display_start_y = int(start_y * scale_y) + draw_y
                            display_end_x = int(end_x * scale_x) + draw_x
                            display_end_y = int(end_y * scale_y) + draw_y

                            # Apply rotation and flips (same as real-time drawing)
                            if self.rotation_angle != 0 or self.flipped_h or self.flipped_v:
                                # Transform start point
                                if self.rotation_angle == 90:
                                    temp_start_x = original_size.width() * scale_x - display_start_y + draw_x
                                    temp_start_y = display_start_x - draw_x + draw_y
                                elif self.rotation_angle == 180:
                                    temp_start_x = original_size.width() * scale_x - display_start_x + draw_x
                                    temp_start_y = original_size.height() * scale_y - display_start_y + draw_y
                                elif self.rotation_angle == 270:
                                    temp_start_x = display_start_y - draw_y + draw_x
                                    temp_start_y = original_size.height() * scale_y - display_start_x + draw_x
                                else:
                                    temp_start_x = display_start_x
                                    temp_start_y = display_start_y

                                # Transform end point
                                if self.rotation_angle == 90:
                                    temp_end_x = original_size.width() * scale_x - display_end_y + draw_x
                                    temp_end_y = display_end_x - draw_x + draw_y
                                elif self.rotation_angle == 180:
                                    temp_end_x = original_size.width() * scale_x - display_end_x + draw_x
                                    temp_end_y = original_size.height() * scale_y - display_end_y + draw_y
                                elif self.rotation_angle == 270:
                                    temp_end_x = display_end_y - draw_y + draw_x
                                    temp_end_y = original_size.height() * scale_y - display_end_x + draw_x
                                else:
                                    temp_end_x = display_end_x
                                    temp_end_y = display_end_y

                                # Apply flips
                                if self.flipped_h:
                                    temp_start_x = (label_size.width() - temp_start_x + draw_x) - draw_x + draw_x
                                    temp_end_x = (label_size.width() - temp_end_x + draw_x) - draw_x + draw_x
                                if self.flipped_v:
                                    temp_start_y = (label_size.height() - temp_start_y + draw_y) - draw_y + draw_y
                                    temp_end_y = (label_size.height() - temp_end_y + draw_y) - draw_y + draw_y

                                display_start_x, display_start_y = temp_start_x, temp_start_y
                                display_end_x, display_end_y = temp_end_x, temp_end_y

                            painter.drawLine(display_start_x, display_start_y, display_end_x, display_end_y)
                    else:
                        # ⚡ FAST: Use optimized Bresenham for maximum speed
                        painter.setRenderHint(QPainter.Antialiasing, False)

                        # Convert stroke points to display coordinates
                        for i in range(len(stroke) - 1):
                            # 🎨 PEN PRESSURE: Handle both old 2-tuple and new 3-tuple formats
                            if len(stroke[i]) == 3:
                                start_x, start_y, start_pressure = stroke[i]
                                end_x, end_y, end_pressure = stroke[i + 1]
                                # 🎨 Shared helper so final width matches the live preview
                                segment_pressure = (start_pressure + end_pressure) / 2.0
                                segment_thickness = self._pressure_to_thickness(segment_pressure)
                            else:
                                start_x, start_y = stroke[i]
                                end_x, end_y = stroke[i + 1]
                                # Scale regular thickness with zoom factor too
                                segment_thickness = max(1, self.line_thickness)

                            # Set pressure-based pen thickness for this segment
                            painter.setPen(QPen(self.line_color, segment_thickness, Qt.SolidLine))

                            # Apply SAME coordinate transformation as real-time drawing
                            display_start_x = int(start_x * scale_x) + draw_x
                            display_start_y = int(start_y * scale_y) + draw_y
                            display_end_x = int(end_x * scale_x) + draw_x
                            display_end_y = int(end_y * scale_y) + draw_y

                            # Apply rotation and flips (same as real-time drawing)
                            if self.rotation_angle != 0 or self.flipped_h or self.flipped_v:
                                # Transform start point
                                if self.rotation_angle == 90:
                                    temp_start_x = original_size.width() * scale_x - display_start_y + draw_x
                                    temp_start_y = display_start_x - draw_x + draw_y
                                elif self.rotation_angle == 180:
                                    temp_start_x = original_size.width() * scale_x - display_start_x + draw_x
                                    temp_start_y = original_size.height() * scale_y - display_start_y + draw_y
                                elif self.rotation_angle == 270:
                                    temp_start_x = display_start_y - draw_y + draw_x
                                    temp_start_y = original_size.height() * scale_y - display_start_x + draw_x
                                else:
                                    temp_start_x = display_start_x
                                    temp_start_y = display_start_y

                                # Transform end point
                                if self.rotation_angle == 90:
                                    temp_end_x = original_size.width() * scale_x - display_end_y + draw_x
                                    temp_end_y = display_end_x - draw_x + draw_y
                                elif self.rotation_angle == 180:
                                    temp_end_x = original_size.width() * scale_x - display_end_x + draw_x
                                    temp_end_y = original_size.height() * scale_y - display_end_y + draw_y
                                elif self.rotation_angle == 270:
                                    temp_end_x = display_end_y - draw_y + draw_x
                                    temp_end_y = original_size.height() * scale_y - display_end_x + draw_x
                                else:
                                    temp_end_x = display_end_x
                                    temp_end_y = display_end_y

                                # Apply flips
                                if self.flipped_h:
                                    temp_start_x = (label_size.width() - temp_start_x + draw_x) - draw_x + draw_x
                                    temp_end_x = (label_size.width() - temp_end_x + draw_x) - draw_x + draw_x
                                if self.flipped_v:
                                    temp_start_y = (label_size.height() - temp_start_y + draw_y) - draw_y + draw_y
                                    temp_end_y = (label_size.height() - temp_end_y + draw_y) - draw_y + draw_y

                                display_start_x, display_start_y = temp_start_x, temp_start_y
                                display_end_x, display_end_y = temp_end_x, temp_end_y

                            # Draw the line segment
                            painter.drawLine(display_start_x, display_start_y, display_end_x, display_end_y)
            
            painter.end()
            # 🧽 Eraser: restore the clean (line-free) image inside erase strokes,
            # punching holes through only the line layer (not the photo).
            self._last_display_scale_x = scale_x
            # Strokes drawn AFTER the most recent erase must survive the erase holes,
            # so you can draw again over an erased area. Composite them onto the
            # "revealed" image that the eraser exposes inside its holes.
            reveal_pixmap = scaled_pixmap
            if self.erase_strokes and self.current_erase_stroke is None and self._erase_state_marks:
                mark = self._erase_state_marks[-1]
                post_strokes = self.drawn_free_strokes[mark.get('free_strokes', 0):]
                post_lines = self.drawn_free_lines[mark.get('free_lines', 0):]
                if post_strokes or post_lines:
                    reveal_pixmap = scaled_pixmap.copy()
                    self._draw_post_erase_overlay(
                        reveal_pixmap, post_strokes, post_lines,
                        scale_x, scale_y, draw_x, draw_y, original_size)
            self._apply_erase_holes(final_pixmap, reveal_pixmap, scale_x, scale_y, draw_x, draw_y, original_size)
            scaled_pixmap = final_pixmap
        
        # Handle image visibility toggle
        if not self.image_visible:
            # Create a blank pixmap with the same size but keep lines visible
            blank_pixmap = QPixmap(scaled_pixmap.size())
            blank_pixmap.fill(Qt.black)  # Fill with black background
            
            # If there are lines, draw them on the blank pixmap
            if self.lines_visible and (self.drawn_lines or self.drawn_horizontal_lines or self.drawn_free_lines or self.drawn_free_strokes):
                painter = QPainter(blank_pixmap)
                painter.setRenderHint(QPainter.Antialiasing, False)
                # Apply zoom scaling to line thickness for consistent visual appearance
                zoom_scaled_thickness = max(1, self.line_thickness)
                painter.setPen(QPen(self.line_color, zoom_scaled_thickness, Qt.SolidLine))
                
                # Use the same coordinate transformations as above
                original_size = self.original_pixmap.size()
                label_size = self.image_label.size()
                zoom_factor = self.image_label.zoom_factor
                
                base_scaled = self.original_pixmap.scaled(label_size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                zoomed_width = int(base_scaled.width() * zoom_factor)
                zoomed_height = int(base_scaled.height() * zoom_factor)
                
                draw_x = (label_size.width() - zoomed_width) // 2 + int(self.image_label.pan_offset_x)
                draw_y = (label_size.height() - zoomed_height) // 2 + int(self.image_label.pan_offset_y)
                
                scale_x = zoomed_width / original_size.width()
                scale_y = zoomed_height / original_size.height()
                
                # Draw vertical lines
                for x in self.drawn_lines:
                    display_x = int(x * scale_x) + draw_x
                    if 0 <= display_x < blank_pixmap.width():
                        painter.drawLine(display_x, 0, display_x, blank_pixmap.height())
                
                # Draw horizontal lines
                for y in self.drawn_horizontal_lines:
                    display_y = int(y * scale_y) + draw_y
                    if 0 <= display_y < blank_pixmap.height():
                        painter.drawLine(0, display_y, blank_pixmap.width(), display_y)
                
                # Draw free lines
                for line in self.drawn_free_lines:
                    start_x, start_y = line['start']
                    end_x, end_y = line['end']
                    display_start_x = int(start_x * scale_x) + draw_x
                    display_start_y = int(start_y * scale_y) + draw_y
                    display_end_x = int(end_x * scale_x) + draw_x
                    display_end_y = int(end_y * scale_y) + draw_y
                    painter.drawLine(display_start_x, display_start_y, display_end_x, display_end_y)
                
                # Draw free strokes
                if self.line_antialiasing and self.line_thickness > 1:
                    # ✨ SMOOTH: Use antialiasing for professional quality
                    painter.setRenderHint(QPainter.Antialiasing, True)
                    painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
                    
                    for stroke in self.drawn_free_strokes:
                        if len(stroke) < 2:
                            continue
                        for i in range(len(stroke) - 1):
                            # 🎨 PEN PRESSURE: Handle both old 2-tuple and new 3-tuple formats
                            if len(stroke[i]) == 3:
                                start_x, start_y, start_pressure = stroke[i]
                                end_x, end_y, end_pressure = stroke[i + 1]
                                # 🎨 Shared helper so final width matches the live preview
                                segment_pressure = (start_pressure + end_pressure) / 2.0
                                segment_thickness = self._pressure_to_thickness(segment_pressure)
                            else:
                                start_x, start_y = stroke[i]
                                end_x, end_y = stroke[i + 1]
                                # Scale regular thickness with zoom factor too
                                segment_thickness = max(1, self.line_thickness)
                            
                            # Set pressure-based pen for this segment
                            pen = QPen(self.line_color, segment_thickness, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
                            painter.setPen(pen)
                            
                            display_start_x = int(start_x * scale_x) + draw_x
                            display_start_y = int(start_y * scale_y) + draw_y
                            display_end_x = int(end_x * scale_x) + draw_x
                            display_end_y = int(end_y * scale_y) + draw_y
                            painter.drawLine(display_start_x, display_start_y, display_end_x, display_end_y)
                else:
                    # ⚡ FAST: Direct rendering for maximum speed
                    painter.setRenderHint(QPainter.Antialiasing, False)
                    
                    for stroke in self.drawn_free_strokes:
                        if len(stroke) < 2:
                            continue
                        for i in range(len(stroke) - 1):
                            # 🎨 PEN PRESSURE: Handle both old 2-tuple and new 3-tuple formats
                            if len(stroke[i]) == 3:
                                start_x, start_y, start_pressure = stroke[i]
                                end_x, end_y, end_pressure = stroke[i + 1]
                                # 🎨 Shared helper so final width matches the live preview
                                segment_pressure = (start_pressure + end_pressure) / 2.0
                                segment_thickness = self._pressure_to_thickness(segment_pressure)
                            else:
                                start_x, start_y = stroke[i]
                                end_x, end_y = stroke[i + 1]
                                # Scale regular thickness with zoom factor too
                                segment_thickness = max(1, self.line_thickness)
                            
                            # Set pressure-based pen for this segment
                            painter.setPen(QPen(self.line_color, segment_thickness, Qt.SolidLine))
                            
                            display_start_x = int(start_x * scale_x) + draw_x
                            display_start_y = int(start_y * scale_y) + draw_y
                            display_end_x = int(end_x * scale_x) + draw_x
                            display_end_y = int(end_y * scale_y) + draw_y
                            painter.drawLine(display_start_x, display_start_y, display_end_x, display_end_y)
                
                painter.end()
                # 🧽 Eraser holes reveal the black background in image-hidden mode
                _clean_black = QPixmap(blank_pixmap.size())
                _clean_black.fill(Qt.black)
                self._apply_erase_holes(blank_pixmap, _clean_black, scale_x, scale_y, draw_x, draw_y, original_size)
            
            scaled_pixmap = blank_pixmap
        
        # Display the final scaled pixmap
        scaled_pixmap = self._apply_fixed_overlays_to_pixmap(scaled_pixmap)
        self.image_label.setPixmap(scaled_pixmap)
        self.image_label.setToolTip("")
        self.current_image = img_path

    def _scale_pixmap(self, pixmap, img_path):
        """Scale pixmap for display with zoom and pan support"""
        size = self.image_label.size()
        zoom_factor = self.image_label.zoom_factor
        pan_x = self.image_label.pan_offset_x
        pan_y = self.image_label.pan_offset_y
        
        # Create cache key including zoom and pan for proper caching
        scale_key = f"{img_path}_{size.width()}_{size.height()}_{zoom_factor}_{pan_x}_{pan_y}"
        
        # UNIFIED coordinate system for ALL zoom levels - no special case for 1.0
        # Always calculate the base scaled size first
        base_scaled = pixmap.scaled(size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        
        # Apply zoom factor to get the final zoomed size
        zoomed_width = int(base_scaled.width() * zoom_factor)
        zoomed_height = int(base_scaled.height() * zoom_factor)
        zoomed_size = QSize(zoomed_width, zoomed_height)
        
        # Scale the original pixmap to the zoomed size
        zoomed_pixmap = pixmap.scaled(zoomed_size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        
        # Create a pixmap the size of the label
        final_pixmap = QPixmap(size)
        final_pixmap.fill(Qt.black)  # Fill with black background
        
        # Calculate the position to draw the zoomed image
        # Center the zoomed image in the label, then apply pan offset
        draw_x = (size.width() - zoomed_width) // 2 + int(pan_x)
        draw_y = (size.height() - zoomed_height) // 2 + int(pan_y)
        
        # Draw the zoomed image onto the final pixmap
        painter = QPainter(final_pixmap)
        painter.drawPixmap(draw_x, draw_y, zoomed_pixmap)
        painter.end()
        
        scaled = final_pixmap
        
        self.last_size = size
        return scaled

    def _manage_cache(self, cache_dict, key, value):
        """Manage cache size with LRU-like behavior"""
        if len(cache_dict) >= self.max_cache_size:
            # Remove oldest entries
            keys_to_remove = list(cache_dict.keys())[:-self.max_cache_size//2]
            for k in keys_to_remove:
                del cache_dict[k]
            # Force garbage collection periodically
            if len(cache_dict) % 10 == 0:
                gc.collect()
        cache_dict[key] = value

    def load_cube_lut(self, file_path):
        """Load a CUBE format LUT file with safety checks - OPTIMIZED VERSION"""
        try:
            # Check LUT cache first for instant loading
            if file_path in self.lut_cache:
                print(f"LUT loaded from cache: {os.path.basename(file_path)}")
                self.status.showMessage(f"LUT loaded from cache: {os.path.basename(file_path)}")
                return self.lut_cache[file_path]
            
            # Check file size to prevent loading extremely large LUTs
            file_size = os.path.getsize(file_path)
            max_file_size = 50 * 1024 * 1024  # 50MB limit
            
            if file_size > max_file_size:
                print(f"LUT file too large: {file_size / (1024*1024):.1f}MB > {max_file_size / (1024*1024):.1f}MB")
                self.status.showMessage(f"LUT file too large: {os.path.basename(file_path)}")
                return None
            
            # Show loading status
            self.status.showMessage(f"Loading LUT: {os.path.basename(file_path)}...")
            QApplication.processEvents()  # Allow UI to update
            
            # OPTIMIZATION 1: Read entire file at once (faster than readlines)
            with open(file_path, 'r', encoding='utf-8', buffering=8192) as f:
                content = f.read()
            
            # OPTIMIZATION 2: Split lines once and filter in one pass
            lines = [line.strip() for line in content.split('\n') if line.strip() and not line.strip().startswith('#')]
            
            lut_size = 32  # Default size
            
            # OPTIMIZATION 3: Find LUT size quickly without parsing every line
            for line in lines:
                if line.startswith('LUT_3D_SIZE'):
                    lut_size = int(line.split()[-1])
                    # Safety check for LUT size
                    if lut_size > 256:
                        print(f"LUT size too large: {lut_size} > 256")
                        self.status.showMessage(f"LUT size too large: {lut_size}")
                        return None
                    break
            
            # OPTIMIZATION 4: Pre-allocate list for better performance
            expected_size = lut_size ** 3
            lut_data = []
            # Note: Python lists don't have reserve(), but we can hint the expected size
            
            # OPTIMIZATION 5: Batch process data lines only (skip metadata)
            data_lines = [line for line in lines 
                         if not line.startswith(('TITLE', 'LUT_3D_SIZE', 'DOMAIN_MIN', 'DOMAIN_MAX')) 
                         and ' ' in line]
            
            # OPTIMIZATION 6: Fast parsing with list comprehension and minimal error checking
            try:
                for line in data_lines:
                    parts = line.split()
                    if len(parts) >= 3:
                        # Direct float conversion - faster than try/except in loop
                        lut_data.append((float(parts[0]), float(parts[1]), float(parts[2])))
                        
                        # Early break when we have enough data
                        if len(lut_data) >= expected_size:
                            break
                            
            except (ValueError, IndexError) as e:
                print(f"Error parsing LUT data: {e}")
                self.status.showMessage(f"Error parsing LUT: {os.path.basename(file_path)}")
                return None
            
            # Verify we have the expected amount of data
            if len(lut_data) != expected_size:
                print(f"Warning: LUT size mismatch. Expected {expected_size}, got {len(lut_data)}")
                self.status.showMessage(f"Invalid LUT format: {os.path.basename(file_path)}")
                return None
            
            # OPTIMIZATION 7: Create optimized LUT structure
            lut_result = {
                'size': lut_size,
                'data': lut_data,
                'file_path': file_path,  # Store for cache identification
                'file_size': file_size   # Store for memory management
            }
            
            # OPTIMIZATION 8: Cache the loaded LUT for instant reuse
            self.lut_cache[file_path] = lut_result
            
            # OPTIMIZATION 9: Manage cache size to prevent memory issues
            if len(self.lut_cache) > 10:  # Keep max 10 LUTs cached
                # Remove oldest cache entries
                oldest_key = next(iter(self.lut_cache))
                del self.lut_cache[oldest_key]
                print(f"LUT cache: Removed {os.path.basename(oldest_key)} to free memory")
            
            print(f"LUT loaded successfully: {lut_size}³ ({len(lut_data)} entries)")
            self.status.showMessage(f"LUT loaded: {os.path.basename(file_path)} ({lut_size}³)")
            
            return lut_result
            
        except Exception as e:
            print(f"Error loading CUBE LUT {file_path}: {e}")
            self.status.showMessage(f"Error loading LUT: {os.path.basename(file_path)}")
            return None

    def apply_lut_to_image(self, pixmap, lut, strength=100):
        """Apply a 3D LUT to a pixmap with specified strength - SMART CACHING VERSION"""
        if not lut or not pixmap or pixmap.isNull():
            return pixmap
        
        try:
            # Create improved cache key that includes image dimensions for zoom awareness
            cache_key = f"lut_{id(pixmap)}_{pixmap.width()}x{pixmap.height()}_{lut['file_path']}_{strength}"
            
            # Check if we already processed this exact combination
            if hasattr(self, '_lut_process_cache') and cache_key in self._lut_process_cache:
                return self._lut_process_cache[cache_key]
            
            # Initialize LUT process cache if not exists
            if not hasattr(self, '_lut_process_cache'):
                self._lut_process_cache = {}
            
            # Adaptive processing based on image size
            image = pixmap.toImage()
            if image.isNull():
                return pixmap
            
            original_size = image.size()
            image_pixels = image.width() * image.height()
            
            # 🎨 IMPROVED QUALITY: Higher resolution limits for better quality
            # Dynamic max size based on image complexity - significantly increased for better quality
            if image_pixels > 16000000:  # Extremely large (>16MP)
                max_lut_size = 3840  # 4K resolution - still very high quality
            elif image_pixels > 8000000:  # Very large (>8MP)
                max_lut_size = 4096  # Full 4K quality
            elif image_pixels > 4000000:  # Large (>4MP)
                max_lut_size = 3200  # Higher quality for large images
            else:
                max_lut_size = 4096  # No scaling for smaller images - preserve full quality
            
            # Only scale down extremely large images, preserve quality as much as possible
            if image.width() > max_lut_size or image.height() > max_lut_size:
                scale_factor = min(max_lut_size / image.width(), max_lut_size / image.height())
                scaled_size = QSize(int(image.width() * scale_factor), int(image.height() * scale_factor))
                # 🎨 QUALITY: Use SmoothTransformation for highest quality scaling
                image = image.scaled(scaled_size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                is_scaled = True
            else:
                is_scaled = False
            
            # Convert to RGB32 format for processing
            if image.format() != image.Format.Format_RGB32:
                image = image.convertToFormat(image.Format.Format_RGB32)
            
            width = image.width()
            height = image.height()
            lut_size = lut['size']
            lut_data = lut['data']
            strength_factor = strength / 100.0
            
            # GPU ACCELERATION: Try GPU processing first
            if self.gpu_processor.is_available():
                print(f"Applying LUT using GPU acceleration ({width}x{height}) - LUT size: {lut_size}³, strength: {strength_factor:.2f}")
                gpu_result = self.gpu_processor.apply_lut_gpu(image, lut_data, lut_size, strength_factor)
                if gpu_result is not None:
                    print("✓ GPU LUT processing successful - colors should now be correct")
                    # GPU processing successful
                    if is_scaled:
                        # 🎨 QUALITY: Use high-quality scaling when upscaling back to original size
                        gpu_result = gpu_result.scaled(original_size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                    
                    result_pixmap = QPixmap.fromImage(gpu_result)
                    
                    # 🎨 QUALITY: Smart cache management with higher limits for quality processing
                    # Reduce cache size since we're processing higher resolution images
                    max_cache_size = 4 if image_pixels < 2000000 else 2
                    if len(self._lut_process_cache) > max_cache_size:
                        oldest_key = next(iter(self._lut_process_cache))
                        del self._lut_process_cache[oldest_key]
                    
                    self._lut_process_cache[cache_key] = result_pixmap
                    
                    # ZOOM OPTIMIZATION: Cache the final processed image for fast zoom
                    self._last_processed_image = gpu_result.copy()
                    self._last_processed_has_lut = True if (self.lut_enabled and self.current_lut and self.lut_strength>0) else False
                    
                    return result_pixmap
                else:
                    print("✗ GPU LUT processing failed, falling back to CPU")
                    print("GPU processing failed, falling back to CPU")
            
            # FALLBACK: CPU processing when GPU is not available or fails
            
            # Choose processing method based on scaled image size
            scaled_pixels = width * height
            if scaled_pixels > 1500000:  # Still large after scaling
                # Use optimized fast mode for very large images
                self._apply_lut_optimized(image, lut_data, lut_size, strength_factor)
            else:
                # Use high quality mode for moderate sized images
                self._apply_lut_with_interpolation(image, lut_data, lut_size, strength_factor)
            
            # If we scaled down, scale back up to original size with quality
            if is_scaled:
                image = image.scaled(original_size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            
            result_pixmap = QPixmap.fromImage(image)
            
            # 🎨 QUALITY: Smart cache management - smaller cache for higher quality processing
            max_cache_size = 4 if image_pixels < 2000000 else 2
            if len(self._lut_process_cache) > max_cache_size:
                # Remove oldest entries
                oldest_key = next(iter(self._lut_process_cache))
                del self._lut_process_cache[oldest_key]
            
            self._lut_process_cache[cache_key] = result_pixmap
            
            # ZOOM OPTIMIZATION: Cache the final processed image for fast zoom  
            self._last_processed_image = image.copy()
            self._last_processed_has_lut = True if (self.lut_enabled and self.current_lut and self.lut_strength>0) else False
            
            return result_pixmap
            
        except Exception as e:
            print(f"Error applying LUT: {e}")
            return pixmap

    def clear_lut_cache(self):
        """Clear LUT processing cache to free memory and force GPU recompilation"""
        if hasattr(self, '_lut_process_cache'):
            self._lut_process_cache.clear()
        
        # Clear zoom optimization cache
        if hasattr(self, '_last_processed_image'):
            self._last_processed_image = None
        self._last_processed_has_lut = False

        # Invalidate the pre-built GIF LUT table so it is rebuilt on next frame
        if hasattr(self, '_gif_np_lut_key'):
            del self._gif_np_lut_key
        
        # Also clear enhancement and scaled caches to ensure fresh processing
        self.enhancement_cache.clear()
        self.scaled_cache.clear()
        
        # GPU processor will reinitialize on next use automatically via lazy init
        if hasattr(self, 'gpu_processor') and self.gpu_processor:
            self.gpu_processor._initialized = False
            self.gpu_processor.gpu_enabled = False
            
    def _apply_lut_optimized(self, image, lut_data, lut_size, strength_factor):
        """Optimized LUT application - faster but still good quality with progress"""
        width = image.width()
        height = image.height()
        
        # Use larger chunks for better performance
        chunk_size = 32  # Larger chunks for speed
        total_chunks = (height + chunk_size - 1) // chunk_size
        processed_chunks = 0
        
        for y_start in range(0, height, chunk_size):
            y_end = min(y_start + chunk_size, height)
            
            for y in range(y_start, y_end):
                scan_line = image.scanLine(y)
                
                # Process every 2nd pixel for speed, then interpolate
                for x in range(0, width, 2):
                    offset = x * 4
                    
                    # Read pixel bytes (BGRA order in Qt)
                    b = scan_line[offset]
                    g = scan_line[offset + 1] 
                    r = scan_line[offset + 2]
                    a = scan_line[offset + 3]
                    
                    # Normalize to 0-1 range
                    r_norm = r / 255.0
                    g_norm = g / 255.0
                    b_norm = b / 255.0
                    
                    # Use faster interpolation
                    lut_result = self._interpolate_lut_fast(r_norm, g_norm, b_norm, lut_data, lut_size)
                    
                    # Blend with original using strength factor
                    final_r = r_norm * (1.0 - strength_factor) + lut_result[0] * strength_factor
                    final_g = g_norm * (1.0 - strength_factor) + lut_result[1] * strength_factor
                    final_b = b_norm * (1.0 - strength_factor) + lut_result[2] * strength_factor
                    
                    # Clamp and convert back to bytes
                    final_r = max(0, min(255, int(final_r * 255 + 0.5)))
                    final_g = max(0, min(255, int(final_g * 255 + 0.5)))
                    final_b = max(0, min(255, int(final_b * 255 + 0.5)))
                    
                    # Write back to image (BGRA order)
                    scan_line[offset] = final_b
                    scan_line[offset + 1] = final_g
                    scan_line[offset + 2] = final_r
                    scan_line[offset + 3] = a
                    
                    # Fill the next pixel with interpolated values for speed
                    if x + 1 < width:
                        next_offset = (x + 1) * 4
                        scan_line[next_offset] = final_b
                        scan_line[next_offset + 1] = final_g
                        scan_line[next_offset + 2] = final_r
                        scan_line[next_offset + 3] = scan_line[next_offset + 3]  # Keep original alpha
            
            # Progress callback every few chunks to keep UI responsive
            processed_chunks += 1
            if hasattr(self, '_lut_progress_callback') and self._lut_progress_callback:
                if processed_chunks % 3 == 0:  # Update every 3 chunks
                    self._lut_progress_callback()  # Allow UI updates

    def _apply_lut_with_interpolation(self, image, lut_data, lut_size, strength_factor):
        """High-quality LUT application with trilinear interpolation"""
        width = image.width()
        height = image.height()
        
        # Process in smaller chunks for better performance while maintaining quality
        chunk_size = 16  # Smaller chunks for better cache performance
        
        for y_start in range(0, height, chunk_size):
            y_end = min(y_start + chunk_size, height)
            
            for y in range(y_start, y_end):
                scan_line = image.scanLine(y)
                
                for x in range(width):
                    offset = x * 4
                    
                    # Read pixel bytes (BGRA order in Qt)
                    b = scan_line[offset]
                    g = scan_line[offset + 1] 
                    r = scan_line[offset + 2]
                    a = scan_line[offset + 3]  # Preserve alpha
                    
                    # Normalize to 0-1 range
                    r_norm = r / 255.0
                    g_norm = g / 255.0
                    b_norm = b / 255.0
                    
                    # Apply LUT with trilinear interpolation for quality
                    lut_result = self._interpolate_lut_quality(r_norm, g_norm, b_norm, lut_data, lut_size)
                    
                    # Blend with original using strength factor
                    final_r = r_norm * (1.0 - strength_factor) + lut_result[0] * strength_factor
                    final_g = g_norm * (1.0 - strength_factor) + lut_result[1] * strength_factor
                    final_b = b_norm * (1.0 - strength_factor) + lut_result[2] * strength_factor
                    
                    # Clamp and convert back to bytes
                    final_r = max(0, min(255, int(final_r * 255 + 0.5)))
                    final_g = max(0, min(255, int(final_g * 255 + 0.5)))
                    final_b = max(0, min(255, int(final_b * 255 + 0.5)))
                    
                    # Write back to image (BGRA order)
                    scan_line[offset] = final_b
                    scan_line[offset + 1] = final_g
                    scan_line[offset + 2] = final_r
                    scan_line[offset + 3] = a  # Preserve alpha
    
    def _interpolate_lut_quality(self, r, g, b, lut_data, lut_size):
        """High-quality trilinear interpolation in 3D LUT"""
        # Scale to LUT coordinate space
        r_scaled = r * (lut_size - 1)
        g_scaled = g * (lut_size - 1)
        b_scaled = b * (lut_size - 1)
        
        # Get integer coordinates with bounds checking
        r_low = max(0, min(int(r_scaled), lut_size - 1))
        g_low = max(0, min(int(g_scaled), lut_size - 1))
        b_low = max(0, min(int(b_scaled), lut_size - 1))
        
        r_high = min(r_low + 1, lut_size - 1)
        g_high = min(g_low + 1, lut_size - 1)
        b_high = min(b_low + 1, lut_size - 1)
        
        # Get fractional parts for smooth interpolation
        r_frac = r_scaled - r_low
        g_frac = g_scaled - g_low
        b_frac = b_scaled - b_low
        
        # Helper function to safely get LUT value
        def get_lut_value(ri, gi, bi):
            try:
                idx = ri + gi * lut_size + bi * lut_size * lut_size
                if 0 <= idx < len(lut_data):
                    return lut_data[idx]
                else:
                    return (r, g, b)  # Fallback to original
            except (IndexError, ValueError):
                return (r, g, b)  # Fallback to original
        
        # Get 8 corner values for trilinear interpolation
        c000 = get_lut_value(r_low, g_low, b_low)
        c001 = get_lut_value(r_low, g_low, b_high)
        c010 = get_lut_value(r_low, g_high, b_low)
        c011 = get_lut_value(r_low, g_high, b_high)
        c100 = get_lut_value(r_high, g_low, b_low)
        c101 = get_lut_value(r_high, g_low, b_high)
        c110 = get_lut_value(r_high, g_high, b_low)
        c111 = get_lut_value(r_high, g_high, b_high)
        
        # Linear interpolation helper
        def lerp(a, b, t):
            return (
                a[0] * (1.0 - t) + b[0] * t,
                a[1] * (1.0 - t) + b[1] * t,
                a[2] * (1.0 - t) + b[2] * t
            )
        
        # Trilinear interpolation
        # Interpolate along b axis
        c00 = lerp(c000, c001, b_frac)
        c01 = lerp(c010, c011, b_frac)
        c10 = lerp(c100, c101, b_frac)
        c11 = lerp(c110, c111, b_frac)
        
        # Interpolate along g axis
        c0 = lerp(c00, c01, g_frac)
        c1 = lerp(c10, c11, g_frac)
        
        # Final interpolation along r axis
        result = lerp(c0, c1, r_frac)
        
        return result
        """Ultra-fast LUT application using lookup tables and nearest neighbor"""
        width = image.width()
        height = image.height()
        
        # Pre-build lookup table for common colors to avoid repeated calculations
        # Process in chunks for better cache performance
        chunk_size = 32  # Process 32 rows at a time
        
        for y_start in range(0, height, chunk_size):
            y_end = min(y_start + chunk_size, height)
            
            for y in range(y_start, y_end):
                scan_line = image.scanLine(y)
                
                # Process pixels in groups of 8 for better performance
                for x in range(0, width, 8):
                    x_end = min(x + 8, width)
                    
                    for px in range(x, x_end):
                        offset = px * 4
                        
                        # Read pixel bytes (BGRA order in Qt)
                        b = scan_line[offset]
                        g = scan_line[offset + 1] 
                        r = scan_line[offset + 2]
                        
                        # Use nearest neighbor for speed (no interpolation)
                        # Quantize to LUT grid directly
                        lut_r = min(int((r / 255.0) * (lut_size - 1) + 0.5), lut_size - 1)
                        lut_g = min(int((g / 255.0) * (lut_size - 1) + 0.5), lut_size - 1)
                        lut_b = min(int((b / 255.0) * (lut_size - 1) + 0.5), lut_size - 1)
                        
                        # Direct lookup - no interpolation for speed
                        idx = lut_r + lut_g * lut_size + lut_b * lut_size * lut_size
                        if 0 <= idx < len(lut_data):
                            new_r, new_g, new_b = lut_data[idx]
                        else:
                            new_r, new_g, new_b = r / 255.0, g / 255.0, b / 255.0
                        
                        # Apply strength blending (simplified)
                        if strength_factor < 1.0:
                            orig_r, orig_g, orig_b = r / 255.0, g / 255.0, b / 255.0
                            new_r = orig_r + (new_r - orig_r) * strength_factor
                            new_g = orig_g + (new_g - orig_g) * strength_factor
                            new_b = orig_b + (new_b - orig_b) * strength_factor
                        
                        # Convert back and clamp
                        final_r = max(0, min(255, int(new_r * 255)))
                        final_g = max(0, min(255, int(new_g * 255)))
                        final_b = max(0, min(255, int(new_b * 255)))
                        
                        # Write back
                        scan_line[offset] = final_b
                        scan_line[offset + 1] = final_g
                        scan_line[offset + 2] = final_r

    def _apply_lut_reduced_sampling(self, image, lut_data, lut_size, strength_factor):
        """Apply LUT with reduced sampling for very large LUTs"""
        width = image.width()
        height = image.height()
        
        # For large LUTs, sample every other pixel to maintain speed
        sample_rate = 2 if lut_size > 64 else 1
        
        for y in range(0, height, sample_rate):
            scan_line = image.scanLine(y)
            
            for x in range(0, width, sample_rate):
                offset = x * 4
                
                # Read pixel bytes
                b = scan_line[offset]
                g = scan_line[offset + 1] 
                r = scan_line[offset + 2]
                
                # Simple nearest neighbor lookup
                lut_r = min(int((r / 255.0) * (lut_size - 1)), lut_size - 1)
                lut_g = min(int((g / 255.0) * (lut_size - 1)), lut_size - 1)
                lut_b = min(int((b / 255.0) * (lut_size - 1)), lut_size - 1)
                
                idx = lut_r + lut_g * lut_size + lut_b * lut_size * lut_size
                if 0 <= idx < len(lut_data):
                    new_r, new_g, new_b = lut_data[idx]
                    
                    # Apply strength
                    if strength_factor < 1.0:
                        orig_r, orig_g, orig_b = r / 255.0, g / 255.0, b / 255.0
                        new_r = orig_r + (new_r - orig_r) * strength_factor
                        new_g = orig_g + (new_g - orig_g) * strength_factor
                        new_b = orig_b + (new_b - orig_b) * strength_factor
                    
                    # Convert and write back
                    final_r = max(0, min(255, int(new_r * 255)))
                    final_g = max(0, min(255, int(new_g * 255)))
                    final_b = max(0, min(255, int(new_b * 255)))
                    
                    scan_line[offset] = final_b
                    scan_line[offset + 1] = final_g
                    scan_line[offset + 2] = final_r
                    
                    # Fill in skipped pixels if sampling
                    if sample_rate > 1:
                        # Copy to adjacent pixels for smoother result
                        for dx in range(1, min(sample_rate, width - x)):
                            next_offset = (x + dx) * 4
                            scan_line[next_offset] = final_b
                            scan_line[next_offset + 1] = final_g
                            scan_line[next_offset + 2] = final_r
            
            # Fill in skipped rows if sampling
            if sample_rate > 1 and y + 1 < height:
                next_scan_line = image.scanLine(y + 1)
                # Copy the processed row to the next row for smoother result
                for i in range(width * 4):
                    next_scan_line[i] = scan_line[i]

    def _interpolate_lut_fast(self, r, g, b, lut_data, lut_size):
        """Optimized trilinear interpolation in 3D LUT"""
        # Scale to LUT coordinate space
        r_scaled = r * (lut_size - 1)
        g_scaled = g * (lut_size - 1)
        b_scaled = b * (lut_size - 1)
        
        # Get integer coordinates with bounds checking
        r_low = max(0, min(int(r_scaled), lut_size - 1))
        g_low = max(0, min(int(g_scaled), lut_size - 1))
        b_low = max(0, min(int(b_scaled), lut_size - 1))
        
        r_high = min(r_low + 1, lut_size - 1)
        g_high = min(g_low + 1, lut_size - 1)
        b_high = min(b_low + 1, lut_size - 1)
        
        # Get fractional parts
        r_frac = r_scaled - r_low
        g_frac = g_scaled - g_low
        b_frac = b_scaled - b_low
        
        # Pre-calculate indices for better performance
        try:
            idx000 = r_low + g_low * lut_size + b_low * lut_size * lut_size
            idx001 = r_low + g_low * lut_size + b_high * lut_size * lut_size
            idx010 = r_low + g_high * lut_size + b_low * lut_size * lut_size
            idx011 = r_low + g_high * lut_size + b_high * lut_size * lut_size
            idx100 = r_high + g_low * lut_size + b_low * lut_size * lut_size
            idx101 = r_high + g_low * lut_size + b_high * lut_size * lut_size
            idx110 = r_high + g_high * lut_size + b_low * lut_size * lut_size
            idx111 = r_high + g_high * lut_size + b_high * lut_size * lut_size
            
            # Get 8 corner values with bounds checking
            c000 = lut_data[idx000] if 0 <= idx000 < len(lut_data) else (r, g, b)
            c001 = lut_data[idx001] if 0 <= idx001 < len(lut_data) else (r, g, b)
            c010 = lut_data[idx010] if 0 <= idx010 < len(lut_data) else (r, g, b)
            c011 = lut_data[idx011] if 0 <= idx011 < len(lut_data) else (r, g, b)
            c100 = lut_data[idx100] if 0 <= idx100 < len(lut_data) else (r, g, b)
            c101 = lut_data[idx101] if 0 <= idx101 < len(lut_data) else (r, g, b)
            c110 = lut_data[idx110] if 0 <= idx110 < len(lut_data) else (r, g, b)
            c111 = lut_data[idx111] if 0 <= idx111 < len(lut_data) else (r, g, b)
            
            # Trilinear interpolation - optimized calculations
            # Interpolate along b axis
            c00_r = c000[0] + (c001[0] - c000[0]) * b_frac
            c00_g = c000[1] + (c001[1] - c000[1]) * b_frac
            c00_b = c000[2] + (c001[2] - c000[2]) * b_frac
            
            c01_r = c010[0] + (c011[0] - c010[0]) * b_frac
            c01_g = c010[1] + (c011[1] - c010[1]) * b_frac
            c01_b = c010[2] + (c011[2] - c010[2]) * b_frac
            
            c10_r = c100[0] + (c101[0] - c100[0]) * b_frac
            c10_g = c100[1] + (c101[1] - c100[1]) * b_frac
            c10_b = c100[2] + (c101[2] - c100[2]) * b_frac
            
            c11_r = c110[0] + (c111[0] - c110[0]) * b_frac
            c11_g = c110[1] + (c111[1] - c110[1]) * b_frac
            c11_b = c110[2] + (c111[2] - c110[2]) * b_frac
            
            # Interpolate along g axis
            c0_r = c00_r + (c01_r - c00_r) * g_frac
            c0_g = c00_g + (c01_g - c00_g) * g_frac
            c0_b = c00_b + (c01_b - c00_b) * g_frac
            
            c1_r = c10_r + (c11_r - c10_r) * g_frac
            c1_g = c10_g + (c11_g - c10_g) * g_frac
            c1_b = c10_b + (c11_b - c10_b) * g_frac
            
            # Final interpolation along r axis
            result_r = c0_r + (c1_r - c0_r) * r_frac
            result_g = c0_g + (c1_g - c0_g) * r_frac
            result_b = c0_b + (c1_b - c0_b) * r_frac
            
            return (result_r, result_g, result_b)
            
        except (IndexError, ValueError):
            # Fallback to original values if interpolation fails
            return (r, g, b)

    def _interpolate_lut(self, r, g, b, lut_data, lut_size):
        """Trilinear interpolation in 3D LUT"""
        # Scale to LUT coordinate space
        r_scaled = r * (lut_size - 1)
        g_scaled = g * (lut_size - 1)
        b_scaled = b * (lut_size - 1)
        
        # Get integer coordinates
        r_low = int(r_scaled)
        g_low = int(g_scaled)
        b_low = int(b_scaled)
        
        r_high = min(r_low + 1, lut_size - 1)
        g_high = min(g_low + 1, lut_size - 1)
        b_high = min(b_low + 1, lut_size - 1)
        
        # Get fractional parts
        r_frac = r_scaled - r_low
        g_frac = g_scaled - g_low
        b_frac = b_scaled - b_low
        
        # Trilinear interpolation
        def get_lut_value(ri, gi, bi):
            index = ri + gi * lut_size + bi * lut_size * lut_size
            if 0 <= index < len(lut_data):
                return lut_data[index]
            return (r, g, b)  # Fallback to original
        
        # Get 8 corner values
        c000 = get_lut_value(r_low, g_low, b_low)
        c001 = get_lut_value(r_low, g_low, b_high)
        c010 = get_lut_value(r_low, g_high, b_low)
        c011 = get_lut_value(r_low, g_high, b_high)
        c100 = get_lut_value(r_high, g_low, b_low)
        c101 = get_lut_value(r_high, g_low, b_high)
        c110 = get_lut_value(r_high, g_high, b_low)
        c111 = get_lut_value(r_high, g_high, b_high)
        
        # Interpolate along each axis
        def lerp(a, b, t):
            return tuple(a[i] + (b[i] - a[i]) * t for i in range(3))
        
        # Interpolate along b axis
        c00 = lerp(c000, c001, b_frac)
        c01 = lerp(c010, c011, b_frac)
        c10 = lerp(c100, c101, b_frac)
        c11 = lerp(c110, c111, b_frac)
        
        # Interpolate along g axis
        c0 = lerp(c00, c01, g_frac)
        c1 = lerp(c10, c11, g_frac)
        
        # Final interpolation along r axis
        result = lerp(c0, c1, r_frac)
        
        return result

    def scan_lut_folder(self, folder_path):
        """Scan a folder and its subfolders for CUBE LUT files"""
        if not folder_path or not os.path.exists(folder_path):
            return []
        
        lut_files = []
        try:
            # Walk through all subdirectories to find .cube files
            for root, dirs, files in os.walk(folder_path):
                for file in files:
                    if file.lower().endswith('.cube'):
                        full_path = os.path.join(root, file)
                        lut_files.append(full_path)
            
            # Sort: first by first-level folder (relative to root), then by filename
            def lut_sort_key(full_path):
                rel = os.path.relpath(full_path, folder_path)
                parts = rel.split(os.sep)
                if len(parts) == 1:
                    # File directly in root: group key empty so it appears before folder groups
                    return ("", parts[0].lower())
                first_folder = parts[0].lower()
                filename = parts[-1].lower()
                return (first_folder, filename)
            lut_files.sort(key=lut_sort_key)
            
            print(f"Found {len(lut_files)} CUBE LUT files in {folder_path} and subfolders")
            
        except Exception as e:
            print(f"Error scanning LUT folder: {e}")
        
        return lut_files

    def _build_channel_lut(self, np, black, white, gamma_slider):
        """Build a 256-entry uint8 levels LUT for one channel.

        Classical levels: remap [black, white] -> [0, 255] then apply a midtone
        (gamma) curve. ``gamma_slider`` is -100..100 (0 = neutral); positive
        brightens midtones, negative darkens them.
        """
        black = max(0, min(254, int(black)))
        white = max(black + 1, min(255, int(white)))
        x = np.arange(256, dtype=np.float32)
        t = np.clip((x - black) / float(white - black), 0.0, 1.0)
        # Map slider to gamma exponent in [0.25, 4.0]; neutral at 1.0
        gamma = 2.0 ** (float(gamma_slider) / 50.0)
        t = np.power(t, 1.0 / gamma)
        return np.clip(t * 255.0 + 0.5, 0, 255).astype(np.uint8)

    def apply_curves(self, pixmap):
        """Apply the classical per-channel curves (levels) effect.

        Builds a 256-entry LUT for master + each RGB channel and applies them
        (master first, then per-channel) via numpy. Returns a new QPixmap. If
        the effect is disabled or numpy is unavailable, returns ``pixmap``.
        """
        try:
            if not pixmap or pixmap.isNull() or not self.curves_enabled:
                return pixmap

            opacity = max(0, min(100, int(self.curves_opacity))) / 100.0
            if opacity <= 0.0:
                return pixmap  # fully transparent effect = original

            import numpy as np
            master = self._build_channel_lut(
                np, self.curves_black["master"], self.curves_white["master"],
                self.curves_gamma["master"])
            channel_luts = []
            for ch in ("r", "g", "b"):
                ch_lut = self._build_channel_lut(
                    np, self.curves_black[ch], self.curves_white[ch],
                    self.curves_gamma[ch])
                # Apply master first, then the channel curve
                channel_luts.append(ch_lut[master])

            # Identity fast-path: nothing to do
            identity = np.arange(256, dtype=np.uint8)
            if all(np.array_equal(l, identity) for l in channel_luts):
                return pixmap

            image = pixmap.toImage()
            if image.isNull():
                return pixmap
            if image.format() != QImage.Format.Format_RGBA8888:
                image = image.convertToFormat(QImage.Format.Format_RGBA8888)

            w, h = image.width(), image.height()
            bpl = image.bytesPerLine()
            ptr = image.constBits()
            buf = bytes(ptr)[: bpl * h]
            arr = np.frombuffer(buf, dtype=np.uint8).reshape(h, bpl)[:, : w * 4].reshape(h, w, 4)

            out = arr.copy()
            out[:, :, 0] = channel_luts[0][arr[:, :, 0]]
            out[:, :, 1] = channel_luts[1][arr[:, :, 1]]
            out[:, :, 2] = channel_luts[2][arr[:, :, 2]]
            # Alpha (index 3) left unchanged

            # Blend the curve result with the original by opacity
            if opacity < 1.0:
                orig = arr[:, :, :3].astype(np.float32)
                curved = out[:, :, :3].astype(np.float32)
                blended = orig * (1.0 - opacity) + curved * opacity
                out[:, :, :3] = np.clip(blended + 0.5, 0, 255).astype(np.uint8)

            out = np.ascontiguousarray(out)
            result = QImage(out.tobytes(), w, h, w * 4, QImage.Format.Format_RGBA8888).copy()
            return QPixmap.fromImage(result)
        except Exception as e:
            print(f"apply_curves error: {e}")
            return pixmap

    def apply_value_filter(self, pixmap):
        """Posterize the image into N evenly-spaced grayscale tones (value study).

        Converts to luminance (Rec. 709) and quantizes to ``self.value_levels``
        discrete brightness bands. Returns a new grayscale QPixmap. If the
        filter is disabled or numpy is unavailable, returns ``pixmap`` unchanged.
        """
        try:
            if not pixmap or pixmap.isNull() or not self.value_filter_enabled:
                return pixmap
            n = max(2, min(10, int(self.value_levels)))

            import numpy as np
            image = pixmap.toImage()
            if image.isNull():
                return pixmap
            if image.format() != QImage.Format.Format_RGBA8888:
                image = image.convertToFormat(QImage.Format.Format_RGBA8888)

            w, h = image.width(), image.height()
            bpl = image.bytesPerLine()
            ptr = image.constBits()
            buf = bytes(ptr)[: bpl * h]
            arr = np.frombuffer(buf, dtype=np.uint8).reshape(h, bpl)[:, : w * 4].reshape(h, w, 4)

            # Rec. 709 luminance
            r = arr[:, :, 0].astype(np.float32)
            g = arr[:, :, 1].astype(np.float32)
            b = arr[:, :, 2].astype(np.float32)
            lum = 0.2126 * r + 0.7152 * g + 0.0722 * b

            # Quantize into N bands then map to N evenly spaced output levels in [0, 255]
            bins = np.clip(np.floor(lum * n / 256.0), 0, n - 1).astype(np.uint8)
            output_levels = np.linspace(0, 255, n, dtype=np.uint8)
            quantized = output_levels[bins]

            # Build a Grayscale8 QImage; copy to detach from the numpy buffer
            quantized = np.ascontiguousarray(quantized)
            out = QImage(quantized.tobytes(), w, h, w, QImage.Format.Format_Grayscale8).copy()
            return QPixmap.fromImage(out)
        except Exception as e:
            print(f"apply_value_filter error: {e}")
            return pixmap

    def _kmeans_palette(self, sample, k, np, iters=8):
        """Compute ``k`` dominant colours from ``sample`` (N,3) via k-means.

        Uses k-means++ seeding for stable, representative centres and a few
        Lloyd iterations. Returns a (k,3) float32 array of palette colours.
        """
        n = sample.shape[0]
        if k >= n:
            return sample.astype(np.float32)
        rng = np.random.default_rng(12345)
        centers = np.empty((k, 3), np.float32)
        centers[0] = sample[rng.integers(0, n)]
        closest = np.sum((sample - centers[0]) ** 2, axis=1)
        for i in range(1, k):
            total = float(closest.sum())
            if total <= 1e-12:
                centers[i] = sample[rng.integers(0, n)]
            else:
                idx = int(rng.choice(n, p=closest / total))
                centers[i] = sample[idx]
            dist = np.sum((sample - centers[i]) ** 2, axis=1)
            closest = np.minimum(closest, dist)
        for _ in range(iters):
            d = np.sum((sample[:, None, :] - centers[None, :, :]) ** 2, axis=2)
            labels = np.argmin(d, axis=1)
            moved = False
            for j in range(k):
                pts = sample[labels == j]
                if pts.shape[0] > 0:
                    nc = pts.mean(axis=0)
                    if not np.allclose(nc, centers[j]):
                        centers[j] = nc
                        moved = True
            if not moved:
                break
        return centers.astype(np.float32)

    def _box_blur_2d(self, a, radius, np):
        """Fast separable box blur of a 2-D float array via cumulative sums.

        Edge pixels are normalised by the actual (clipped) window size, so
        borders stay correct. O(n) regardless of ``radius``. Returns a new
        float32 array of the same shape.
        """
        r = int(radius)
        if r < 1:
            return a
        a = a.astype(np.float32, copy=False)
        h, w = a.shape
        # Horizontal pass
        cs = np.cumsum(a, axis=1)
        cs = np.concatenate([np.zeros((h, 1), np.float32), cs], axis=1)
        left = np.clip(np.arange(w) - r, 0, w)
        right = np.clip(np.arange(w) + r + 1, 0, w)
        horiz = (cs[:, right] - cs[:, left]) / (right - left).astype(np.float32)[None, :]
        # Vertical pass
        cs2 = np.cumsum(horiz, axis=0)
        cs2 = np.concatenate([np.zeros((1, w), np.float32), cs2], axis=0)
        top = np.clip(np.arange(h) - r, 0, h)
        bot = np.clip(np.arange(h) + r + 1, 0, h)
        out = (cs2[bot, :] - cs2[top, :]) / (bot - top).astype(np.float32)[:, None]
        return out

    def _smooth_color_fields(self, color_img, palette_u8, radius, np):
        """Round jagged quantised boundaries into smooth curves.

        Runs a soft majority (mode) filter: each palette colour's membership
        mask is box-blurred, then every pixel takes whichever colour wins
        locally. This removes single-pixel stair-stepping at field edges while
        preserving the flat colour fields. Returns a new (h,w,3) uint8 array.
        """
        r = max(1, int(radius))
        h, w, _ = color_img.shape
        best_score = None
        best_idx = np.zeros((h, w), np.int32)
        for k in range(palette_u8.shape[0]):
            mask = np.all(color_img == palette_u8[k], axis=2).astype(np.float32)
            if not mask.any():
                continue
            blurred = self._box_blur_2d(mask, r, np)
            if best_score is None:
                best_score = blurred
                best_idx[:] = k
            else:
                better = blurred > best_score
                best_idx[better] = k
                np.maximum(best_score, blurred, out=best_score)
        return palette_u8[best_idx]

    def apply_color_groups(self, pixmap):
        """Reduce the image to N dominant colours sampled from the image itself.

        Builds a small palette via numpy k-means on a downsampled colour
        sample, then maps every pixel to its nearest palette colour, producing
        flat colour fields (a "colour map" of the image). ``color_groups_count``
        sets the number of colours (2-32); ``color_groups_field`` optionally
        pre-smooths the image to merge small regions into larger fields
        (0 = off). Alpha is preserved. Returns ``pixmap`` unchanged if disabled
        or numpy is unavailable.
        """
        try:
            if not pixmap or pixmap.isNull() or not self.color_groups_enabled:
                return pixmap
            n = max(2, min(32, int(self.color_groups_count)))
            field = max(0, min(20, int(self.color_groups_field)))

            import numpy as np
            image = pixmap.toImage()
            if image.isNull():
                return pixmap
            if image.format() != QImage.Format.Format_RGBA8888:
                image = image.convertToFormat(QImage.Format.Format_RGBA8888)

            w, h = image.width(), image.height()
            if w < 1 or h < 1:
                return pixmap
            bpl = image.bytesPerLine()
            ptr = image.constBits()
            buf = bytes(ptr)[: bpl * h]
            arr = np.frombuffer(buf, dtype=np.uint8).reshape(h, bpl)[:, : w * 4].reshape(h, w, 4)

            rgb = arr[:, :, :3].astype(np.float32)
            alpha = arr[:, :, 3].copy()

            # Optional field-size pre-smoothing: a smooth separable box blur
            # (not block averaging) so colour fields merge with soft, curved
            # boundaries instead of hard pixel-art blocks. Two passes approximate
            # a Gaussian for extra smoothness.
            work = rgb
            if field > 0:
                radius = max(1, int(field))
                work = np.empty_like(rgb)
                for c in range(3):
                    ch = self._box_blur_2d(rgb[:, :, c], radius, np)
                    ch = self._box_blur_2d(ch, radius, np)
                    work[:, :, c] = ch

            # Build palette from a downsampled colour sample (speed), with cache.
            flat = work.reshape(-1, 3)
            cache_key = (self.current_image, n, field, w, h)
            palette = self._color_palette_cache.get(cache_key)
            if palette is None:
                max_samples = 20000
                if flat.shape[0] > max_samples:
                    idx = np.linspace(0, flat.shape[0] - 1, max_samples).astype(np.int64)
                    sample = flat[idx]
                else:
                    sample = flat
                k = min(n, sample.shape[0])
                palette = self._kmeans_palette(sample, k, np)
                if len(self._color_palette_cache) > 12:
                    self._color_palette_cache.clear()
                self._color_palette_cache[cache_key] = palette

            # Quantize to the palette and (when a field size is set) smooth the
            # colour fields. Prefer a single GPU pass — the majority smoothing
            # is far too slow on the CPU. Fall back to numpy when unavailable.
            smooth_radius = max(1, field // 2) if field > 0 else 0
            color_img = None
            gpu = getattr(self, 'gpu_processor', None)
            if gpu is not None:
                gpu_res = gpu.color_groups_gpu(flat, palette, w, h, smooth_radius)
                if gpu_res is not None:
                    color_img = gpu_res.reshape(h, w, 3)

            if color_img is None:
                # CPU fallback: chunked nearest-colour assignment...
                labels = np.empty(flat.shape[0], np.int64)
                chunk = 1 << 20
                for start in range(0, flat.shape[0], chunk):
                    seg = flat[start:start + chunk]
                    d = np.sum((seg[:, None, :] - palette[None, :, :]) ** 2, axis=2)
                    labels[start:start + chunk] = np.argmin(d, axis=1)
                color_img = np.clip(palette[labels].reshape(h, w, 3), 0, 255).astype(np.uint8)
                # ...then round jagged boundaries into smooth curves.
                if smooth_radius > 0:
                    palette_u8 = np.clip(np.rint(palette), 0, 255).astype(np.uint8)
                    color_img = self._smooth_color_fields(color_img, palette_u8, smooth_radius, np)

            out = np.empty((h, w, 4), np.uint8)
            out[:, :, :3] = color_img
            out[:, :, 3] = alpha
            out = np.ascontiguousarray(out)
            qout = QImage(out.tobytes(), w, h, w * 4, QImage.Format.Format_RGBA8888).copy()
            return QPixmap.fromImage(qout)
        except Exception as e:
            print(f"apply_color_groups error: {e}")
            return pixmap

    # Segmentation runs on a downscaled copy for speed; labels are upsampled
    # back to full resolution before the per-object colours are measured.
    _OBJECT_SEG_MAX_DIM = 900

    # Seed palette size for the first quantization pass. Only needs to be fine
    # enough that real object boundaries survive it — the neighbour merge below
    # is what actually decides where one object ends and the next begins.
    _OBJECT_SEED_COLORS = 24

    def _seed_regions(self, small_rgb, cv2, np):
        """Split ``small_rgb`` into connected patches of near-identical colour.

        Quantizes to a small k-means palette (via a 32³ colour-cube lookup, so
        the per-pixel step is a table index rather than a distance search), then
        runs connected components per palette colour. Two objects sharing a
        colour therefore land in different patches — the cryptomatte-style
        spatial separation that Color Groups can't make. Returns an (h,w) int32
        label image with ids ``1..n`` and the patch count.
        """
        h, w = small_rgb.shape[:2]
        flat = small_rgb.reshape(-1, 3).astype(np.float32)
        max_samples = 20000
        if flat.shape[0] > max_samples:
            idx = np.linspace(0, flat.shape[0] - 1, max_samples).astype(np.int64)
            sample = np.ascontiguousarray(flat[idx])
        else:
            sample = flat
        k = int(min(self._OBJECT_SEED_COLORS, sample.shape[0]))
        criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 8, 1.0)
        # Fixed seed: k-means++ is randomised, and an unseeded run would give a
        # slightly different segmentation every time the image is re-rendered.
        cv2.setRNGSeed(12345)
        _, _, centers = cv2.kmeans(sample, k, None, criteria, 1, cv2.KMEANS_PP_CENTERS)

        # Nearest palette colour for every cell of a 5-bit RGB cube, then one
        # fancy-index to label the image.
        grid = np.arange(32, dtype=np.float32) * 8.0 + 4.0
        cube = np.stack(np.meshgrid(grid, grid, grid, indexing='ij'), axis=-1).reshape(-1, 3)
        cube_lut = np.argmin(((cube[:, None, :] - centers[None, :, :]) ** 2).sum(axis=2), axis=1)
        codes = (((small_rgb[:, :, 0] >> 3).astype(np.int32) << 10)
                 | ((small_rgb[:, :, 1] >> 3).astype(np.int32) << 5)
                 | (small_rgb[:, :, 2] >> 3).astype(np.int32))
        quantized = cube_lut[codes]

        quantized = quantized.astype(np.uint8)
        labels = np.zeros((h, w), np.int32)
        offset = 0
        for i in range(k):
            # cv2.compare gives the 0/255 mask connectedComponents wants without
            # a Python-level boolean pass over the image.
            mask = cv2.compare(quantized, i, cv2.CMP_EQ)
            n_comp, comp = cv2.connectedComponents(mask, connectivity=4)
            if n_comp <= 1:
                continue
            # Masked add writes only this colour's pixels, leaving the rest.
            cv2.add(labels, comp + offset, dst=labels, mask=mask)
            offset += n_comp - 1
        return labels, offset

    def _merge_similar_neighbors(self, labels, count, source, tol, np):
        """Agglomerate touching patches whose colours are close (< ``tol``).

        This is what turns quantization patches back into objects: a shaded
        surface arrives as a stack of thin bands, and merging neighbours by
        colour distance walks along it until a real edge stops the chain, while
        a genuinely different object beside it stays separate. Merges are taken
        cheapest-first and re-checked against the running cluster mean, so one
        object never swallows the frame. Returns a densely relabelled map and
        the new count.
        """
        flat = labels.ravel()
        sizes = np.bincount(flat, minlength=count + 1).astype(np.float64)
        sums = np.empty((count + 1, 3), np.float64)
        for c in range(3):
            sums[:, c] = np.bincount(flat, weights=source[:, :, c].ravel().astype(np.float64),
                                     minlength=count + 1)
        means = sums / np.maximum(sizes, 1.0)[:, None]

        # Unique unordered pairs of 4-neighbour-adjacent labels.
        left = np.concatenate([labels[:, :-1].ravel(), labels[:-1, :].ravel()])
        right = np.concatenate([labels[:, 1:].ravel(), labels[1:, :].ravel()])
        differing = left != right
        left, right = left[differing], right[differing]
        if left.size == 0:
            return labels, count
        lo = np.minimum(left, right).astype(np.int64)
        hi = np.maximum(left, right).astype(np.int64)
        stride = np.int64(count + 1)
        pairs = np.unique(lo * stride + hi)
        pa = (pairs // stride).astype(np.int32)
        pb = (pairs % stride).astype(np.int32)

        dist = np.sqrt(((means[pa] - means[pb]) ** 2).sum(axis=1))
        # Only pairs already within tolerance can ever merge; ordering the rest
        # of the work cheapest-first keeps the agglomeration stable.
        candidates = np.flatnonzero(dist <= tol)
        order = candidates[np.argsort(dist[candidates], kind='stable')]

        # Plain Python lists for the union-find: this loop runs once per
        # candidate pair (tens of thousands at high detail) and scalar list
        # access is several times cheaper than indexing numpy arrays.
        parent = list(range(count + 1))
        area = sizes.tolist()
        acc = [sums[:, 0].tolist(), sums[:, 1].tolist(), sums[:, 2].tolist()]
        avg = [means[:, 0].tolist(), means[:, 1].tolist(), means[:, 2].tolist()]
        pa_l, pb_l = pa.tolist(), pb.tolist()
        tol_sq = float(tol) * float(tol)

        def find(node):
            root = node
            while parent[root] != root:
                root = parent[root]
            while parent[node] != root:
                parent[node], node = root, parent[node]
            return root

        for i in order.tolist():
            ra, rb = find(pa_l[i]), find(pb_l[i])
            if ra == rb:
                continue
            dr = avg[0][ra] - avg[0][rb]
            dg = avg[1][ra] - avg[1][rb]
            db = avg[2][ra] - avg[2][rb]
            if dr * dr + dg * dg + db * db > tol_sq:
                continue
            if area[ra] < area[rb]:
                ra, rb = rb, ra
            parent[rb] = ra
            total = area[ra] + area[rb]
            area[ra] = total
            inv = 1.0 / total if total else 0.0
            for c in range(3):
                acc[c][ra] += acc[c][rb]
                avg[c][ra] = acc[c][ra] * inv

        # Resolve every label to its root by pointer jumping (log-depth, and
        # each step is one vectorised gather instead of count Python calls).
        roots = np.asarray(parent, dtype=np.int32)
        while True:
            jumped = roots[roots]
            if np.array_equal(jumped, roots):
                break
            roots = jumped
        unique_roots, dense = np.unique(roots, return_inverse=True)
        return dense.astype(np.int32)[labels], int(unique_roots.size) - 1

    def _merge_small_objects(self, labels, count, guide_rgb, min_area, cv2, np):
        """Drop regions below ``min_area`` and grow the survivors over them.

        The kept regions become watershed markers, so the reclaimed pixels are
        handed to whichever neighbour the image's own edges point at rather
        than to the nearest label by distance. Returns a relabelled int32 map.
        """
        counts = np.bincount(labels.ravel(), minlength=count + 1)
        keep = counts >= min_area
        keep[0] = False
        if not keep.any():
            # Everything is below the threshold — keep the biggest handful so
            # the watershed still has markers to grow from.
            biggest = np.argsort(counts[1:])[::-1][:8] + 1
            keep[biggest] = True
        kept_ids = np.flatnonzero(keep)
        if kept_ids.size == count:
            return labels
        remap = np.zeros(count + 1, np.int32)
        remap[kept_ids] = np.arange(1, kept_ids.size + 1, dtype=np.int32)
        markers = np.ascontiguousarray(remap[labels])
        cv2.watershed(cv2.cvtColor(guide_rgb, cv2.COLOR_RGB2BGR), markers)
        # watershed leaves -1 on the ridges it found; hand those pixels to a
        # neighbouring region so no unassigned seams remain.
        if (markers <= 0).any():
            m = markers.astype(np.float32)
            m[m < 0] = 0
            kernel = np.ones((3, 3), np.uint8)
            for _ in range(3):
                if not (m <= 0).any():
                    break
                grown = cv2.dilate(m, kernel)
                m = np.where(m <= 0, grown, m)
            markers = np.maximum(m, 0).astype(np.int32)
        return markers

    def _segment_objects(self, rgb, cv2, np):
        """Split ``rgb`` into per-object regions (the cryptomatte-style pass).

        Flattens surface texture, cuts the frame into connected same-colour
        patches, merges neighbouring patches that belong to the same object,
        then absorbs anything smaller than the requested minimum. Returns a
        full-resolution int32 label map.
        """
        h, w = rgb.shape[:2]
        detail = max(0, min(100, int(self.object_groups_detail)))
        min_size = max(0, min(100, int(self.object_groups_min_size)))

        scale = min(1.0, self._OBJECT_SEG_MAX_DIM / float(max(h, w)))
        if scale < 1.0:
            sw, sh = max(1, int(round(w * scale))), max(1, int(round(h * scale)))
            small = cv2.resize(rgb, (sw, sh), interpolation=cv2.INTER_AREA)
        else:
            sw, sh, small = w, h, rgb

        # Flatten texture/noise but keep object silhouettes crisp: two cheap
        # bilateral passes approximate the mean-shift look at a fraction of the
        # cost. Low detail smooths harder, so fine surface variation stops
        # spawning objects of its own.
        sigma_color = float(np.interp(detail, [0, 100], [90.0, 25.0]))
        smooth = cv2.bilateralFilter(small, 9, sigma_color, 9)
        smooth = np.ascontiguousarray(cv2.bilateralFilter(smooth, 9, sigma_color, 9))

        labels, count = self._seed_regions(smooth, cv2, np)
        if count < 1:
            return np.zeros((h, w), np.int32)

        # Detail -> how far apart two touching areas must be to stay separate
        # objects. High detail = tight tolerance = more, smaller objects.
        tol = float(np.interp(detail, [0, 100], [72.0, 6.0]))
        labels, count = self._merge_similar_neighbors(labels, count, smooth, tol, np)

        # Minimum object size as a fraction of the frame (0 keeps every speck).
        min_area = int((sw * sh) * ((min_size / 100.0) ** 2) * 0.05)
        if min_area > 1 and count > 1:
            labels = self._merge_small_objects(labels, count, smooth, min_area, cv2, np)

        if scale < 1.0:
            labels = cv2.resize(labels.astype(np.float32), (w, h),
                                interpolation=cv2.INTER_NEAREST).astype(np.int32)
        return labels

    def _object_id_palette(self, count, cv2, np):
        """Deterministic, well-separated ID colours (the cryptomatte look)."""
        ids = np.arange(count, dtype=np.float32)
        hue = np.mod(ids * 0.61803398875, 1.0) * 179.0  # golden-ratio hue spacing
        hsv = np.stack([
            hue.astype(np.uint8),
            np.full(count, 190, np.uint8),
            np.full(count, 235, np.uint8),
        ], axis=1).reshape(1, count, 3)
        return cv2.cvtColor(hsv, cv2.COLOR_HSV2RGB).reshape(count, 3)

    def apply_object_groups(self, pixmap):
        """Simplify the image object by object, cryptomatte style.

        Where Color Groups collapses the whole image onto one global palette,
        this segments it spatially first: every region gets its own id, so two
        objects that happen to share a colour stay separate. Each region is then
        filled flat with the mean of its *own* pixels, keeping local object
        colour. ``object_groups_mode`` picks the look:

          - local: each object flat-filled with its own average colour
          - local_edges: the same, plus region outlines in the line colour
          - id: random-but-stable ID colours (a literal cryptomatte matte)

        ``object_groups_detail`` sets how finely the image is split and
        ``object_groups_min_size`` merges regions below that size into their
        neighbours. Alpha is preserved. Returns ``pixmap`` unchanged if
        disabled, on error, or if OpenCV is missing.
        """
        try:
            if not pixmap or pixmap.isNull() or not self.object_groups_enabled:
                return pixmap

            try:
                import cv2
            except ImportError:
                # Graceful fallback: disable and inform the user once
                self.object_groups_enabled = False
                if getattr(self, 'object_groups_toggle_btn', None) is not None:
                    self.object_groups_toggle_btn.blockSignals(True)
                    self.object_groups_toggle_btn.setChecked(False)
                    self.object_groups_toggle_btn.blockSignals(False)
                self.statusBar().showMessage(
                    "Object Groups requires opencv-python (pip install opencv-python)", 5000)
                return pixmap

            import numpy as np
            image = pixmap.toImage()
            if image.isNull():
                return pixmap
            if image.format() != QImage.Format.Format_RGBA8888:
                image = image.convertToFormat(QImage.Format.Format_RGBA8888)

            w, h = image.width(), image.height()
            if w < 2 or h < 2:
                return pixmap
            bpl = image.bytesPerLine()
            ptr = image.constBits()
            buf = bytes(ptr)[: bpl * h]
            arr = np.frombuffer(buf, dtype=np.uint8).reshape(h, bpl)[:, : w * 4].reshape(h, w, 4)
            rgb = np.ascontiguousarray(arr[:, :, :3])
            alpha = arr[:, :, 3].copy()

            labels = self._segment_objects(rgb, cv2, np)
            flat_labels = labels.ravel()
            count = int(flat_labels.max()) + 1

            mode = self.object_groups_mode
            if mode == "id":
                lut = self._object_id_palette(count, cv2, np)
            else:
                # Per-region mean of the original pixels — the "local colour".
                sizes = np.bincount(flat_labels, minlength=count).astype(np.float32)
                sizes[sizes == 0] = 1.0
                means = np.empty((count, 3), np.float32)
                for c in range(3):
                    means[:, c] = np.bincount(
                        flat_labels, weights=rgb[:, :, c].ravel().astype(np.float32),
                        minlength=count) / sizes
                lut = np.clip(np.rint(means), 0, 255).astype(np.uint8)

            out_rgb = lut[labels]

            if mode == "local_edges":
                boundary = np.zeros((h, w), bool)
                boundary[:, 1:] |= labels[:, 1:] != labels[:, :-1]
                boundary[1:, :] |= labels[1:, :] != labels[:-1, :]
                lc = self.edge_color if self.edge_color is not None else self.line_color
                out_rgb[boundary] = (lc.red(), lc.green(), lc.blue())

            out = np.empty((h, w, 4), np.uint8)
            out[:, :, :3] = out_rgb
            out[:, :, 3] = alpha
            out = np.ascontiguousarray(out)
            qout = QImage(out.tobytes(), w, h, w * 4, QImage.Format.Format_RGBA8888).copy()
            return QPixmap.fromImage(qout)
        except Exception as e:
            print(f"apply_object_groups error: {e}")
            return pixmap

    def apply_edge_detection(self, pixmap):
        """Run Canny edge detection to reveal plane changes (art reference).

        Renders the result according to ``self.edge_mode``:
          - white_on_black: edge lines on a black background (default white)
          - black_on_white: edge lines on a white background (default black)
          - overlay: edge lines drawn over the original image

        Edge line color follows ``self.edge_color`` when set by the line-color
        tools/presets; otherwise each mode uses its default look.

        ``self.edge_sensitivity`` (0-100) drives the Canny thresholds. Returns
        ``pixmap`` unchanged if disabled, on error, or if OpenCV is missing.
        """
        try:
            if not pixmap or pixmap.isNull() or not self.edge_detection_enabled:
                return pixmap

            try:
                import cv2
            except ImportError:
                # Graceful fallback: disable and inform the user once
                self.edge_detection_enabled = False
                if hasattr(self, 'edge_toggle_btn') and self.edge_toggle_btn is not None:
                    self.edge_toggle_btn.blockSignals(True)
                    self.edge_toggle_btn.setChecked(False)
                    self.edge_toggle_btn.blockSignals(False)
                self.statusBar().showMessage(
                    "Edge detection requires opencv-python (pip install opencv-python)", 5000)
                return pixmap

            import numpy as np
            image = pixmap.toImage()
            if image.isNull():
                return pixmap
            if image.format() != QImage.Format.Format_RGBA8888:
                image = image.convertToFormat(QImage.Format.Format_RGBA8888)

            w, h = image.width(), image.height()
            bpl = image.bytesPerLine()
            ptr = image.constBits()
            buf = bytes(ptr)[: bpl * h]
            arr = np.frombuffer(buf, dtype=np.uint8).reshape(h, bpl)[:, : w * 4].reshape(h, w, 4)
            rgb = np.ascontiguousarray(arr[:, :, :3])

            gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
            # Light blur reduces noise edges; map sensitivity (0-100) -> thresholds.
            # Higher sensitivity => lower thresholds => more edges.
            s = max(0, min(100, int(self.edge_sensitivity)))
            low = int(10 + (100 - s) * 1.4)      # ~10..150
            high = min(255, low * 2)
            blurred = cv2.GaussianBlur(gray, (3, 3), 0)
            edges = cv2.Canny(blurred, low, high)  # uint8 (h, w), 0 or 255

            mode = self.edge_mode
            mask = edges > 0
            # Resolve the edge line color. None => each mode's default look;
            # once the user picks a line color it drives every mode.
            if self.edge_color is not None:
                ec = (self.edge_color.red(), self.edge_color.green(), self.edge_color.blue())
            else:
                ec = None

            if mode == "overlay":
                out_rgb = rgb.copy()
                lc = self.line_color
                line_rgb = ec if ec is not None else (lc.red(), lc.green(), lc.blue())
                out_rgb[mask] = line_rgb
            elif mode == "black_on_white":
                # White background; default edge color black
                out_rgb = np.full((h, w, 3), 255, dtype=np.uint8)
                out_rgb[mask] = ec if ec is not None else (0, 0, 0)
            else:  # white_on_black
                # Black background; default edge color white
                out_rgb = np.zeros((h, w, 3), dtype=np.uint8)
                out_rgb[mask] = ec if ec is not None else (255, 255, 255)

            out_rgba = np.dstack([
                out_rgb,
                np.full((h, w), 255, dtype=np.uint8),
            ])
            out_rgba = np.ascontiguousarray(out_rgba)
            out = QImage(out_rgba.tobytes(), w, h, w * 4,
                         QImage.Format.Format_RGBA8888).copy()
            return QPixmap.fromImage(out)
        except Exception as e:
            print(f"apply_edge_detection error: {e}")
            return pixmap

    def apply_fast_enhancements(self, pixmap):
        """Apply fast image enhancements using Qt's optimized color effects."""
        try:
            if not pixmap or pixmap.isNull():
                return pixmap
                
            # Fast grayscale conversion using Qt's built-in weighted average
            if self.grayscale_value > 0:
                # Create a grayscale version using Qt's optimized conversion
                image = pixmap.toImage()
                gray_image = image.convertToFormat(image.Format.Format_Grayscale8)
                gray_pixmap = QPixmap.fromImage(gray_image)
                
                if self.grayscale_value == 100:
                    pixmap = gray_pixmap
                else:
                    # Fast blend using Qt's composition modes
                    result = QPixmap(pixmap.size())
                    result.fill(Qt.transparent)
                    
                    painter = QPainter(result)
                    painter.setRenderHint(QPainter.Antialiasing)
                    
                    # Draw original image
                    painter.setOpacity(1.0 - (self.grayscale_value / 100.0))
                    painter.drawPixmap(0, 0, pixmap)
                    
                    # Draw grayscale overlay
                    painter.setOpacity(self.grayscale_value / 100.0)
                    painter.drawPixmap(0, 0, gray_pixmap)
                    
                    painter.end()
                    pixmap = result
            
            # Apply contrast and gamma using fast QPainter effects instead of pixel manipulation
            if self.contrast_value != 50 or self.gamma_value != 0:
                # Create enhanced version using QPainter composition
                enhanced = QPixmap(pixmap.size())
                enhanced.fill(Qt.transparent)
                
                painter = QPainter(enhanced)
                painter.setRenderHint(QPainter.Antialiasing)
                
                # Fast contrast approximation using opacity and blend modes
                if self.contrast_value != 50:
                    # Range: -130 to +200, where 50 is normal
                    contrast_offset = self.contrast_value - 50  # Convert to offset from normal
                    contrast_factor = contrast_offset / 50.0  # Normalize to reasonable range
                    
                    if contrast_factor > 0:
                        # Increase contrast using multiple overlay passes
                        painter.drawPixmap(0, 0, pixmap)
                        painter.setCompositionMode(QPainter.CompositionMode_Overlay)
                        
                        # Strong base effect for immediate visibility
                        base_opacity = min(1.0, abs(contrast_factor) * 0.8)
                        painter.setOpacity(base_opacity)
                        painter.drawPixmap(0, 0, pixmap)
                        
                        # Add multiple passes for stronger effect
                        num_passes = max(1, int(abs(contrast_factor) * 2))
                        for i in range(min(num_passes, 4)):  # Up to 4 passes
                            painter.setOpacity(min(0.7, abs(contrast_factor) * 0.3))
                            painter.drawPixmap(0, 0, pixmap)
                            
                        # For extreme values, add even more dramatic effects
                        if self.contrast_value > 150:
                            painter.setCompositionMode(QPainter.CompositionMode_HardLight)
                            painter.setOpacity(0.6)
                            painter.drawPixmap(0, 0, pixmap)
                    else:
                        # Decrease contrast - make image flat and gray
                        # Create a much more dramatic low-contrast effect
                        
                        # Start with original image
                        painter.drawPixmap(0, 0, pixmap)
                        
                        # Blend heavily with gray using multiple techniques for maximum effect
                        mid_gray = QPixmap(pixmap.size())
                        mid_gray.fill(QColor(128, 128, 128))  # 50% gray
                        
                        # Method 1: Direct overlay with gray using multiply mode for washing out
                        painter.setCompositionMode(QPainter.CompositionMode_Multiply)
                        gray_strength = abs(contrast_factor)  # 0 to 3.6 for range -130 to 50
                        
                        # Much stronger effect - make it very noticeable immediately
                        base_opacity = min(1.0, gray_strength * 0.7)  # Strong immediate effect
                        painter.setOpacity(base_opacity)
                        painter.drawPixmap(0, 0, mid_gray)
                        
                        # Method 2: Add screen blend to further wash out the image
                        painter.setCompositionMode(QPainter.CompositionMode_Screen)
                        painter.setOpacity(min(0.8, gray_strength * 0.5))
                        painter.drawPixmap(0, 0, mid_gray)
                        
                        # Method 3: For extreme negative values, add direct color burn for maximum flattening
                        if self.contrast_value < 0:  # For truly negative values
                            painter.setCompositionMode(QPainter.CompositionMode_ColorBurn)
                            painter.setOpacity(min(0.6, gray_strength * 0.3))
                            painter.drawPixmap(0, 0, mid_gray)
                            
                        # Method 4: Final soft light pass to complete the washed-out look
                        painter.setCompositionMode(QPainter.CompositionMode_SoftLight)
                        painter.setOpacity(min(0.9, gray_strength * 0.6))
                        painter.drawPixmap(0, 0, mid_gray)
                else:
                    painter.drawPixmap(0, 0, pixmap)
                
                # Fast gamma approximation using multiply blend
                if self.gamma_value != 0:
                    # Range: -200 to +500, where 0 is normal  
                    gamma_factor = self.gamma_value / 100.0  # -2 to +5
                    
                    if gamma_factor > 0:
                        # Brighten dramatically using multiple screen passes
                        painter.setCompositionMode(QPainter.CompositionMode_Screen)
                        
                        # Much stronger base brightening effect
                        base_opacity = min(1.0, abs(gamma_factor) * 0.7)  # Much stronger: 0.7 instead of 0.2
                        painter.setOpacity(base_opacity)
                        painter.drawPixmap(0, 0, pixmap)
                        
                        # Add multiple screen passes for dramatic brightening even at low values
                        num_passes = max(1, int(abs(gamma_factor) * 1.8))  # More passes
                        for i in range(min(num_passes, 4)):  # Up to 4 passes
                            painter.setOpacity(min(0.6, abs(gamma_factor) * 0.25))  # Much stronger passes
                            painter.drawPixmap(0, 0, pixmap)
                        
                        # For extreme brightness, add color dodge for blown-out effect
                        if self.gamma_value > 300:
                            painter.setCompositionMode(QPainter.CompositionMode_ColorDodge)
                            painter.setOpacity(0.4)
                            painter.drawPixmap(0, 0, pixmap)
                    else:
                        # Darken dramatically using multiply with very dark overlays
                        painter.setCompositionMode(QPainter.CompositionMode_Multiply)
                        
                        # Create much darker overlay for dramatic effect
                        dark_overlay = QPixmap(pixmap.size())
                        # Make it much darker: range from black to dark gray
                        darkness_level = max(5, int(60 + gamma_factor * 40))  # Much darker range
                        dark_overlay.fill(QColor(darkness_level, darkness_level, darkness_level))
                        
                        # Much stronger base darkening effect
                        base_opacity = min(1.0, abs(gamma_factor) * 0.8)  # Much stronger: 0.8 instead of 0.2
                        painter.setOpacity(base_opacity)
                        painter.drawPixmap(0, 0, dark_overlay)
                        
                        # Add multiple dark overlay passes for very dark effect
                        num_passes = max(1, int(abs(gamma_factor) * 1.5))
                        for i in range(min(num_passes, 3)):
                            # Use even darker overlay for additional passes
                            very_dark = QPixmap(pixmap.size())
                            very_dark.fill(QColor(20, 20, 20))  # Very dark overlay
                            painter.setOpacity(min(0.7, abs(gamma_factor) * 0.3))
                            painter.drawPixmap(0, 0, very_dark)
                
                painter.end()
                pixmap = enhanced
        
            # Apply LUT if one is loaded
            if self.current_lut and self.lut_strength > 0:
                pixmap = self.apply_lut_to_image(pixmap, self.current_lut, self.lut_strength)
        
            return pixmap
        
        except Exception as e:
            print(f"Error in apply_fast_enhancements: {e}")
            # Return original pixmap if enhancement fails
            import traceback
            traceback.print_exc()
            return pixmap

    def resizeEvent(self, event):
        # Debounced resize handling — longer delay prevents the slider row
        # from flickering hide/show while the user drags the window border.
        self.resize_timer.start(300)  # 300ms debounce
        super().resizeEvent(event)

    # ── toolbar overflow ('»') popup: replace Qt's hover-close native
    #     popup with our own click-outside-to-close QMenu ──
    def _install_popup_persistence(self):
        """Hijack the native QToolBarExtension '»' button on each toolbar.

        Qt creates the extension button lazily and *reconfigures or
        recreates* it on every relayout, and it uses InstantPopup mode
        (its native popup opens on mouse-press and closes on hover-out).
        We therefore (1) poll continuously, (2) install an event filter
        on whatever extension button currently exists, and (3) swallow
        its mouse-press so the native popup never opens — showing our own
        QMenu instead, which only closes on outside-click / select / Esc."""
        self._filtered_ext_ids = set()   # id() of buttons we've filtered
        self._ext_menu_open = False
        self._ext_capture_timer = QTimer(self)
        self._ext_capture_timer.setInterval(250)
        self._ext_capture_timer.timeout.connect(self._capture_extension_buttons)
        self._ext_capture_timer.start()
        self._capture_extension_buttons()

    def _capture_extension_buttons(self):
        for tb in (getattr(self, 'main_toolbar', None),
                   getattr(self, 'slider_toolbar', None)):
            if tb is None:
                continue
            btn = tb.findChild(QToolButton, "qt_toolbar_ext_button")
            if btn is None:
                continue
            btn._owning_toolbar = tb
            # Re-apply each tick in case Qt recreated/reconfigured the
            # button; only (re)install the filter for buttons we haven't
            # already filtered (tracked by object id).
            if id(btn) not in self._filtered_ext_ids:
                self._filtered_ext_ids.add(id(btn))
                btn.installEventFilter(self)

    def _show_overflow_menu(self, toolbar, anchor_btn):
        """Show our own click-outside-to-close popup for the toolbar's
        clipped items.

        We do NOT move the live widgets into the menu (sliders/combos
        render blank inside a QMenu). Instead, for each clipped button we
        add a proxy QAction that re-fires the original button's click, so
        everything renders natively and the menu only closes on
        outside-click / item-select / Esc — never on hover-leave."""
        if self._ext_menu_open:
            return
        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu {
                background-color: #26292c;
                border: 1px solid #3a3d40;
                border-radius: 8px;
                padding: 6px;
            }
            QMenu::item {
                color: #e6e6e6;
                padding: 8px 18px 8px 14px;
                margin: 2px 4px;
                border-radius: 6px;
                font-size: 12px;
            }
            QMenu::item:selected {
                background-color: #3d6fb4;
                color: #ffffff;
            }
            QMenu::item:checked {
                background-color: #2f3338;
            }
            QMenu::separator {
                height: 1px;
                background: #3a3d40;
                margin: 6px 10px;
            }
            QMenu::indicator {
                width: 14px;
                height: 14px;
                left: 6px;
            }
        """)
        vis_w = toolbar.width()
        added = 0
        for action in toolbar.actions():
            w = toolbar.widgetForAction(action)
            # Only buttons make sense as menu entries; skip spacers/labels/sliders/combos.
            if not isinstance(w, QToolButton):
                continue
            geom = toolbar.actionGeometry(action)
            # A button is hidden if Qt gave it no geometry, or it extends
            # past the visible right edge, or the widget itself is hidden.
            clipped = (geom.isEmpty() or geom.right() > vis_w
                       or geom.x() < 0 or not w.isVisible())
            if not clipped:
                continue
            label = w.toolTip() or w.text() or "(button)"
            text = f"{w.text()}  {label}".strip()
            proxy = menu.addAction(text)
            proxy.setCheckable(w.isCheckable())
            if w.isCheckable():
                proxy.setChecked(w.isChecked())
            proxy.triggered.connect(lambda _checked=False, b=w: b.click())
            added += 1
        # Guarantee the menu is never empty: if detection found nothing
        # (window-state quirks), list every button on the toolbar so the
        # user always gets a usable popup.
        if added == 0:
            for action in toolbar.actions():
                w = toolbar.widgetForAction(action)
                if not isinstance(w, QToolButton):
                    continue
                label = w.toolTip() or w.text() or "(button)"
                text = f"{w.text()}  {label}".strip()
                proxy = menu.addAction(text)
                proxy.setCheckable(w.isCheckable())
                if w.isCheckable():
                    proxy.setChecked(w.isChecked())
                proxy.triggered.connect(lambda _checked=False, b=w: b.click())
                added += 1
        if added == 0:
            return
        pos = anchor_btn.mapToGlobal(QPoint(0, anchor_btn.height()))
        self._ext_menu_open = True
        try:
            menu.exec(pos)
        finally:
            self._ext_menu_open = False

    def eventFilter(self, obj, event):
        # Reposition floating panels whenever the canvas is resized.
        if getattr(self, '_using_floating_panels', False) and obj is getattr(self, 'image_label', None):
            if event.type() == QEvent.Resize:
                # Restore saved layout once, after the canvas has a real size.
                if not getattr(self, '_panel_layout_restored', True) and self.image_label.width() > 100:
                    self._panel_layout_restored = True
                    self._restore_panel_layout()
                else:
                    self._arrange_floating_panels()
        # Show/hide the context panel when a PDF/video control appears.
        if getattr(self, '_using_floating_panels', False) and obj in (
                getattr(self, '_pdf_nav_widget', None),
                getattr(self, '_video_controls_widget', None)):
            if event.type() in (QEvent.Show, QEvent.Hide):
                QTimer.singleShot(0, self._refresh_context_panel)
        # Swallow the extension button's mouse-press (which would open
        # Qt's native hover-closing popup) and show our own menu instead.
        if getattr(obj, '_owning_toolbar', None) is not None:
            et = event.type()
            if et in (QEvent.MouseButtonPress, QEvent.MouseButtonDblClick):
                try:
                    if event.button() == Qt.LeftButton:
                        self._show_overflow_menu(obj._owning_toolbar, obj)
                        return True
                except Exception:
                    pass
        return super().eventFilter(obj, event)

    def _delayed_resize(self):
        """Handle resize events with a delay to improve performance"""
        # Check if we need to move sliders to second row
        current_width = self.width()
        should_use_two_rows = current_width < self.width_threshold
        
        # Asymmetric hysteresis: easy to ENTER two-row mode, much harder to
        # LEAVE it — once the second row is visible, the user needs to
        # widen the window substantially before it collapses back. This
        # prevents the second panel from flickering away while the user
        # moves the cursor toward its leftmost icons.
        if not getattr(self, '_using_floating_panels', False) and should_use_two_rows != self.two_row_mode:
            enter_buffer = 40   # px below threshold to switch to two rows
            exit_buffer = 250   # px above threshold to switch back to one row
            if should_use_two_rows and current_width < (self.width_threshold - enter_buffer):
                self._update_toolbar_layout(current_width)
            elif not should_use_two_rows and current_width > (self.width_threshold + exit_buffer):
                self._update_toolbar_layout(current_width)
        
        # Handle image display resize using smart caching system
        if self.current_image:
            # If an animated GIF is playing, update the QMovie source size
            # so frames are rendered at an appropriate resolution.
            # The frame callback (_on_gif_frame_changed) handles the rest.
            if self.image_label.is_animation_active():
                from PySide6.QtGui import QImageReader
                reader = QImageReader(self.current_image)
                gif_size = reader.size()
                if gif_size.isValid():
                    label_size = self.image_label.size()
                    scaled = gif_size.scaled(label_size, Qt.KeepAspectRatio)
                    self.image_label.update_animation_size(scaled)
                    if self.image_label.is_animation_paused():
                        self._redraw_paused_gif_frame()
                return
            # Use smart zoom display to preserve LUT caching during resize
            self._smart_zoom_display()

    def add_to_history(self, img_path):
        # If we've navigated back in history and now show a new random image,
        # remove all forward history.
        if self.history_index < len(self.history) - 1:
            self.history = self.history[:self.history_index + 1]
            self.history_list.clear()
            for path in self.history:
                self._add_history_item(path)
        # Only add if not duplicating last
        if not self.history or (self.history and self.history[-1] != img_path):
            self.history.append(img_path)
            self._add_history_item(img_path)
        self.history_index = len(self.history) - 1

    def _add_history_item(self, img_path):
        item = QListWidgetItem(os.path.basename(img_path))
        
        # Only create thumbnails if history panel is visible to improve performance
        if self.show_history_checkbox.isChecked() and self.history_list.isVisible():
            try:
                # Use faster thumbnail loading for large collections
                thumb = load_thumbnail_pixmap(img_path, 40)
                if thumb:
                    item.setIcon(thumb)
            except Exception:
                # Skip thumbnail on error to avoid slowdown
                pass
        
        item.setToolTip(img_path)
        item.setData(Qt.UserRole, img_path)
        self.history_list.addItem(item)
        
        # Only scroll to bottom if history panel is visible
        if self.show_history_checkbox.isChecked() and self.history_list.isVisible():
            self.history_list.scrollToBottom()

    def on_history_clicked(self, item):
        img_path = item.data(Qt.UserRole)
        if img_path:
            # Update history_index to match clicked item
            try:
                idx = self.history.index(img_path)
                self.history_index = idx
            except ValueError:
                self.history_index = len(self.history) - 1
            self.display_image(img_path)
            self.current_image = img_path
            self.update_image_info(img_path)
            self.set_status_path(img_path)
            if self._auto_advance_active:
                self.timer_remaining = self.timer_spin.value()
                self._update_ring()

    def toggle_history_panel(self, checked):
        self.history_list.setVisible(bool(checked))

    def update_timer_interval(self, value):
        self.timer_interval = value
        self.circle_timer.set_total_time(value)
        if self._auto_advance_active:
            self.timer_remaining = value
            self._update_ring()

    def toggle_timer(self, checked):
        self._auto_advance_active = bool(checked)
        self._reset_timer()

    def _auto_advance_has_content(self):
        """True when auto-advance has something to browse: an image playlist or
        an open document (PDF / EPUB / CBR)."""
        return bool(self.images
                    or getattr(self, '_pdf_doc', None)
                    or getattr(self, '_epub_doc', None)
                    or getattr(self, '_cbr_doc', None))

    def _auto_advance_next(self):
        """Advance one step for the auto-advance timer.

        For a standalone document (PDF / EPUB / CBR that is not part of a mixed
        playlist) this walks to the next page/spread and loops back to the first
        page at the end, so timed page-browsing keeps going. Otherwise it falls
        back to the normal image navigation (random or sequential)."""
        doc_pdf = getattr(self, '_pdf_doc', None)
        doc_epub = getattr(self, '_epub_doc', None)
        doc_cbr = getattr(self, '_cbr_doc', None)

        if (doc_pdf or doc_epub or doc_cbr) and not self._in_playlist():
            if doc_pdf:
                step = self._spread_count()
                nxt = self._pdf_page + step
                if nxt >= doc_pdf.page_count:
                    nxt = 0
                self.clear_lines()
                self._show_pdf_page(nxt)
            elif doc_cbr:
                step = self._spread_count()
                nxt = self._cbr_page + step
                if nxt >= doc_cbr.page_count:
                    nxt = 0
                self.clear_lines()
                self._show_cbr_page(nxt)
            else:  # EPUB
                nxt = self._epub_page + 1
                if nxt >= doc_epub.page_count:
                    nxt = 0
                self.clear_lines()
                self._show_epub_page(nxt)
            return

        # Images / mixed playlist: keep the existing mode-aware behavior.
        if self.random_mode:
            self.show_random_image()
        else:
            self._manual_next_image()

    def set_status_path(self, image_path):
        # For Windows, convert slashes and prepend file:///
        url = os.path.abspath(image_path)
        # Cross-platform file URL
        file_url = 'file:///' + url.replace("\\", "/") if os.name == "nt" else 'file://' + url
        display_name = os.path.basename(url)
        color = "#b7bcc1"
        self.path_label.setText(
            f'<a href="{file_url}" style="color: {color}; text-decoration: none;">{display_name}</a>'
        )
        self.path_label.setToolTip(url)
        self.path_label.setTextInteractionFlags(Qt.TextBrowserInteraction)
        self.path_label.setOpenExternalLinks(False)  # We'll handle clicks

    def open_in_explorer(self, file_url):
        path = file_url.replace('file:///', '') if os.name == "nt" else file_url.replace('file://', '')
        path = os.path.abspath(path)
        folder = os.path.dirname(path)
        if os.name == "nt":
            os.startfile(folder)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", folder])
        else:  # linux
            subprocess.Popen(["xdg-open", folder])

    def _reset_timer(self):
        if self._auto_advance_active and self._auto_advance_has_content():
            self.timer.stop()
            self.timer_remaining = self.timer_spin.value()
            self.circle_timer.set_total_time(self.timer_spin.value())
            self._update_ring()
            self.timer.start()
        else:
            self.timer.stop()
            self.circle_timer.set_remaining_time(0)

    def _on_timer_tick(self):
        if not self._auto_advance_active or not self._auto_advance_has_content():
            self.timer.stop()
            self.circle_timer.set_remaining_time(0)
            return
        
        # A video / GIF that will advance itself keeps the countdown parked,
        # so a short interval can't cut a long clip short.
        if self._media_holds_auto_advance():
            self._update_ring()
            return

        # Don't decrease timer if paused
        if not self._timer_paused:
            self.timer_remaining -= 1
            if self.timer_remaining <= 0:
                # Advance images or document pages (mode-aware).
                self._auto_advance_next()
                # Reset the countdown for the next page/image. Image paths
                # (_manual_next_image / show_random_image) also reset this, but
                # the document paths (_show_pdf_page etc.) do not — without this
                # the timer would fire every tick instead of every interval.
                self.timer_remaining = self.timer_spin.value()
                self._update_ring()
            else:
                self._update_ring()
        # If paused, just update the ring display
        else:
            self._update_ring()

    def toggle_timer_pause(self):
        """Toggle pause/resume state of the timer when it's active"""
        if self._auto_advance_active:
            self._timer_paused = not self._timer_paused
            self.circle_timer.set_paused(self._timer_paused)
            
            # Update tooltip to show current state
            if self._timer_paused:
                self.circle_timer.setToolTip("Timer Paused - Click to Resume")
            else:
                self.circle_timer.setToolTip("Timer Running - Click to Pause")

    def _update_ring(self):
        self.circle_timer.set_total_time(self.timer_spin.value())
        self.circle_timer.set_remaining_time(self.timer_remaining)

    def toggle_sort_order(self, checked):
        """Toggle between random and alphabetical image order."""
        self.random_mode = checked
        if self.random_mode:
            self.sort_order_button.setToolTip("Order: Random")
            self.status.showMessage("Image order set to Random")
        else:
            self.sort_order_button.setToolTip("Order: Alphabetical")
            self.status.showMessage("Image order set to Alphabetical")

    def _ui_chrome_visible(self):
        """Return whether the tool UI (floating panels or toolbar) is visible."""
        if getattr(self, '_using_floating_panels', False):
            for p in getattr(self, '_floating_panels', []):
                if p is getattr(self, '_context_panel', None):
                    continue
                if p.isVisible():
                    return True
            return False
        return self.main_toolbar.isVisible()

    def toggle_toolbar_visibility(self, checked):
        """Toggle visibility of toolbars, status bar, and window decorations for immersive viewing."""
        if checked:
            # Show all UI elements
            if getattr(self, '_using_floating_panels', False):
                for p in getattr(self, '_floating_panels', []):
                    if p is getattr(self, '_context_panel', None):
                        self._refresh_context_panel()
                    else:
                        p.show()
                        p.raise_()
                self._arrange_floating_panels()
            else:
                self.main_toolbar.show()
                if self.two_row_mode:
                    self.slider_toolbar.show()
            self.status.show()
            
            # Restore original window decorations
            if self.original_window_flags is not None:
                self.setWindowFlags(self.original_window_flags)
                self.show()  # Need to call show() after changing window flags
            
            self.status.showMessage("UI elements restored")
        else:
            # Hide all UI elements for immersive experience
            if getattr(self, '_using_floating_panels', False):
                for p in getattr(self, '_floating_panels', []):
                    p.hide()
            self.main_toolbar.hide()
            self.slider_toolbar.hide()
            
            # Remove window decorations (borderless window) while preserving always on top
            window_flags = Qt.Window | Qt.FramelessWindowHint
            if self.always_on_top:
                window_flags |= Qt.WindowStaysOnTopHint
            self.setWindowFlags(window_flags)
            self.show()  # Need to call show() after changing window flags
            
            # Show temporary message before hiding status bar
            self.status.showMessage("Immersive mode - Right-click to restore UI")
            QTimer.singleShot(2000, lambda: self.status.hide() if not self._ui_chrome_visible() else None)  # Hide status after 2 seconds

    def toggle_line_drawing(self, checked):
        self.line_drawing_mode = checked
        if checked:
            # Disable other line modes when this one is activated
            self.horizontal_line_drawing_mode = False
            self.free_line_drawing_mode = False
            self.current_line_start = None
            self.hline_tool_btn.setChecked(False)
            self.free_line_tool_btn.setChecked(False)
            self._disable_eraser_silent()
            self._disable_color_snap_silent()
        # Don't clear lines when mode is deactivated - keep them visible
        self._update_cursor_and_status()

    def toggle_hline_drawing(self, checked):
        self.horizontal_line_drawing_mode = checked
        if checked:
            # Disable other line modes when this one is activated
            self.line_drawing_mode = False
            self.free_line_drawing_mode = False
            self.current_line_start = None
            self.line_tool_btn.setChecked(False)
            self.free_line_tool_btn.setChecked(False)
            self._disable_eraser_silent()
            self._disable_color_snap_silent()
        # Don't clear lines when mode is deactivated - keep them visible
        self._update_cursor_and_status()

    def toggle_free_line_drawing(self, checked):
        self.free_line_drawing_mode = checked
        if checked:
            # Disable other line modes when this one is activated
            self.line_drawing_mode = False
            self.horizontal_line_drawing_mode = False
            self.free_draw_mode = False
            self.line_tool_btn.setChecked(False)
            self.hline_tool_btn.setChecked(False)
            if hasattr(self, 'free_draw_tool_btn'):
                self.free_draw_tool_btn.setChecked(False)
            self._disable_eraser_silent()
            self._disable_color_snap_silent()
        if not checked:
            # Reset current line start when mode is deactivated
            had_pending = self.current_line_start is not None
            self.current_line_start = None
            self._clear_line_preview()
            # If a preview line was on screen, repaint the committed image cleanly
            if had_pending and self.current_image:
                self.display_image(self.current_image)
        self._update_cursor_and_status()

    def toggle_free_draw(self, checked):
        """Toggle free draw mode (continuous drawing)"""
        self.free_draw_mode = checked
        if checked:
            # Disable other line modes when this one is activated
            self.line_drawing_mode = False
            self.horizontal_line_drawing_mode = False
            self.free_line_drawing_mode = False
            self.line_tool_btn.setChecked(False)
            self.hline_tool_btn.setChecked(False)
            self.free_line_tool_btn.setChecked(False)
            self._disable_eraser_silent()
            self._disable_color_snap_silent()
            # Enable tablet tracking for pressure sensitivity, but handle events carefully
            if hasattr(self, 'image_label'):
                self.image_label.setAttribute(Qt.WA_TabletTracking, True)
            print(f"Free draw mode activated - pen pressure enabled")
        if not checked:
            # Reset drawing state when mode is deactivated
            self.current_stroke = None
            self.is_drawing = False
            # Disable tablet tracking for normal UI interaction
            if hasattr(self, 'image_label'):
                self.image_label.setAttribute(Qt.WA_TabletTracking, False)
            print(f"Free draw mode deactivated")
        self._update_cursor_and_status()

    # ───────────────────── Eraser tool ─────────────────────
    def toggle_eraser(self, checked):
        """Toggle the eraser tool (partial pixel-erase of the drawing layer)."""
        self.eraser_mode = bool(checked)
        if self.eraser_mode:
            # Disable all other drawing/line modes
            self.line_drawing_mode = False
            self.horizontal_line_drawing_mode = False
            self.free_line_drawing_mode = False
            self.free_draw_mode = False
            self.current_line_start = None
            for btn_name in ('line_tool_btn', 'hline_tool_btn',
                             'free_line_tool_btn', 'free_draw_tool_btn'):
                btn = getattr(self, btn_name, None)
                if btn is not None and btn.isChecked():
                    btn.blockSignals(True); btn.setChecked(False); btn.blockSignals(False)
            self._disable_color_snap_silent()
            # Tablet tracking not required; eraser is mouse-driven
            if hasattr(self, 'image_label'):
                self.image_label.setAttribute(Qt.WA_TabletTracking, False)
        else:
            self.current_erase_stroke = None
            self.is_erasing = False
        self._update_eraser_cursor()
        self._update_cursor_and_status()

    def _disable_eraser_silent(self):
        """Turn the eraser off without recursive signal emission."""
        self.eraser_mode = False
        self.current_erase_stroke = None
        self.is_erasing = False
        btn = getattr(self, 'eraser_tool_btn', None)
        if btn is not None and btn.isChecked():
            btn.blockSignals(True); btn.setChecked(False); btn.blockSignals(False)
        self._update_eraser_cursor()

    def update_eraser_size(self, value):
        """Update eraser diameter (1-30 screen px) and refresh the cursor."""
        self.eraser_size = int(value)
        self._update_eraser_cursor()

    def _update_eraser_cursor(self):
        """Show a round cursor sized to the eraser while active, else default."""
        if not hasattr(self, 'image_label'):
            return
        if getattr(self, 'eraser_mode', False):
            d = max(4, int(self.eraser_size))
            pix = QPixmap(d + 2, d + 2)
            pix.fill(Qt.transparent)
            p = QPainter(pix)
            p.setRenderHint(QPainter.Antialiasing, True)
            p.setPen(QPen(QColor(0, 0, 0, 200), 1))
            p.drawEllipse(1, 1, d, d)
            p.setPen(QPen(QColor(255, 255, 255, 200), 1))
            p.drawEllipse(2, 2, d - 2, d - 2)
            p.end()
            self.image_label.setCursor(QCursor(pix))
        else:
            self.image_label.unsetCursor()

    def start_erase_stroke(self, x, y):
        """Begin an erase stroke at ORIGINAL image coords (x, y)."""
        radius = self._eraser_radius_in_original_px()
        self.current_erase_stroke = [(x, y, radius)]
        self.is_erasing = True
        # ⚡ Build a one-time coordinate cache so each live erase point maps
        # label→original WITHOUT reloading/decoding the image per event (that
        # per-event reload is what froze the UI under the tablet's high event rate).
        self.eraser_cache = self._build_display_geometry_cache()
        self._invalidate_line_caches()
        if self.current_image:
            self.display_image(self.current_image)

    def _build_display_geometry_cache(self):
        """Precompute display geometry for fast original↔label coordinate mapping.

        Mirrors the math in ImageLabel._map_label_pos_to_original but resolves the
        original image size ONCE here instead of on every move event. Shared by the
        eraser hot-path and the free-line live preview."""
        try:
            label = self.image_label
            if not label or not label.pixmap() or label.pixmap().isNull():
                return None
            original_pixmap, error = safe_load_pixmap(self.current_image)
            if error or original_pixmap.isNull():
                return None
            original_size = original_pixmap.size()
            rotation = self.rotation_angle
            if rotation == 90 or rotation == 270:
                display_reference_size = QSize(original_size.height(), original_size.width())
            else:
                display_reference_size = original_size
            label_size = label.size()
            base_scaled = display_reference_size.scaled(label_size, Qt.KeepAspectRatio)
            zoom_factor = label.zoom_factor
            zoomed_width = int(base_scaled.width() * zoom_factor)
            zoomed_height = int(base_scaled.height() * zoom_factor)
            draw_x = (label_size.width() - zoomed_width) // 2 + int(label.pan_offset_x)
            draw_y = (label_size.height() - zoomed_height) // 2 + int(label.pan_offset_y)
            if rotation == 90 or rotation == 270:
                scale_x = zoomed_width / original_size.height() if original_size.height() else 1.0
                scale_y = zoomed_height / original_size.width() if original_size.width() else 1.0
            else:
                scale_x = zoomed_width / original_size.width() if original_size.width() else 1.0
                scale_y = zoomed_height / original_size.height() if original_size.height() else 1.0
            return {
                'original_size': original_size,
                'rotation': rotation,
                'flipped_h': self.flipped_h,
                'flipped_v': self.flipped_v,
                'zoomed_width': zoomed_width,
                'zoomed_height': zoomed_height,
                'draw_x': draw_x,
                'draw_y': draw_y,
                'scale_x': scale_x,
                'scale_y': scale_y,
            }
        except Exception as e:
            print(f"_build_display_geometry_cache error: {e}")
            return None

    def _build_eraser_cache(self):
        """Backward-compatible alias for the shared geometry cache."""
        return self._build_display_geometry_cache()

    def add_erase_point(self, x, y):
        """Append a point to the active erase stroke and live-update."""
        if not self.is_erasing or self.current_erase_stroke is None:
            return
        radius = self._eraser_radius_in_original_px()
        self.current_erase_stroke.append((x, y, radius))
        # ⚡ Throttle: coalesce bursts of tablet move events into ~60 FPS redraws
        # instead of running the full erase-aware redraw on every single event.
        if not self.erase_update_timer.isActive():
            self.erase_update_timer.start()

    def _flush_erase_update(self):
        """Render the in-progress erase stroke (driven by the throttle timer)."""
        if self.current_image and (self.is_erasing or self.current_erase_stroke):
            self.display_image(self.current_image)

    def end_erase_stroke(self):
        """Finalize the active erase stroke."""
        self.erase_update_timer.stop()
        if self.current_erase_stroke and len(self.current_erase_stroke) > 0:
            self.erase_strokes.append(self.current_erase_stroke)
            # Snapshot the current drawing counts so anything drawn AFTER this erase
            # is rendered on top of the erase holes (lets you re-draw over erased areas).
            self._erase_state_marks.append({
                'free_strokes': len(self.drawn_free_strokes),
                'free_lines': len(self.drawn_free_lines),
            })
            self._undo_stack.append('erase')
        self.current_erase_stroke = None
        self.is_erasing = False
        self.eraser_cache = None
        self._invalidate_line_caches()
        if self.current_image:
            self.display_image(self.current_image)

    def _eraser_radius_in_original_px(self):
        """Convert the eraser screen-px radius to original-image px so erased
        holes stay anchored to the image content at any zoom level."""
        scale = getattr(self, '_last_display_scale_x', None)
        if not scale or scale <= 0:
            scale = 1.0
        return max(1.0, (self.eraser_size / 2.0) / scale)

    def _transform_point_to_display(self, x, y, scale_x, scale_y, draw_x, draw_y, original_size):
        """Map an ORIGINAL-coords point to display coords using the same flip/
        rotation/scale sequence the line renderer uses for free strokes."""
        fx, fy = x, y
        if self.flipped_h:
            fx = original_size.width() - x
        if self.flipped_v:
            fy = original_size.height() - y
        rot = self.rotation_angle
        if rot == 90:
            tx = fy; ty = original_size.width() - fx
        elif rot == 180:
            tx = original_size.width() - fx; ty = original_size.height() - fy
        elif rot == 270:
            tx = original_size.height() - fy; ty = fx
        else:
            tx, ty = fx, fy
        dx = int(tx * scale_x) + draw_x
        dy = int(ty * scale_y) + draw_y
        return dx, dy

    def _draw_post_erase_overlay(self, pixmap, strokes, free_lines, scale_x, scale_y, draw_x, draw_y, original_size):
        """Draw drawings made AFTER the most recent erase onto the revealed image.

        The eraser exposes this image inside its holes, so these strokes stay
        visible there — letting the user draw again over an erased area. Uses the
        same point transform as the erase mask so the overlay aligns with the holes."""
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing, True)
        try:
            # Free draw strokes (continuous paths) — always safe to overlay since
            # _transform_point_to_display matches the free-stroke renderer exactly.
            for stroke in strokes:
                if len(stroke) < 2:
                    continue
                for i in range(len(stroke) - 1):
                    a = stroke[i]
                    b = stroke[i + 1]
                    if len(a) == 3 and self.pen_pressure_enabled:
                        ax, ay, ap = a
                        bx, by, bp = b
                        thickness = max(1, int(self.line_thickness * ((ap + bp) / 2.0)))
                    else:
                        ax, ay = a[0], a[1]
                        bx, by = b[0], b[1]
                        thickness = max(1, self.line_thickness)
                    sx, sy = self._transform_point_to_display(ax, ay, scale_x, scale_y, draw_x, draw_y, original_size)
                    ex, ey = self._transform_point_to_display(bx, by, scale_x, scale_y, draw_x, draw_y, original_size)
                    painter.setPen(QPen(self.line_color, thickness, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
                    painter.drawLine(sx, sy, ex, ey)
            # Two-point free lines — only overlay when un-rotated/un-flipped, where
            # the transform matches the main free-line renderer (avoids misalignment).
            if free_lines and self.rotation_angle == 0 and not self.flipped_h and not self.flipped_v:
                pen = QPen(self.line_color, max(1, self.line_thickness), Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
                painter.setPen(pen)
                for line in free_lines:
                    s = line['start']
                    e = line['end']
                    sx, sy = self._transform_point_to_display(s[0], s[1], scale_x, scale_y, draw_x, draw_y, original_size)
                    ex, ey = self._transform_point_to_display(e[0], e[1], scale_x, scale_y, draw_x, draw_y, original_size)
                    painter.drawLine(sx, sy, ex, ey)
        finally:
            painter.end()

    def _apply_erase_holes(self, target_pixmap, clean_pixmap, scale_x, scale_y, draw_x, draw_y, original_size):
        """Restore the clean (line-free) image inside each erase stroke, which
        visually erases only the touched portions of lines/drawings."""
        if not (self.erase_strokes or self.current_erase_stroke):
            return
        if clean_pixmap is None or clean_pixmap.isNull():
            return
        from PySide6.QtGui import QPolygonF
        from PySide6.QtCore import QPointF
        strokes = list(self.erase_strokes)
        if self.current_erase_stroke:
            strokes.append(self.current_erase_stroke)

        # ── Build a SOLID coverage mask of every erased region ──────────────
        # Painting opaque white strokes onto a transparent mask is robust against
        # self-overlap (back-and-forth drags): overlapping opaque paint just stays
        # opaque, with none of the odd/even or winding cancellation that made
        # path-union clipping leave holes. Cost is O(points), so it stays fast
        # even on long tablet strokes.
        mask = QPixmap(target_pixmap.size())
        mask.fill(Qt.transparent)
        mp = QPainter(mask)
        mp.setRenderHint(QPainter.Antialiasing, True)
        white = QColor(255, 255, 255, 255)
        any_drawn = False
        for stroke in strokes:
            if not stroke:
                continue
            pts = []
            max_r = 1.0
            for (x, y, r) in stroke:
                dx, dy = self._transform_point_to_display(x, y, scale_x, scale_y, draw_x, draw_y, original_size)
                rd = max(1.0, r * scale_x)
                pts.append((dx, dy))
                if rd > max_r:
                    max_r = rd
            if len(pts) == 1:
                # Single tap → a filled dot
                mp.setPen(Qt.NoPen)
                mp.setBrush(white)
                mp.drawEllipse(QPointF(pts[0][0], pts[0][1]), max_r, max_r)
            else:
                # Continuous band: a round-cap/round-join pen stroke is fully
                # solid and self-overlap-safe.
                mp.setBrush(Qt.NoBrush)
                pen = QPen(white, max_r * 2.0, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
                mp.setPen(pen)
                mp.drawPolyline(QPolygonF([QPointF(px, py) for px, py in pts]))
            any_drawn = True
        mp.end()
        if not any_drawn:
            return

        # ── Keep the clean image only inside the mask, then paint it over the
        #    drawn pixmap → reveals the untouched image exactly where erased ──
        patch = QPixmap(clean_pixmap.size())
        patch.fill(Qt.transparent)
        pp = QPainter(patch)
        pp.drawPixmap(0, 0, clean_pixmap)
        pp.setCompositionMode(QPainter.CompositionMode_DestinationIn)
        pp.drawPixmap(0, 0, mask)
        pp.end()

        ep = QPainter(target_pixmap)
        ep.drawPixmap(0, 0, patch)
        ep.end()

    def _invalidate_line_caches(self):
        """Clear caches so the next render repaints lines/erases."""
        if isinstance(getattr(self, 'enhancement_cache', None), dict):
            self.enhancement_cache.clear()
        if isinstance(getattr(self, 'scaled_cache', None), dict):
            self.scaled_cache.clear()

    # ───────────────────── Color Snap (eyedropper) tool ─────────────────────
    def toggle_color_snap(self, checked):
        """Toggle the eyedropper color-snap tool.

        While active: cursor becomes a crosshair over the image; a floating
        preview swatch follows the mouse showing the (5×5 averaged) sampled
        color; left-click commits that color as the current line color.
        Mode stays active until toggled off.
        """
        self.color_snap_mode = bool(checked)
        if self.color_snap_mode:
            # Mutually exclude with all drawing tools
            self.line_drawing_mode = False
            self.horizontal_line_drawing_mode = False
            self.free_line_drawing_mode = False
            self.free_draw_mode = False
            for btn_name in ('line_tool_btn', 'hline_tool_btn',
                             'free_line_tool_btn', 'free_draw_tool_btn', 'eraser_tool_btn'):
                btn = getattr(self, btn_name, None)
                if btn is not None and btn.isChecked():
                    btn.blockSignals(True); btn.setChecked(False); btn.blockSignals(False)
            self._disable_eraser_silent()
            if self._color_snap_preview is None:
                self._color_snap_preview = ColorSnapPreview(self)
            # Show floating palette panel (re-open if user previously closed it)
            self._show_snap_palette_window()
        else:
            if self._color_snap_preview is not None:
                self._color_snap_preview.hide()
            self._color_snap_last_label_pos = None
            self._color_snap_last_color = None
            # Cancel any pending debounced hover sample
            if hasattr(self, '_color_snap_hover_timer'):
                self._color_snap_hover_timer.stop()
            self._color_snap_pending_label_pos = None
            self._color_snap_pending_global_pos = None
            # NOTE: palette panel stays open — only the ✕ close button hides it.
            try:
                self._update_display_with_overlay()
            except Exception:
                pass
        self._update_cursor_and_status()

    def _disable_color_snap_silent(self):
        """Turn off color snap without triggering recursive toggle events."""
        if getattr(self, 'color_snap_mode', False):
            self.color_snap_mode = False
            btn = getattr(self, 'color_snap_btn', None)
            if btn is not None:
                btn.blockSignals(True); btn.setChecked(False); btn.blockSignals(False)
            if self._color_snap_preview is not None:
                self._color_snap_preview.hide()
            self._color_snap_last_label_pos = None
            self._color_snap_last_color = None
            # Cancel any pending debounced hover sample
            if hasattr(self, '_color_snap_hover_timer'):
                self._color_snap_hover_timer.stop()
            self._color_snap_pending_label_pos = None
            self._color_snap_pending_global_pos = None
            # NOTE: palette panel stays open — only the ✕ close button hides it.

    def _sample_color_at(self, original_x, original_y, radius=2):
        """Sample a (2*radius+1)² averaged color from the cached source QImage."""
        img = self._get_color_snap_source_image()
        if img is None:
            return None
        try:
            w, h = img.width(), img.height()
            cx = int(round(original_x)); cy = int(round(original_y))
            if not (0 <= cx < w and 0 <= cy < h):
                return None
            x0 = max(0, cx - radius); x1 = min(w - 1, cx + radius)
            y0 = max(0, cy - radius); y1 = min(h - 1, cy + radius)
            r_sum = g_sum = b_sum = n = 0
            for y in range(y0, y1 + 1):
                for x in range(x0, x1 + 1):
                    c = img.pixelColor(x, y)
                    r_sum += c.red(); g_sum += c.green(); b_sum += c.blue()
                    n += 1
            if n == 0:
                return None
            return QColor(r_sum // n, g_sum // n, b_sum // n)
        except Exception:
            return None

    def _get_color_snap_source_image(self):
        """Lazy-load and cache the current image as a QImage for cheap sampling.

        Invalidates when self.current_image path changes.
        """
        path = self.current_image
        if not path:
            return None
        if (self._color_snap_src_image is not None
                and self._color_snap_src_path == path):
            return self._color_snap_src_image
        try:
            pix, err = safe_load_pixmap(path)
            if err or pix.isNull():
                self._color_snap_src_image = None
                self._color_snap_src_path = None
                return None
            self._color_snap_src_image = pix.toImage()
            self._color_snap_src_path = path
            return self._color_snap_src_image
        except Exception:
            self._color_snap_src_image = None
            self._color_snap_src_path = None
            return None

    def request_color_snap_hover_sample(self, label_pos, global_pos):
        """Debounced hover entry point called by ImageLabel.

        Stores pending positions and (re)starts the idle timer. Hides preview
        immediately so the user gets feedback that sampling is paused while
        the cursor is moving.
        """
        self._color_snap_pending_label_pos = label_pos
        self._color_snap_pending_global_pos = global_pos
        prev = self._color_snap_preview
        if prev is not None and prev.isVisible():
            prev.hide()
        self._color_snap_hover_timer.start(self._color_snap_hover_ms)

    def _on_color_snap_hover_timeout(self):
        """Fired when cursor has been still long enough — sample + show preview."""
        if not self.color_snap_mode:
            return
        label_pos = self._color_snap_pending_label_pos
        global_pos = self._color_snap_pending_global_pos
        if label_pos is None or global_pos is None:
            return
        if not (hasattr(self, 'image_label') and self.image_label):
            return
        ox, oy = self.image_label._map_label_pos_to_original(label_pos)
        if ox is None:
            return
        color = self._sample_color_at(ox, oy)
        if color is not None:
            self._update_color_snap_preview(global_pos, color, label_pos)

    def _update_color_snap_preview(self, global_pos, color, label_point):
        """Reposition + recolor the floating preview swatch."""
        if not self.color_snap_mode or color is None:
            return
        if self._color_snap_preview is None:
            self._color_snap_preview = ColorSnapPreview(self)
        prev = self._color_snap_preview
        prev.set_color(color)
        try:
            screen = self.screen() if hasattr(self, 'screen') else None
            screen_rect = screen.availableGeometry() if screen else None
        except Exception:
            screen_rect = None
        offset_x, offset_y = 18, 18
        x = int(global_pos.x()) + offset_x
        y = int(global_pos.y()) + offset_y
        if screen_rect is not None:
            if x + prev.width() > screen_rect.right():
                x = int(global_pos.x()) - offset_x - prev.width()
            if y + prev.height() > screen_rect.bottom():
                y = int(global_pos.y()) - offset_y - prev.height()
        prev.move(x, y)
        if not prev.isVisible():
            prev.show()
        self._color_snap_last_label_pos = label_point
        self._color_snap_last_color = color

    def commit_color_snap(self, color):
        """Commit a sampled color as current line color + add to saved palette."""
        if color is None:
            return
        self.set_line_color(color.name())
        self._add_to_snapped_palette(color.name())
        btn = getattr(self, 'line_color_btn', None)
        if btn is not None:
            try:
                original_style = btn.styleSheet()
                pulse_style = (f"QToolButton {{ background-color: {color.name()}; "
                               f"border: 2px solid #ffcc00; }}")
                btn.setStyleSheet(pulse_style)
                QTimer.singleShot(180, lambda: btn.setStyleSheet(original_style))
            except Exception:
                pass

    # ───────────── Snap palette (visible swatches) ─────────────
    def _palette_contains(self, hex_color):
        """Global dedup check: True if hex_color appears in any row."""
        h = (hex_color or "").lower()
        for row in self.snapped_rows:
            if h in row:
                return True
        return False

    def _add_to_snapped_palette(self, hex_color):
        """Append a hex color to the most-recent row (creates one if needed).

        Globally deduplicated — if the color already exists in any row, no-op.
        """
        if not hex_color:
            return
        h = hex_color.lower()
        if self._palette_contains(h):
            return
        if not self.snapped_rows:
            self.snapped_rows.append([])
        self.snapped_rows[-1].append(h)
        self._rebuild_snap_palette_ui()

    def _remove_from_snapped_palette(self, hex_color):
        h = (hex_color or "").lower()
        for row in self.snapped_rows:
            if h in row:
                row.remove(h)
                break
        # Drop any rows that are now empty
        self.snapped_rows = [r for r in self.snapped_rows if r]
        self._rebuild_snap_palette_ui()

    def clear_snapped_palette(self):
        """Clear all saved palette swatches."""
        if not self.snapped_rows:
            return
        self.snapped_rows = []
        self._rebuild_snap_palette_ui()

    def _rebuild_snap_palette_ui(self):
        """Push current snapped_rows into the floating palette window (if any)."""
        win = getattr(self, '_snap_palette_window', None)
        if win is not None:
            try:
                win.set_rows(self.snapped_rows)
            except Exception:
                pass

    def _ensure_snap_palette_window(self):
        """Lazy-create the floating palette window and wire its signals."""
        if self._snap_palette_window is None:
            win = SnappedPaletteWindow(self)
            win.swatch_clicked.connect(self.set_line_color)
            win.swatch_removed.connect(self._remove_from_snapped_palette)
            win.closed.connect(self._hide_snap_palette_window)
            self._snap_palette_window = win
        return self._snap_palette_window

    def _show_snap_palette_window(self):
        """Show the floating palette panel.

        If already visible, only refresh its contents — don't reposition
        it (preserves any user drag). If hidden, place at last remembered
        drag position, or below the 💉 button on first open.
        """
        win = self._ensure_snap_palette_window()
        try:
            win.set_rows(self.snapped_rows)
        except Exception:
            pass
        if win.isVisible():
            # Already on-screen — leave it where the user put it
            win.raise_()
            return
        # Decide position for first show / reopen after close
        target = self._snap_palette_window_pos
        if target is None:
            # First-time placement: just below the 💉 button on the toolbar
            btn = getattr(self, 'color_snap_btn', None)
            if btn is not None:
                try:
                    target = btn.mapToGlobal(btn.rect().bottomLeft())
                    from PySide6.QtCore import QPoint
                    target = target + QPoint(0, 4)
                except Exception:
                    target = None
        if target is not None:
            # Screen-edge clamp
            try:
                from PySide6.QtGui import QGuiApplication
                scr = QGuiApplication.screenAt(target)
                if scr is None:
                    scr = QGuiApplication.primaryScreen()
                geom = scr.availableGeometry()
                w = win.sizeHint().width() or win.width() or 200
                h = win.sizeHint().height() or win.height() or 60
                x = max(geom.left(), min(target.x(), geom.right() - w))
                y = max(geom.top(), min(target.y(), geom.bottom() - h))
                win.move(x, y)
            except Exception:
                win.move(target)
        win.show()
        win.raise_()

    def _hide_snap_palette_window(self):
        """Hide the floating palette panel; remember its current position."""
        win = getattr(self, '_snap_palette_window', None)
        if win is not None and win.isVisible():
            try:
                self._snap_palette_window_pos = win.pos()
            except Exception:
                pass
            win.hide()

    def extract_palette_from_image(self):
        """Auto-extract a small set of dominant colors from the current image.

        Method: downscale image to ≤80px on the long edge, bin RGB into a
        4×4×4 cube (64 buckets), keep the top buckets by population, use the
        per-bucket average color. Cheap (~ms) and good enough for a UI palette.
        """
        img = self._get_color_snap_source_image()
        if img is None:
            self.status.showMessage("Color palette: no image loaded")
            return
        try:
            # Downscale for speed
            target = 80
            w, h = img.width(), img.height()
            if max(w, h) > target:
                if w >= h:
                    sw = target; sh = max(1, int(h * target / w))
                else:
                    sh = target; sw = max(1, int(w * target / h))
                small = img.scaled(sw, sh, Qt.KeepAspectRatio, Qt.FastTransformation)
            else:
                small = img
            sw, sh = small.width(), small.height()
            # Bin into 4×4×4 cube — bucket key = (r>>6, g>>6, b>>6)
            buckets = {}  # key -> [r_sum, g_sum, b_sum, count]
            for y in range(sh):
                for x in range(sw):
                    c = small.pixelColor(x, y)
                    r, g, b = c.red(), c.green(), c.blue()
                    # Skip near-pure black/white to avoid filling palette with borders
                    if r + g + b < 30 or r + g + b > 735:
                        # Allow if image is dominated by them — still add later if needed
                        pass
                    key = ((r >> 6) << 4) | ((g >> 6) << 2) | (b >> 6)
                    bk = buckets.get(key)
                    if bk is None:
                        buckets[key] = [r, g, b, 1]
                    else:
                        bk[0] += r; bk[1] += g; bk[2] += b; bk[3] += 1
            if not buckets:
                self.status.showMessage("Color palette: no colors extracted")
                return
            # Sort by population desc, take top 6 — build a NEW ROW
            top = sorted(buckets.values(), key=lambda v: -v[3])[:6]
            new_row = []
            for r_sum, g_sum, b_sum, n in top:
                avg = QColor(r_sum // n, g_sum // n, b_sum // n)
                hex_color = avg.name().lower()
                if not self._palette_contains(hex_color) and hex_color not in new_row:
                    new_row.append(hex_color)
            if not new_row:
                self.status.showMessage("Color palette: no new colors to extract (all already in palette)")
                # Still show the panel so user sees existing palette
                self._show_snap_palette_window()
                return
            self.snapped_rows.append(new_row)
            # Enforce row FIFO cap
            if len(self.snapped_rows) > self.snapped_rows_max:
                self.snapped_rows = self.snapped_rows[-self.snapped_rows_max:]
            self._rebuild_snap_palette_ui()
            # Auto-show panel so user can see the new row
            self._show_snap_palette_window()
            self.status.showMessage(f"Color palette: extracted {len(new_row)} color(s) into new row")
        except Exception as e:
            print(f"extract_palette_from_image failed: {e}")
            self.status.showMessage("Color palette: extraction failed")

    def _update_cursor_and_status(self):
        """Update cursor and status message based on active drawing modes"""
        if getattr(self, 'eraser_mode', False):
            self._update_eraser_cursor()
            self.status.showMessage("Eraser: drag over lines/drawings to erase them (the image stays intact)")
        elif getattr(self, 'color_snap_mode', False):
            self.image_label.setCursor(Qt.CrossCursor)
            self.status.showMessage("Color Snap: hover over image and click to pick a color (toggle button to exit)")
        elif self.free_draw_mode:
            self.image_label.setCursor(Qt.CrossCursor)
            self.status.showMessage("Free draw mode - Drag mouse to draw continuous lines")
        elif self.free_line_drawing_mode:
            self.image_label.setCursor(Qt.CrossCursor)
            if self.current_line_start is None:
                self.status.showMessage("Free line drawing mode - Click first point to start line")
            else:
                self.status.showMessage("Free line drawing mode - Click second point to complete line")
        elif self.line_drawing_mode and self.horizontal_line_drawing_mode:
            self.image_label.setCursor(Qt.CrossCursor)
            self.status.showMessage("Drawing mode active - Click to draw both vertical and horizontal lines")
        elif self.line_drawing_mode:
            self.image_label.setCursor(Qt.CrossCursor)
            self.status.showMessage("Vertical line drawing mode active - Click on image to draw vertical lines")
        elif self.horizontal_line_drawing_mode:
            self.image_label.setCursor(Qt.CrossCursor)
            self.status.showMessage("Horizontal line drawing mode active - Click on image to draw horizontal lines")
        else:
            self.image_label.setCursor(Qt.ArrowCursor)
            self.status.showMessage("")

    def toggle_pen_pressure(self, checked):
        """Toggle pen pressure sensitivity"""
        self.pen_pressure_enabled = checked
        # Reset current pressure and tablet pressure to default when disabled
        if not checked:
            self._current_pressure = 1.0
            self._tablet_pressure = 1.0
        # Update button appearance
        if hasattr(self, 'pen_pressure_btn'):
            self.pen_pressure_btn.setText("🎨" if checked else "✏")
            self.pen_pressure_btn.setToolTip("Pen Pressure: ON (varies thickness)" if checked else "Pen Pressure: OFF (fixed thickness)")
        
        # Clear caches to force redraw with new pressure setting
        if hasattr(self, '_lut_process_cache'):
            self._lut_process_cache.clear()
        self.enhancement_cache.clear()
        self.scaled_cache.clear()
        
        if self.current_image:
            self.display_image(self.current_image)
        
        self.status.showMessage(f"Pen pressure sensitivity {'enabled' if checked else 'disabled'}")

    def test_pen_pressure(self):
        """Test pen pressure detection - call this to check if your tablet is working"""
        print("🎨 PEN PRESSURE TEST: Click and drag with your pen/stylus to test pressure detection")
        print("🎨 PEN PRESSURE TEST: Look for pressure values in the console output")
        print("🎨 PEN PRESSURE TEST: If you see values other than 1.000, your tablet is working!")
        self.status.showMessage("Pen pressure test active - draw with your pen to check detection")

    def toggle_line_antialiasing(self, checked):
        """Toggle antialiasing for smoother line drawing"""
        self.line_antialiasing = checked
        # Clear caches to force redraw with new antialiasing setting
        if hasattr(self, '_lut_process_cache'):
            self._lut_process_cache.clear()
        self.enhancement_cache.clear()
        self.scaled_cache.clear()
        # Update button appearance
        if hasattr(self, 'antialiasing_btn'):
            self.antialiasing_btn.setText("✨" if checked else "⚡")
            self.antialiasing_btn.setToolTip("Antialiasing: ON (Smoother)" if checked else "Antialiasing: OFF (Faster)")
        if self.current_image:
            self.display_image(self.current_image)
        self.status.showMessage(f"Line antialiasing {'enabled' if checked else 'disabled'}")

    def update_line_thickness(self, value):
        self.line_thickness = value
        # Clear LUT cache since line appearance changed
        if hasattr(self, '_lut_process_cache'):
            self._lut_process_cache.clear()
        # Clear enhancement cache to force full redraw with new thickness
        self.enhancement_cache.clear()
        self.scaled_cache.clear()
        if self.current_image and (self.drawn_lines or self.drawn_horizontal_lines or self.drawn_free_lines or self.drawn_free_strokes
                                     or self.crosshair_overlay or self.grid_overlay):
            # Force full display_image to ensure changes are visible
            self.display_image(self.current_image)

    def update_line_transparency(self, value):
        """Update line transparency and refresh display"""
        self.line_transparency = value
        # Update the existing line color's alpha channel
        self.line_color.setAlpha(value)
        # Clear caches to force redraw with new transparency
        if hasattr(self, '_lut_process_cache'):
            self._lut_process_cache.clear()
        self.enhancement_cache.clear()
        self.scaled_cache.clear()
        # Refresh display to show transparency changes
        if self.current_image and (self.drawn_lines or self.drawn_horizontal_lines or self.drawn_free_lines or self.drawn_free_strokes
                                    or self.crosshair_overlay or self.grid_overlay):
            self.display_image(self.current_image)

    def choose_line_color(self):
        """Open color picker dialog to choose line color"""
        color = QColorDialog.getColor(self.line_color, self, "Choose Line Color")
        if color.isValid():
            self.line_color = color
            # Preserve current transparency setting
            self.line_color.setAlpha(self.line_transparency)
            # Drive the edge-detection line color too (opaque copy)
            self.edge_color = QColor(color.red(), color.green(), color.blue())
            # Update button background to show selected color
            self.line_color_btn.setStyleSheet(f"QToolButton {{ background-color: {self.line_color.name()}; border: 1px solid #666; }}")
            # Clear LUT cache since line appearance changed
            if hasattr(self, '_lut_process_cache'):
                self._lut_process_cache.clear()
            # Clear enhancement cache to force full redraw with new color
            self.enhancement_cache.clear()
            self.scaled_cache.clear()
            # Redraw current image with new color if there are lines
            if self.current_image and (self.drawn_lines or self.drawn_horizontal_lines or self.drawn_free_lines or self.drawn_free_strokes or self.edge_detection_enabled):
                # Force full display_image to ensure changes are visible
                self.display_image(self.current_image)

    def set_line_color(self, color_hex):
        """Set line color from hex string (used by preset color buttons)"""
        self.line_color = QColor(color_hex)
        # Preserve current transparency setting
        self.line_color.setAlpha(self.line_transparency)
        # Drive the edge-detection line color too (opaque copy)
        self.edge_color = QColor(color_hex)
        # Update main color button background to show selected color
        self.line_color_btn.setStyleSheet(f"QToolButton {{ background-color: {self.line_color.name()}; border: 1px solid #666; }}")
        # Clear LUT cache since line appearance changed
        if hasattr(self, '_lut_process_cache'):
            self._lut_process_cache.clear()
        # Clear enhancement cache to force full redraw with new color
        self.enhancement_cache.clear()
        self.scaled_cache.clear()
        # Redraw current image with new color if there are lines
        if self.current_image and (self.drawn_lines or self.drawn_horizontal_lines or self.drawn_free_lines or self.drawn_free_strokes or self.edge_detection_enabled):
            # Force full display_image to ensure changes are visible
            self.display_image(self.current_image)

    def add_line(self, x_position):
        if x_position not in self.drawn_lines:
            self.drawn_lines.append(x_position)
            self._undo_stack.append('line')
            # Clear LUT cache since lines changed
            if hasattr(self, '_lut_process_cache'):
                self._lut_process_cache.clear()
            # Clear enhancement cache to force full redraw with lines
            self.enhancement_cache.clear()
            self.scaled_cache.clear()
            if self.current_image:
                # Use fast GPU-accelerated line update instead of full display_image
                self._fast_line_update()

    def add_hline(self, y_position):
        if y_position not in self.drawn_horizontal_lines:
            self.drawn_horizontal_lines.append(y_position)
            self._undo_stack.append('hline')
            # Clear LUT cache since lines changed
            if hasattr(self, '_lut_process_cache'):
                self._lut_process_cache.clear()
            # Clear enhancement cache to force full redraw with lines
            self.enhancement_cache.clear()
            self.scaled_cache.clear()
            if self.current_image:
                # Use fast GPU-accelerated line update instead of full display_image
                self._fast_line_update()

    def add_free_line_point(self, x, y):
        """Handle clicks for free line drawing - first click sets start, second click completes line"""
        if self.current_line_start is None:
            # First click - set start point
            self.current_line_start = (x, y)
            # ⚡ Prepare live-preview state: snapshot the currently displayed pixmap
            # (with all committed lines baked in) and precompute display geometry so
            # the rubber-band preview can be composited cheaply on each pen move.
            self._line_preview_geom = self._build_display_geometry_cache()
            base = self.image_label.pixmap()
            self._line_preview_base = base.copy() if base and not base.isNull() else None
            self.status.showMessage(f"Line start set at ({x:.0f}, {y:.0f}) - Click second point to complete line")
        else:
            # Second click - complete the line
            start_x, start_y = self.current_line_start
            end_x, end_y = x, y
            
            # Add the completed line to our list
            line = {
                'start': (start_x, start_y),
                'end': (end_x, end_y)
            }
            self.drawn_free_lines.append(line)
            self._undo_stack.append('free_line')
            
            # Clear LUT cache since lines changed
            if hasattr(self, '_lut_process_cache'):
                self._lut_process_cache.clear()
            # Clear enhancement cache to force full redraw with lines
            self.enhancement_cache.clear()
            self.scaled_cache.clear()
            
            # Reset for next line
            self.current_line_start = None
            self._clear_line_preview()
            
            # Update display - use fast GPU-accelerated update instead of full display_image
            if self.current_image:
                # Use display_image for proper line rendering with GPU acceleration
                self.display_image(self.current_image)
            
            self.status.showMessage(f"Line drawn from ({start_x:.0f}, {start_y:.0f}) to ({end_x:.0f}, {end_y:.0f})")

    def _clear_line_preview(self):
        """Drop the free-line live-preview snapshot and geometry."""
        self._line_preview_base = None
        self._line_preview_geom = None
        if hasattr(self, 'line_preview_timer'):
            self.line_preview_timer.stop()
        self._line_preview_pending_pos = None

    def update_free_line_preview(self, label_pos):
        """Render a live rubber-band preview from the first click to the current
        pen position. Throttled to ~60 FPS so the tablet's high event rate can't
        saturate the UI thread."""
        if self.current_line_start is None or self._line_preview_base is None:
            return
        self._line_preview_pending_pos = (label_pos.x(), label_pos.y())
        if not self.line_preview_timer.isActive():
            self.line_preview_timer.start()

    def _flush_line_preview(self):
        """Composite the pending free-line preview onto the snapshot and show it."""
        if (self.current_line_start is None or self._line_preview_base is None
                or self._line_preview_pending_pos is None):
            return
        geom = self._line_preview_geom
        if not geom:
            return
        sx, sy = self.current_line_start
        start_dx, start_dy = self._transform_point_to_display(
            sx, sy, geom['scale_x'], geom['scale_y'],
            geom['draw_x'], geom['draw_y'], geom['original_size'])
        end_x, end_y = self._line_preview_pending_pos

        preview = self._line_preview_base.copy()
        painter = QPainter(preview)
        painter.setRenderHint(QPainter.Antialiasing, True)
        pen_color = QColor(self.line_color)
        pen_color.setAlpha(self.line_transparency)
        painter.setPen(QPen(pen_color, self.line_thickness, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        painter.drawLine(int(start_dx), int(start_dy), int(end_x), int(end_y))
        painter.end()
        self.image_label.setPixmap(preview)

    def start_free_draw_stroke(self, x, y, pressure=1.0):
        """⚡ PERFORMANCE: Start a new free draw stroke with aggressive caching"""
        print(f"Starting optimized free draw stroke at ({x:.1f}, {y:.1f}) with pressure {pressure:.2f}")
        
        # 🎨 PEN PRESSURE: Store initial pressure immediately
        if self.pen_pressure_enabled:
            self._current_pressure = pressure
            print(f"🎨 INITIAL PRESSURE: Set to {pressure:.3f}")
        
        # Initialize stroke data with pressure information, but mark first point's pressure as pending
        self.current_stroke = [(x, y, -1)] # Use -1 to indicate pending pressure
        self.is_drawing_free_stroke = True
        self.last_draw_point = None # Reset last draw point
        
        # ⚡ AGGRESSIVE CACHING: Pre-calculate all coordinate conversion data
        try:
            # Get current display state once and cache it
            label = self.image_label
            if not label or not label.pixmap() or label.pixmap().isNull():
                return
                
            # Cache display parameters for ultra-fast coordinate conversion
            self.drawing_cache = {
                'label_size': label.size(),
                'zoom_factor': label.zoom_factor,
                'pan_offset_x': label.pan_offset_x,
                'pan_offset_y': label.pan_offset_y,
                'rotation': self.rotation_angle,
                'flipped_h': self.flipped_h,
                'flipped_v': self.flipped_v
            }
            
            # Load original image dimensions once
            original_pixmap, error = safe_load_pixmap(self.current_image)
            if error or original_pixmap.isNull():
                return
            self.drawing_cache['original_size'] = original_pixmap.size()
            
            # Pre-calculate display transformation matrices
            rotation = self.drawing_cache['rotation']
            original_size = self.drawing_cache['original_size']
            
            if rotation == 90 or rotation == 270:
                display_reference_size = QSize(original_size.height(), original_size.width())
            else:
                display_reference_size = original_size
            
            label_size = self.drawing_cache['label_size']
            base_scaled = display_reference_size.scaled(label_size, Qt.KeepAspectRatio)
            
            zoom_factor = self.drawing_cache['zoom_factor']
            zoomed_width = int(base_scaled.width() * zoom_factor)
            zoomed_height = int(base_scaled.height() * zoom_factor)
            
            draw_x = (label_size.width() - zoomed_width) // 2 + int(self.drawing_cache['pan_offset_x'])
            draw_y = (label_size.height() - zoomed_height) // 2 + int(self.drawing_cache['pan_offset_y'])
            
            # Cache all transformation parameters
            self.drawing_cache.update({
                'display_reference_size': display_reference_size,
                'zoomed_width': zoomed_width,
                'zoomed_height': zoomed_height,
                'draw_x': draw_x,
                'draw_y': draw_y
            })
            
            # ⚡ DIRECT-TO-SCREEN: Create temporary overlay for real-time drawing
            current_pixmap = label.pixmap()
            self.temp_stroke_overlay = QPixmap(current_pixmap.size())
            self.temp_stroke_overlay.fill(Qt.transparent)
            
            # ⚡ PRE-ALLOCATE: Create QImage for direct pixel access
            self._stroke_image = QImage(current_pixmap.size(), QImage.Format.Format_ARGB32)
            self._stroke_image.fill(Qt.transparent)
            
            # Initialize last point for incremental drawing
            self.last_draw_point = None
            
            print(f"Optimized drawing cache initialized - ready for true real-time performance")
            self.status.showMessage("Ultra-fast real-time free drawing active")
            
        except Exception as e:
            print(f"Error initializing drawing cache: {e}")
            self.is_drawing_free_stroke = False

    def add_free_draw_point(self, x, y, pressure=1.0):
        """⚡ INCREMENTAL PAINTING: Add point with ultra-fast incremental drawing"""
        if not self.is_drawing_free_stroke or not self.drawing_cache:
            return

        # Add to stroke data (store original coordinates with pressure)
        self.current_stroke.append((x, y, pressure))

        # ⚡ ULTRA-FAST coordinate conversion using cached data
        cache = self.drawing_cache
        label_size = cache['label_size']
        zoom_factor = cache['zoom_factor']

        # Convert to screen coordinates using cached parameters
        rotation = cache['rotation']
        original_size = cache['original_size']

        if rotation == 90 or rotation == 270:
            scale_x = cache['zoomed_width'] / original_size.height()
            scale_y = cache['zoomed_height'] / original_size.width()
        else:
            scale_x = cache['zoomed_width'] / original_size.width()
            scale_y = cache['zoomed_height'] / original_size.height()

        # Apply transformations using cached data
        display_x = x * scale_x
        display_y = y * scale_y

        # Apply rotation (reverse transformation)
        if rotation == 90:
            screen_x = original_size.width() * scale_x - display_y
            screen_y = display_x
        elif rotation == 180:
            screen_x = original_size.width() * scale_x - display_x
            screen_y = original_size.height() * scale_y - display_y
        elif rotation == 270:
            screen_x = display_y
            screen_y = original_size.height() * scale_y - display_x
        else:  # 0 degrees
            screen_x = display_x
            screen_y = display_y

        # Apply flips
        if cache['flipped_h']:
            screen_x = cache['zoomed_width'] - screen_x
        if cache['flipped_v']:
            screen_y = cache['zoomed_height'] - screen_y

        # Final screen position
        final_x = screen_x + cache['draw_x']
        final_y = screen_y + cache['draw_y']

        # ⚡ INCREMENTAL PAINTING: Only paint the new segment
        if self.last_draw_point is not None:
            # A single round-capped/round-joined line already renders smoothly, so we
            # paint one segment per point instead of subdividing it into many collinear
            # micro-segments (which only added CPU cost and made the pen feel heavy).
            self._paint_stroke_segment_realtime(self.last_draw_point, (final_x, final_y), pressure)
        else:
            # First point - paint a small dot with correct pressure
            # 🎨 PEN PRESSURE: Ensure first point uses the actual pressure, not default
            if self.pen_pressure_enabled:
                self._current_pressure = pressure
            self._paint_stroke_segment_realtime((final_x, final_y), (final_x, final_y), pressure)

        self.last_draw_point = (final_x, final_y)

    def _pressure_to_thickness(self, pressure):
        """🎨 SINGLE SOURCE OF TRUTH: Map pen pressure to line thickness.

        Used by BOTH the live preview (while drawing) and the final committed
        render (after pen release) so the stroke keeps the exact same width.
        When pen pressure is disabled, returns the plain base thickness.
        """
        if not self.pen_pressure_enabled:
            return max(1, self.line_thickness)

        # 🎨 ENHANCED PRESSURE MAPPING: Use a curve for more natural feel
        # Apply a slight curve to make light pressure more usable
        curved_pressure = max(0.0, pressure) ** 0.8  # Power curve for natural response

        # Map pressure to thickness with a better range
        min_thickness = max(1, int(self.line_thickness * 0.2))  # Minimum 20% of base thickness
        max_thickness = int(self.line_thickness * 1.5)  # Maximum 150% of base thickness
        thickness_range = max_thickness - min_thickness
        base_thickness = min_thickness + int(thickness_range * curved_pressure)
        return max(1, base_thickness)

    def _paint_stroke_segment_realtime(self, start_point, end_point, pressure=1.0):
        """⚡ ULTRA-FAST: Hybrid drawing with performance/quality options and pressure support"""
        if not self.temp_stroke_overlay:
            return
            
        # Choose drawing method based on performance mode and antialiasing settings
        # Use smooth drawing more aggressively for better quality, especially for free draw
        use_smooth = self.line_antialiasing or not self.performance_mode or self.free_draw_mode
        
        # 🎨 PEN PRESSURE: Improved pressure handling with better interpolation
        if self.pen_pressure_enabled:
            # Use the pressure parameter with better fallback logic
            if pressure != 1.0:
                actual_pressure = pressure
            elif hasattr(self, '_current_pressure') and self._current_pressure != 1.0:
                actual_pressure = self._current_pressure
            elif hasattr(self, '_tablet_pressure') and self._tablet_pressure != 1.0:
                actual_pressure = self._tablet_pressure
            else:
                actual_pressure = 1.0

            # 🎨 Use shared formula so the final render matches this preview exactly
            dynamic_thickness = self._pressure_to_thickness(actual_pressure)
        else:
            dynamic_thickness = max(1, self.line_thickness)
        
        if use_smooth:
            # ✨ HIGH-QUALITY: Use QPainter with antialiasing for smooth lines
            self._paint_smooth_segment(start_point, end_point, dynamic_thickness)
        else:
            # ⚡ ULTRA-FAST: Direct pixel manipulation for maximum speed
            self._paint_fast_segment(start_point, end_point, dynamic_thickness)
        
        # ⚡ TIMER-BASED UPDATE: Schedule a display refresh. Use a "start only if
        # not already pending" pattern (NOT stop+restart): restarting on every pen
        # move meant a continuous fast stroke never left an idle gap for the timer
        # to fire, so the line only appeared once the pen slowed/lifted. This way
        # the overlay refreshes at a steady cadence *during* the stroke.
        if not self.stroke_update_timer.isActive():
            self.stroke_update_timer.start()

    def _paint_smooth_segment(self, start_point, end_point, thickness=None):
        """✨ HIGH-QUALITY: Smooth antialiased line drawing with improved interpolation"""
        if not hasattr(self, '_stroke_image'):
            self._stroke_image = self.temp_stroke_overlay.toImage()
            
        # Use provided thickness directly - it's already calculated with pressure
        line_thickness = thickness if thickness is not None else self.line_thickness
        
        # Create painter for smooth drawing with proper error handling
        painter = QPainter()
        if not painter.begin(self._stroke_image):
            print("ERROR: Failed to begin painting on stroke image")
            return
            
        try:
            # ✨ ENHANCED ANTIALIASING: Use highest quality rendering
            painter.setRenderHint(QPainter.Antialiasing, True)
            painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
            painter.setRenderHint(QPainter.VerticalSubpixelPositioning, True)
            painter.setRenderHint(QPainter.LosslessImageRendering, True)
            
            # Configure pen for ultra-smooth lines with the exact thickness provided
            # Ensure transparency is applied to the line color
            line_color_with_alpha = QColor(self.line_color)
            line_color_with_alpha.setAlpha(self.line_transparency)
            pen = QPen(line_color_with_alpha, line_thickness, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
            painter.setPen(pen)
            
            # Get coordinates
            x0, y0 = start_point
            x1, y1 = end_point

            # A round cap + round join pen renders a straight segment perfectly
            # smooth on its own; subdividing it into collinear sub-segments only
            # multiplied painter work and made fast strokes feel laggy.
            painter.drawLine(x0, y0, x1, y1)

        finally:
            painter.end()

    def _paint_fast_segment(self, start_point, end_point, thickness=None):
        """⚡ ULTRA-FAST: Direct pixel manipulation for maximum speed"""
        if not hasattr(self, '_stroke_image'):
            self._stroke_image = self.temp_stroke_overlay.toImage()
            
        # Use provided thickness directly - it's already calculated with pressure
        line_thickness = thickness if thickness is not None else self.line_thickness
            
        image = self._stroke_image
        
        # ⚡ ULTRA-FAST: Bresenham's algorithm for direct pixel access
        x0, y0 = int(start_point[0]), int(start_point[1])
        x1, y1 = int(end_point[0]), int(end_point[1])
        
        # Handle single point (dot) - ultra-fast
        if x0 == x1 and y0 == y1:
            if 0 <= x0 < image.width() and 0 <= y0 < image.height():
                image.setPixel(x0, y0, self._get_pixel_color())
            return
            
        # ⚡ OPTIMIZED BRESENHAM: Integer-only arithmetic for maximum speed
        dx = abs(x1 - x0)
        dy = abs(y1 - y0)
        sx = 1 if x0 < x1 else -1
        sy = 1 if y0 < y1 else -1
        err = dx - dy
        
        # Pre-calculate bounds and color
        width = image.width()
        height = image.height()
        pixel_color = self._get_pixel_color()
        
        # Draw thicker lines if needed
        if line_thickness == 1:
            # Single pixel line
            while True:
                if 0 <= x0 < width and 0 <= y0 < height:
                    image.setPixel(x0, y0, pixel_color)
                    
                if x0 == x1 and y0 == y1:
                    break
                    
                e2 = 2 * err
                if e2 > -dy:
                    err -= dy
                    x0 += sx
                if e2 < dx:
                    err += dx
                    y0 += sy
        else:
            # Multi-pixel thickness - draw multiple parallel lines
            for t in range(-(line_thickness//2), line_thickness//2 + 1):
                cx0, cy0 = x0, y0
                cx1, cy1 = x1, y1
                
                # Offset perpendicular to line direction
                if dx > dy:
                    cy0 += t
                    cy1 += t
                else:
                    cx0 += t
                    cy1 += t
                
                # Draw offset line
                dx_t = abs(cx1 - cx0)
                dy_t = abs(cy1 - cy0)
                sx_t = 1 if cx0 < cx1 else -1
                sy_t = 1 if cy0 < cy1 else -1
                err_t = dx_t - dy_t
                
                while True:
                    if 0 <= cx0 < width and 0 <= cy0 < height:
                        image.setPixel(cx0, cy0, pixel_color)
                        
                    if cx0 == cx1 and cy0 == cy1:
                        break
                        
                    e2 = 2 * err_t
                    if e2 > -dy_t:
                        err_t -= dy_t
                        cx0 += sx_t
                    if e2 < dx_t:
                        err_t += dx_t
                        cy0 += sy_t

    def _get_pixel_color(self):
        """Get pixel color value for direct pixel access in ARGB32 format"""
        # Convert QColor to ARGB32 format for QImage.setPixel with current transparency
        color = QColor(self.line_color)
        color.setAlpha(self.line_transparency)
        return (color.alpha() << 24) | (color.red() << 16) | (color.green() << 8) | color.blue()

    def _reset_stroke_image(self):
        """Reset the stroke image to prevent painter conflicts"""
        if hasattr(self, '_stroke_image'):
            # Clear the reference to allow garbage collection
            self._stroke_image = None

    def _update_display_with_overlay(self):
        """⚡ TIMER-BASED: Update display only when timer fires for optimal performance"""
        if not hasattr(self, '_stroke_image') or not self.temp_stroke_overlay:
            return
            
        # ⚠️ FIX: Ensure no active painters before converting back to pixmap
        # Create a copy of the image to avoid conflicts with active painters
        stroke_image_copy = QImage(self._stroke_image)
        self.temp_stroke_overlay = QPixmap.fromImage(stroke_image_copy)
            
        label = self.image_label
        if not label or not label.pixmap():
            return
        
        # ⚡ PRE-ALLOCATED COMPOSITE: Reuse composite buffer to avoid allocation overhead
        if not hasattr(self, '_composite_buffer'):
            base_pixmap = label.pixmap()
            self._composite_buffer = QPixmap(base_pixmap.size())
            
        base_pixmap = label.pixmap()
        
        # ⚡ FAST COMPOSITE: Direct painter operations without transparency fill
        painter = QPainter(self._composite_buffer)
        painter.drawPixmap(0, 0, base_pixmap)
        painter.drawPixmap(0, 0, self.temp_stroke_overlay)
        painter.end()
        
        # ⚡ INSTANT DISPLAY: Set pixmap directly
        label.setPixmap(self._composite_buffer)

    def end_free_draw_stroke(self):
        """⚡ OPTIMIZED FINALIZATION: Clean finalization with single redraw"""
        print(f"Ending optimized free draw stroke")
        
        if self.current_stroke is not None and len(self.current_stroke) > 1:
            # Add completed stroke to the permanent list
            self.drawn_free_strokes.append(self.current_stroke.copy())
            self._undo_stack.append('free_stroke')
            print(f"Stroke completed with {len(self.current_stroke)} points, total strokes: {len(self.drawn_free_strokes)}")
            
            # ⚡ CLEAN FINALIZATION: Clear caches and perform single clean redraw
            if hasattr(self, '_lut_process_cache'):
                self._lut_process_cache.clear()
            if hasattr(self, 'enhancement_cache'):
                self.enhancement_cache.clear()
            if hasattr(self, 'scaled_cache'):
                self.scaled_cache.clear()
            
            self.status.showMessage(f"Stroke finalized ({len(self.current_stroke)} points)")
        else:
            print(f"Stroke too short or invalid: {self.current_stroke}")
        
        # ⚡ CLEANUP: Reset all performance optimization state
        self.current_stroke = None
        self.is_drawing_free_stroke = False
        self.drawing_cache = None
        self.temp_stroke_overlay = None
        self.last_draw_point = None
        
        # ⚡ CLEANUP: Reset ultra-fast drawing buffers
        if hasattr(self, '_stroke_image'):
            delattr(self, '_stroke_image')
        if hasattr(self, '_composite_buffer'):
            delattr(self, '_composite_buffer')
        
        # ⚡ SINGLE FINAL REDRAW: "Bake" the stroke into the image with full processing
        if self.current_image:
            print(f"DEBUG: Performing final optimized redraw")
            self.display_image(self.current_image)

    def clear_lines(self):
        self.drawn_lines.clear()
        self.drawn_horizontal_lines.clear()
        self.drawn_free_lines.clear()
        self.drawn_free_strokes.clear()  # NEW: Clear free draw strokes
        self.erase_strokes.clear()  # 🧽 Clear eraser strokes
        self._erase_state_marks.clear()
        self.current_erase_stroke = None
        self.is_erasing = False
        self.current_line_start = None
        self.current_stroke = None  # NEW: Clear current stroke
        self.is_drawing = False  # NEW: Reset drawing state
        # Clear LUT cache since lines changed
        if hasattr(self, '_lut_process_cache'):
            self._lut_process_cache.clear()
        # Clear enhancement cache to force full redraw
        self.enhancement_cache.clear()
        self.scaled_cache.clear()
        if self.current_image:
            # Force full display_image to ensure changes are visible
            self.display_image(self.current_image)

    def undo_last_line(self):
        """Remove the most recently added annotation in chronological order."""
        # Cancel a half-drawn free line first (no stack entry for an uncommitted line).
        if self.current_line_start is not None:
            self.current_line_start = None
            self._clear_line_preview()
            self._update_cursor_and_status()
            self.status.showMessage("Cancelled free line")
            return

        if not self._undo_stack:
            self.status.showMessage("No lines to remove")
            return

        action = self._undo_stack.pop()
        if action == 'erase':
            if self.erase_strokes:
                self.erase_strokes.pop()
            if self._erase_state_marks:
                self._erase_state_marks.pop()
        elif action == 'free_stroke':
            if self.drawn_free_strokes:
                self.drawn_free_strokes.pop()
        elif action == 'free_line':
            if self.drawn_free_lines:
                self.drawn_free_lines.pop()
        elif action == 'hline':
            if self.drawn_horizontal_lines:
                self.drawn_horizontal_lines.pop()
        elif action == 'line':
            if self.drawn_lines:
                self.drawn_lines.pop()

        if hasattr(self, '_lut_process_cache'):
            self._lut_process_cache.clear()
        self.enhancement_cache.clear()
        self.scaled_cache.clear()
        if self.current_image:
            self.display_image(self.current_image)
        self.status.showMessage("Removed last line")

    def toggle_line_visibility(self, checked):
        """Toggle visibility of all drawn lines"""
        self.lines_visible = checked
        # Update button appearance
        if hasattr(self, 'toggle_lines_btn'):
            self.toggle_lines_btn.setText("👁" if checked else "🙈")  # Eye open/closed
            self.toggle_lines_btn.setToolTip("Hide Lines" if checked else "Show Lines")
        
        # Clear LUT cache since line visibility changed
        if hasattr(self, '_lut_process_cache'):
            self._lut_process_cache.clear()
        # Clear enhancement cache to force full redraw
        self.enhancement_cache.clear()
        self.scaled_cache.clear()
        
        # Redraw current image to show/hide lines
        if self.current_image:
            # Force full display_image to ensure visibility changes are applied
            self.display_image(self.current_image)

    def toggle_crosshair_overlay(self, checked):
        """Toggle the centered crosshair overlay."""
        self.crosshair_overlay = bool(checked)
        if hasattr(self, 'crosshair_tool_btn'):
            self.crosshair_tool_btn.blockSignals(True)
            self.crosshair_tool_btn.setChecked(self.crosshair_overlay)
            self.crosshair_tool_btn.blockSignals(False)
        self.enhancement_cache.clear()
        self.scaled_cache.clear()
        if self.current_image:
            self.display_image(self.current_image)

    def toggle_grid_overlay(self, checked):
        """Toggle the 3x3 grid overlay."""
        self.grid_overlay = bool(checked)
        if hasattr(self, 'grid_tool_btn'):
            self.grid_tool_btn.blockSignals(True)
            self.grid_tool_btn.setChecked(self.grid_overlay)
            self.grid_tool_btn.blockSignals(False)
        self.enhancement_cache.clear()
        self.scaled_cache.clear()
        if self.current_image:
            self.display_image(self.current_image)

    def save_current_view(self):
        """Save the currently displayed view to a file, including LUT/enhancements and visible lines."""
        # Create the final pixmap representing current view
        try:
            # If there is a currently displayed pixmap, use it (includes LUT/enhancements and possibly lines)
            if self.image_label and self.image_label.pixmap() and not self.image_label.pixmap().isNull():
                final_pixmap = self.image_label.pixmap().copy()
            else:
                # Fallback: render current image as display_image would
                if not self.current_image:
                    self.status.showMessage("No image loaded to save")
                    return
                # Force a full render into pixmap
                self.display_image(self.current_image)
                if not self.image_label.pixmap() or self.image_label.pixmap().isNull():
                    self.status.showMessage("Failed to render image for saving")
                    return
                final_pixmap = self.image_label.pixmap().copy()

            # Auto-save to the Downloads folder with a unique name (no dialog)
            downloads_dir = os.path.join(os.path.expanduser("~"), "Downloads")
            try:
                if not os.path.isdir(downloads_dir):
                    os.makedirs(downloads_dir, exist_ok=True)
            except Exception:
                # Fall back to the home directory if Downloads is unavailable
                downloads_dir = os.path.expanduser("~")

            # Build a unique filename from the source name + timestamp
            if self.current_image:
                base = os.path.splitext(os.path.basename(self.current_image))[0]
            else:
                base = "view"
            from datetime import datetime
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            file_name = f"OvaViewer_{base}_{timestamp}.png"
            file_path = os.path.join(downloads_dir, file_name)

            # Guard against collisions (e.g. multiple saves within the same second)
            counter = 1
            while os.path.exists(file_path):
                file_name = f"OvaViewer_{base}_{timestamp}_{counter}.png"
                file_path = os.path.join(downloads_dir, file_name)
                counter += 1

            # Save as PNG - preserves pixel data including lines and transparency
            saved = final_pixmap.save(file_path, 'PNG')
            if saved:
                self.status.showMessage(f"Saved to Downloads: {file_name}")
            else:
                self.status.showMessage("Failed to save image")

        except Exception as e:
            print(f"Error saving current view: {e}")
            self.status.showMessage("Error saving current view")

    def toggle_always_on_top(self, checked):
        self.always_on_top = checked
        self.setWindowFlag(Qt.WindowStaysOnTopHint, checked)
        self._update_title()  # Update title to reflect always on top status
        self.show()  # Necessary to apply the window flag change immediately

    def toggle_fullscreen(self, checked=None):
        """Toggle fullscreen mode with enhanced error handling"""
        # If no parameter provided, toggle current state
        if checked is None:
            checked = not self.is_fullscreen
            
        print(f"toggle_fullscreen called with checked={checked}, current state={self.is_fullscreen}")
        
        try:
            if checked and not self.is_fullscreen:
                # Entering fullscreen
                print("Entering fullscreen mode...")
                self.normal_geometry = self.geometry()
                print(f"Stored geometry: {self.normal_geometry}")
                
                # Hide status bar in fullscreen
                self.statusBar().hide()
                
                # Use Qt's fullscreen method
                self.setWindowState(Qt.WindowFullScreen)
                self.showFullScreen()
                
                self.is_fullscreen = True
                self.activateWindow()
                self.raise_()
                self.setFocus()
                self.status.showMessage("FULLSCREEN MODE - Press Alt+F4 to exit, or Esc")
                
            elif not checked and self.is_fullscreen:
                # Exiting fullscreen
                print("Exiting fullscreen mode...")
                
                # Show status bar again
                self.statusBar().show()
                
                # Exit fullscreen using multiple methods
                self.setWindowState(Qt.WindowNoState)
                self.showNormal()
                
                if self.normal_geometry and self.normal_geometry.isValid():
                    print(f"Restoring geometry: {self.normal_geometry}")
                    self.setGeometry(self.normal_geometry)
                else:
                    print("Using default geometry")
                    self.resize(950, 650)
                    self.move(100, 100)
                
                self.is_fullscreen = False
                self.activateWindow()
                self.raise_()
                self.setFocus()
                self.status.showMessage("Fullscreen mode disabled")
            
            # Update button state
            if hasattr(self, 'fullscreen_btn'):
                self.fullscreen_btn.blockSignals(True)
                self.fullscreen_btn.setChecked(self.is_fullscreen)
                self.fullscreen_btn.blockSignals(False)
                
            print(f"Fullscreen toggle complete. New state: {self.is_fullscreen}")
            
        except Exception as e:
            print(f"Error in toggle_fullscreen: {e}")
            # Fallback: force exit
            self.force_exit_fullscreen()

    def exit_fullscreen(self):
        """Explicitly exit fullscreen mode"""
        print("exit_fullscreen called")
        if self.is_fullscreen:
            self.toggle_fullscreen(False)

    def force_exit_fullscreen(self):
        """Force exit fullscreen mode using multiple methods"""
        print("force_exit_fullscreen called - using all available methods")
        
        try:
            # Method 1: Set state and use showNormal
            self.is_fullscreen = False
            self.showNormal()
            
            # Method 2: Try setWindowState
            self.setWindowState(Qt.WindowNoState)
            
            # Method 3: Windows-specific API call (if available)
            if os.name == "nt" and ctypes:
                try:
                    # Get window handle
                    hwnd = int(self.winId())
                    # Force window to normal state using Windows API
                    ctypes.windll.user32.ShowWindow(hwnd, 1)  # SW_SHOWNORMAL = 1
                    ctypes.windll.user32.SetForegroundWindow(hwnd)
                    print("Used Windows API to force normal window state")
                except Exception as e:
                    print(f"Windows API call failed: {e}")
            
            # Method 4: Show status bar
            self.statusBar().show()
            
            # Method 5: Restore geometry if available
            if self.normal_geometry and self.normal_geometry.isValid():
                print(f"Force restoring geometry: {self.normal_geometry}")
                self.setGeometry(self.normal_geometry)
            else:
                print("No stored geometry, using default size")
                self.resize(950, 650)
                self.move(100, 100)
            
            # Method 6: Force window focus and update
            self.activateWindow()
            self.raise_()
            self.setFocus()
            self.update()
            self.repaint()
            
            # Update button states
            if hasattr(self, 'fullscreen_btn'):
                self.fullscreen_btn.blockSignals(True)
                self.fullscreen_btn.setChecked(False)
                self.fullscreen_btn.blockSignals(False)
            
            self.status.showMessage("Fullscreen mode force exited")
            print(f"Force exit complete. Window state: {self.windowState()}")
            
        except Exception as e:
            print(f"Error in force_exit_fullscreen: {e}")
            # Last resort: try to close and restart
            print("Last resort: attempting emergency close...")
            self.close()

    def toggle_grayscale(self, checked):
        try:
            self.grayscale_value = 100 if checked else 0
            
            # Safely update slider value
            if hasattr(self, 'grayscale_slider') and self.grayscale_slider is not None:
                self.grayscale_slider.setValue(self.grayscale_value)
            
            # Update toggle button state (block signals to prevent infinite loop)
            if hasattr(self, 'grayscale_toggle_btn') and self.grayscale_toggle_btn is not None:
                self.grayscale_toggle_btn.blockSignals(True)
                self.grayscale_toggle_btn.setChecked(checked)
                self.grayscale_toggle_btn.blockSignals(False)
            
            # Clear caches and force immediate update
            self.enhancement_cache.clear()
            self.scaled_cache.clear()
            self._update_enhancement_menu_states()
            
            # Safely update image display
            if self.current_image:
                self.display_image(self.current_image)
                
        except Exception as e:
            print(f"Error in toggle_grayscale: {e}")
            # Don't let the error crash the app, just log it
            import traceback
            traceback.print_exc()

    def toggle_contrast(self, checked=None):
        """Toggle contrast between normal (50) and enhanced (100)"""
        try:
            if checked is None:
                # Toggle between current and normal
                self.contrast_value = 50 if self.contrast_value != 50 else 100
                checked = self.contrast_value != 50
            else:
                # Set based on checked state
                self.contrast_value = 100 if checked else 50
            
            # Safely update slider value
            if hasattr(self, 'contrast_slider') and self.contrast_slider is not None:
                self.contrast_slider.setValue(self.contrast_value)
            
            # Update toggle button state (block signals to prevent infinite loop)
            if hasattr(self, 'contrast_toggle_btn') and self.contrast_toggle_btn is not None:
                self.contrast_toggle_btn.blockSignals(True)
                self.contrast_toggle_btn.setChecked(checked)
                self.contrast_toggle_btn.blockSignals(False)
            
            # Clear caches and force immediate update
            self.enhancement_cache.clear()
            self.scaled_cache.clear()
            self._update_enhancement_menu_states()
            
            # Safely update image display
            if self.current_image:
                self.display_image(self.current_image)
                
        except Exception as e:
            print(f"Error in toggle_contrast: {e}")
            # Don't let the error crash the app, just log it
            import traceback
            traceback.print_exc()

    def toggle_gamma(self, checked=None):
        """Toggle gamma between normal (0) and enhanced (100)"""
        try:
            if checked is None:
                # Toggle between current and normal
                self.gamma_value = 0 if self.gamma_value != 0 else 100
                checked = self.gamma_value != 0
            else:
                # Set based on checked state
                self.gamma_value = 100 if checked else 0
            
            # Safely update slider value
            if hasattr(self, 'gamma_slider') and self.gamma_slider is not None:
                self.gamma_slider.setValue(self.gamma_value)
            
            # Update toggle button state (block signals to prevent infinite loop)
            if hasattr(self, 'gamma_toggle_btn') and self.gamma_toggle_btn is not None:
                self.gamma_toggle_btn.blockSignals(True)
                self.gamma_toggle_btn.setChecked(checked)
                self.gamma_toggle_btn.blockSignals(False)
            
            # Clear caches and force immediate update
            self.enhancement_cache.clear()
            self.scaled_cache.clear()
            self._update_enhancement_menu_states()
            
            # Safely update image display
            if self.current_image:
                self.display_image(self.current_image)
                
        except Exception as e:
            print(f"Error in toggle_gamma: {e}")
            # Don't let the error crash the app, just log it
            import traceback
            traceback.print_exc()

    def toggle_image_visibility(self, checked):
        """Toggle image visibility while keeping lines visible"""
        try:
            self.image_visible = checked
            
            # Update button state
            if hasattr(self, 'toggle_image_btn'):
                self.toggle_image_btn.blockSignals(True)
                self.toggle_image_btn.setChecked(checked)
                self.toggle_image_btn.blockSignals(False)
            
            # Force immediate update
            if self.current_image:
                self.display_image(self.current_image)
                
            # Update status
            if checked:
                self.status.showMessage("Image visible")
            else:
                self.status.showMessage("Image hidden - lines only")
                
        except Exception as e:
            print(f"Error in toggle_image_visibility: {e}")
            import traceback
            traceback.print_exc()

    def _update_enhancement_menu_states(self):
        """Update the checked state of enhancement menu actions"""
        if hasattr(self, 'grayscale_menu_action'):
            self.grayscale_menu_action.setChecked(self.grayscale_value > 0)
        if hasattr(self, 'contrast_menu_action'):
            self.contrast_menu_action.setChecked(self.contrast_value != 50)
        if hasattr(self, 'gamma_menu_action'):
            self.gamma_menu_action.setChecked(self.gamma_value != 0)

    def update_grayscale(self, value):
        self.grayscale_value = value
        # Update toggle button state (block signals to prevent loops)
        if hasattr(self, 'grayscale_toggle_btn') and self.grayscale_toggle_btn is not None:
            self.grayscale_toggle_btn.blockSignals(True)
            self.grayscale_toggle_btn.setChecked(value > 0)
            self.grayscale_toggle_btn.blockSignals(False)
        # Clear enhancement cache when settings change
        self.enhancement_cache.clear()
        self.scaled_cache.clear()  # Also clear scaled cache to force refresh
        self._update_enhancement_menu_states()
        if self.current_image:
            self.display_image(self.current_image)

    def _curves_signature(self):
        """Compact string of all curves params for cache invalidation."""
        if not self.curves_enabled:
            return "0"
        parts = ["1"]
        for ch in ("master", "r", "g", "b"):
            parts.append(f"{self.curves_black[ch]}.{self.curves_white[ch]}.{self.curves_gamma[ch]}")
        parts.append(f"o{self.curves_opacity}")
        return "_".join(parts)

    def _sync_curves_sliders(self):
        """Push the active channel's stored values into the curves window."""
        win = getattr(self, '_curves_window', None)
        if win is not None:
            ch = self.curves_channel
            win.set_values(self.curves_black[ch], self.curves_white[ch],
                           self.curves_gamma[ch])
            win.set_opacity(self.curves_opacity)

    def _refresh_curves(self):
        """Redisplay after a curve value changes, auto-enabling the effect.

        Mirrors the contrast/gamma sliders: dragging a curve slider turns the
        effect on so the change is visible immediately. The window's Enable
        checkbox and the toolbar button are kept in sync.
        """
        if not self.curves_enabled:
            self.curves_enabled = True
            self._sync_curves_enabled_ui()
        self.enhancement_cache.clear()
        self.scaled_cache.clear()
        if self.current_image:
            self.display_image(self.current_image)

    def _sync_curves_enabled_ui(self):
        """Reflect ``curves_enabled`` on the window checkbox and toolbar button."""
        win = getattr(self, '_curves_window', None)
        if win is not None:
            win.set_enabled_state(self.curves_enabled)

    def toggle_curves(self, checked):
        """Enable/disable the classical curves (RGB levels) effect."""
        self.curves_enabled = bool(checked)
        self._sync_curves_enabled_ui()
        self.enhancement_cache.clear()
        self.scaled_cache.clear()
        if self.current_image:
            self.display_image(self.current_image)

    def set_curves_channel(self, channel):
        """Switch which channel (master/r/g/b) the curve sliders edit."""
        if channel not in ("master", "r", "g", "b"):
            return
        self.curves_channel = channel
        win = getattr(self, '_curves_window', None)
        if win is not None:
            win.set_channel(channel)
        self._sync_curves_sliders()

    def update_curves_black(self, value):
        """Set the black point for the active channel (0-254)."""
        try:
            value = int(value)
        except (TypeError, ValueError):
            return
        value = max(0, min(254, value))
        # Keep black below white
        if value >= self.curves_white[self.curves_channel]:
            value = self.curves_white[self.curves_channel] - 1
            self._sync_curves_sliders()
        self.curves_black[self.curves_channel] = value
        self._refresh_curves()

    def update_curves_white(self, value):
        """Set the white point for the active channel (1-255)."""
        try:
            value = int(value)
        except (TypeError, ValueError):
            return
        value = max(1, min(255, value))
        # Keep white above black
        if value <= self.curves_black[self.curves_channel]:
            value = self.curves_black[self.curves_channel] + 1
            self._sync_curves_sliders()
        self.curves_white[self.curves_channel] = value
        self._refresh_curves()

    def update_curves_gamma(self, value):
        """Set the midtone (gamma) slider for the active channel (-100..100)."""
        try:
            value = int(value)
        except (TypeError, ValueError):
            return
        self.curves_gamma[self.curves_channel] = max(-100, min(100, value))
        self._refresh_curves()

    def update_curves_opacity(self, value):
        """Set the curve effect opacity/strength (0-100)."""
        try:
            value = int(value)
        except (TypeError, ValueError):
            return
        self.curves_opacity = max(0, min(100, value))
        self._refresh_curves()

    def reset_curves(self):
        """Reset all curve channels to neutral (keeps the panel open)."""
        self.curves_black = {"master": 0, "r": 0, "g": 0, "b": 0}
        self.curves_white = {"master": 255, "r": 255, "g": 255, "b": 255}
        self.curves_gamma = {"master": 0, "r": 0, "g": 0, "b": 0}
        self.curves_opacity = 100
        self.curves_enabled = False
        self._sync_curves_enabled_ui()
        self._sync_curves_sliders()
        if hasattr(self, 'curves_btn') and self.curves_btn is not None:
            # keep the panel-open state; only the effect is reset
            pass
        self.enhancement_cache.clear()
        self.scaled_cache.clear()
        if self.current_image:
            self.display_image(self.current_image)

    # ───────────── Curves floating window ─────────────
    def _ensure_curves_window(self):
        """Lazy-create the curves window and wire its signals."""
        if self._curves_window is None:
            win = CurvesWindow(self)
            win.enable_toggled.connect(self.toggle_curves)
            win.channel_changed.connect(self.set_curves_channel)
            win.black_changed.connect(self.update_curves_black)
            win.white_changed.connect(self.update_curves_white)
            win.gamma_changed.connect(self.update_curves_gamma)
            win.opacity_changed.connect(self.update_curves_opacity)
            win.reset_requested.connect(self.reset_curves)
            win.closed.connect(self._on_curves_window_closed)
            self._curves_window = win
            # Push current state into the freshly created window
            win.set_channel(self.curves_channel)
            win.set_enabled_state(self.curves_enabled)
            self._sync_curves_sliders()
        return self._curves_window

    def _toggle_curves_window(self, checked):
        """Show/hide the curves window from the toolbar 📈 button."""
        if checked:
            self._show_curves_window()
        else:
            self._hide_curves_window()

    def _show_curves_window(self):
        """Show the floating curves panel near the 📈 button on first open."""
        win = self._ensure_curves_window()
        win.set_channel(self.curves_channel)
        win.set_enabled_state(self.curves_enabled)
        self._sync_curves_sliders()
        if win.isVisible():
            win.raise_()
            return
        target = self._curves_window_pos
        if target is None:
            btn = getattr(self, 'curves_btn', None)
            if btn is not None:
                try:
                    from PySide6.QtCore import QPoint
                    target = btn.mapToGlobal(btn.rect().bottomLeft()) + QPoint(0, 4)
                except Exception:
                    target = None
        if target is not None:
            try:
                from PySide6.QtGui import QGuiApplication
                scr = QGuiApplication.screenAt(target) or QGuiApplication.primaryScreen()
                geom = scr.availableGeometry()
                w = win.sizeHint().width() or win.width() or 280
                h = win.sizeHint().height() or win.height() or 180
                x = max(geom.left(), min(target.x(), geom.right() - w))
                y = max(geom.top(), min(target.y(), geom.bottom() - h))
                win.move(x, y)
            except Exception:
                win.move(target)
        win.show()
        win.raise_()

    def _hide_curves_window(self):
        """Hide the curves panel; remember its position."""
        win = getattr(self, '_curves_window', None)
        if win is not None and win.isVisible():
            try:
                self._curves_window_pos = win.pos()
            except Exception:
                pass
            win.hide()

    def _on_curves_window_closed(self):
        """Handle the window's ✕ button: remember pos and un-check toolbar button."""
        win = getattr(self, '_curves_window', None)
        if win is not None:
            try:
                self._curves_window_pos = win.pos()
            except Exception:
                pass
        if hasattr(self, 'curves_btn') and self.curves_btn is not None:
            self.curves_btn.blockSignals(True)
            self.curves_btn.setChecked(False)
            self.curves_btn.blockSignals(False)

    def toggle_value_filter(self, checked):
        """Enable/disable the posterize value filter."""
        self.value_filter_enabled = bool(checked)
        if hasattr(self, 'value_filter_toggle_btn') and self.value_filter_toggle_btn is not None:
            self.value_filter_toggle_btn.blockSignals(True)
            self.value_filter_toggle_btn.setChecked(self.value_filter_enabled)
            self.value_filter_toggle_btn.blockSignals(False)
        self.enhancement_cache.clear()
        self.scaled_cache.clear()
        if self.current_image:
            self.display_image(self.current_image)

    def update_value_levels(self, value):
        """Change the number of posterize tones (2-10). Does not auto-enable."""
        try:
            value = int(value)
        except (TypeError, ValueError):
            return
        self.value_levels = max(2, min(10, value))
        # Only need to re-render if the filter is currently on
        if self.value_filter_enabled:
            self.enhancement_cache.clear()
            self.scaled_cache.clear()
            if self.current_image:
                self.display_image(self.current_image)

    def toggle_color_groups(self, checked):
        """Enable/disable the Color Groups (palette quantization) effect."""
        self.color_groups_enabled = bool(checked)
        if hasattr(self, 'color_groups_toggle_btn') and self.color_groups_toggle_btn is not None:
            self.color_groups_toggle_btn.blockSignals(True)
            self.color_groups_toggle_btn.setChecked(self.color_groups_enabled)
            self.color_groups_toggle_btn.blockSignals(False)
        self.enhancement_cache.clear()
        self.scaled_cache.clear()
        if self.current_image:
            self.display_image(self.current_image)

    def update_color_groups_count(self, value):
        """Change the number of palette colours (2-32). Does not auto-enable."""
        try:
            value = int(value)
        except (TypeError, ValueError):
            return
        self.color_groups_count = max(2, min(32, value))
        self._color_palette_cache.clear()
        if self.color_groups_enabled:
            self.enhancement_cache.clear()
            self.scaled_cache.clear()
            if self.current_image:
                self.display_image(self.current_image)

    def update_color_groups_field(self, value):
        """Change the field-size pre-smoothing (0-20). Does not auto-enable."""
        try:
            value = int(value)
        except (TypeError, ValueError):
            return
        self.color_groups_field = max(0, min(20, value))
        self._color_palette_cache.clear()
        if self.color_groups_enabled:
            self.enhancement_cache.clear()
            self.scaled_cache.clear()
            if self.current_image:
                self.display_image(self.current_image)

    def _refresh_object_groups(self):
        """Re-render only when the object-group effect is actually on."""
        if self.object_groups_enabled:
            self.enhancement_cache.clear()
            self.scaled_cache.clear()
            if self.current_image:
                self.display_image(self.current_image)

    def toggle_object_groups(self, checked):
        """Enable/disable the Object Groups (cryptomatte-style) effect."""
        self.object_groups_enabled = bool(checked)
        if getattr(self, 'object_groups_toggle_btn', None) is not None:
            self.object_groups_toggle_btn.blockSignals(True)
            self.object_groups_toggle_btn.setChecked(self.object_groups_enabled)
            self.object_groups_toggle_btn.blockSignals(False)
        self.enhancement_cache.clear()
        self.scaled_cache.clear()
        if self.current_image:
            self.display_image(self.current_image)

    def set_object_groups_mode(self, mode):
        """Set the Object Groups look (local/local_edges/id)."""
        if mode not in ("local", "local_edges", "id"):
            return
        self.object_groups_mode = mode
        for key, act in getattr(self, '_object_groups_mode_actions', {}).items():
            act.blockSignals(True)
            act.setChecked(key == mode)
            act.blockSignals(False)
        self._refresh_object_groups()

    def update_object_groups_detail(self, value):
        """Change the object detail level (0-100). Does not auto-enable."""
        try:
            value = int(value)
        except (TypeError, ValueError):
            return
        self.object_groups_detail = max(0, min(100, value))
        self._refresh_object_groups()

    def update_object_groups_min_size(self, value):
        """Change the minimum object size (0-100). Does not auto-enable."""
        try:
            value = int(value)
        except (TypeError, ValueError):
            return
        self.object_groups_min_size = max(0, min(100, value))
        self._refresh_object_groups()

    def toggle_edge_detection(self, checked):
        """Enable/disable the Canny edge-detection filter."""
        self.edge_detection_enabled = bool(checked)
        if hasattr(self, 'edge_toggle_btn') and self.edge_toggle_btn is not None:
            self.edge_toggle_btn.blockSignals(True)
            self.edge_toggle_btn.setChecked(self.edge_detection_enabled)
            self.edge_toggle_btn.blockSignals(False)
        self.enhancement_cache.clear()
        self.scaled_cache.clear()
        if self.current_image:
            self.display_image(self.current_image)

    def set_edge_mode(self, mode):
        """Set the edge-detection look (white_on_black/black_on_white/overlay)."""
        if mode not in ("white_on_black", "black_on_white", "overlay"):
            return
        self.edge_mode = mode
        if hasattr(self, '_edge_mode_actions'):
            for key, act in self._edge_mode_actions.items():
                act.blockSignals(True)
                act.setChecked(key == mode)
                act.blockSignals(False)
        if self.edge_detection_enabled:
            self.enhancement_cache.clear()
            self.scaled_cache.clear()
            if self.current_image:
                self.display_image(self.current_image)

    def update_edge_sensitivity(self, value):
        """Change edge sensitivity (0-100). Does not auto-enable."""
        try:
            value = int(value)
        except (TypeError, ValueError):
            return
        self.edge_sensitivity = max(0, min(100, value))
        if self.edge_detection_enabled:
            self.enhancement_cache.clear()
            self.scaled_cache.clear()
            if self.current_image:
                self.display_image(self.current_image)

    def update_contrast(self, value):
        self.contrast_value = value
        # Update toggle button state (block signals to prevent loops)
        if hasattr(self, 'contrast_toggle_btn') and self.contrast_toggle_btn is not None:
            self.contrast_toggle_btn.blockSignals(True)
            self.contrast_toggle_btn.setChecked(value != 50)
            self.contrast_toggle_btn.blockSignals(False)
        # Clear enhancement cache when settings change
        self.enhancement_cache.clear()
        self.scaled_cache.clear()  # Also clear scaled cache to force refresh
        self._update_enhancement_menu_states()
        if self.current_image:
            self.display_image(self.current_image)

    def update_gamma(self, value):
        self.gamma_value = value
        # Update toggle button state (block signals to prevent loops)
        if hasattr(self, 'gamma_toggle_btn') and self.gamma_toggle_btn is not None:
            self.gamma_toggle_btn.blockSignals(True)
            self.gamma_toggle_btn.setChecked(value != 0)  # Fixed: was checking != 50, should be != 0
            self.gamma_toggle_btn.blockSignals(False)
        # Clear enhancement cache when settings change
        self.enhancement_cache.clear()
        self.scaled_cache.clear()  # Also clear scaled cache to force refresh
        self._update_enhancement_menu_states()
        if self.current_image:
            self.display_image(self.current_image)

    def reset_enhancements(self):
        # Block signals to prevent triggering toggle functions during reset
        if hasattr(self, 'grayscale_slider') and self.grayscale_slider is not None:
            self.grayscale_slider.blockSignals(True)
            self.grayscale_slider.setValue(0)
            self.grayscale_slider.blockSignals(False)
        
        if hasattr(self, 'contrast_slider') and self.contrast_slider is not None:
            self.contrast_slider.blockSignals(True)
            self.contrast_slider.setValue(50)
            self.contrast_slider.blockSignals(False)
        
        if hasattr(self, 'gamma_slider') and self.gamma_slider is not None:
            self.gamma_slider.blockSignals(True)
            self.gamma_slider.setValue(0)
            self.gamma_slider.blockSignals(False)
        
        if hasattr(self, 'lut_strength_slider') and self.lut_strength_slider is not None:
            self.lut_strength_slider.blockSignals(True)
            self.lut_strength_slider.setValue(100)
            self.lut_strength_slider.blockSignals(False)
        
        if hasattr(self, 'lut_combo') and self.lut_combo is not None:
            self.lut_combo.blockSignals(True)
            self.lut_combo.setCurrentText("None")
            self.lut_combo.blockSignals(False)
        
        self.grayscale_value = 0
        self.contrast_value = 50
        self.gamma_value = 0
        self.current_lut = None
        self.current_lut_name = "None"
        self.lut_strength = 100
        self.value_filter_enabled = False
        self.value_levels = 4

        # Reset value-filter UI
        if hasattr(self, 'value_filter_toggle_btn') and self.value_filter_toggle_btn is not None:
            self.value_filter_toggle_btn.blockSignals(True)
            self.value_filter_toggle_btn.setChecked(False)
            self.value_filter_toggle_btn.blockSignals(False)
        if hasattr(self, 'value_levels_spin') and self.value_levels_spin is not None:
            self.value_levels_spin.blockSignals(True)
            self.value_levels_spin.setValue(4)
            self.value_levels_spin.blockSignals(False)

        # Reset Color Groups (palette quantization)
        self.color_groups_enabled = False
        self.color_groups_count = 8
        self.color_groups_field = 0
        if hasattr(self, '_color_palette_cache'):
            self._color_palette_cache.clear()
        if hasattr(self, 'color_groups_toggle_btn') and self.color_groups_toggle_btn is not None:
            self.color_groups_toggle_btn.blockSignals(True)
            self.color_groups_toggle_btn.setChecked(False)
            self.color_groups_toggle_btn.blockSignals(False)
        if hasattr(self, 'color_groups_count_spin') and self.color_groups_count_spin is not None:
            self.color_groups_count_spin.blockSignals(True)
            self.color_groups_count_spin.setValue(8)
            self.color_groups_count_spin.blockSignals(False)
        if hasattr(self, 'color_groups_field_spin') and self.color_groups_field_spin is not None:
            self.color_groups_field_spin.blockSignals(True)
            self.color_groups_field_spin.setValue(0)
            self.color_groups_field_spin.blockSignals(False)

        # Reset Object Groups (cryptomatte-style per-object flattening)
        self.object_groups_enabled = False
        self.object_groups_detail = 45
        self.object_groups_min_size = 12
        self.object_groups_mode = "local"
        for key, act in getattr(self, '_object_groups_mode_actions', {}).items():
            act.blockSignals(True)
            act.setChecked(key == "local")
            act.blockSignals(False)
        if getattr(self, 'object_groups_toggle_btn', None) is not None:
            self.object_groups_toggle_btn.blockSignals(True)
            self.object_groups_toggle_btn.setChecked(False)
            self.object_groups_toggle_btn.blockSignals(False)
        if getattr(self, 'object_groups_detail_spin', None) is not None:
            self.object_groups_detail_spin.blockSignals(True)
            self.object_groups_detail_spin.setValue(45)
            self.object_groups_detail_spin.blockSignals(False)
        if getattr(self, 'object_groups_min_spin', None) is not None:
            self.object_groups_min_spin.blockSignals(True)
            self.object_groups_min_spin.setValue(12)
            self.object_groups_min_spin.blockSignals(False)

        # Reset Curves (classical RGB levels)
        self.curves_enabled = False
        self.curves_channel = "master"
        self.curves_black = {"master": 0, "r": 0, "g": 0, "b": 0}
        self.curves_white = {"master": 255, "r": 255, "g": 255, "b": 255}
        self.curves_gamma = {"master": 0, "r": 0, "g": 0, "b": 0}
        self.curves_opacity = 100
        win = getattr(self, '_curves_window', None)
        if win is not None:
            win.set_channel("master")
            win.set_enabled_state(False)
        self._sync_curves_sliders()

        # Update toggle button states (block signals to prevent loops)
        if hasattr(self, 'grayscale_toggle_btn') and self.grayscale_toggle_btn is not None:
            self.grayscale_toggle_btn.blockSignals(True)
            self.grayscale_toggle_btn.setChecked(False)
            self.grayscale_toggle_btn.blockSignals(False)
        
        if hasattr(self, 'contrast_toggle_btn') and self.contrast_toggle_btn is not None:
            self.contrast_toggle_btn.blockSignals(True)
            self.contrast_toggle_btn.setChecked(False)
            self.contrast_toggle_btn.blockSignals(False)
        
        if hasattr(self, 'gamma_toggle_btn') and self.gamma_toggle_btn is not None:
            self.gamma_toggle_btn.blockSignals(True)
            self.gamma_toggle_btn.setChecked(False)
            self.gamma_toggle_btn.blockSignals(False)
        
        # Clear all caches when resetting
        self.enhancement_cache.clear()
        self.scaled_cache.clear()
        
        # CRITICAL: Clear zoom optimization cache when resetting enhancements
        if hasattr(self, '_lut_process_cache'):
            self._lut_process_cache.clear()
        if hasattr(self, '_last_processed_image'):
            self._last_processed_image = None
        self._last_processed_has_lut = False
        self._update_enhancement_menu_states()
        if self.current_image:
            self.display_image(self.current_image)

    def choose_lut_folder(self):
        """Choose a folder containing CUBE LUT files"""
        # Prefer previously chosen LUT folder; else default LUT path; else ONLY then fall back to image folder
        if self.lut_folder and os.path.isdir(self.lut_folder):
            start_dir = self.lut_folder
        elif hasattr(self, 'default_lut_path') and self.default_lut_path and os.path.isdir(self.default_lut_path):
            start_dir = self.default_lut_path
        elif self.folder and os.path.isdir(self.folder):
            start_dir = self.folder  # final fallback
        else:
            start_dir = ""

        folder = QFileDialog.getExistingDirectory(self, "Select LUT Folder (will search subfolders)", start_dir)
        if folder:
            self.lut_folder = folder
            
            # Show scanning status
            self.status.showMessage(f"Scanning {os.path.basename(folder)} and subfolders for CUBE files...")
            QApplication.processEvents()  # Allow UI to update
            
            self.lut_files = self.scan_lut_folder(folder)
            self.update_lut_combo()
            
            # Count subfolders searched
            subfolder_count = 0
            try:
                for root, dirs, files in os.walk(folder):
                    if root != folder:  # Don't count the root folder itself
                        subfolder_count += 1
            except:
                subfolder_count = 0
            
            if subfolder_count > 0:
                self.status.showMessage(f"Found {len(self.lut_files)} LUT files in {os.path.basename(folder)} (+{subfolder_count} subfolders)")
            else:
                self.status.showMessage(f"Found {len(self.lut_files)} LUT files in {os.path.basename(folder)}")

    def _update_lut_item_tooltips(self):
        """Update the LUT combo tooltip to show the currently applied LUT."""
        if not hasattr(self, 'lut_combo'):
            return
        applied = getattr(self, 'current_lut_name', 'None') or 'None'
        if applied and applied != 'None':
            tip = f"Select LUT  (applied: {applied})"
        else:
            tip = "Select LUT"
        self.lut_combo.setToolTip(tip)

    def update_lut_combo(self):
        """Update the LUT combo box with available LUT files"""
        if hasattr(self, 'lut_combo'):
            self.lut_combo.blockSignals(True)
            self.lut_combo.clear()
            self.lut_combo.addItem("None")
            
            for lut_file in self.lut_files:
                # Create a display name that shows subfolder structure
                relative_path = os.path.relpath(lut_file, self.lut_folder)
                
                # Remove the .cube extension and use forward slashes for consistency
                display_name = os.path.splitext(relative_path)[0].replace('\\', '/')
                
                # If it's just a filename (no subfolder), show only the name
                if '/' not in display_name:
                    display_name = os.path.basename(display_name)
                
                self.lut_combo.addItem(display_name)
            
            self.lut_combo.blockSignals(False)
            self._update_lut_item_tooltips()

    def apply_selected_lut(self, lut_name):
        """Apply the selected LUT from the combo box with fast preview"""
        if lut_name == "None" or not lut_name:
            self.current_lut = None
            self.current_lut_name = "None"
            self.lut_enabled = False  # Disable LUT when "None" is selected
            self.status.showMessage("LUT disabled")
            # Sync toggle button state
            if hasattr(self, 'lut_toggle_btn'):
                self.lut_toggle_btn.blockSignals(True)
                self.lut_toggle_btn.setChecked(False)
                self.lut_toggle_btn.blockSignals(False)
        else:
            # Show loading status immediately
            self.status.showMessage(f"Loading LUT: {lut_name}...")
            QApplication.processEvents()  # Allow UI to update
            
            # Find the full path for this LUT name (handling subfolder structure)
            lut_file_found = None
            # Normalise the requested name so legacy/persisted raw paths (with
            # backslashes and/or a .cube extension) still resolve.
            requested = os.path.splitext(lut_name)[0].replace('\\', '/')
            for lut_file in self.lut_files:
                # Create the same display name format as in update_lut_combo
                relative_path = os.path.relpath(lut_file, self.lut_folder)
                display_name = os.path.splitext(relative_path)[0].replace('\\', '/')

                # If it's just a filename (no subfolder), show only the name
                if '/' not in display_name:
                    display_name = os.path.basename(display_name)

                if display_name == lut_name or display_name == requested:
                    lut_file_found = lut_file
                    break
            
            if lut_file_found:
                # Check if LUT is already cached
                if hasattr(self, 'lut_cache') and lut_file_found in self.lut_cache:
                    self.current_lut = self.lut_cache[lut_file_found]
                    self.current_lut_name = lut_name
                    lut_size = self.current_lut['size']
                    self.status.showMessage(f"LUT loaded (cached): {lut_name} ({lut_size}³)")
                else:
                    # Load new LUT
                    self.current_lut = self.load_cube_lut(lut_file_found)
                    self.current_lut_name = lut_name
                    
                    # Cache the loaded LUT
                    if not hasattr(self, 'lut_cache'):
                        self.lut_cache = {}
                    if self.current_lut:
                        self.lut_cache[lut_file_found] = self.current_lut
                        lut_size = self.current_lut['size']
                        self.status.showMessage(f"LUT loaded: {lut_name} ({lut_size}³)")
                    else:
                        self.status.showMessage(f"Failed to load LUT: {lut_name}")
                        self.current_lut_name = "None"
                        self.lut_enabled = False  # Disable when LUT fails to load
                        # Reset combo box to "None" if loading failed
                        if hasattr(self, 'lut_combo'):
                            self.lut_combo.blockSignals(True)
                            self.lut_combo.setCurrentText("None")
                            self.lut_combo.blockSignals(False)
            else:
                self.status.showMessage(f"LUT file not found: {lut_name}")
                self.current_lut = None
                self.current_lut_name = "None"
                self.lut_enabled = False  # Disable when LUT file not found
            # Sync toggle button state when a LUT successfully loaded
            if self.current_lut_name != "None" and hasattr(self, 'lut_toggle_btn'):
                self.lut_toggle_btn.blockSignals(True)
                self.lut_toggle_btn.setChecked(True)
                self.lut_toggle_btn.blockSignals(False)
                # IMPORTANT: Also set the internal enabled flag when LUT is loaded
                self.lut_enabled = True
        
        # Clear caches and update display with progressive preview
        self.enhancement_cache.clear()
        self.scaled_cache.clear()
        if hasattr(self, '_lut_process_cache'):
            self._lut_process_cache.clear()  # Clear LUT cache when LUT changes
        
        # CRITICAL: Clear zoom optimization cache when LUT changes to prevent wrong LUT being applied during pan/zoom
        if hasattr(self, '_last_processed_image'):
            self._last_processed_image = None
        self._last_processed_has_lut = False
        
        if self.current_image:
            # Use unified processing pipeline for consistency
            self.display_image(self.current_image)
            if self.current_lut and self.lut_enabled:
                lut_name_display = self.current_lut_name if self.current_lut_name != "None" else "LUT"
                self.status.showMessage(f"{lut_name_display} applied")
        self._update_lut_item_tooltips()

    def toggle_lut_enabled(self, checked):
        """Toggle current LUT on/off without losing selection."""
        # If turning off, remember current selection (if any) and switch combo to None
        if not checked:
            self.lut_enabled = False
            if getattr(self, 'current_lut_name', 'None') != 'None':
                self._saved_lut_selection = self.current_lut_name
            if hasattr(self, 'lut_combo'):
                self.lut_combo.blockSignals(True)
                self.lut_combo.setCurrentText("None")
                self.lut_combo.blockSignals(False)
            self.apply_selected_lut("None")
            # Force immediate redraw WITHOUT LUT (previous enhanced cache may contain LUT-applied pixmaps)
            try:
                if hasattr(self, 'enhancement_cache'): self.enhancement_cache.clear()
                if hasattr(self, 'scaled_cache'): self.scaled_cache.clear()
                if hasattr(self, '_lut_process_cache'): self._lut_process_cache.clear()
                if hasattr(self, '_last_processed_image'): self._last_processed_image = None
                self._last_processed_has_lut = False
                # Do NOT clear base pixmap_cache so we avoid re-reading image from disk
                if self.current_image:
                    self.display_image(self.current_image)
            except Exception as e:
                print(f"toggle_lut_enabled redraw failure: {e}")
            return
        # Turning on: restore previously saved selection if available
        self.lut_enabled = True
        target = getattr(self, '_saved_lut_selection', None)
        if target and hasattr(self, 'lut_combo'):
            if self.lut_combo.findText(target) >= 0:
                self.lut_combo.blockSignals(True)
                self.lut_combo.setCurrentText(target)
                self.lut_combo.blockSignals(False)
                self.apply_selected_lut(target)
                return
        # If no saved selection, do nothing (button will remain on but no LUT)
        if hasattr(self, 'lut_toggle_btn') and self.current_lut_name == 'None':
            # No LUT to enable; uncheck to reflect reality
            self.lut_toggle_btn.blockSignals(True)
            self.lut_toggle_btn.setChecked(False)
            self.lut_toggle_btn.blockSignals(False)

    def _create_fast_lut_preview(self):
        """Create an instant high-quality preview optimized for display size"""
        if not self.current_image or not self.current_lut:
            print("DEBUG: No current image or LUT for preview")
            return
            
        try:
            # Load base image quickly
            if self.current_image in self.pixmap_cache:
                base_pixmap = self.pixmap_cache[self.current_image]
            else:
                base_pixmap, error = safe_load_pixmap(self.current_image)
                if error:
                    return
            
            # ENHANCED PREVIEW SIZING: When lines are present, maintain higher quality
            has_lines = (self.lines_visible and 
                        (self.drawn_lines or self.drawn_horizontal_lines or self.drawn_free_lines or self.drawn_free_strokes))
            
            display_width = self.image_label.width()
            display_height = self.image_label.height()
            zoom_factor = getattr(self.image_label, 'zoom_factor', 1.0)
            
            # Calculate the effective display size considering zoom
            effective_width = int(display_width * zoom_factor)
            effective_height = int(display_height * zoom_factor)
            
            # IMPROVED QUALITY: Use higher resolution preview when lines are present
            if has_lines:
                # When lines are present, use higher quality to maintain line clarity
                if effective_width > 1200 or effective_height > 800:
                    # Very high quality for large displays with lines
                    preview_size = min(1600, 
                                     int(base_pixmap.width() * 0.8), 
                                     int(base_pixmap.height() * 0.8))
                elif effective_width > 800 or effective_height > 600:
                    # High quality for medium displays with lines
                    preview_size = min(1200, 
                                     int(base_pixmap.width() * 0.75), 
                                     int(base_pixmap.height() * 0.75))
                else:
                    # Medium quality for smaller displays with lines
                    preview_size = min(900, 
                                     int(base_pixmap.width() * 0.6), 
                                     int(base_pixmap.height() * 0.6))
                transform_quality = Qt.SmoothTransformation  # Always use smooth for lines
            else:
                # Standard quality sizing for no lines
                if effective_width > 1200 or effective_height > 800:
                    preview_size = min(1200, 
                                     int(base_pixmap.width() * 0.67), 
                                     int(base_pixmap.height() * 0.67))
                    transform_quality = Qt.SmoothTransformation
                elif effective_width > 800 or effective_height > 600:
                    preview_size = min(900, 
                                     base_pixmap.width() // 2, 
                                     base_pixmap.height() // 2)
                    transform_quality = Qt.SmoothTransformation
                else:
                    preview_size = min(600, 
                                     base_pixmap.width() // 3, 
                                     base_pixmap.height() // 3)
                    transform_quality = Qt.FastTransformation
            
            # Scale with appropriate quality
            fast_preview = base_pixmap.scaled(
                preview_size, preview_size,
                Qt.KeepAspectRatio, transform_quality
            )
            
            # IMPORTANT: Apply LUT using GPU acceleration when available
            lut_dict = {
                'data': self.current_lut['data'],
                'size': self.current_lut['size'],
                'file_path': getattr(self.current_lut, 'file_path', 'preview')
            }
            
            # Use GPU acceleration for preview if available - ENHANCED for lines
            if (self.gpu_processor.is_available() and 
                fast_preview.width() * fast_preview.height() > 150000) or \
               (has_lines and self.gpu_processor.is_available() and 
                fast_preview.width() * fast_preview.height() > 50000):
                
                # Force GPU for previews with lines (much lower threshold)
                print(f"Using GPU for fast LUT preview ({fast_preview.width()}x{fast_preview.height()}, lines: {has_lines})")
                
                # Convert pixmap to image for GPU processing
                preview_image = fast_preview.toImage()
                if preview_image.format() != preview_image.Format.Format_RGB32:
                    preview_image = preview_image.convertToFormat(preview_image.Format.Format_RGB32)
                
                # Apply LUT using GPU
                gpu_result = self.gpu_processor.apply_lut_gpu(
                    preview_image, 
                    lut_dict['data'], 
                    lut_dict['size'], 
                    self.lut_strength / 100.0
                )
                
                if gpu_result is not None:
                    fast_preview = QPixmap.fromImage(gpu_result)
                else:
                    # Fallback to CPU if GPU fails
                    print("GPU preview failed, falling back to CPU")
                    fast_preview = self.apply_lut_to_image(fast_preview, lut_dict, self.lut_strength)
            else:
                # Use CPU LUT processing for smaller previews
                fast_preview = self.apply_lut_to_image(fast_preview, lut_dict, self.lut_strength)
            
            # Apply basic transformations (rotation, flips) with appropriate quality
            fast_preview = self._apply_quick_transforms(fast_preview, transform_quality)
            
            # Scale to display size and show immediately
            final_preview = self._scale_pixmap(fast_preview, self.current_image)
            # Apply lines after LUT preview scaling if they should be visible
            if (self.lines_visible and (self.drawn_lines or self.drawn_horizontal_lines or self.drawn_free_lines or self.drawn_free_strokes)):
                # Temporarily set pixmap then fast overlay
                self.image_label.setPixmap(final_preview)
                if hasattr(self, '_fast_line_update'):
                    self._fast_line_update()
                else:
                    final_preview = self._add_lines_to_pixmap(final_preview)
            else:
                self.image_label.setPixmap(final_preview)
            QApplication.processEvents()
            
        except Exception as e:
            print(f"Error creating fast preview: {e}")
            import traceback
            traceback.print_exc()

    def _apply_full_quality_lut(self):
        """Apply full quality LUT processing asynchronously to prevent freezing"""
        if not self.current_image or not self.current_lut:
            return
            
        try:
            # Update status to show full quality processing
            lut_name = self.current_lut_name if self.current_lut_name != "None" else "LUT"
            self.status.showMessage(f"Applying {lut_name}... (full quality)")
            QApplication.processEvents()
            
            # Start async chunked processing to prevent any freezing
            self._start_async_lut_processing()
            
        except Exception as e:
            print(f"Error in full quality LUT processing: {e}")
            self.status.showMessage("LUT processing error - using preview")

    def _start_async_lut_processing(self):
        """Start asynchronous LUT processing to prevent any UI freezing"""
        if not self.current_image or not self.current_lut:
            return
        
        # Check if we're already processing the same image/LUT combination
        current_cache_key = self._get_lut_cache_key()
        if (hasattr(self, '_async_processing_state') and 
            self._async_processing_state and 
            hasattr(self._async_processing_state, 'cache_key') and
            self._async_processing_state['cache_key'] == current_cache_key):
            # Already processing the same LUT combination - don't restart
            print("LUT processing already in progress for this image/LUT combination")
            return
        
        # Stop any existing processing for different image/LUT combination
        if hasattr(self, '_async_processing_state') and self._async_processing_state:
            if hasattr(self, '_async_processing_timer'):
                self._async_processing_timer.stop()
            self._async_processing_state = None
            
        try:
            # Load base image
            if self.current_image in self.pixmap_cache:
                base_pixmap = self.pixmap_cache[self.current_image]
            else:
                base_pixmap, error = safe_load_pixmap(self.current_image)
                if error:
                    self.status.showMessage("Error loading image for LUT processing")
                    return
                # Cache the loaded image
                self._manage_cache(self.pixmap_cache, self.current_image, base_pixmap)
            
            # Convert to image for processing
            image = base_pixmap.toImage()
            if image.format() != image.Format.Format_RGB32:
                image = image.convertToFormat(image.Format.Format_RGB32)
            
            # GPU ACCELERATION: Try GPU processing first - LOWER threshold for better performance
            # Use GPU for any image larger than 250k pixels (500x500) - especially important with lines
            gpu_threshold = 250000  # Reduced from 500,000 to ensure GPU is used more often
            
            # IMPORTANT: Use GPU processing even when lines are present
            has_lines = (self.lines_visible and 
                        (self.drawn_lines or self.drawn_horizontal_lines or self.drawn_free_lines or self.drawn_free_strokes))
            
            # If lines are present, FORCE GPU processing for better performance (no freezing)
            if has_lines:
                gpu_threshold = 50000  # Very aggressive threshold when lines are present
                print(f"Lines detected - using very aggressive GPU processing (threshold: {gpu_threshold})")
            else:
                gpu_threshold = 150000  # Still lower than default for better performance
            
            if self.gpu_processor.is_available() and image.width() * image.height() > gpu_threshold:
                print(f"Using GPU for async LUT processing ({image.width()}x{image.height()}, lines: {has_lines})")
                
                # Use GPU processing
                lut_data = self.current_lut['data']
                lut_size = self.current_lut['size']
                strength_factor = self.lut_strength / 100.0
                
                try:
                    processed_image = self.gpu_processor.apply_lut_gpu(image, lut_data, lut_size, strength_factor)
                    if processed_image is not None:
                        # GPU processing successful - convert to pixmap and finalize immediately
                        processed_pixmap = QPixmap.fromImage(processed_image)
                        
                        # Store final processing state for finalization
                        self._final_processing_state = {
                            'pixmap': processed_pixmap,
                            'processing_time': 0.1  # GPU is very fast
                        }
                        
                        # Update status and finalize immediately
                        if has_lines:
                            self.status.showMessage("GPU LUT processing complete (with lines) - finalizing...")
                        else:
                            self.status.showMessage("GPU LUT processing complete - finalizing...")
                        QApplication.processEvents()
                        
                        # Call finalization directly - no delay needed for GPU
                        self._async_finalize_step()
                        return
                except Exception as e:
                    print(f"GPU async processing failed: {e}, falling back to CPU")
            else:
                # Debug: Show why GPU wasn't used
                if not self.gpu_processor.is_available():
                    print("GPU not available - using CPU processing")
                    print(f"GPU status: {self.gpu_processor.get_device_info()}")
                else:
                    print(f"Image too small for GPU processing: {image.width() * image.height()} pixels (threshold: {gpu_threshold}, lines: {has_lines})")
                    print(f"GPU device available: {self.gpu_processor.get_device_info()}")
            
            # FALLBACK: CPU async processing with enhanced chunking when lines are present
            print(f"Starting CPU async processing (lines present: {has_lines})")
            
            # When lines are present, use smaller chunks to prevent freezing
            chunk_size = 4 if has_lines else 8  # Smaller chunks when lines are present
            
            # Setup async processing state with cache key tracking
            self._async_processing_state = {
                'image': image,
                'width': image.width(),
                'height': image.height(),
                'current_row': 0,
                'total_rows': image.height(),
                'chunk_size': chunk_size,  # Use smaller chunks when lines are present
                'lut_data': self.current_lut['data'],
                'lut_size': self.current_lut['size'],
                'strength_factor': self.lut_strength / 100.0,
                'start_time': time.time(),
                'cache_key': current_cache_key  # Track what we're processing
            }
            
            # Start the async processing timer
            if not hasattr(self, '_async_processing_timer'):
                self._async_processing_timer = QTimer()
                self._async_processing_timer.timeout.connect(self._process_lut_chunk)
            
            # Process first chunk immediately, then continue with timer
            self._process_lut_chunk()
            self._async_processing_timer.start(1)  # 1ms interval for smooth processing
            
        except Exception as e:
            print(f"Error starting async LUT processing: {e}")
            import traceback
            traceback.print_exc()
    
    def _process_lut_chunk(self):
        """Process a small chunk of the image asynchronously"""
        if not hasattr(self, '_async_processing_state') or not self._async_processing_state:
            if hasattr(self, '_async_processing_timer'):
                self._async_processing_timer.stop()
            return
            
        try:
            state = self._async_processing_state
            image = state['image']
            current_row = state['current_row']
            chunk_size = state['chunk_size']
            width = state['width']
            height = state['height']
            lut_data = state['lut_data']
            lut_size = state['lut_size']
            strength_factor = state['strength_factor']
            
            # Process chunk_size rows with yield points for responsiveness
            end_row = min(current_row + chunk_size, height)
            
            # Check if lines are present for more frequent yield points
            has_lines = (self.lines_visible and 
                        (self.drawn_lines or self.drawn_horizontal_lines or self.drawn_free_lines or self.drawn_free_strokes))
            pixels_processed = 0
            yield_frequency = 2000 if has_lines else 5000  # More frequent yields with lines
            
            for y in range(current_row, end_row):
                scan_line = image.scanLine(y)
                
                for x in range(width):
                    offset = x * 4
                    
                    # Read pixel bytes (BGRA order in Qt)
                    b = scan_line[offset]
                    g = scan_line[offset + 1] 
                    r = scan_line[offset + 2]
                    a = scan_line[offset + 3]
                    
                    # Normalize to 0-1 range
                    r_norm = r / 255.0
                    g_norm = g / 255.0
                    b_norm = b / 255.0
                    
                    # Apply LUT with quality interpolation
                    lut_result = self._interpolate_lut_quality(r_norm, g_norm, b_norm, lut_data, lut_size)
                    
                    # Blend with original using strength factor
                    final_r = r_norm * (1.0 - strength_factor) + lut_result[0] * strength_factor
                    final_g = g_norm * (1.0 - strength_factor) + lut_result[1] * strength_factor
                    final_b = b_norm * (1.0 - strength_factor) + lut_result[2] * strength_factor
                    
                    # Clamp and convert back to bytes
                    final_r = max(0, min(255, int(final_r * 255 + 0.5)))
                    final_g = max(0, min(255, int(final_g * 255 + 0.5)))
                    final_b = max(0, min(255, int(final_b * 255 + 0.5)))
                    
                    # Write back to image (BGRA order)
                    scan_line[offset] = final_b
                    scan_line[offset + 1] = final_g
                    scan_line[offset + 2] = final_r
                    scan_line[offset + 3] = a
                    
                    # Yield control more frequently when lines are present
                    pixels_processed += 1
                    if pixels_processed >= yield_frequency:
                        # Allow Qt event processing to prevent freezing
                        QApplication.processEvents()
                        pixels_processed = 0
            
            # Update progress
            state['current_row'] = end_row
            progress = (end_row / height) * 100
            
            # Update status every few chunks
            if end_row % (chunk_size * 3) == 0 or end_row >= height:
                lut_name = self.current_lut_name if self.current_lut_name != "None" else "LUT"
                self.status.showMessage(f"Applying {lut_name}... {progress:.0f}%")
            
            # Check if processing is complete
            if end_row >= height:
                self._finish_async_lut_processing()
                
        except Exception as e:
            print(f"Error in LUT chunk processing: {e}")
            import traceback
            traceback.print_exc()
            # Stop processing on error
            if hasattr(self, '_async_processing_timer'):
                self._async_processing_timer.stop()
            self._async_processing_state = None
    
    def _finish_async_lut_processing(self):
        """Complete the async LUT processing and display the result - NOW FULLY ASYNC"""
        try:
            if hasattr(self, '_async_processing_timer'):
                self._async_processing_timer.stop()
            
            if not hasattr(self, '_async_processing_state') or not self._async_processing_state:
                return
                
            state = self._async_processing_state
            processed_image = state['image']
            
            # Convert back to pixmap
            processed_pixmap = QPixmap.fromImage(processed_image)
            
            # REMOVED: Don't cache here - cache at the END after lines are added
            # The cache will be updated in the final step with the complete image
            
            # INSTANT FINALIZATION: Complete synchronously for maximum speed
            self.status.showMessage("Finalizing LUT...")
            
            # Store final processing state
            self._final_processing_state = {
                'pixmap': processed_pixmap,
                'processing_time': time.time() - state['start_time']
            }
            
            # Clear the main processing state 
            self._async_processing_state = None
            
            # Call finalization immediately - NO delay for instant completion
            self._async_finalize_step()
            
        except Exception as e:
            print(f"Error finishing async LUT processing: {e}")
            import traceback
            traceback.print_exc()
            # Fallback to current display
            self.status.showMessage("LUT processing completed with errors")
            self._async_processing_state = None
    
    def _async_finalize_step(self):
        """Process final LUT steps instantly - ZERO delay completion"""
        if not hasattr(self, '_final_processing_state') or not self._final_processing_state:
            return
            
        try:
            state = self._final_processing_state
            processed_pixmap = state['pixmap']
            
            # INSTANT FINALIZATION: Complete everything synchronously in one go
            self.status.showMessage("Finalizing LUT...")
            
            # Apply ALL transformations immediately
            if self.rotation_angle != 0:
                transform = QTransform()
                transform.rotate(self.rotation_angle)
                processed_pixmap = processed_pixmap.transformed(transform, Qt.SmoothTransformation)
            
            if self.flipped_h:
                processed_pixmap = processed_pixmap.transformed(QTransform().scale(-1, 1), Qt.SmoothTransformation)
            if self.flipped_v:
                processed_pixmap = processed_pixmap.transformed(QTransform().scale(1, -1), Qt.SmoothTransformation)
            
            if (self.grayscale_value != 0 or self.contrast_value != 50 or self.gamma_value != 0):
                processed_pixmap = self.apply_fast_enhancements(processed_pixmap)
            
            if (self.lines_visible and (self.drawn_lines or self.drawn_horizontal_lines or self.drawn_free_lines or self.drawn_free_strokes)):
                processed_pixmap = self._add_lines_to_pixmap(processed_pixmap)
            
            # Scale, display and cache immediately - NO DELAYS
            final_pixmap = self._scale_pixmap(processed_pixmap, self.current_image)
            self.image_label.setPixmap(self._apply_fixed_overlays_to_pixmap(final_pixmap))
            # Reapply lines post-scale to avoid being lost by scaling
            if (self.lines_visible and (self.drawn_lines or self.drawn_horizontal_lines or self.drawn_free_lines or self.drawn_free_strokes)) and hasattr(self, '_fast_line_update'):
                self._fast_line_update()
            
            # Update cache with complete processed image
            cache_key = self._get_lut_cache_key()
            if cache_key:
                if not hasattr(self, '_lut_process_cache'):
                    self._lut_process_cache = {}
                
                self._lut_process_cache[cache_key] = processed_pixmap.copy()
                
                # Manage cache size (keep max 3 processed images for memory)
                max_cache_size = 3
                if len(self._lut_process_cache) > max_cache_size:
                    oldest_key = next(iter(self._lut_process_cache))
                    del self._lut_process_cache[oldest_key]
            
            # Instant completion status
            processing_time = state['processing_time']
            final_msg = f"LUT applied: {self.current_lut_name} ({processing_time:.1f}s) - INSTANT completion!"
            self.status.showMessage(final_msg)
            
            # Clear finalization state immediately
            self._final_processing_state = None
                
        except Exception as e:
            print(f"Error in instant finalization: {e}")
            import traceback
            traceback.print_exc()
            self.status.showMessage("LUT finalization completed with errors")
            self._final_processing_state = None
    
    def _add_lines_to_pixmap(self, pixmap):
        """Add drawn lines to a pixmap with GPU acceleration when available"""
        if not pixmap or pixmap.isNull():
            return pixmap
            
        if not (self.drawn_lines or self.drawn_horizontal_lines or self.drawn_free_lines or self.drawn_free_strokes):
            return pixmap
            
        try:
            # Try GPU acceleration first for better performance
            # The GPU kernel overwrites pixels (no alpha blend), so only use it
            # for fully-opaque lines; semi-transparent lines fall through to the
            # CPU painter which blends correctly.
            if (getattr(self, 'line_transparency', 255) >= 255 and
                self.gpu_processor.is_available() and 
                pixmap.width() * pixmap.height() > 100000):  # Use GPU for images > 100k pixels
                
                print(f"Using GPU for line drawing ({pixmap.width()}x{pixmap.height()})")
                
                # Prepare line data with proper scaling
                scale_x = 1.0
                scale_y = 1.0
                
                if hasattr(self, 'original_pixmap') and self.original_pixmap:
                    original_size = self.original_pixmap.size()
                    current_size = pixmap.size()
                    scale_x = current_size.width() / original_size.width()
                    scale_y = current_size.height() / original_size.height()
                
                # Scale line coordinates for GPU
                scaled_vertical = [int(x * scale_x) for x in self.drawn_lines] if self.drawn_lines else []
                scaled_horizontal = [int(y * scale_y) for y in self.drawn_horizontal_lines] if self.drawn_horizontal_lines else []
                
                scaled_free_lines = []
                if self.drawn_free_lines:
                    for line in self.drawn_free_lines:
                        start_x, start_y = line['start']
                        end_x, end_y = line['end']
                        scaled_free_lines.append({
                            'start': (int(start_x * scale_x), int(start_y * scale_y)),
                            'end': (int(end_x * scale_x), int(end_y * scale_y))
                        })
                
                # Convert pixmap to image for GPU processing
                image = pixmap.toImage()
                if image.format() != image.Format.Format_RGBA8888:
                    image = image.convertToFormat(image.Format.Format_RGBA8888)
                
                # Try GPU line drawing
                gpu_result = self.gpu_processor.draw_lines_gpu(
                    image,
                    scaled_vertical,
                    scaled_horizontal, 
                    scaled_free_lines,
                    self.line_color,
                    self.line_thickness
                )
                
                if gpu_result is not None:
                    print("GPU line drawing successful")
                    return gpu_result
                else:
                    print("GPU line drawing failed, falling back to CPU")
            
            # Fallback to CPU line drawing
            print(f"Using CPU for line drawing ({pixmap.width()}x{pixmap.height()})")
            final_pixmap = pixmap.copy()
            painter = QPainter(final_pixmap)
            painter.setRenderHint(QPainter.Antialiasing, False)
            
            # Use user-selected color and thickness
            pen_color = self.line_color
            pen_thickness = self.line_thickness
            painter.setPen(QPen(pen_color, pen_thickness, Qt.SolidLine))
            
            # Get basic scale factors (simplified approach)
            if hasattr(self, 'original_pixmap') and self.original_pixmap:
                original_size = self.original_pixmap.size()
                current_size = pixmap.size()
                scale_x = current_size.width() / original_size.width()
                scale_y = current_size.height() / original_size.height()
            else:
                scale_x = 1.0
                scale_y = 1.0
            
            # Draw simple lines (basic version for async processing)
            for x in self.drawn_lines:
                display_x = int(x * scale_x)
                if 0 <= display_x < final_pixmap.width():
                    painter.drawLine(display_x, 0, display_x, final_pixmap.height())
            
            for y in self.drawn_horizontal_lines:
                display_y = int(y * scale_y)
                if 0 <= display_y < final_pixmap.height():
                    painter.drawLine(0, display_y, final_pixmap.width(), display_y)
            
            for line in self.drawn_free_lines:
                start_x, start_y = line['start']
                end_x, end_y = line['end']
                
                display_start_x = int(start_x * scale_x)
                display_start_y = int(start_y * scale_y)
                display_end_x = int(end_x * scale_x)
                display_end_y = int(end_y * scale_y)
                
                painter.drawLine(display_start_x, display_start_y, display_end_x, display_end_y)
            
            painter.end()
            return final_pixmap
            
        except Exception as e:
            print(f"Error adding lines to pixmap: {e}")
            return pixmap

    def _display_image_with_lut_preview(self, img_path):
        """Display image with smart LUT preview handling to prevent freezing"""
        # Clear all drawn lines when switching images
        self.clear_lines()

        # Animated GIF: play with QMovie instead of static pixmap
        if is_animated_gif(img_path):
            self._display_animated_gif(img_path)
            return
        
        if self.current_lut and self.lut_strength > 0:
            # If LUT is active, use instant preview system
            self.current_image = img_path  # Set this first
            
            # Show instant fast preview
            self.status.showMessage(f"Loading image with {self.current_lut_name}... (instant preview)")
            QApplication.processEvents()
            
            # Create fast preview first
            self._create_fast_image_lut_preview(img_path)
            
            # Then apply full quality in background  
            QTimer.singleShot(15, lambda: self._apply_full_quality_to_current_image())
        else:
            # No LUT active, use normal fast display
            self.display_image(img_path)

    def _create_fast_image_lut_preview(self, img_path):
        """Create instant preview for new image with LUT - Enhanced GPU quality"""
        try:
            # Load base image quickly
            if img_path in self.pixmap_cache:
                base_pixmap = self.pixmap_cache[img_path]
            else:
                base_pixmap, error = safe_load_pixmap(img_path)
                if error:
                    self.display_image(img_path)  # Fallback to normal display
                    return
                # Cache it for future use
                self._manage_cache(self.pixmap_cache, img_path, base_pixmap)
            
            # ENHANCED PREVIEW: Better quality when lines are present
            has_lines = (self.lines_visible and 
                        (self.drawn_lines or self.drawn_horizontal_lines or self.drawn_free_lines or self.drawn_free_strokes))
            
            if has_lines:
                # Higher quality preview when lines are present
                preview_size = min(1000, base_pixmap.width() // 1.5, base_pixmap.height() // 1.5)
                transform_quality = Qt.SmoothTransformation
            else:
                # Standard quality for no lines
                preview_size = min(600, base_pixmap.width() // 2, base_pixmap.height() // 2)
                transform_quality = Qt.FastTransformation
            
            fast_preview = base_pixmap.scaled(
                preview_size, preview_size,
                Qt.KeepAspectRatio, transform_quality
            )
            
            # Apply LUT using GPU acceleration when available
            if self.current_lut:
                lut_dict = {
                    'data': self.current_lut['data'],
                    'size': self.current_lut['size'],
                    'file_path': getattr(self.current_lut, 'file_path', 'preview')
                }
                
                # Use GPU for better quality when available - ENHANCED for lines
                if (self.gpu_processor.is_available() and 
                    fast_preview.width() * fast_preview.height() > 100000) or \
                   (has_lines and self.gpu_processor.is_available() and 
                    fast_preview.width() * fast_preview.height() > 30000):
                    
                    # Very aggressive GPU usage when lines are present
                    print(f"Using GPU for fast image LUT preview ({fast_preview.width()}x{fast_preview.height()}, lines: {has_lines})")
                    
                    # Convert pixmap to image for GPU processing
                    preview_image = fast_preview.toImage()
                    if preview_image.format() != preview_image.Format.Format_RGB32:
                        preview_image = preview_image.convertToFormat(preview_image.Format.Format_RGB32)
                    
                    # Apply LUT using GPU
                    gpu_result = self.gpu_processor.apply_lut_gpu(
                        preview_image, 
                        lut_dict['data'], 
                        lut_dict['size'], 
                        self.lut_strength / 100.0
                    )
                    
                    if gpu_result is not None:
                        fast_preview = QPixmap.fromImage(gpu_result)
                    else:
                        # Fallback to CPU if GPU fails
                        print("GPU image preview failed, falling back to CPU")
                        fast_preview = self.apply_lut_to_image(fast_preview, lut_dict, self.lut_strength)
                else:
                    # Use CPU LUT processing for smaller previews
                    fast_preview = self.apply_lut_to_image(fast_preview, lut_dict, self.lut_strength)
            
            # Apply basic transformations (rotation, flips)
            fast_preview = self._apply_quick_transforms(fast_preview, transform_quality)
            
            # Scale to display size and show immediately
            final_preview = self._scale_pixmap(fast_preview, img_path)
            self.image_label.setPixmap(final_preview)
            QApplication.processEvents()
            
        except Exception as e:
            print(f"Error creating fast image preview: {e}")
            import traceback
            traceback.print_exc()
            # Fallback to normal display
            self.display_image(img_path)

    def _apply_quick_transforms(self, pixmap, quality=Qt.FastTransformation):
        """Apply rotation and flips with specified quality for preview"""
        if not pixmap or pixmap.isNull():
            return pixmap
            
        # Apply rotation if needed
        if self.rotation_angle != 0:
            transform = QTransform()
            transform.rotate(self.rotation_angle)
            pixmap = pixmap.transformed(transform, quality)
        
        # Apply flips if needed
        if self.flipped_h:
            pixmap = pixmap.transformed(QTransform().scale(-1, 1), quality)
        if self.flipped_v:
            pixmap = pixmap.transformed(QTransform().scale(1, -1), quality)
            
        return pixmap

    def _apply_full_quality_to_current_image(self):
        """Apply full quality processing to the current image"""
        if self.current_image:
            self.display_image(self.current_image)

    def _apply_ultra_fast_lut(self, pixmap):
        """Ultra-fast LUT application for instant preview (sacrifices quality for speed)"""
        if not self.current_lut or not pixmap or pixmap.isNull():
            return pixmap
            
        try:
            image = pixmap.toImage()
            if image.isNull():
                return pixmap
                
            # Convert to fast format
            if image.format() != image.Format.Format_RGB32:
                image = image.convertToFormat(image.Format.Format_RGB32)
            
            width = image.width()
            height = image.height()
            lut_data = self.current_lut['data']
            lut_size = self.current_lut['size']
            strength_factor = self.lut_strength / 100.0
            
            # Ultra-fast processing: sample every 2nd pixel for better quality preview
            sample_rate = 2  # Reduced from 4 to 2 for better quality
            
            for y in range(0, height, sample_rate):
                scan_line = image.scanLine(y)
                
                for x in range(0, width, sample_rate):
                    offset = x * 4
                    
                    # Read pixel
                    b = scan_line[offset]
                    g = scan_line[offset + 1]
                    r = scan_line[offset + 2]
                    a = scan_line[offset + 3]
                    
                    # Normalize and apply nearest-neighbor LUT lookup
                    r_norm = r / 255.0
                    g_norm = g / 255.0
                    b_norm = b / 255.0
                    
                    # Fast nearest-neighbor lookup
                    lut_result = self._nearest_neighbor_lut(r_norm, g_norm, b_norm, lut_data, lut_size)
                    
                    # Blend and write back
                    final_r = int((r_norm * (1.0 - strength_factor) + lut_result[0] * strength_factor) * 255)
                    final_g = int((g_norm * (1.0 - strength_factor) + lut_result[1] * strength_factor) * 255)
                    final_b = int((b_norm * (1.0 - strength_factor) + lut_result[2] * strength_factor) * 255)
                    
                    # Clamp values
                    final_r = max(0, min(255, final_r))
                    final_g = max(0, min(255, final_g))
                    final_b = max(0, min(255, final_b))
                    
                    # Write to current pixel
                    scan_line[offset] = final_b
                    scan_line[offset + 1] = final_g
                    scan_line[offset + 2] = final_r
                    scan_line[offset + 3] = a
                    
                    # Fill adjacent pixel for smoother result (only horizontally)
                    if x + 1 < width:
                        next_offset = (x + 1) * 4
                        scan_line[next_offset] = final_b
                        scan_line[next_offset + 1] = final_g
                        scan_line[next_offset + 2] = final_r
                        scan_line[next_offset + 3] = scan_line[next_offset + 3]
                
                # Fill the next row with same data for smoother preview
                if y + 1 < height:
                    next_line = image.scanLine(y + 1)
                    # Copy current line to next line for smoother blocks
                    for x in range(0, width, sample_rate):
                        if x < width:
                            src_offset = x * 4
                            # Only copy processed pixels
                            next_line[src_offset] = scan_line[src_offset]
                            next_line[src_offset + 1] = scan_line[src_offset + 1]
                            next_line[src_offset + 2] = scan_line[src_offset + 2]
                            next_line[src_offset + 3] = scan_line[src_offset + 3]
                            
                            # Also copy the adjacent pixel if it exists
                            if x + 1 < width:
                                next_src_offset = (x + 1) * 4
                                next_line[next_src_offset] = scan_line[next_src_offset]
                                next_line[next_src_offset + 1] = scan_line[next_src_offset + 1]
                                next_line[next_src_offset + 2] = scan_line[next_src_offset + 2]
                                next_line[next_src_offset + 3] = scan_line[next_src_offset + 3]
            
            return QPixmap.fromImage(image)
            
        except Exception as e:
            print(f"Error in ultra-fast LUT: {e}")
            return pixmap

    def _nearest_neighbor_lut(self, r, g, b, lut_data, lut_size):
        """Fastest possible LUT lookup - pure nearest neighbor"""
        try:
            # Scale to LUT coordinates
            r_idx = max(0, min(lut_size - 1, int(r * (lut_size - 1) + 0.5)))
            g_idx = max(0, min(lut_size - 1, int(g * (lut_size - 1) + 0.5)))
            b_idx = max(0, min(lut_size - 1, int(b * (lut_size - 1) + 0.5)))
            
            # Direct lookup - no interpolation
            index = r_idx + g_idx * lut_size + b_idx * lut_size * lut_size
            if 0 <= index < len(lut_data):
                return lut_data[index]
            else:
                return (r, g, b)
        except:
            return (r, g, b)

    def _apply_basic_enhancements(self, pixmap):
        """Apply basic enhancements quickly for preview"""
        if not pixmap or pixmap.isNull():
            return pixmap
            
        # For fast preview, skip complex enhancements
        # Just return the LUT-processed pixmap
        return pixmap

    def update_lut_strength(self, value):
        """Update LUT application strength using unified processing pipeline"""
        self.lut_strength = value
        
        # Clear caches for immediate update (including LUT process cache)
        self.enhancement_cache.clear()
        self.scaled_cache.clear()
        if hasattr(self, '_lut_process_cache'):
            self._lut_process_cache.clear()  # Clear LUT cache when strength changes
        
        # CRITICAL: Clear zoom optimization cache when LUT strength changes
        if hasattr(self, '_last_processed_image'):
            self._last_processed_image = None
        self._last_processed_has_lut = False
        
        # Use the same processing path as display_image for consistency
        if self.current_image:
            self.display_image(self.current_image)
            if self.lut_enabled and self.current_lut and value > 0:
                lut_name = self.current_lut_name if self.current_lut_name != "None" else "LUT"
                self.status.showMessage(f"{lut_name} strength: {value}%")
            elif value == 0:
                self.status.showMessage("LUT disabled (strength: 0%)")
            else:
                self.status.showMessage(f"LUT strength: {value}% (no LUT selected)")

    def reset_zoom(self):
        """Reset image zoom and pan to 100% with smart LUT caching"""
        if self.image_label:
            self.image_label.reset_zoom()
            if self.current_image:
                self._smart_zoom_display()
            
            # Update status with processing awareness
            if (self.lut_enabled and hasattr(self, '_async_processing_state') and 
                self._async_processing_state and self.current_lut):
                progress = (self._async_processing_state['current_row'] / 
                          self._async_processing_state['total_rows']) * 100
                lut_name = self.current_lut_name if self.current_lut_name != "None" else "LUT"
                self.status.showMessage(f"Zoom reset to 100% - {lut_name} processing... {progress:.0f}%")
            else:
                self.status.showMessage("Zoom reset to 100%")

    def zoom_in(self):
        """Zoom in by 15% with smart LUT caching"""
        if self.image_label:
            current_zoom = getattr(self.image_label, 'zoom_factor', 1.0)
            new_zoom = min(current_zoom * 1.15, 8.0)
            self.image_label.zoom_factor = new_zoom
            if self.current_image:
                self._smart_zoom_display()
            
            # Update status with processing awareness
            if (self.lut_enabled and hasattr(self, '_async_processing_state') and 
                self._async_processing_state and self.current_lut):
                progress = (self._async_processing_state['current_row'] / 
                          self._async_processing_state['total_rows']) * 100
                lut_name = self.current_lut_name if self.current_lut_name != "None" else "LUT"
                self.status.showMessage(f"Zoom: {new_zoom:.1f}x - {lut_name} processing... {progress:.0f}%")
            else:
                self.status.showMessage(f"Zoom: {new_zoom:.1f}x")

    def zoom_out(self):
        """Zoom out by 15% with smart LUT caching"""
        if self.image_label:
            current_zoom = getattr(self.image_label, 'zoom_factor', 1.0)
            new_zoom = max(current_zoom / 1.15, 0.1)
            self.image_label.zoom_factor = new_zoom
            if self.current_image:
                self._smart_zoom_display()
            
            # Update status with processing awareness
            if (self.lut_enabled and hasattr(self, '_async_processing_state') and 
                self._async_processing_state and self.current_lut):
                progress = (self._async_processing_state['current_row'] / 
                          self._async_processing_state['total_rows']) * 100
                lut_name = self.current_lut_name if self.current_lut_name != "None" else "LUT"
                self.status.showMessage(f"Zoom: {new_zoom:.1f}x - {lut_name} processing... {progress:.0f}%")
            else:
                self.status.showMessage(f"Zoom: {new_zoom:.1f}x")
    
    def _maybe_schedule_pdf_hires(self):
        """Decide whether the current PDF page/spread needs a sharper re-render.

        Runs for single-page and 2/3-page spread PDF views (never for images,
        GIFs or video). At zoom ≤ 1.0 it restores the normal fit-resolution page
        so memory stays low; when zoomed in far enough that the current base
        pixmap would be upscaled, it (re)starts a short debounce before
        rendering at a matching higher resolution on a background thread.
        """
        doc = getattr(self, '_pdf_doc', None)
        if doc is None:
            return
        if self.image_label.is_animation_playing():
            return

        zoom = getattr(self.image_label, 'zoom_factor', 1.0)

        # Back at (or below) 100%: drop any hi-res render and restore the normal
        # fit-resolution base so we don't keep a big pixmap around.
        if zoom <= 1.0:
            if self._pdf_hires_page != -1:
                self._restore_pdf_base_resolution()
            return

        base = self.pixmap_cache.get(self.current_image)
        if base is None or base.isNull():
            return

        label = self.image_label.size()
        # Longest side the zoomed page/spread will occupy on screen. It fits
        # inside the label, so label long edge × zoom is a safe upper bound.
        required = int(max(label.width(), label.height()) * zoom)
        current_long = max(base.width(), base.height())
        count = self._spread_count()

        # Already sharp enough (current base has at least the needed pixels), or
        # we already have a hi-res render for this exact page/spread at ≥ size.
        if current_long >= required:
            return
        if (self._pdf_hires_page == self._pdf_page
                and self._pdf_hires_count == count
                and self._pdf_hires_target >= required):
            return

        self._pdf_hires_timer.start()

    def _start_pdf_hires_render(self):
        """Kick off the background hi-res render for the current page/spread."""
        doc = getattr(self, '_pdf_doc', None)
        if doc is None:
            return
        zoom = getattr(self.image_label, 'zoom_factor', 1.0)
        if zoom <= 1.0:
            return
        # Don't stack workers; a newer one will be scheduled if still needed.
        if self._pdf_hires_thread is not None and self._pdf_hires_thread.isRunning():
            return

        base = self.pixmap_cache.get(self.current_image)
        if base is None or base.isNull():
            return

        label = self.image_label.size()
        required = int(max(label.width(), label.height()) * zoom)
        count = self._spread_count()
        page = self._pdf_page

        if count > 1:
            # For spreads the long edge is the composite width; scale the whole
            # composite by (required / current width) and pass the target HEIGHT
            # so render_spread_qimage reproduces the same side-by-side layout.
            base_long = max(base.width(), base.height())
            scale = required / base_long if base_long else 1.0
            target = max(1, int(base.height() * scale))
        else:
            target = required  # single page: target is the long edge

        worker = PdfHiresWorker(doc, page, count, target, self)
        worker.ready.connect(self._on_pdf_hires_ready)
        worker.finished.connect(self._on_pdf_hires_finished)
        self._pdf_hires_thread = worker
        self.status.showMessage(f"Sharpening page {page + 1}…")
        worker.start()

    def _on_pdf_hires_finished(self):
        """Clear the worker handle once the QThread has finished."""
        self._pdf_hires_thread = None

    def _on_pdf_hires_ready(self, page, count, target, qimage):
        """Swap in the freshly rendered high-resolution page/spread (main thread)."""
        # Ignore stale results: PDF closed, page/spread changed, or zoomed out.
        if getattr(self, '_pdf_doc', None) is None:
            return
        if page != self._pdf_page or count != self._spread_count():
            return
        if getattr(self.image_label, 'zoom_factor', 1.0) <= 1.0:
            return
        if qimage is None or qimage.isNull():
            return

        pixmap = QPixmap.fromImage(qimage)
        if pixmap.isNull():
            return

        # Replace the base pixmap for this page so display/zoom paths pick up the
        # sharper raster, then invalidate derived caches and redraw.
        self.pixmap_cache[self.current_image] = pixmap
        self.original_pixmap = pixmap
        self._pdf_hires_page = page
        self._pdf_hires_count = count
        self._pdf_hires_target = target
        self.enhancement_cache.clear()
        self.scaled_cache.clear()
        if hasattr(self, '_lut_process_cache'):
            self._lut_process_cache.clear()
        self._last_processed_image = None
        self._last_processed_has_lut = False
        # Full redraw so any LUT/enhancements re-apply at the new resolution.
        self.display_image(self.current_image)
        zoom = getattr(self.image_label, 'zoom_factor', 1.0)
        self.status.showMessage(f"Zoom: {zoom:.1f}x (sharp)")

    def _restore_pdf_base_resolution(self):
        """Drop the hi-res render and reload the normal fit-resolution page."""
        self._pdf_hires_timer.stop()
        self._pdf_hires_page = -1
        self._pdf_hires_count = 1
        self._pdf_hires_target = 0
        doc = getattr(self, '_pdf_doc', None)
        if doc is None:
            return
        path = self.current_image
        if not path:
            return
        base, error = safe_load_pixmap(path)
        if error or base is None or base.isNull():
            return
        self.pixmap_cache[path] = base
        self.original_pixmap = base
        self.enhancement_cache.clear()
        self.scaled_cache.clear()
        if hasattr(self, '_lut_process_cache'):
            self._lut_process_cache.clear()
        self._last_processed_image = None
        self._last_processed_has_lut = False
        self.display_image(path)

    def _smart_zoom_display(self):
        """OPTIMIZED zoom display - avoids GPU reprocessing during interactive zoom"""
        if not self.current_image:
            return

        # For animated GIFs the frame callback already handles zoom via _scale_pixmap;
        # nothing extra to do here — next frame will pick up the new zoom factor.
        if self.image_label.is_animation_playing():
            return
        if self.image_label.is_animation_paused():
            self._redraw_paused_gif_frame()
            return

        # PDF only: schedule a sharper re-render of the page if we've zoomed in
        # past the current raster's resolution (no-op for images/spreads).
        if getattr(self, '_pdf_doc', None) is not None:
            self._maybe_schedule_pdf_hires()

        # The fast zoom path reuses a cached pre-posterize image, which would
        # drop the value filter or edge detection on resize/zoom. When either
        # is on, fall back to the full display path so the effect is preserved.
        if (getattr(self, 'value_filter_enabled', False) or getattr(self, 'edge_detection_enabled', False)
                or getattr(self, 'color_groups_enabled', False)
                or getattr(self, 'curves_enabled', False)
                or getattr(self, 'erase_strokes', None) or getattr(self, 'current_erase_stroke', None)):
            self.display_image(self.current_image)
            return
            
        try:
            # CRITICAL OPTIMIZATION: During zoom, use the last processed image to avoid GPU calls
            # This prevents freezing during interactive zoom operations
            
            # If we have a recently processed image cached, use it for zoom
            if (hasattr(self, '_last_processed_image') and 
                self._last_processed_image and
                not self._last_processed_image.isNull() and
                (self.lut_enabled or not self._last_processed_has_lut)):
                
                # Use the cached processed image for zoom operations
                processed_pixmap = QPixmap.fromImage(self._last_processed_image)
                
                # Apply transforms if needed (these are fast)
                if self.rotation_angle != 0 or self.flipped_h or self.flipped_v:
                    processed_pixmap = self._apply_cached_transforms(processed_pixmap)
                
                # Scale and display - FAST operation, no GPU processing
                final_pixmap = self._scale_pixmap(processed_pixmap, self.current_image)
                
                # Draw lines directly on the scaled pixmap if any exist
                if self.lines_visible and (self.drawn_lines or self.drawn_horizontal_lines or self.drawn_free_lines or self.drawn_free_strokes):
                    painter = QPainter(final_pixmap)
                    painter.setRenderHint(QPainter.Antialiasing, False)
                    # Determine zoom_factor first, then compute base thickness for non-pressure primitives
                    zoom_factor = getattr(self.image_label, 'zoom_factor', 1.0)
                    zoom_scaled_thickness = max(1, self.line_thickness)
                    painter.setPen(QPen(self.line_color, zoom_scaled_thickness, Qt.SolidLine))
                    
                    # Get transformation parameters
                    original_size = processed_pixmap.size()
                    label_size = self.image_label.size()
                    
                    # Calculate scaling factors
                    if self.rotation_angle == 90 or self.rotation_angle == 270:
                        display_ref = QSize(original_size.height(), original_size.width())
                    else:
                        display_ref = original_size
                        
                    base_scaled = display_ref.scaled(label_size, Qt.KeepAspectRatio)
                    zoomed_width = int(base_scaled.width() * zoom_factor)
                    zoomed_height = int(base_scaled.height() * zoom_factor)
                    
                    draw_x = (label_size.width() - zoomed_width) // 2 + int(self.image_label.pan_offset_x)
                    draw_y = (label_size.height() - zoomed_height) // 2 + int(self.image_label.pan_offset_y)
                    
                    scale_x = zoomed_width / original_size.width()
                    scale_y = zoomed_height / original_size.height()
                    
                    # Draw vertical lines
                    for x in self.drawn_lines:
                        display_x = int(x * scale_x) + draw_x
                        if 0 <= display_x < final_pixmap.width():
                            painter.drawLine(display_x, 0, display_x, final_pixmap.height())
                    
                    # Draw horizontal lines
                    for y in self.drawn_horizontal_lines:
                        display_y = int(y * scale_y) + draw_y
                        if 0 <= display_y < final_pixmap.height():
                            painter.drawLine(0, display_y, final_pixmap.width(), display_y)
                    
                    # Draw free lines
                    for line in self.drawn_free_lines:
                        start_x, start_y = line['start']
                        end_x, end_y = line['end']
                        display_start_x = int(start_x * scale_x) + draw_x
                        display_start_y = int(start_y * scale_y) + draw_y
                        display_end_x = int(end_x * scale_x) + draw_x
                        display_end_y = int(end_y * scale_y) + draw_y
                        painter.drawLine(display_start_x, display_start_y, display_end_x, display_end_y)
                    
                    # Draw free strokes (pressure + zoom aware)
                    painter.setRenderHint(QPainter.Antialiasing, True)
                    if self.drawn_free_strokes:
                        for stroke in self.drawn_free_strokes:
                            if len(stroke) < 2:
                                continue
                            for i in range(len(stroke) - 1):
                                # Extract start point
                                if len(stroke[i]) == 3 and self.pen_pressure_enabled:
                                    start_x, start_y, p1 = stroke[i]
                                else:
                                    start_x, start_y = stroke[i][:2]
                                    p1 = 1.0
                                # Extract end point
                                if len(stroke[i + 1]) == 3 and self.pen_pressure_enabled:
                                    end_x, end_y, p2 = stroke[i + 1]
                                else:
                                    end_x, end_y = stroke[i + 1][:2]
                                    p2 = 1.0
                                avg_pressure = (p1 + p2) / 2.0 if self.pen_pressure_enabled else 1.0
                                seg_thick = self._pressure_to_thickness(avg_pressure)
                                painter.setPen(QPen(self.line_color, seg_thick, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
                                display_start_x = int(start_x * scale_x) + draw_x
                                display_start_y = int(start_y * scale_y) + draw_y
                                display_end_x = int(end_x * scale_x) + draw_x
                                display_end_y = int(end_y * scale_y) + draw_y
                                painter.drawLine(display_start_x, display_start_y, display_end_x, display_end_y)
                    painter.setRenderHint(QPainter.Antialiasing, False)
                    
                    painter.end()
                
                # Handle image visibility toggle
                if not self.image_visible:
                    # Create a blank pixmap with the same size but keep lines visible
                    blank_pixmap = QPixmap(final_pixmap.size())
                    blank_pixmap.fill(Qt.black)  # Fill with black background
                    
                    # If there are lines, copy them from final_pixmap to blank_pixmap
                    if self.lines_visible and (self.drawn_lines or self.drawn_horizontal_lines or self.drawn_free_lines or self.drawn_free_strokes):
                        painter = QPainter(blank_pixmap)
                        painter.setRenderHint(QPainter.Antialiasing, False)
                        # Apply zoom scaling to line thickness for consistent visual appearance
                        zoom_scaled_thickness = max(1, self.line_thickness)
                        painter.setPen(QPen(self.line_color, zoom_scaled_thickness, Qt.SolidLine))
                        
                        # Use the same coordinate transformations as above
                        original_size = processed_pixmap.size()
                        label_size = self.image_label.size()
                        zoom_factor = getattr(self.image_label, 'zoom_factor', 1.0)
                        
                        if self.rotation_angle == 90 or self.rotation_angle == 270:
                            display_ref = QSize(original_size.height(), original_size.width())
                        else:
                            display_ref = original_size
                            
                        base_scaled = display_ref.scaled(label_size, Qt.KeepAspectRatio)
                        zoomed_width = int(base_scaled.width() * zoom_factor)
                        zoomed_height = int(base_scaled.height() * zoom_factor)
                        
                        draw_x = (label_size.width() - zoomed_width) // 2 + int(self.image_label.pan_offset_x)
                        draw_y = (label_size.height() - zoomed_height) // 2 + int(self.image_label.pan_offset_y)
                        
                        scale_x = zoomed_width / original_size.width()
                        scale_y = zoomed_height / original_size.height()
                        
                        # Draw vertical lines
                        for x in self.drawn_lines:
                            display_x = int(x * scale_x) + draw_x
                            if 0 <= display_x < blank_pixmap.width():
                                painter.drawLine(display_x, 0, display_x, blank_pixmap.height())
                        
                        # Draw horizontal lines
                        for y in self.drawn_horizontal_lines:
                            display_y = int(y * scale_y) + draw_y
                            if 0 <= display_y < blank_pixmap.height():
                                painter.drawLine(0, display_y, blank_pixmap.width(), display_y)
                        
                        # Draw free lines
                        for line in self.drawn_free_lines:
                            start_x, start_y = line['start']
                            end_x, end_y = line['end']
                            display_start_x = int(start_x * scale_x) + draw_x
                            display_start_y = int(start_y * scale_y) + draw_y
                            display_end_x = int(end_x * scale_x) + draw_x
                            display_end_y = int(end_y * scale_y) + draw_y
                            painter.drawLine(display_start_x, display_start_y, display_end_x, display_end_y)
                        
                        # Draw free strokes (pressure + zoom aware)
                        painter.setRenderHint(QPainter.Antialiasing, True)
                        if self.drawn_free_strokes:
                            for stroke in self.drawn_free_strokes:
                                if len(stroke) < 2:
                                    continue
                                for i in range(len(stroke) - 1):
                                    if len(stroke[i]) == 3 and self.pen_pressure_enabled:
                                        start_x, start_y, p1 = stroke[i]
                                    else:
                                        start_x, start_y = stroke[i][:2]
                                        p1 = 1.0
                                    if len(stroke[i + 1]) == 3 and self.pen_pressure_enabled:
                                        end_x, end_y, p2 = stroke[i + 1]
                                    else:
                                        end_x, end_y = stroke[i + 1][:2]
                                        p2 = 1.0
                                    avg_pressure = (p1 + p2)/2.0 if self.pen_pressure_enabled else 1.0
                                    seg_thick = self._pressure_to_thickness(avg_pressure)
                                    painter.setPen(QPen(self.line_color, seg_thick, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
                                    display_start_x = int(start_x * scale_x) + draw_x
                                    display_start_y = int(start_y * scale_y) + draw_y
                                    display_end_x = int(end_x * scale_x) + draw_x
                                    display_end_y = int(end_y * scale_y) + draw_y
                                    painter.drawLine(display_start_x, display_start_y, display_end_x, display_end_y)
                        painter.setRenderHint(QPainter.Antialiasing, False)
                        
                        painter.end()
                    
                    final_pixmap = blank_pixmap
                
                self.image_label.setPixmap(self._apply_fixed_overlays_to_pixmap(final_pixmap))
                
                # Show zoom status
                zoom = getattr(self.image_label, 'zoom_factor', 1.0)
                self.status.showMessage(f"Zoom: {zoom:.1f}x (GPU cached)")
                return
            
            # If no cached processed image, check LUT cache
            if self.lut_enabled and self.current_lut and self.lut_strength > 0:
                cache_key = self._get_lut_cache_key()
                
                # Check if we have cached LUT result
                if (hasattr(self, '_lut_process_cache') and 
                    cache_key in self._lut_process_cache):
                    
                    cached_pixmap = self._lut_process_cache[cache_key]
                    
                    # Apply transforms if needed
                    if self.rotation_angle != 0 or self.flipped_h or self.flipped_v:
                        processed_pixmap = self._apply_cached_transforms(cached_pixmap)
                    else:
                        processed_pixmap = cached_pixmap
                    
                    # Scale and display with lines
                    final_pixmap = self._scale_pixmap(processed_pixmap, self.current_image)
                    
                    # Draw lines directly on the scaled pixmap if any exist
                    if self.lines_visible and (self.drawn_lines or self.drawn_horizontal_lines or self.drawn_free_lines or self.drawn_free_strokes):
                        painter = QPainter(final_pixmap)
                        painter.setRenderHint(QPainter.Antialiasing, False)
                        # Apply zoom scaling to line thickness for consistent visual appearance
                        zoom_factor = getattr(self.image_label, 'zoom_factor', 1.0)
                        zoom_scaled_thickness = max(1, self.line_thickness)
                        painter.setPen(QPen(self.line_color, zoom_scaled_thickness, Qt.SolidLine))
                        
                        # Get transformation parameters
                        original_size = processed_pixmap.size()
                        label_size = self.image_label.size()
                        zoom_factor = self.image_label.zoom_factor
                        
                        # Calculate scaling factors
                        if self.rotation_angle == 90 or self.rotation_angle == 270:
                            display_ref = QSize(original_size.height(), original_size.width())
                        else:
                            display_ref = original_size
                            
                        base_scaled = display_ref.scaled(label_size, Qt.KeepAspectRatio)
                        zoomed_width = int(base_scaled.width() * zoom_factor)
                        zoomed_height = int(base_scaled.height() * zoom_factor)
                        
                        draw_x = (label_size.width() - zoomed_width) // 2 + int(self.image_label.pan_offset_x)
                        draw_y = (label_size.height() - zoomed_height) // 2 + int(self.image_label.pan_offset_y)
                        
                        scale_x = zoomed_width / original_size.width()
                        scale_y = zoomed_height / original_size.height()
                        
                        # Draw vertical lines
                        for x in self.drawn_lines:
                            display_x = int(x * scale_x) + draw_x
                            if 0 <= display_x < final_pixmap.width():
                                painter.drawLine(display_x, 0, display_x, final_pixmap.height())
                        
                        # Draw horizontal lines
                        for y in self.drawn_horizontal_lines:
                            display_y = int(y * scale_y) + draw_y
                            if 0 <= display_y < final_pixmap.height():
                                painter.drawLine(0, display_y, final_pixmap.width(), display_y)
                        
                        # Draw free lines
                        for line in self.drawn_free_lines:
                            start_x, start_y = line['start']
                            end_x, end_y = line['end']
                            display_start_x = int(start_x * scale_x) + draw_x
                            display_start_y = int(start_y * scale_y) + draw_y
                            display_end_x = int(end_x * scale_x) + draw_x
                            display_end_y = int(end_y * scale_y) + draw_y
                            painter.drawLine(display_start_x, display_start_y, display_end_x, display_end_y)
                        
                        # Draw free strokes (pressure + zoom aware)
                        painter.setRenderHint(QPainter.Antialiasing, True)
                        if self.drawn_free_strokes:
                            for stroke in self.drawn_free_strokes:
                                if len(stroke) < 2:
                                    continue
                                for i in range(len(stroke) - 1):
                                    if len(stroke[i]) == 3 and self.pen_pressure_enabled:
                                        start_x, start_y, p1 = stroke[i]
                                    else:
                                        start_x, start_y = stroke[i][:2]
                                        p1 = 1.0
                                    if len(stroke[i + 1]) == 3 and self.pen_pressure_enabled:
                                        end_x, end_y, p2 = stroke[i + 1]
                                    else:
                                        end_x, end_y = stroke[i + 1][:2]
                                        p2 = 1.0
                                    avg_pressure = (p1 + p2)/2.0 if self.pen_pressure_enabled else 1.0
                                    seg_thick = self._pressure_to_thickness(avg_pressure)
                                    painter.setPen(QPen(self.line_color, seg_thick, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
                                    display_start_x = int(start_x * scale_x) + draw_x
                                    display_start_y = int(start_y * scale_y) + draw_y
                                    display_end_x = int(end_x * scale_x) + draw_x
                                    display_end_y = int(end_y * scale_y) + draw_y
                                    painter.drawLine(display_start_x, display_start_y, display_end_x, display_end_y)
                        painter.setRenderHint(QPainter.Antialiasing, False)
                        
                        painter.end()
                    
                    # Handle image visibility toggle
                    if not self.image_visible:
                        # Create a blank pixmap with the same size but keep lines visible
                        blank_pixmap = QPixmap(final_pixmap.size())
                        blank_pixmap.fill(Qt.black)  # Fill with black background
                        
                        # If there are lines, copy them from final_pixmap to blank_pixmap
                        if self.lines_visible and (self.drawn_lines or self.drawn_horizontal_lines or self.drawn_free_lines or self.drawn_free_strokes):
                            painter = QPainter(blank_pixmap)
                            painter.setRenderHint(QPainter.Antialiasing, False)
                            # Apply zoom scaling to line thickness for consistent visual appearance
                            zoom_factor = getattr(self.image_label, 'zoom_factor', 1.0)
                            zoom_scaled_thickness = max(1, self.line_thickness)
                            painter.setPen(QPen(self.line_color, zoom_scaled_thickness, Qt.SolidLine))
                            
                            # Use the same coordinate transformations as above
                            original_size = processed_pixmap.size()
                            label_size = self.image_label.size()
                            zoom_factor = getattr(self.image_label, 'zoom_factor', 1.0)
                            
                            if self.rotation_angle == 90 or self.rotation_angle == 270:
                                display_ref = QSize(original_size.height(), original_size.width())
                            else:
                                display_ref = original_size
                                
                            base_scaled = display_ref.scaled(label_size, Qt.KeepAspectRatio)
                            zoomed_width = int(base_scaled.width() * zoom_factor)
                            zoomed_height = int(base_scaled.height() * zoom_factor)
                            
                            draw_x = (label_size.width() - zoomed_width) // 2 + int(self.image_label.pan_offset_x)
                            draw_y = (label_size.height() - zoomed_height) // 2 + int(self.image_label.pan_offset_y)
                            
                            scale_x = zoomed_width / original_size.width()
                            scale_y = zoomed_height / original_size.height()
                            
                            # Draw vertical lines
                            for x in self.drawn_lines:
                                display_x = int(x * scale_x) + draw_x
                                if 0 <= display_x < blank_pixmap.width():
                                    painter.drawLine(display_x, 0, display_x, blank_pixmap.height())
                            
                            # Draw horizontal lines
                            for y in self.drawn_horizontal_lines:
                                display_y = int(y * scale_y) + draw_y
                                if 0 <= display_y < blank_pixmap.height():
                                    painter.drawLine(0, display_y, blank_pixmap.width(), display_y)
                            
                            # Draw free lines
                            for line in self.drawn_free_lines:
                                start_x, start_y = line['start']
                                end_x, end_y = line['end']
                                display_start_x = int(start_x * scale_x) + draw_x
                                display_start_y = int(start_y * scale_y) + draw_y
                                display_end_x = int(end_x * scale_x) + draw_x
                                display_end_y = int(end_y * scale_y) + draw_y
                                painter.drawLine(display_start_x, display_start_y, display_end_x, display_end_y)
                            
                            # Draw free strokes (pressure + zoom aware)
                            painter.setRenderHint(QPainter.Antialiasing, True)
                            if self.drawn_free_strokes:
                                for stroke in self.drawn_free_strokes:
                                    if len(stroke) < 2:
                                        continue
                                    for i in range(len(stroke) - 1):
                                        if len(stroke[i]) == 3 and self.pen_pressure_enabled:
                                            start_x, start_y, p1 = stroke[i]
                                        else:
                                            start_x, start_y = stroke[i][:2]
                                            p1 = 1.0
                                        if len(stroke[i + 1]) == 3 and self.pen_pressure_enabled:
                                            end_x, end_y, p2 = stroke[i + 1]
                                        else:
                                            end_x, end_y = stroke[i + 1][:2]
                                            p2 = 1.0
                                        avg_pressure = (p1 + p2)/2.0 if self.pen_pressure_enabled else 1.0
                                        seg_thick = self._pressure_to_thickness(avg_pressure)
                                        painter.setPen(QPen(self.line_color, seg_thick, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
                                        display_start_x = int(start_x * scale_x) + draw_x
                                        display_start_y = int(start_y * scale_y) + draw_y
                                        display_end_x = int(end_x * scale_x) + draw_x
                                        display_end_y = int(end_y * scale_y) + draw_y
                                        painter.drawLine(display_start_x, display_start_y, display_end_x, display_end_y)
                            painter.setRenderHint(QPainter.Antialiasing, False)
                            
                            painter.end()
                        
                        final_pixmap = blank_pixmap
                    
                    self.image_label.setPixmap(self._apply_fixed_overlays_to_pixmap(final_pixmap))
                    
                    zoom = getattr(self.image_label, 'zoom_factor', 1.0)
                    self.status.showMessage(f"Zoom: {zoom:.1f}x (LUT cached)")
                    return
            
            # Last resort: use original image without LUT processing during zoom
            # This prevents freezing when no cache is available, but respect current LUT strength
            base_pixmap = None
            if self.current_image in self.pixmap_cache:
                base_pixmap = self.pixmap_cache[self.current_image]
            else:
                base_pixmap, error = safe_load_pixmap(self.current_image)
                if error or (not base_pixmap) or base_pixmap.isNull():
                    return
            # Apply fast enhancements first (grayscale/contrast/gamma)
            preview_pixmap = self.apply_fast_enhancements(base_pixmap.copy()) if (self.grayscale_value!=0 or self.contrast_value!=50 or self.gamma_value!=0) else base_pixmap
            # Apply lightweight LUT preview at current strength (no caching write to avoid thrash)
            if self.lut_enabled and self.current_lut and self.lut_strength>0:
                try:
                    preview_pixmap = self.apply_lut_to_image(preview_pixmap, self.current_lut, self.lut_strength)
                except Exception as _e:
                    pass
            # Apply transforms
            if self.rotation_angle != 0 or self.flipped_h or self.flipped_v:
                preview_pixmap = self._apply_cached_transforms(preview_pixmap)
            final_pixmap = self._scale_pixmap(preview_pixmap, self.current_image)
            
            # Draw lines directly on the scaled pixmap if any exist
            if self.lines_visible and (self.drawn_lines or self.drawn_horizontal_lines or self.drawn_free_lines or self.drawn_free_strokes):
                painter = QPainter(final_pixmap)
                painter.setRenderHint(QPainter.Antialiasing, False)
                # Apply zoom scaling to line thickness for consistent visual appearance
                zoom_factor = getattr(self.image_label, 'zoom_factor', 1.0)
                zoom_scaled_thickness = max(1, self.line_thickness)
                painter.setPen(QPen(self.line_color, zoom_scaled_thickness, Qt.SolidLine))
                
                # Get transformation parameters
                original_size = preview_pixmap.size()
                label_size = self.image_label.size()
                zoom_factor = self.image_label.zoom_factor
                
                # Calculate scaling factors
                if self.rotation_angle == 90 or self.rotation_angle == 270:
                    display_ref = QSize(original_size.height(), original_size.width())
                else:
                    display_ref = original_size
                    
                base_scaled = display_ref.scaled(label_size, Qt.KeepAspectRatio)
                zoomed_width = int(base_scaled.width() * zoom_factor)
                zoomed_height = int(base_scaled.height() * zoom_factor)
                
                draw_x = (label_size.width() - zoomed_width) // 2 + int(self.image_label.pan_offset_x)
                draw_y = (label_size.height() - zoomed_height) // 2 + int(self.image_label.pan_offset_y)
                
                scale_x = zoomed_width / original_size.width()
                scale_y = zoomed_height / original_size.height()
                
                # Draw vertical lines
                for x in self.drawn_lines:
                    display_x = int(x * scale_x) + draw_x
                    if 0 <= display_x < final_pixmap.width():
                        painter.drawLine(display_x, 0, display_x, final_pixmap.height())
                
                # Draw horizontal lines
                for y in self.drawn_horizontal_lines:
                    display_y = int(y * scale_y) + draw_y
                    if 0 <= display_y < final_pixmap.height():
                        painter.drawLine(0, display_y, final_pixmap.width(), display_y)
                
                # Draw free lines
                for line in self.drawn_free_lines:
                    start_x, start_y = line['start']
                    end_x, end_y = line['end']
                    display_start_x = int(start_x * scale_x) + draw_x
                    display_start_y = int(start_y * scale_y) + draw_y
                    display_end_x = int(end_x * scale_x) + draw_x
                    display_end_y = int(end_y * scale_y) + draw_y
                    painter.drawLine(display_start_x, display_start_y, display_end_x, display_end_y)
                
                # Draw free strokes (pressure + zoom aware)
                painter.setRenderHint(QPainter.Antialiasing, True)
                if self.drawn_free_strokes:
                    for stroke in self.drawn_free_strokes:
                        if len(stroke) < 2:
                            continue
                        for i in range(len(stroke) - 1):
                            if len(stroke[i]) == 3 and self.pen_pressure_enabled:
                                start_x, start_y, p1 = stroke[i]
                            else:
                                start_x, start_y = stroke[i][:2]
                                p1 = 1.0
                            if len(stroke[i + 1]) == 3 and self.pen_pressure_enabled:
                                end_x, end_y, p2 = stroke[i + 1]
                            else:
                                end_x, end_y = stroke[i + 1][:2]
                                p2 = 1.0
                            avg_pressure = (p1 + p2)/2.0 if self.pen_pressure_enabled else 1.0
                            seg_thick = self._pressure_to_thickness(avg_pressure)
                            painter.setPen(QPen(self.line_color, seg_thick, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
                            display_start_x = int(start_x * scale_x) + draw_x
                            display_start_y = int(start_y * scale_y) + draw_y
                            display_end_x = int(end_x * scale_x) + draw_x
                            display_end_y = int(end_y * scale_y) + draw_y
                            painter.drawLine(display_start_x, display_start_y, display_end_x, display_end_y)
                painter.setRenderHint(QPainter.Antialiasing, False)
                
                painter.end()
            
            # Handle image visibility toggle
            if not self.image_visible:
                # Create a blank pixmap with the same size but keep lines visible
                blank_pixmap = QPixmap(final_pixmap.size())
                blank_pixmap.fill(Qt.black)  # Fill with black background
                
                # If there are lines, copy them from final_pixmap to blank_pixmap
                if self.lines_visible and (self.drawn_lines or self.drawn_horizontal_lines or self.drawn_free_lines or self.drawn_free_strokes):
                    painter = QPainter(blank_pixmap)
                    painter.setRenderHint(QPainter.Antialiasing, False)
                    # Apply zoom scaling to line thickness for consistent visual appearance
                    zoom_factor = getattr(self.image_label, 'zoom_factor', 1.0)
                    zoom_scaled_thickness = max(1, self.line_thickness)
                    painter.setPen(QPen(self.line_color, zoom_scaled_thickness, Qt.SolidLine))
                    
                    # Use the same coordinate transformations as above
                    original_size = preview_pixmap.size()
                    label_size = self.image_label.size()
                    zoom_factor = self.image_label.zoom_factor
                    
                    if self.rotation_angle == 90 or self.rotation_angle == 270:
                        display_ref = QSize(original_size.height(), original_size.width())
                    else:
                        display_ref = original_size
                        
                    base_scaled = display_ref.scaled(label_size, Qt.KeepAspectRatio)
                    zoomed_width = int(base_scaled.width() * zoom_factor)
                    zoomed_height = int(base_scaled.height() * zoom_factor)
                    
                    draw_x = (label_size.width() - zoomed_width) // 2 + int(self.image_label.pan_offset_x)
                    draw_y = (label_size.height() - zoomed_height) // 2 + int(self.image_label.pan_offset_y)
                    
                    scale_x = zoomed_width / original_size.width()
                    scale_y = zoomed_height / original_size.height()
                    
                    # Draw vertical lines
                    for x in self.drawn_lines:
                        display_x = int(x * scale_x) + draw_x
                        if 0 <= display_x < blank_pixmap.width():
                            painter.drawLine(display_x, 0, display_x, blank_pixmap.height())
                    
                    # Draw horizontal lines
                    for y in self.drawn_horizontal_lines:
                        display_y = int(y * scale_y) + draw_y
                        if 0 <= display_y < blank_pixmap.height():
                            painter.drawLine(0, display_y, blank_pixmap.width(), display_y)
                    
                    # Draw free lines
                    for line in self.drawn_free_lines:
                        start_x, start_y = line['start']
                        end_x, end_y = line['end']
                        display_start_x = int(start_x * scale_x) + draw_x
                        display_start_y = int(start_y * scale_y) + draw_y
                        display_end_x = int(end_x * scale_x) + draw_x
                        display_end_y = int(end_y * scale_y) + draw_y
                        painter.drawLine(display_start_x, display_start_y, display_end_x, display_end_y)
                    
                    # Draw free strokes (pressure + zoom aware)
                    painter.setRenderHint(QPainter.Antialiasing, True)
                    if self.drawn_free_strokes:
                        for stroke in self.drawn_free_strokes:
                            if len(stroke) < 2:
                                continue
                            for i in range(len(stroke) - 1):
                                if len(stroke[i]) == 3 and self.pen_pressure_enabled:
                                    start_x, start_y, p1 = stroke[i]
                                else:
                                    start_x, start_y = stroke[i][:2]
                                    p1 = 1.0
                                if len(stroke[i + 1]) == 3 and self.pen_pressure_enabled:
                                    end_x, end_y, p2 = stroke[i + 1]
                                else:
                                    end_x, end_y = stroke[i + 1][:2]
                                    p2 = 1.0
                                avg_pressure = (p1 + p2)/2.0 if self.pen_pressure_enabled else 1.0
                                seg_thick = self._pressure_to_thickness(avg_pressure)
                                painter.setPen(QPen(self.line_color, seg_thick, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
                                display_start_x = int(start_x * scale_x) + draw_x
                                display_start_y = int(start_y * scale_y) + draw_y
                                display_end_x = int(end_x * scale_x) + draw_x
                                display_end_y = int(end_y * scale_y) + draw_y
                                painter.drawLine(display_start_x, display_start_y, display_end_x, display_end_y)
                    painter.setRenderHint(QPainter.Antialiasing, False)
                    
                    painter.end()
                
                final_pixmap = blank_pixmap
            
            self.image_label.setPixmap(self._apply_fixed_overlays_to_pixmap(final_pixmap))
            zoom = getattr(self.image_label, 'zoom_factor', 1.0)
            # Show more specific status based on whether LUT was applied
            if self.lut_enabled and self.current_lut and self.lut_strength > 0:
                lut_name = self.current_lut_name if self.current_lut_name != "None" else "LUT"
                self.status.showMessage(f"Zoom: {zoom:.1f}x (live {lut_name} preview)")
            else:
                self.status.showMessage(f"Zoom: {zoom:.1f}x (no LUT)")
            
        except Exception as e:
            print(f"Error in smart zoom display: {e}")
            # Emergency fallback - show original image
            try:
                if self.current_image in self.pixmap_cache:
                    original_pixmap = self.pixmap_cache[self.current_image]
                else:
                    original_pixmap, error = safe_load_pixmap(self.current_image)
                    if error or original_pixmap.isNull():
                        return
                final_pixmap = self._scale_pixmap(original_pixmap, self.current_image)
                
                # Draw lines on emergency fallback if any exist
                if self.lines_visible and (self.drawn_lines or self.drawn_horizontal_lines or self.drawn_free_lines or self.drawn_free_strokes):
                    painter = QPainter(final_pixmap)
                    painter.setRenderHint(QPainter.Antialiasing, False)
                    # Apply zoom scaling to line thickness for consistent visual appearance
                    zoom_factor = getattr(self.image_label, 'zoom_factor', 1.0)
                    zoom_scaled_thickness = max(1, self.line_thickness)
                    painter.setPen(QPen(self.line_color, zoom_scaled_thickness, Qt.SolidLine))
                    
                    # Simple scaling for emergency fallback
                    original_size = original_pixmap.size()
                    scale_x = final_pixmap.width() / original_size.width()
                    scale_y = final_pixmap.height() / original_size.height()
                    
                    # Draw vertical lines
                    for x in self.drawn_lines:
                        display_x = int(x * scale_x)
                        if 0 <= display_x < final_pixmap.width():
                            painter.drawLine(display_x, 0, display_x, final_pixmap.height())
                    
                    # Draw horizontal lines
                    for y in self.drawn_horizontal_lines:
                        display_y = int(y * scale_y)
                        if 0 <= display_y < final_pixmap.height():
                            painter.drawLine(0, display_y, final_pixmap.width(), display_y)
                    
                    # Draw free lines
                    for line in self.drawn_free_lines:
                        start_x, start_y = line['start']
                        end_x, end_y = line['end']
                        display_start_x = int(start_x * scale_x)
                        display_start_y = int(start_y * scale_y)
                        display_end_x = int(end_x * scale_x)
                        display_end_y = int(end_y * scale_y)
                        painter.drawLine(display_start_x, display_start_y, display_end_x, display_end_y)
                    
                    # Draw free strokes (pressure + zoom aware)
                    painter.setRenderHint(QPainter.Antialiasing, True)
                    if self.drawn_free_strokes:
                        for stroke in self.drawn_free_strokes:
                            if len(stroke) < 2:
                                continue
                            for i in range(len(stroke) - 1):
                                if len(stroke[i]) == 3 and self.pen_pressure_enabled:
                                    start_x, start_y, p1 = stroke[i]
                                else:
                                    start_x, start_y = stroke[i][:2]
                                    p1 = 1.0
                                if len(stroke[i + 1]) == 3 and self.pen_pressure_enabled:
                                    end_x, end_y, p2 = stroke[i + 1]
                                else:
                                    end_x, end_y = stroke[i + 1][:2]
                                    p2 = 1.0
                                avg_pressure = (p1 + p2)/2.0 if self.pen_pressure_enabled else 1.0
                                seg_thick = self._pressure_to_thickness(avg_pressure)
                                painter.setPen(QPen(self.line_color, seg_thick, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
                                display_start_x = int(start_x * scale_x)
                                display_start_y = int(start_y * scale_y)
                                display_end_x = int(end_x * scale_x)
                                display_end_y = int(end_y * scale_y)
                                painter.drawLine(display_start_x, display_start_y, display_end_x, display_end_y)
                    painter.setRenderHint(QPainter.Antialiasing, False)
                    
                    painter.end()
                
                # Handle image visibility toggle
                if not self.image_visible:
                    # Create a blank pixmap with the same size but keep lines visible
                    blank_pixmap = QPixmap(final_pixmap.size())
                    blank_pixmap.fill(Qt.black)  # Fill with black background
                    
                    # If there are lines, copy them from final_pixmap to blank_pixmap
                    if self.lines_visible and (self.drawn_lines or self.drawn_horizontal_lines or self.drawn_free_lines or self.drawn_free_strokes):
                        painter = QPainter(blank_pixmap)
                        painter.setRenderHint(QPainter.Antialiasing, False)
                        # Apply zoom scaling to line thickness for consistent visual appearance
                        zoom_factor = getattr(self.image_label, 'zoom_factor', 1.0)
                        zoom_scaled_thickness = max(1, self.line_thickness)
                        painter.setPen(QPen(self.line_color, zoom_scaled_thickness, Qt.SolidLine))
                        
                        # Use the same coordinate transformations as above
                        original_size = original_pixmap.size()
                        scale_x = final_pixmap.width() / original_size.width()
                        scale_y = final_pixmap.height() / original_size.height()
                        
                        # Draw vertical lines
                        for x in self.drawn_lines:
                            display_x = int(x * scale_x)
                            if 0 <= display_x < blank_pixmap.width():
                                painter.drawLine(display_x, 0, display_x, blank_pixmap.height())
                        
                        # Draw horizontal lines
                        for y in self.drawn_horizontal_lines:
                            display_y = int(y * scale_y)
                            if 0 <= display_y < blank_pixmap.height():
                                painter.drawLine(0, display_y, blank_pixmap.width(), display_y)
                        
                        # Draw free lines
                        for line in self.drawn_free_lines:
                            start_x, start_y = line['start']
                            end_x, end_y = line['end']
                            display_start_x = int(start_x * scale_x)
                            display_start_y = int(start_y * scale_y)
                            display_end_x = int(end_x * scale_x)
                            display_end_y = int(end_y * scale_y)
                            painter.drawLine(display_start_x, display_start_y, display_end_x, display_end_y)
                        
                        # Draw free strokes
                        painter.setRenderHint(QPainter.Antialiasing, True)
                        for stroke in self.drawn_free_strokes:
                            if len(stroke) < 2:
                                continue
                            for i in range(len(stroke) - 1):
                                # 🎨 PEN PRESSURE: Handle both old 2-tuple and new 3-tuple formats
                                if len(stroke[i]) == 3:
                                    start_x, start_y, _ = stroke[i]
                                else:
                                    start_x, start_y = stroke[i]
                                if len(stroke[i + 1]) == 3:
                                    end_x, end_y, _ = stroke[i + 1]
                                else:
                                    end_x, end_y = stroke[i + 1]
                                display_start_x = int(start_x * scale_x)
                                display_start_y = int(start_y * scale_y)
                                display_end_x = int(end_x * scale_x)
                                display_end_y = int(end_y * scale_y)
                                painter.drawLine(display_start_x, display_start_y, display_end_x, display_end_y)
                        painter.setRenderHint(QPainter.Antialiasing, False)
                        
                        painter.end()
                    
                    final_pixmap = blank_pixmap
                
                self.image_label.setPixmap(self._apply_fixed_overlays_to_pixmap(final_pixmap))
            except Exception as fallback_e:
                print(f"Fallback also failed: {fallback_e}")
                pass
    
    def _get_lut_cache_key(self):
        """Generate cache key for LUT processing - includes lines and enhancements"""
        if not self.lut_enabled or not self.current_lut or not self.current_image:
            return None
            
        # Create comprehensive cache key including all visual modifications
        lut_file_path = getattr(self.current_lut, 'file_path', 'unknown')
        
        # Include line information in cache key
        lines_key = ""
        if self.lines_visible and (self.drawn_lines or self.drawn_horizontal_lines or self.drawn_free_lines or self.drawn_free_strokes):
            # Create a compact representation of all lines
            vlines = f"v{len(self.drawn_lines)}" if self.drawn_lines else ""
            hlines = f"h{len(self.drawn_horizontal_lines)}" if self.drawn_horizontal_lines else ""
            flines = f"f{len(self.drawn_free_lines)}" if self.drawn_free_lines else ""
            strokes = f"s{len(self.drawn_free_strokes)}" if self.drawn_free_strokes else ""
            color_key = self.line_color.name()
            thickness_key = str(self.line_thickness)
            lines_key = f"_lines_{vlines}{hlines}{flines}{strokes}_{color_key}_{thickness_key}"
        
        # Include enhancement settings in cache key
        enhancements_key = ""
        if (self.grayscale_value != 0 or self.contrast_value != 50 or self.gamma_value != 0):
            enhancements_key = f"_enh_{self.grayscale_value}_{self.contrast_value}_{self.gamma_value}"
        
        return f"{self.current_image}_{lut_file_path}_{self.lut_strength}{lines_key}{enhancements_key}"
    
    def _apply_cached_transforms(self, pixmap):
        """Apply rotation and flips to cached pixmap"""
        if not pixmap or pixmap.isNull():
            return pixmap
            
        # Apply rotation if needed
        if self.rotation_angle != 0:
            transform = QTransform()
            transform.rotate(self.rotation_angle)
            pixmap = pixmap.transformed(transform, Qt.SmoothTransformation)
        
        # Apply flips if needed
        if self.flipped_h:
            pixmap = pixmap.transformed(QTransform().scale(-1, 1), Qt.SmoothTransformation)
        if self.flipped_v:
            pixmap = pixmap.transformed(QTransform().scale(1, -1), Qt.SmoothTransformation)
            
        return pixmap

    def flip_horizontal(self):
        """Flip the current image horizontally"""
        if self.current_image:
            self.flipped_h = not self.flipped_h
            # Update button appearance to show state
            if hasattr(self, 'flip_h_btn'):
                self.flip_h_btn.setChecked(self.flipped_h)
            # Clear caches since flip changes the image
            self.enhancement_cache.clear()
            self.scaled_cache.clear()
            self.display_image(self.current_image)
            self.status.showMessage(f"Horizontal flip: {'ON' if self.flipped_h else 'OFF'}")
            # Disable/enable line tools when flip/rotation state changes
            try:
                self._sync_line_tools_state()
            except Exception:
                pass

    def flip_vertical(self):
        """Flip the current image vertically"""
        if self.current_image:
            self.flipped_v = not self.flipped_v
            # Update button appearance to show state
            if hasattr(self, 'flip_v_btn'):
                self.flip_v_btn.setChecked(self.flipped_v)
            # Clear caches since flip changes the image
            self.enhancement_cache.clear()
            self.scaled_cache.clear()
            self.display_image(self.current_image)
            self.status.showMessage(f"Vertical flip: {'ON' if self.flipped_v else 'OFF'}")
            # Disable/enable line tools when flip/rotation state changes
            try:
                self._sync_line_tools_state()
            except Exception:
                pass

    def rotate_image_90(self):
        """Rotate the current image by 90 degrees"""
        if self.current_image:
            self.rotation_angle = (self.rotation_angle + 90) % 360
            # Clear caches since rotation changes the image
            self.enhancement_cache.clear()
            self.scaled_cache.clear()
            # Redisplay the image with new rotation
            self.display_image(self.current_image)
            self.status.showMessage(f"Rotated to {self.rotation_angle}°")
            # Disable/enable line tools when flip/rotation state changes
            try:
                self._sync_line_tools_state()
            except Exception:
                pass

    def _sync_line_tools_state(self):
        """Enable or disable line tool buttons depending on rotation/flip state.
        When image is rotated or flipped we disable line drawing tools because
        coordinate math for drawing over rotated/flipped images is disabled.
        """
        # If any transform is active, disable line tools
        transforms_active = (getattr(self, 'rotation_angle', 0) != 0) or getattr(self, 'flipped_h', False) or getattr(self, 'flipped_v', False)

        # Buttons may not exist yet during startup; guard with hasattr
        for btn_name in ('line_tool_btn', 'hline_tool_btn', 'free_line_tool_btn'):
            if hasattr(self, btn_name):
                btn = getattr(self, btn_name)
                try:
                    btn.setEnabled(not transforms_active)
                    # When disabling, also uncheck and cancel drawing modes
                    if transforms_active:
                        if hasattr(btn, 'setChecked'):
                            try:
                                btn.setChecked(False)
                            except Exception:
                                pass
                except Exception:
                    # ignore failures to set UI state
                    pass

        # Clear any active drawing modes when transforms are active
        if transforms_active:
            self.line_drawing_mode = False
            self.horizontal_line_drawing_mode = False
            self.free_line_drawing_mode = False
            # Ensure the toolbar buttons reflect that
            if hasattr(self, 'line_tool_btn'):
                try: self.line_tool_btn.setChecked(False)
                except Exception: pass
            if hasattr(self, 'hline_tool_btn'):
                try: self.hline_tool_btn.setChecked(False)
                except Exception: pass
            if hasattr(self, 'free_line_tool_btn'):
                try: self.free_line_tool_btn.setChecked(False)
                except Exception: pass


    def copy_to_clipboard(self):
        """Copy the current displayed image (with all enhancements and lines) to clipboard"""
        if not self.current_image or not self.image_label.pixmap():
            self.status.showMessage("No image to copy")
            return
        
        try:
            # Get the currently displayed pixmap (this includes all enhancements and lines)
            current_pixmap = self.image_label.pixmap()
            
            if current_pixmap and not current_pixmap.isNull():
                # Copy to clipboard
                clipboard = QApplication.clipboard()
                clipboard.setPixmap(current_pixmap)
                
                # Show confirmation message
                filename = os.path.basename(self.current_image)
                self.status.showMessage(f"Copied {filename} to clipboard (with enhancements and lines)")
            else:
                self.status.showMessage("No image available to copy")
                
        except Exception as e:
            self.status.showMessage(f"Error copying to clipboard: {str(e)}")

    def keyPressEvent(self, event):
        """Handle key press events with robust fullscreen exit"""
        # Debug print to help troubleshoot
        print(f"Key pressed: {event.key()}, Fullscreen: {self.is_fullscreen}")
        
        if event.key() == Qt.Key_Left:
            self.show_previous_image()
        elif event.key() == Qt.Key_Right:
            self.show_next_image()
        elif event.key() == Qt.Key_F11:
            # Toggle fullscreen mode
            print("F11 pressed - toggling fullscreen")
            self.toggle_fullscreen()
        elif event.key() == Qt.Key_Escape:
            # Priority 1: Exit fullscreen if in fullscreen mode
            if self.is_fullscreen:
                print("Escape pressed - exiting fullscreen")
                if event.modifiers() & Qt.ControlModifier:
                    print("Ctrl+Esc pressed - force exiting fullscreen")
                    self.force_exit_fullscreen()
                else:
                    self.exit_fullscreen()
            # Priority 2: Show UI if in minimal mode (UI hidden)
            elif not self._ui_chrome_visible():
                print("Escape pressed - restoring UI from minimal mode")
                self.toggle_toolbar_visibility(True)  # Show UI
            else:
                super().keyPressEvent(event)
        elif event.modifiers() & Qt.ControlModifier:
            if event.key() in (Qt.Key_Plus, Qt.Key_Equal):
                self.zoom_in()
            elif event.key() == Qt.Key_Minus:
                self.zoom_out()
            elif event.key() == Qt.Key_0:
                self.reset_zoom()
            elif event.key() == Qt.Key_H:
                self.flip_horizontal()
            elif event.key() == Qt.Key_V:
                self.flip_vertical()
            elif event.key() == Qt.Key_U:
                # Toggle UI visibility (minimal mode)
                is_ui_visible = self._ui_chrome_visible()
                self.toggle_toolbar_visibility(not is_ui_visible)
            elif event.key() == Qt.Key_R:
                # Reset all enhancements
                self.reset_enhancements()
            elif event.key() == Qt.Key_G:
                # Go to PDF/EPUB page or image number
                self._go_to_image_or_page()
            else:
                super().keyPressEvent(event)
        else:
            super().keyPressEvent(event)

    def mouseDoubleClickEvent(self, event):
        """Handle double-click events - exit fullscreen if in fullscreen mode"""
        if self.is_fullscreen:
            print("Double-click detected - exiting fullscreen")
            self.exit_fullscreen()
        else:
            super().mouseDoubleClickEvent(event)

    def closeEvent(self, event):
        """Handle window close event"""
        self._close_pdf()
        self._close_epub()
        self._close_cbr()
        super().closeEvent(event)

    def showEvent(self, event):
        """Override showEvent to ensure dark title bar is applied"""
        super().showEvent(event)
        
        # Apply dark title bar when window is shown for the first time
        if not hasattr(self, '_dark_title_applied'):
            self._dark_title_applied = True
            if is_windows_dark_mode():
                print("DEBUG: Ensuring dark title bar in showEvent")
                # Single clean attempt to apply dark mode
                success = enable_windows_dark_title_bar(self)
                if not success:
                    # If first attempt failed, try once more after a brief delay
                    QTimer.singleShot(100, lambda: enable_windows_dark_title_bar(self))

    def show_previous_image(self):
        # PDF mode: go to previous page (or previous spread in 2/3-page mode).
        # In a mixed playlist, "previous" past page 1 walks to the prior item.
        if getattr(self, '_pdf_doc', None):
            if self._pdf_page > 0:
                self.clear_lines()
                step = self._spread_count()
                self._show_pdf_page(self._pdf_page - step)
            elif self._in_playlist():
                self._advance_in_playlist(-1)
            return

        # EPUB mode: go to previous page
        if getattr(self, '_epub_doc', None):
            if self._epub_page > 0:
                self.clear_lines()
                self._show_epub_page(self._epub_page - 1)
            elif self._in_playlist():
                self._advance_in_playlist(-1)
            return

        # CBR mode: go to previous page (or previous spread in 2/3-page mode)
        if getattr(self, '_cbr_doc', None):
            if self._cbr_page > 0:
                self.clear_lines()
                step = self._spread_count()
                self._show_cbr_page(self._cbr_page - step)
            elif self._in_playlist():
                self._advance_in_playlist(-1)
            return

        if self.history_index > 0:
            self.history_index -= 1
            img_path = self.history[self.history_index]
            # Re-load via the playlist dispatcher so PDF/CBR/EPUB history
            # items open in their correct viewer.
            self._load_playlist_item(img_path, update_history=False)

    def show_next_image(self):
        """Shows the next image, respecting the random or sequential mode."""
        # PDF mode: go to next page (or next spread in 2/3-page mode). In a
        # mixed playlist, advancing past the last page walks to the next item.
        if getattr(self, '_pdf_doc', None):
            step = self._spread_count()
            if self._pdf_page + step < self._pdf_doc.page_count:
                self.clear_lines()
                self._show_pdf_page(self._pdf_page + step)
            elif self._in_playlist():
                # Timer-driven auto-advance: jump to next item; respect
                # random mode the same way as for images.
                if self._auto_advance_active and self.random_mode:
                    self.show_random_image()
                else:
                    self._advance_in_playlist(+1)
            return

        # EPUB mode: go to next page
        if getattr(self, '_epub_doc', None):
            if self._epub_page < self._epub_doc.page_count - 1:
                self.clear_lines()
                self._show_epub_page(self._epub_page + 1)
            elif self._in_playlist():
                if self._auto_advance_active and self.random_mode:
                    self.show_random_image()
                else:
                    self._advance_in_playlist(+1)
            return

        # CBR mode: go to next page (or next spread in 2/3-page mode)
        if getattr(self, '_cbr_doc', None):
            step = self._spread_count()
            if self._cbr_page + step < self._cbr_doc.page_count:
                self.clear_lines()
                self._show_cbr_page(self._cbr_page + step)
            elif self._in_playlist():
                if self._auto_advance_active and self.random_mode:
                    self.show_random_image()
                else:
                    self._advance_in_playlist(+1)
            return

        if not self.images:
            return

        if self.random_mode:
            # In random mode, if at end of history show new random image
            if self.history_index < len(self.history) - 1:
                self.history_index += 1
                img_path = self.history[self.history_index]
                # Re-load via the playlist dispatcher so PDF/CBR/EPUB items
                # in history open correctly instead of failing as images.
                self._load_playlist_item(img_path, update_history=False)
            else:
                self.show_random_image()
        else:
            # Sequential mode - navigate alphabetically through the playlist
            if self.current_image and self.current_image in self.images:
                try:
                    current_list_index = self.images.index(self.current_image)
                    next_index = (current_list_index + 1) % len(self.images)
                except ValueError:
                    next_index = 0
            else:
                next_index = 0

            img_path = self.images[next_index]
            self._load_playlist_item(img_path)

    def choose_folder(self):
        # Set default folder if it exists
        default_folder = r"Y:\_REFERENCES_MAIN"
        start_dir = default_folder if os.path.exists(default_folder) else ""
        
        folder = QFileDialog.getExistingDirectory(self, "Select Image Folder", start_dir)
        if folder:
            # Mixed playlist: images, videos, GIFs, and PDF/EPUB/CBR all walk
            # through the same Next/Previous flow.
            self._load_playlist([folder], source="from folder dialog")

    def delete_current_file(self):
        """Move the currently displayed file to the Recycle Bin."""
        # Guard: only available for regular media files, not document page viewers
        if not self.current_image:
            return
        if getattr(self, '_pdf_doc', None) or getattr(self, '_epub_doc', None) or getattr(self, '_cbr_doc', None):
            return

        file_path = self.current_image
        file_name = os.path.basename(file_path)

        # Determine next item to show before removing from playlist
        images = self.images
        try:
            idx = images.index(file_path)
        except ValueError:
            idx = -1

        # Remove from playlist list
        if idx >= 0:
            images.pop(idx)
        try:
            all_idx = self._all_items.index(file_path)
        except ValueError:
            all_idx = -1
        if all_idx >= 0:
            self._all_items.pop(all_idx)

        # Clean from history
        self.history = [p for p in self.history if p != file_path]
        self.history_index = max(0, min(self.history_index, len(self.history) - 1))

        # Attempt trash
        from PySide6.QtCore import QFile
        ok = QFile.moveToTrash(file_path)

        if not ok:
            # Restore to playlist and report failure
            if idx >= 0:
                images.insert(idx, file_path)
            if all_idx >= 0:
                self._all_items.insert(all_idx, file_path)
            self.statusBar().showMessage(f"Failed to move to Recycle Bin: {file_name}", 4000)
            return

        self._refresh_type_filter_panel()

        self.statusBar().showMessage(f"Moved to Recycle Bin: {file_name}", 3000)
        self.current_image = None

        if not images:
            # Nothing left — clear display
            self.image_label.clear()
            self._stop_current_animation()
            return

        # Advance: clamp index, then load
        next_idx = min(idx, len(images) - 1)
        self._load_playlist_item(images[next_idx])

    def mousePressEvent(self, event):
        """Handle mouse press for window dragging and resizing in frameless mode."""
        # Only handle dragging/resizing when UI is hidden (frameless mode)
        if self.main_toolbar.isVisible() or not (self.windowFlags() & Qt.FramelessWindowHint):
            super().mousePressEvent(event)
            return

        if event.button() == Qt.LeftButton or event.button() == Qt.RightButton:
            self.drag_start_position = event.globalPosition().toPoint()
            
            # Check if we're near an edge for resizing (only for left-click)
            pos = event.position().toPoint()
            window_rect = self.rect()
            margin = self.resize_margin
            
            # Determine resize edge with proper corner detection
            resize_edge = ""
            at_left = pos.x() <= margin
            at_right = pos.x() >= window_rect.width() - margin
            at_top = pos.y() <= margin
            at_bottom = pos.y() >= window_rect.height() - margin
            
            # Handle corners first (combination of edges)
            if at_top and at_left:
                resize_edge = "topleft"
            elif at_top and at_right:
                resize_edge = "topright"
            elif at_bottom and at_left:
                resize_edge = "bottomleft"
            elif at_bottom and at_right:
                resize_edge = "bottomright"
            # Then handle single edges
            elif at_top:
                resize_edge = "top"
            elif at_bottom:
                resize_edge = "bottom"
            elif at_left:
                resize_edge = "left"
            elif at_right:
                resize_edge = "right"
            
            # Left-click: resize if on edge, otherwise drag
            # Right-click: always drag (even on edges)
            if event.button() == Qt.LeftButton and resize_edge:
                self.resizing = True
                self.resize_edge = resize_edge
                self.setCursor(self._get_resize_cursor(resize_edge))
            else:
                self.dragging = True
                self.setCursor(Qt.ClosedHandCursor)
        
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        """Handle mouse move for window dragging and resizing in frameless mode."""
        # Only handle dragging/resizing when UI is hidden (frameless mode)
        if self.main_toolbar.isVisible() or not (self.windowFlags() & Qt.FramelessWindowHint):
            super().mouseMoveEvent(event)
            return

        if event.buttons() & (Qt.LeftButton | Qt.RightButton):
            if self.dragging and self.drag_start_position:
                # Move window (works with both left and right mouse buttons)
                diff = event.globalPosition().toPoint() - self.drag_start_position
                self.move(self.pos() + diff)
                self.drag_start_position = event.globalPosition().toPoint()
            
            elif self.resizing and self.resize_edge and self.drag_start_position:
                # Resize window (only works with left mouse button)
                current_pos = event.globalPosition().toPoint()
                diff = current_pos - self.drag_start_position
                
                geometry = self.geometry()
                min_size = self.minimumSize()
                
                if "left" in self.resize_edge:
                    new_width = geometry.width() - diff.x()
                    if new_width >= min_size.width():
                        geometry.setLeft(geometry.left() + diff.x())
                        geometry.setWidth(new_width)
                
                if "right" in self.resize_edge:
                    new_width = geometry.width() + diff.x()
                    if new_width >= min_size.width():
                        geometry.setWidth(new_width)
                
                if "top" in self.resize_edge:
                    new_height = geometry.height() - diff.y()
                    if new_height >= min_size.height():
                        geometry.setTop(geometry.top() + diff.y())
                        geometry.setHeight(new_height)
                
                if "bottom" in self.resize_edge:
                    new_height = geometry.height() + diff.y()
                    if new_height >= min_size.height():
                        geometry.setHeight(new_height)
                
                self.setGeometry(geometry)
                self.drag_start_position = current_pos
        else:
            # Update cursor when hovering near edges
            pos = event.position().toPoint()
            window_rect = self.rect()
            margin = self.resize_margin
            
            resize_edge = ""
            at_left = pos.x() <= margin
            at_right = pos.x() >= window_rect.width() - margin
            at_top = pos.y() <= margin
            at_bottom = pos.y() >= window_rect.height() - margin
            
            # Handle corners first (combination of edges)
            if at_top and at_left:
                resize_edge = "topleft"
            elif at_top and at_right:
                resize_edge = "topright"
            elif at_bottom and at_left:
                resize_edge = "bottomleft"
            elif at_bottom and at_right:
                resize_edge = "bottomright"
            # Then handle single edges
            elif at_top:
                resize_edge = "top"
            elif at_bottom:
                resize_edge = "bottom"
            elif at_left:
                resize_edge = "left"
            elif at_right:
                resize_edge = "right"
            
            if resize_edge:
                self.setCursor(self._get_resize_cursor(resize_edge))
            else:
                self.setCursor(Qt.ArrowCursor)
        
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        """Handle mouse release to end dragging/resizing."""
        if event.button() == Qt.LeftButton or event.button() == Qt.RightButton:
            self.dragging = False
            self.resizing = False
            self.resize_edge = None
            self.drag_start_position = None
            self.setCursor(Qt.ArrowCursor)
        
        super().mouseReleaseEvent(event)

    def _get_resize_cursor(self, edge):
        """Get appropriate cursor for resize edge - matches standard window resize cursors."""
        if edge == "top" or edge == "bottom":
            return Qt.SizeVerCursor  # Vertical resize (↕)
        elif edge == "left" or edge == "right":
            return Qt.SizeHorCursor  # Horizontal resize (↔)
        elif edge == "topleft" or edge == "bottomright":
            return Qt.SizeFDiagCursor  # Diagonal resize (\)
        elif edge == "topright" or edge == "bottomleft":
            return Qt.SizeBDiagCursor  # Diagonal resize (/)
        else:
            return Qt.ArrowCursor

    def dragEnterEvent(self, event):
        """Accept folder, document, image, and video drops (single or multiple)."""
        if event.mimeData().hasUrls():
            urls = event.mimeData().urls()
            for url in urls:
                if url.isLocalFile():
                    file_path = url.toLocalFile()
                    if os.path.isdir(file_path):
                        event.acceptProposedAction()
                        return
                    ext = os.path.splitext(file_path)[1].lower()
                    if ext in PLAYLIST_EXTENSIONS:
                        event.acceptProposedAction()
                        return
        event.ignore()

    def dragMoveEvent(self, event):
        """Handle drag move events."""
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        """Open dropped folders, documents, images, or videos.

        A single dropped file/folder uses the standalone open path. Multiple
        URLs, or any folder drop, build a mixed playlist that walks images,
        videos, GIFs, PDFs, EPUBs, and CBR/CBZ archives in sequence.
        """
        if not event.mimeData().hasUrls():
            event.ignore()
            return

        # If a background scan is already running, refuse new drops rather
        # than building a half-merged playlist on top of the in-flight one.
        if self._scan_in_progress:
            self.status.showMessage(
                "Already scanning a folder — please wait for the current "
                "scan to finish."
            )
            event.ignore()
            return

        local_paths = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
        local_paths = [p for p in local_paths if p]
        if not local_paths:
            event.ignore()
            return

        has_folder = any(os.path.isdir(p) for p in local_paths)
        if len(local_paths) == 1 and not has_folder:
            # Single file → existing standalone behavior.
            if self._open_local_path(local_paths[0], source="dropped"):
                event.acceptProposedAction()
                return
            event.ignore()
            return

        # Multiple files, or any folder → build a mixed playlist.
        if self._load_playlist(local_paths, source="dropped"):
            event.acceptProposedAction()
        else:
            event.ignore()

    def _open_local_path(self, file_path, source="opened"):
        """Open a local file or folder (PDF, EPUB, image, video, or directory).

        Returns True if the path was handled, False otherwise. Used by drop and
        clipboard paste handlers to share the same loading logic.
        """
        if not file_path:
            return False

        # --- PDF ---
        if is_pdf_file(file_path):
            print(f"PDF {source}: {file_path}")
            self._load_pdf(file_path)
            return True

        # --- EPUB ---
        if is_epub_file(file_path):
            print(f"EPUB {source}: {file_path}")
            self._load_epub(file_path)
            return True

        # --- CBR / CBZ (comic archive) ---
        if is_cbr_file(file_path):
            print(f"CBR {source}: {file_path}")
            self._load_cbr(file_path)
            return True

        # --- Image / Video file ---
        from random_image_viewer.constants import MEDIA_EXTENSIONS
        ext = os.path.splitext(file_path)[1].lower()
        if ext in MEDIA_EXTENSIONS and os.path.isfile(file_path):
            print(f"Media file {source}: {file_path}")
            self._close_pdf()
            self._close_epub()
            self._close_cbr()
            # Load the parent folder so navigation works
            parent_folder = os.path.dirname(file_path)
            self.folder = parent_folder
            # Opening a file explicitly is a request to see it, so make sure its
            # own type is visible rather than filtering the thing just opened.
            self._hidden_types.discard(ext)
            self._set_playlist_items(get_images_in_folder(parent_folder))
            self.history.clear()
            self.history_list.clear()
            self.history_list.repaint()
            self.current_image = None
            self.history_index = -1
            self.update_image_info()
            self._update_title()
            if file_path in self.images:
                self.current_index = self.images.index(file_path)
            self._display_image_with_lut_preview(file_path)
            self.add_to_history(file_path)
            self.current_image = file_path
            self.update_image_info(file_path)
            self.set_status_path(file_path)
            if self._auto_advance_active:
                self.timer_remaining = self.timer_spin.value()
                self._update_ring()
            self._reset_timer()
            self.status.showMessage(f"Loaded: {os.path.basename(file_path)} ({len(self.images)} files in folder)")
            return True

        # --- Folder ---
        if os.path.isdir(file_path):
            print(f"Folder {source}: {file_path}")
            # A single dropped folder is just a one-element playlist.
            return self._load_playlist([file_path], source=source)

        return False

    # ── File-type filter (File Types palette) ──────────────────────────

    @staticmethod
    def _item_type(path):
        """Lowercase extension used as the filter key (".mp4", ".gif", …)."""
        return os.path.splitext(path or "")[1].lower()

    def _playlist_type_counts(self):
        """Map every extension in the unfiltered playlist to its file count."""
        counts = {}
        for path in self._all_items:
            ext = self._item_type(path)
            if ext:
                counts[ext] = counts.get(ext, 0) + 1
        return counts

    def _rebuild_filtered_items(self):
        """Recompute self.images from self._all_items and the hidden-type set.

        Every navigation path (random, sequential, prev/next, go-to-number,
        delete) already reads self.images, so applying a filter is just a
        matter of installing a narrower list here.
        """
        if self._hidden_types:
            hidden = self._hidden_types
            self.images = [path for path in self._all_items
                           if self._item_type(path) not in hidden]
        else:
            self.images = list(self._all_items)

    def _set_playlist_items(self, items):
        """Install a freshly scanned playlist and apply the current filter.

        Callers load their first item from self.images afterwards, so this
        deliberately does not navigate on its own.
        """
        self._all_items = list(items)
        # Forget hidden types the new playlist has none of, so a folder can
        # never carry an invisible filter for files it does not contain.
        self._hidden_types &= set(self._playlist_type_counts())
        self._rebuild_filtered_items()
        self._refresh_type_filter_panel()

    def _apply_type_filter(self):
        """React to a filter change: narrow the list, then re-anchor the view."""
        previous = self.current_image
        self._rebuild_filtered_items()
        self._refresh_type_filter_panel()
        self._update_title()

        if not self._all_items:
            return
        if not self.images:
            # Everything is hidden. Leave the current item on screen rather
            # than blanking the window, and say why browsing stopped.
            self.status.showMessage(
                "All file types are hidden — check a type to browse again.")
            return
        if previous is not None and previous in self.images:
            self._show_filter_status()
            return

        # The item on screen no longer passes the filter, so move to one that
        # does, honouring the current random/sequential mode.
        if self.random_mode:
            self.show_random_image()
        else:
            self._load_playlist_item(self.images[0])
        self._show_filter_status()

    def _show_filter_status(self):
        """Report the active filter in the status bar."""
        total = len(self._all_items)
        shown = len(self.images)
        if shown == total:
            self.status.showMessage(f"Showing all {total:,} files.")
        else:
            kinds = ", ".join(sorted(
                ext.lstrip(".").upper() for ext in self._playlist_type_counts()
                if ext not in self._hidden_types))
            self.status.showMessage(
                f"Showing {shown:,} of {total:,} files ({kinds or 'none'}).")

    def _refresh_type_filter_panel(self):
        """Push the current type counts / checked state into the panel."""
        win = getattr(self, "_type_filter_window", None)
        if win is None:
            return
        win.set_types(self._playlist_type_counts(), self._hidden_types)
        win.set_summary(len(self.images), len(self._all_items))

    def _on_hidden_types_changed(self, hidden):
        """Slot: the panel's checkboxes changed."""
        self._hidden_types = {str(ext).lower() for ext in hidden}
        self._apply_type_filter()

    def _ensure_type_filter_window(self):
        """Lazy-create the File Types panel and wire its signals."""
        if self._type_filter_window is None:
            win = TypeFilterWindow(self)
            win.types_changed.connect(self._on_hidden_types_changed)
            win.closed.connect(self._on_type_filter_window_closed)
            self._type_filter_window = win
            self._refresh_type_filter_panel()
        return self._type_filter_window

    def _toggle_type_filter_window(self, checked):
        """Show/hide the File Types panel from the toolbar button."""
        if checked:
            self._show_type_filter_window()
        else:
            self._hide_type_filter_window()

    def _show_type_filter_window(self):
        """Show the panel near its toolbar button on first open."""
        win = self._ensure_type_filter_window()
        self._refresh_type_filter_panel()
        if win.isVisible():
            win.raise_()
            return
        target = self._type_filter_window_pos
        if target is None:
            btn = getattr(self, "type_filter_btn", None)
            if btn is not None:
                try:
                    from PySide6.QtCore import QPoint
                    target = btn.mapToGlobal(btn.rect().bottomLeft()) + QPoint(0, 4)
                except Exception:
                    target = None
        if target is not None:
            try:
                from PySide6.QtGui import QGuiApplication
                scr = QGuiApplication.screenAt(target) or QGuiApplication.primaryScreen()
                geom = scr.availableGeometry()
                w = win.sizeHint().width() or win.width() or 200
                h = win.sizeHint().height() or win.height() or 220
                x = max(geom.left(), min(target.x(), geom.right() - w))
                y = max(geom.top(), min(target.y(), geom.bottom() - h))
                win.move(x, y)
            except Exception:
                win.move(target)
        win.show()
        win.raise_()

    def _hide_type_filter_window(self):
        """Hide the panel; remember its position."""
        win = getattr(self, "_type_filter_window", None)
        if win is not None and win.isVisible():
            try:
                self._type_filter_window_pos = win.pos()
            except Exception:
                pass
            win.hide()

    def _on_type_filter_window_closed(self):
        """Handle the panel's close button: remember pos, un-check the toolbar."""
        win = getattr(self, "_type_filter_window", None)
        if win is not None:
            try:
                self._type_filter_window_pos = win.pos()
            except Exception:
                pass
        btn = getattr(self, "type_filter_btn", None)
        if btn is not None:
            btn.blockSignals(True)
            btn.setChecked(False)
            btn.blockSignals(False)

    # ── Mixed playlist (folder / multi-drop with images + docs) ─────────

    def _is_document_path(self, path):
        """Return True if *path* is a PDF, EPUB, or CBR/CBZ document."""
        if not path:
            return False
        return is_pdf_file(path) or is_epub_file(path) or is_cbr_file(path)

    def _in_playlist(self):
        """True if there is a multi-item playlist anchored on the current item."""
        return (bool(self.images)
                and self.current_image is not None
                and self.current_image in self.images)

    def _load_playlist(self, paths, source="dropped"):
        """Build a unified playlist from *paths* (files and/or folders).

        The actual recursive walk runs on a background :class:`QThread` via
        :class:`PlaylistScanner` so the GUI stays responsive when the user
        drops a folder containing tens of thousands of files. The first
        playlist item is loaded from :meth:`_finish_playlist` once the
        worker emits ``finished``.

        Returns True if a scan was started, False if the request was rejected
        (e.g. another scan is already running, or no paths were given).
        """
        if not paths:
            return False

        # Re-entrancy guard: a scan is already in progress. We ignore the new
        # request rather than building a half-merged playlist.
        if self._scan_in_progress:
            self.status.showMessage(
                "Already scanning a folder — please wait for the current "
                "scan to finish."
            )
            return False

        self._close_pdf()
        self._close_epub()
        self._close_cbr()

        # First folder in the drop is remembered for self.folder (used by
        # status text / open-in-explorer). Loose files don't define a folder.
        first_folder = None
        for p in paths:
            if p and os.path.isdir(p):
                first_folder = p
                break

        self._begin_scan(list(paths), source=source, first_folder=first_folder)
        return True

    def _begin_scan(self, paths, source, first_folder=None, mode="playlist"):
        """Start a background :class:`PlaylistScanner` for *paths*.

        Sets a wait cursor, shows an initial status message and wires the
        worker's signals to :meth:`_on_scan_progress` /
        :meth:`_on_scan_finished` / :meth:`_on_scan_cancelled`.
        """
        self._scan_in_progress = True
        self._scan_source = source
        self._scan_first_folder = first_folder

        QApplication.setOverrideCursor(Qt.WaitCursor)
        self._scan_wait_cursor_active = True
        self.status.showMessage(f"Scanning folder… 0 files found ({source})")

        thread = QThread(self)
        worker = PlaylistScanner(paths, mode=mode)
        worker.moveToThread(thread)

        thread.started.connect(worker.run)
        worker.progress.connect(self._on_scan_progress)
        worker.finished.connect(self._on_scan_finished)
        worker.cancelled.connect(self._on_scan_cancelled)

        # Cleanup: when the worker signals done (either path), quit the
        # thread; when the thread finishes, delete both objects.
        worker.finished.connect(thread.quit)
        worker.cancelled.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)

        self._scan_thread = thread
        self._scan_worker = worker
        thread.start()

    def _restore_scan_cursor(self):
        """Restore the application cursor if we installed a wait cursor."""
        if self._scan_wait_cursor_active:
            QApplication.restoreOverrideCursor()
            self._scan_wait_cursor_active = False

    def _on_scan_progress(self, found, current_dir):
        """Slot: update the status bar with the running file count."""
        # current_dir can be empty (initial emit). Keep the message short so
        # it doesn't push the permanent path label off-screen.
        if current_dir:
            base = os.path.basename(current_dir.rstrip(os.sep)) or current_dir
            self.status.showMessage(
                f"Scanning folder… {found:,} files found — {base}"
            )
        else:
            self.status.showMessage(f"Scanning folder… {found:,} files found")

    def _on_scan_finished(self, items):
        """Slot: scanner completed, build the playlist and load first item."""
        source = self._scan_source
        first_folder = getattr(self, "_scan_first_folder", None)
        self._scan_in_progress = False
        self._scan_thread = None
        self._scan_worker = None
        self._restore_scan_cursor()

        self._finish_playlist(items, source=source, first_folder=first_folder)

    def _on_scan_cancelled(self):
        """Slot: scanner was cancelled before completion."""
        self._scan_in_progress = False
        self._scan_thread = None
        self._scan_worker = None
        self._restore_scan_cursor()
        self.status.showMessage("Folder scan cancelled.")

    def _finish_playlist(self, items, source="dropped", first_folder=None):
        """Apply a freshly scanned playlist to viewer state and show first item."""
        if not items:
            msg = f"No supported files {source}."
            self.image_label.setText(msg)
            self.status.showMessage(msg)
            return False

        self.folder = first_folder
        self._set_playlist_items(items)
        self.history.clear()
        self.history_list.clear()
        self.history_list.repaint()
        self.current_image = None
        self.history_index = -1
        self.update_image_info()
        self._update_title()

        if not self.images:
            # The scan found files, but every type in it is unchecked.
            msg = (f"Loaded {len(items):,} items ({source}), but all their file "
                   "types are hidden — check a type in the File Types panel.")
            self.image_label.setText("All file types are hidden.")
            self.status.showMessage(msg)
            return True

        if self.random_mode:
            self.show_random_image()
        else:
            self._load_playlist_item(self.images[0])

        self._reset_timer()
        if len(self.images) == len(items):
            self.status.showMessage(f"Loaded {len(items):,} items ({source}).")
        else:
            self.status.showMessage(
                f"Loaded {len(items):,} items ({source}) — showing "
                f"{len(self.images):,} after the file-type filter."
            )
        return True

    def _load_playlist_item(self, path, jump_to_last_page=False, update_history=True):
        """Load a single playlist item.

        Dispatches to the appropriate document loader (with
        ``from_playlist=True`` so the playlist itself is preserved) or to
        the regular image/video display path. When *jump_to_last_page* is
        True and the item is a document, navigates to its final page after
        load — used when stepping *backwards* into a doc so the user lands
        at the end and the next "previous" press continues the flow. When
        *update_history* is False, the item is loaded without being pushed
        onto the history stack (used by history-back/forward navigation).
        """
        if not path:
            return

        if is_pdf_file(path):
            self._load_pdf(path, from_playlist=True)
            if jump_to_last_page and getattr(self, '_pdf_doc', None):
                last = self._pdf_doc.page_count - 1
                self._show_pdf_page(self._spread_anchor(last))
        elif is_epub_file(path):
            self._load_epub(path, from_playlist=True)
            if jump_to_last_page and getattr(self, '_epub_doc', None):
                self._show_epub_page(self._epub_doc.page_count - 1)
        elif is_cbr_file(path):
            self._load_cbr(path, from_playlist=True)
            if jump_to_last_page and getattr(self, '_cbr_doc', None):
                last = self._cbr_doc.page_count - 1
                self._show_cbr_page(self._spread_anchor(last))
        else:
            # Image, video, or animated GIF.
            self._close_pdf()
            self._close_epub()
            self._close_cbr()
            self.clear_lines()
            self._display_image_with_lut_preview(path)
            self.current_image = path
            self.update_image_info(path)
            self.set_status_path(path)
            if self._auto_advance_active:
                self.timer_remaining = self.timer_spin.value()
                self._update_ring()

        # Add the item to history (works for both media and docs so the
        # random-mode back/forward history can step across types).
        if update_history:
            try:
                self.add_to_history(path)
            except Exception:
                pass

        # Append playlist position to the status bar.
        if self.images:
            try:
                i = self.images.index(path)
            except ValueError:
                return
            current_msg = self.status.currentMessage()
            suffix = f"  [item {i + 1}/{len(self.images)}]"
            if suffix not in current_msg:
                self.status.showMessage(current_msg + suffix)

    def _advance_in_playlist(self, direction):
        """Move *direction* (+1 or -1) items in the current playlist.

        Wraps around the ends. Used by next/previous when the current item
        is a document and the user has reached its last/first page.
        Returns True if navigation actually happened.
        """
        if not self.images:
            return False
        if self.current_image and self.current_image in self.images:
            try:
                idx = self.images.index(self.current_image)
            except ValueError:
                idx = 0
        else:
            idx = 0
        new_idx = (idx + direction) % len(self.images)
        new_path = self.images[new_idx]
        self.clear_lines()
        # When stepping backwards into a doc, land on its last page so the
        # user keeps reading continuously.
        self._load_playlist_item(new_path, jump_to_last_page=(direction < 0))
        return True

    def paste_from_clipboard(self):
        """Open content from the clipboard (Ctrl+V).

        Supported clipboard contents:
        - Raw image data (e.g. screenshots) — saved to a temp PNG and displayed.
        - File URLs — opened via the same path as drag & drop.
        - Plain text containing a local file or folder path — opened directly.
        """
        clipboard = QApplication.clipboard()
        if clipboard is None:
            return
        mime = clipboard.mimeData()
        if mime is None:
            self.status.showMessage("Clipboard is empty")
            return

        # 1) File URLs (e.g. files copied in Explorer)
        if mime.hasUrls():
            for url in mime.urls():
                if url.isLocalFile():
                    if self._open_local_path(url.toLocalFile(), source="pasted"):
                        return
            # fall through to other types if no URL was openable

        # 2) Raw image data (e.g. screenshot / Snipping Tool / browser image copy)
        if mime.hasImage():
            image = clipboard.image()
            if not image.isNull():
                import tempfile
                tmp_dir = os.path.join(tempfile.gettempdir(), "ova_viewer_paste")
                try:
                    os.makedirs(tmp_dir, exist_ok=True)
                except OSError:
                    tmp_dir = tempfile.gettempdir()
                fname = f"pasted_{int(time.time() * 1000)}.png"
                tmp_path = os.path.join(tmp_dir, fname)
                if image.save(tmp_path, "PNG"):
                    print(f"Pasted clipboard image saved to: {tmp_path}")
                    if self._open_local_path(tmp_path, source="pasted"):
                        self.status.showMessage(f"Pasted image: {fname}")
                        return
                self.status.showMessage("Failed to save pasted image")
                return

        # 3) Plain text that resolves to an existing path
        if mime.hasText():
            text = mime.text().strip().strip('"').strip("'")
            if text:
                # Strip optional file:// prefix
                if text.lower().startswith("file:///"):
                    text = text[8:]
                elif text.lower().startswith("file://"):
                    text = text[7:]
                candidate = os.path.expandvars(os.path.expanduser(text))
                if os.path.exists(candidate):
                    if self._open_local_path(candidate, source="pasted"):
                        return
                self.status.showMessage("Clipboard text is not a valid file or folder path")
                return

        self.status.showMessage("Clipboard does not contain an image or file")

    def _load_pdf(self, pdf_path, from_playlist=False):
        """Open a PDF for on-demand page-by-page viewing.

        When *from_playlist* is True the doc is being loaded as one item
        of a mixed playlist (`self.images`); in that case we keep the
        playlist + history intact and just point `current_image` at the
        PDF's path.
        """
        # Close any previously open PDF, EPUB, or CBR
        self._close_pdf()
        self._close_epub()
        self._close_cbr()

        self._pdf_name = os.path.basename(pdf_path)
        self.status.showMessage(f"Opening PDF: {self._pdf_name} …")

        try:
            self._pdf_doc = PdfDocument(pdf_path)
        except Exception as e:
            self.image_label.setText(f"Failed to open PDF: {e}")
            self.status.showMessage("PDF open failed")
            return

        self._pdf_page = 0  # 0-based current page
        page_count = self._pdf_doc.page_count

        if not from_playlist:
            # Reset viewer state (standalone document mode)
            self.folder = None
            self._set_playlist_items([])
            self.history.clear()
            self.history_list.clear()
            self.history_list.repaint()
            self.current_image = None
            self.history_index = -1
            self.update_image_info()

            if self.random_mode:
                self.random_mode = False
                if hasattr(self, 'random_mode_btn'):
                    self.random_mode_btn.setChecked(False)
        else:
            # Playlist mode: keep self.images/history; just point current_image
            # at this doc so playlist index lookups work.
            self.current_image = pdf_path
            self.update_image_info(pdf_path)

        self.setWindowTitle(
            f"Ova Viewer - {self._pdf_name} ({page_count} pages)")

        # Show PDF page nav in toolbar
        self._pdf_page_spin.blockSignals(True)
        self._pdf_page_spin.setRange(1, page_count)
        self._pdf_page_spin.setValue(1)
        self._pdf_page_spin.blockSignals(False)
        self._pdf_total_label.setText(f"/ {page_count}")
        self._pdf_nav_widget.show()

        # Apply persisted spread mode: sync toolbar checks, range label, and
        # disable drawing tools while in 2/3-page spread.
        in_spread = self._pdf_spread_mode != "single"
        self._set_drawing_tools_enabled(not in_spread)
        self._sync_pdf_spread_ui()

        # Show first page (or first spread)
        self._show_pdf_page(0)
        self._reset_timer()
        self.status.showMessage(
            f"Loaded PDF: {self._pdf_name} ({page_count} pages) — "
            f"use arrows/middle-click to browse, or type page number in toolbar")

    def _spread_count(self):
        """Return number of pages per spread for the current view mode."""
        return {"single": 1, "2page": 2, "3page": 3}.get(
            getattr(self, "_pdf_spread_mode", "single"), 1)

    def _spread_anchor(self, page_num, count=None):
        """Snap *page_num* down to the start of its spread (multiples of count)."""
        if count is None:
            count = self._spread_count()
        if count <= 1:
            return page_num
        return (page_num // count) * count

    def set_pdf_spread_mode(self, mode):
        """Switch single / 2-page / 3-page spread view and refresh the display."""
        if mode not in ("single", "2page", "3page"):
            return
        if mode == self._pdf_spread_mode:
            # Still re-sync UI in case checked state was clicked off
            self._sync_pdf_spread_ui()
            return
        self._pdf_spread_mode = mode
        try:
            self._settings.setValue("pdf_spread_mode", mode)
        except Exception:
            pass

        # Drawing tools are disabled while in 2/3-page spread because
        # coordinates would have to be split across pages.
        in_spread = mode != "single"
        if in_spread:
            self.clear_lines()
        self._set_drawing_tools_enabled(not in_spread)

        self._sync_pdf_spread_ui()

        # If a PDF is open, snap current page to a spread anchor and refresh.
        if getattr(self, "_pdf_doc", None):
            anchor = self._spread_anchor(self._pdf_page)
            self._show_pdf_page(anchor)
        # Same for CBR/CBZ.
        if getattr(self, "_cbr_doc", None):
            anchor = self._spread_anchor(self._cbr_page)
            self._show_cbr_page(anchor)

    def _sync_pdf_spread_ui(self):
        """Update toolbar button + range label + context-menu checks."""
        mode = self._pdf_spread_mode
        # Toolbar button checked actions
        for m, act in getattr(self, "_pdf_spread_actions", {}).items():
            try:
                act.setChecked(m == mode)
            except Exception:
                pass
        # Range label visible only in 2/3-page mode and only with a PDF or
        # CBR open (EPUB doesn't support spreads).
        label = getattr(self, "_pdf_range_label", None)
        if label is not None:
            active_doc = getattr(self, "_pdf_doc", None) or getattr(self, "_cbr_doc", None)
            current_page = self._pdf_page if getattr(self, "_pdf_doc", None) else getattr(self, "_cbr_page", 0)
            if active_doc and mode != "single":
                count = self._spread_count()
                start = self._spread_anchor(current_page, count)
                end = min(start + count - 1, active_doc.page_count - 1)
                if start == end:
                    label.setText(f"  [{start + 1}]")
                else:
                    label.setText(f"  [{start + 1}-{end + 1}]")
                label.show()
            else:
                label.setText("")
                label.hide()

    def _set_drawing_tools_enabled(self, enabled):
        """Enable/disable line/free-draw toolbar buttons (used in spread mode)."""
        for name in (
            "line_tool_btn", "hline_tool_btn", "free_line_tool_btn",
            "free_draw_tool_btn", "antialiasing_btn", "pen_pressure_btn",
            "line_thickness_spin", "line_transparency_slider",
            "line_color_btn", "undo_line_btn",
        ):
            btn = getattr(self, name, None)
            if btn is not None:
                try:
                    btn.setEnabled(enabled)
                except Exception:
                    pass

    def _show_pdf_page(self, page_num):
        """Render and display a single PDF page (or a 2/3-page spread)."""
        if not self._pdf_doc:
            return
        page_num = max(0, min(page_num, self._pdf_doc.page_count - 1))

        # Snap to spread anchor so 2/3-page mode always starts on a multiple.
        count = self._spread_count()
        page_num = self._spread_anchor(page_num, count)
        self._pdf_page = page_num

        # A different page invalidates any high-resolution render we were holding
        # for the previous page; the base pixmap below is the fit-resolution one.
        self._pdf_hires_timer.stop()
        self._pdf_hires_page = -1
        self._pdf_hires_count = 1
        self._pdf_hires_target = 0

        if count > 1:
            img_path = self._pdf_doc.render_spread(page_num, count)
        else:
            img_path = self._pdf_doc.render_page(page_num)
        if not img_path:
            self.image_label.setText(f"Failed to render page {page_num + 1}")
            return

        self._display_image_with_lut_preview(img_path)
        self.current_image = img_path
        self.update_image_info(img_path)
        self.set_status_path(img_path)

        total = self._pdf_doc.page_count
        if count > 1:
            end = min(page_num + count - 1, total - 1)
            if end == page_num:
                msg = f"{self._pdf_name}  —  page {page_num + 1} / {total}"
            else:
                msg = (f"{self._pdf_name}  —  pages "
                       f"{page_num + 1}-{end + 1} / {total}")
        else:
            msg = f"{self._pdf_name}  —  page {page_num + 1} / {total}"
        self.status.showMessage(msg)

        # Keep toolbar spinner in sync (always shows the left/anchor page)
        self._pdf_page_spin.blockSignals(True)
        self._pdf_page_spin.setValue(page_num + 1)
        self._pdf_page_spin.blockSignals(False)

        # Update the spread range label next to the spinner
        self._sync_pdf_spread_ui()

        # Pre-fetch nearby pages/spreads in background
        if count > 1:
            self._pdf_doc.prefetch_spread_around(page_num, count)
        else:
            self._pdf_doc.prefetch_around(page_num)

        # If the user was already zoomed in, sharpen the new page/spread too.
        if getattr(self.image_label, 'zoom_factor', 1.0) > 1.0:
            self._maybe_schedule_pdf_hires()

    def _go_to_image_or_page(self):
        """Ctrl+G handler: jump to PDF/EPUB/CBR page or image number."""
        if getattr(self, '_pdf_doc', None):
            self._pdf_go_to_page()
        elif getattr(self, '_epub_doc', None):
            self._epub_go_to_page()
        elif getattr(self, '_cbr_doc', None):
            self._cbr_go_to_page()
        else:
            self._go_to_image()

    def _go_to_image(self):
        """Show an input dialog to jump to a specific image by number."""
        if not self.images:
            return
        from PySide6.QtWidgets import QInputDialog
        total = len(self.images)
        current = 1
        if self.current_image and self.current_image in self.images:
            try:
                current = self.images.index(self.current_image) + 1
            except ValueError:
                current = 1
        num, ok = QInputDialog.getInt(
            self, "Go to Image",
            f"Image number (1\u2013{total}):",
            value=current, minValue=1, maxValue=total)
        if ok:
            img_path = self.images[num - 1]
            self.clear_lines()
            self._display_image_with_lut_preview(img_path)
            self.add_to_history(img_path)
            self.current_image = img_path
            self.update_image_info(img_path)
            self.set_status_path(img_path)

    def _pdf_go_to_page(self):
        """Show an input dialog to jump to a specific PDF page."""
        if not self._pdf_doc:
            return
        from PySide6.QtWidgets import QInputDialog
        total = self._pdf_doc.page_count
        page, ok = QInputDialog.getInt(
            self, "Go to Page",
            f"Page number (1\u2013{total}):",
            value=self._pdf_page + 1, minValue=1, maxValue=total)
        if ok:
            self._show_pdf_page(page - 1)

    def _on_pdf_page_spin_changed(self, value):
        """User typed or arrow-clicked a page number in the toolbar spinner."""
        if self._pdf_doc:
            self._show_pdf_page(value - 1)
        elif self._epub_doc:
            self._show_epub_page(value - 1)
        elif getattr(self, '_cbr_doc', None):
            self._show_cbr_page(value - 1)

    def _close_pdf(self):
        """Close the current PDF document if one is open."""
        # Stop any pending / running high-resolution zoom render first.
        if hasattr(self, '_pdf_hires_timer'):
            self._pdf_hires_timer.stop()
        self._pdf_hires_page = -1
        self._pdf_hires_count = 1
        self._pdf_hires_target = 0
        worker = getattr(self, '_pdf_hires_thread', None)
        if worker is not None and worker.isRunning():
            worker.wait(2000)
        self._pdf_hires_thread = None
        if hasattr(self, '_pdf_doc') and self._pdf_doc is not None:
            self._pdf_doc.close()
            self._pdf_doc = None
            self._pdf_page = 0
        # Hide nav widget only if no EPUB or CBR is open either
        if (hasattr(self, '_pdf_nav_widget')
                and not getattr(self, '_epub_doc', None)
                and not getattr(self, '_cbr_doc', None)):
            self._pdf_nav_widget.hide()
        # Restore drawing tools and clear the spread range label
        self._set_drawing_tools_enabled(True)
        if hasattr(self, '_pdf_range_label'):
            self._pdf_range_label.hide()
            self._pdf_range_label.setText("")

    # ── EPUB support ────────────────────────────────────────────

    def _load_epub(self, epub_path, from_playlist=False):
        """Open an EPUB for on-demand page-by-page viewing.

        See :meth:`_load_pdf` for the semantics of *from_playlist*.
        """
        self._close_pdf()
        self._close_epub()
        self._close_cbr()

        self._epub_name = os.path.basename(epub_path)
        self.status.showMessage(f"Opening EPUB: {self._epub_name} \u2026")

        try:
            self._epub_doc = EpubDocument(epub_path)
        except Exception as e:
            self.image_label.setText(f"Failed to open EPUB: {e}")
            self.status.showMessage("EPUB open failed")
            return

        self._epub_page = 0
        page_count = self._epub_doc.page_count

        if not from_playlist:
            # Reset viewer state (standalone document mode)
            self.folder = None
            self._set_playlist_items([])
            self.history.clear()
            self.history_list.clear()
            self.history_list.repaint()
            self.current_image = None
            self.history_index = -1
            self.update_image_info()

            if self.random_mode:
                self.random_mode = False
                if hasattr(self, 'random_mode_btn'):
                    self.random_mode_btn.setChecked(False)
        else:
            self.current_image = epub_path
            self.update_image_info(epub_path)

        self.setWindowTitle(
            f"Ova Viewer - {self._epub_name} ({page_count} pages)")

        # Show page nav in toolbar (reuses PDF nav widget)
        self._pdf_page_spin.blockSignals(True)
        self._pdf_page_spin.setRange(1, page_count)
        self._pdf_page_spin.setValue(1)
        self._pdf_page_spin.blockSignals(False)
        self._pdf_total_label.setText(f"/ {page_count}")
        self._pdf_nav_widget.show()

        self._show_epub_page(0)
        self._reset_timer()
        self.status.showMessage(
            f"Loaded EPUB: {self._epub_name} ({page_count} pages) \u2014 "
            f"use arrows/middle-click to browse, or type page number in toolbar")

    def _show_epub_page(self, page_num):
        """Render and display a single EPUB page."""
        if not self._epub_doc:
            return
        page_num = max(0, min(page_num, self._epub_doc.page_count - 1))
        self._epub_page = page_num

        img_path = self._epub_doc.render_page(page_num)
        if not img_path:
            self.image_label.setText(f"Failed to render page {page_num + 1}")
            return

        self._display_image_with_lut_preview(img_path)
        self.current_image = img_path
        self.update_image_info(img_path)
        self.set_status_path(img_path)

        total = self._epub_doc.page_count
        self.status.showMessage(
            f"{self._epub_name}  \u2014  page {page_num + 1} / {total}")

        self._pdf_page_spin.blockSignals(True)
        self._pdf_page_spin.setValue(page_num + 1)
        self._pdf_page_spin.blockSignals(False)

        self._epub_doc.prefetch_around(page_num)

    def _epub_go_to_page(self):
        """Show an input dialog to jump to a specific EPUB page."""
        if not self._epub_doc:
            return
        from PySide6.QtWidgets import QInputDialog
        total = self._epub_doc.page_count
        page, ok = QInputDialog.getInt(
            self, "Go to Page",
            f"Page number (1\u2013{total}):",
            value=self._epub_page + 1, minValue=1, maxValue=total)
        if ok:
            self._show_epub_page(page - 1)

    def _close_epub(self):
        """Close the current EPUB document if one is open."""
        if hasattr(self, '_epub_doc') and self._epub_doc is not None:
            self._epub_doc.close()
            self._epub_doc = None
            self._epub_page = 0
        # Hide nav widget only if no PDF or CBR is open either
        if (hasattr(self, '_pdf_nav_widget')
                and not getattr(self, '_pdf_doc', None)
                and not getattr(self, '_cbr_doc', None)):
            self._pdf_nav_widget.hide()

    # ── CBR / CBZ support ───────────────────────────────────────

    def _load_cbr(self, archive_path, from_playlist=False):
        """Open a CBR/CBZ comic archive for on-demand page viewing.

        See :meth:`_load_pdf` for the semantics of *from_playlist*.
        """
        self._close_pdf()
        self._close_epub()
        self._close_cbr()

        self._cbr_name = os.path.basename(archive_path)
        self.status.showMessage(f"Opening comic: {self._cbr_name} \u2026")

        try:
            self._cbr_doc = CbrDocument(archive_path)
        except Exception as e:
            self.image_label.setText(f"Failed to open comic archive: {e}")
            self.status.showMessage(f"Comic open failed: {e}")
            return

        self._cbr_page = 0
        page_count = self._cbr_doc.page_count

        if not from_playlist:
            # Reset viewer state (standalone document mode)
            self.folder = None
            self._set_playlist_items([])
            self.history.clear()
            self.history_list.clear()
            self.history_list.repaint()
            self.current_image = None
            self.history_index = -1
            self.update_image_info()

            if self.random_mode:
                self.random_mode = False
                if hasattr(self, 'random_mode_btn'):
                    self.random_mode_btn.setChecked(False)
        else:
            self.current_image = archive_path
            self.update_image_info(archive_path)

        self.setWindowTitle(
            f"Ova Viewer - {self._cbr_name} ({page_count} pages)")

        # Show page nav in toolbar (reuses PDF nav widget)
        self._pdf_page_spin.blockSignals(True)
        self._pdf_page_spin.setRange(1, page_count)
        self._pdf_page_spin.setValue(1)
        self._pdf_page_spin.blockSignals(False)
        self._pdf_total_label.setText(f"/ {page_count}")
        self._pdf_nav_widget.show()

        # Apply persisted spread mode (shared with PDF).
        in_spread = self._pdf_spread_mode != "single"
        self._set_drawing_tools_enabled(not in_spread)
        self._sync_pdf_spread_ui()

        self._show_cbr_page(0)
        self._reset_timer()
        self.status.showMessage(
            f"Loaded comic: {self._cbr_name} ({page_count} pages) \u2014 "
            f"use arrows/middle-click to browse, or type page number in toolbar")

    def _show_cbr_page(self, page_num):
        """Render and display a single CBR page (or a 2/3-page spread)."""
        if not self._cbr_doc:
            return
        page_num = max(0, min(page_num, self._cbr_doc.page_count - 1))

        # Snap to spread anchor so 2/3-page mode always starts on a multiple.
        count = self._spread_count()
        page_num = self._spread_anchor(page_num, count)
        self._cbr_page = page_num

        if count > 1:
            img_path = self._cbr_doc.render_spread(page_num, count)
        else:
            img_path = self._cbr_doc.render_page(page_num)
        if not img_path:
            self.image_label.setText(f"Failed to render page {page_num + 1}")
            return

        self._display_image_with_lut_preview(img_path)
        self.current_image = img_path
        self.update_image_info(img_path)
        self.set_status_path(img_path)

        total = self._cbr_doc.page_count
        if count > 1:
            end = min(page_num + count - 1, total - 1)
            if end == page_num:
                msg = f"{self._cbr_name}  \u2014  page {page_num + 1} / {total}"
            else:
                msg = (f"{self._cbr_name}  \u2014  pages "
                       f"{page_num + 1}-{end + 1} / {total}")
        else:
            msg = f"{self._cbr_name}  \u2014  page {page_num + 1} / {total}"
        self.status.showMessage(msg)

        # Keep toolbar spinner in sync (always shows the left/anchor page)
        self._pdf_page_spin.blockSignals(True)
        self._pdf_page_spin.setValue(page_num + 1)
        self._pdf_page_spin.blockSignals(False)

        self._sync_pdf_spread_ui()

        # Pre-fetch nearby pages/spreads in background
        if count > 1:
            self._cbr_doc.prefetch_spread_around(page_num, count)
        else:
            self._cbr_doc.prefetch_around(page_num)

    def _cbr_go_to_page(self):
        """Show an input dialog to jump to a specific CBR page."""
        if not self._cbr_doc:
            return
        from PySide6.QtWidgets import QInputDialog
        total = self._cbr_doc.page_count
        page, ok = QInputDialog.getInt(
            self, "Go to Page",
            f"Page number (1\u2013{total}):",
            value=self._cbr_page + 1, minValue=1, maxValue=total)
        if ok:
            self._show_cbr_page(page - 1)

    def _close_cbr(self):
        """Close the current CBR/CBZ document if one is open."""
        if hasattr(self, '_cbr_doc') and self._cbr_doc is not None:
            try:
                self._cbr_doc.close()
            except Exception:
                pass
            self._cbr_doc = None
            self._cbr_page = 0
        # Hide nav widget only if no PDF or EPUB is open either
        if (hasattr(self, '_pdf_nav_widget')
                and not getattr(self, '_pdf_doc', None)
                and not getattr(self, '_epub_doc', None)):
            self._pdf_nav_widget.hide()
        # Restore drawing tools and clear the spread range label
        self._set_drawing_tools_enabled(True)
        if hasattr(self, '_pdf_range_label'):
            self._pdf_range_label.hide()
            self._pdf_range_label.setText("")

