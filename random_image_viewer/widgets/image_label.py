from PySide6.QtWidgets import QLabel, QApplication, QMenu
from PySide6.QtGui import QPainter, QColor, QPen, QAction, QTabletEvent, QMovie
from PySide6.QtCore import Qt, QTimer, QSize
from PySide6.QtMultimedia import QMediaPlayer, QVideoSink, QAudioOutput

from random_image_viewer.image_utils import safe_load_pixmap


class ImageLabel(QLabel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.parent_viewer = None
        self.setMouseTracking(True)

        # Animated GIF support
        self._current_movie = None

        # Video playback support
        self._media_player = None
        self._video_sink = None
        self._audio_output = None
        self._video_last_frame_time = 0  # for frame throttling

        # PEN PRESSURE: Start with tablet tracking disabled to allow normal UI interaction
        # It will be enabled only when free draw mode is active
        self.setTabletTracking(False)
        self.setAttribute(Qt.WA_TabletTracking, False)

        # Zoom functionality
        self.zoom_factor = 1.0
        self.min_zoom = 0.1
        self.max_zoom = 10.0
        self.zoom_step = 0.1

        # Pan functionality for when zoomed
        self.pan_offset_x = 0
        self.pan_offset_y = 0
        self.is_panning = False
        self.last_pan_point = None

        # Store original pixmap size for proper zoom calculations
        self.original_pixmap = None

        # Enable context menu
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self.show_context_menu)

    def start_animation(self, file_path, scaled_size=None):
        """Start playing an animated GIF using QMovie as a frame source.
        
        Frames are not rendered by QLabel directly — instead the parent viewer
        receives each frame via the frameChanged signal so it can apply
        enhancements, LUT, zoom, etc. before displaying.
        """
        self.stop_animation()
        movie = QMovie(file_path)
        if not movie.isValid():
            return False
        self._current_movie = movie
        if scaled_size and scaled_size.isValid():
            movie.setScaledSize(scaled_size)
        # Do NOT call self.setMovie(movie) — frames are handled by the viewer
        if self.parent_viewer and hasattr(self.parent_viewer, '_on_gif_frame_changed'):
            movie.frameChanged.connect(self.parent_viewer._on_gif_frame_changed)
        movie.start()
        return True

    def stop_animation(self):
        """Stop and clean up any running QMovie animation"""
        if self._current_movie is not None:
            self._current_movie.stop()
            try:
                self._current_movie.frameChanged.disconnect()
            except RuntimeError:
                pass
            self.setMovie(None)
            self._current_movie.deleteLater()
            self._current_movie = None

    def _map_label_pos_to_original(self, pos):
        """Map a label-space position to original-image (x, y) coordinates.

        Mirrors the transformation used by the line-drawing pipeline so the
        color-snap tool samples the exact pixel the cursor points at.
        Returns (original_x, original_y) or (None, None) if cursor is outside
        the visible image content.
        """
        if not (self.parent_viewer and self.pixmap() and not self.pixmap().isNull()
                and self.parent_viewer.current_image):
            return None, None
        try:
            original_pixmap, error = safe_load_pixmap(self.parent_viewer.current_image)
            if error or original_pixmap.isNull():
                return None, None
        except Exception:
            return None, None

        original_size = original_pixmap.size()
        rotation = self.parent_viewer.rotation_angle
        if rotation == 90 or rotation == 270:
            display_reference_size = QSize(original_size.height(), original_size.width())
        else:
            display_reference_size = original_size

        label_size = self.size()
        base_scaled = display_reference_size.scaled(label_size, Qt.KeepAspectRatio)
        zoomed_width = int(base_scaled.width() * self.zoom_factor)
        zoomed_height = int(base_scaled.height() * self.zoom_factor)
        draw_x = (label_size.width() - zoomed_width) // 2 + int(self.pan_offset_x)
        draw_y = (label_size.height() - zoomed_height) // 2 + int(self.pan_offset_y)

        rel_x = pos.x() - draw_x
        rel_y = pos.y() - draw_y
        if not (0 <= rel_x <= zoomed_width and 0 <= rel_y <= zoomed_height):
            return None, None

        if rotation == 90 or rotation == 270:
            scale_x = zoomed_width / original_size.height()
            scale_y = zoomed_height / original_size.width()
        else:
            scale_x = zoomed_width / original_size.width()
            scale_y = zoomed_height / original_size.height()
        if scale_x == 0 or scale_y == 0:
            return None, None
        display_x = rel_x / scale_x
        display_y = rel_y / scale_y

        if rotation == 0:
            unrotated_x, unrotated_y = display_x, display_y
        elif rotation == 90:
            unrotated_x, unrotated_y = display_y, original_size.width() - display_x
        elif rotation == 180:
            unrotated_x, unrotated_y = original_size.width() - display_x, original_size.height() - display_y
        elif rotation == 270:
            unrotated_x, unrotated_y = original_size.height() - display_y, display_x
        else:
            unrotated_x, unrotated_y = display_x, display_y

        original_x, original_y = unrotated_x, unrotated_y
        if self.parent_viewer.flipped_h:
            original_x = original_size.width() - unrotated_x
        if self.parent_viewer.flipped_v:
            original_y = original_size.height() - unrotated_y
        return original_x, original_y

    def update_animation_size(self, scaled_size):
        """Update the scaled size of the running animation (e.g. on resize)"""
        if self._current_movie is not None and self._current_movie.state() == QMovie.Running:
            self._current_movie.setScaledSize(scaled_size)

    def is_animation_playing(self):
        """Return True if an animated GIF is currently playing"""
        return self._current_movie is not None and self._current_movie.state() == QMovie.Running

    # ── Video playback ──────────────────────────────────────────────

    def start_video(self, file_path):
        """Start playing a video file using QMediaPlayer + QVideoSink.

        Frames are delivered to the parent viewer via the videoFrameChanged
        signal so it can apply LUT / enhancements before display — same
        pattern as start_animation() for GIFs.
        """
        self.stop_video()

        from PySide6.QtCore import QUrl
        player = QMediaPlayer(self)
        sink = QVideoSink(self)
        audio = QAudioOutput(self)

        player.setVideoOutput(sink)
        player.setAudioOutput(audio)
        audio.setVolume(0.5)

        # Connect frame signal to parent viewer
        if self.parent_viewer and hasattr(self.parent_viewer, '_on_video_frame_changed'):
            sink.videoFrameChanged.connect(self.parent_viewer._on_video_frame_changed)

        # Expose player signals for toolbar wiring (done by main_window after start)
        self._media_player = player
        self._video_sink = sink
        self._audio_output = audio
        self._video_last_frame_time = 0

        player.setSource(QUrl.fromLocalFile(file_path))
        player.play()
        return True

    def stop_video(self):
        """Stop and clean up any running video playback"""
        if self._media_player is not None:
            self._media_player.stop()
            try:
                if self._video_sink is not None:
                    self._video_sink.videoFrameChanged.disconnect()
            except RuntimeError:
                pass
            self._media_player.setVideoOutput(None)
            self._media_player.setAudioOutput(None)
            self._media_player.deleteLater()
            if self._video_sink is not None:
                self._video_sink.deleteLater()
            if self._audio_output is not None:
                self._audio_output.deleteLater()
            self._media_player = None
            self._video_sink = None
            self._audio_output = None

    def is_video_playing(self):
        """Return True if a video is currently playing"""
        return (self._media_player is not None and
                self._media_player.playbackState() == QMediaPlayer.PlaybackState.PlayingState)

    def video_toggle_play_pause(self):
        if self._media_player is None:
            return
        if self._media_player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._media_player.pause()
        else:
            self._media_player.play()

    def video_seek(self, position_ms):
        if self._media_player is not None:
            self._media_player.setPosition(int(position_ms))

    def video_set_volume(self, value):
        """Set volume 0-100 int → 0.0-1.0 float"""
        if self._audio_output is not None:
            self._audio_output.setVolume(value / 100.0)

    def video_set_muted(self, muted):
        if self._audio_output is not None:
            self._audio_output.setMuted(muted)

    def wheelEvent(self, event):
        """Handle mouse wheel events for image navigation (when not zoomed) or zooming (when zoomed)"""
        if (self.parent_viewer and
            self.parent_viewer.current_image and
            self.pixmap() and
            not self.pixmap().isNull()):

            # Accept the event to prevent it from propagating
            event.accept()

            # Get the wheel delta (positive = scroll up, negative = scroll down)
            delta = event.angleDelta().y()

            # Zoom mode: Ctrl+wheel OR right-click+wheel
            zoom_mode = (event.modifiers() & Qt.ControlModifier) or (event.buttons() & Qt.RightButton)

            # When not zoomed in and not in zoom mode, use wheel for image navigation
            if self.zoom_factor <= 1.0 and not zoom_mode:
                if delta > 0:
                    self.parent_viewer.show_previous_image()
                elif delta < 0:
                    self.parent_viewer.show_next_image()
                return

            # Store mouse position for zoom centering
            mouse_pos = event.position()

            old_zoom = self.zoom_factor

            if delta > 0:
                # Zoom in
                new_zoom = min(self.zoom_factor * 1.1, self.max_zoom)
            else:
                # Zoom out
                new_zoom = max(self.zoom_factor / 1.1, self.min_zoom)

            if new_zoom != self.zoom_factor:
                # Calculate zoom center point
                if new_zoom > old_zoom:  # Zooming in
                    # Adjust pan offset to keep mouse position centered
                    zoom_ratio = new_zoom / old_zoom
                    widget_center_x = self.width() / 2
                    widget_center_y = self.height() / 2

                    # Calculate offset from center
                    offset_from_center_x = mouse_pos.x() - widget_center_x
                    offset_from_center_y = mouse_pos.y() - widget_center_y

                    # Adjust pan to keep point under mouse
                    self.pan_offset_x = self.pan_offset_x * zoom_ratio - offset_from_center_x * (zoom_ratio - 1)
                    self.pan_offset_y = self.pan_offset_y * zoom_ratio - offset_from_center_y * (zoom_ratio - 1)
                else:  # Zooming out
                    zoom_ratio = new_zoom / old_zoom
                    self.pan_offset_x *= zoom_ratio
                    self.pan_offset_y *= zoom_ratio

                self.zoom_factor = new_zoom

                # Reset pan when back to 100% or below
                if self.zoom_factor <= 1.0:
                    self.pan_offset_x = 0
                    self.pan_offset_y = 0
                    self.zoom_factor = 1.0

                # ZOOM OPTIMIZATION: Use debounced update to prevent excessive processing
                # during rapid wheel scrolling
                if hasattr(self.parent_viewer, '_zoom_update_timer'):
                    self.parent_viewer._zoom_update_timer.stop()
                else:
                    self.parent_viewer._zoom_update_timer = QTimer()
                    self.parent_viewer._zoom_update_timer.setSingleShot(True)
                    self.parent_viewer._zoom_update_timer.timeout.connect(
                        self.parent_viewer._smart_zoom_display)

                # Trigger immediate fast preview, then debounced final update
                self.parent_viewer._smart_zoom_display()  # Immediate cached display
                self.parent_viewer._zoom_update_timer.start(100)  # Debounced final update

                # Update status to show zoom level (but preserve LUT processing status if active)
                zoom_percent = int(self.zoom_factor * 100)

                # Check if LUT is processing in background
                if (hasattr(self.parent_viewer, '_async_processing_state') and
                    self.parent_viewer._async_processing_state and
                    self.parent_viewer.current_lut):
                    # LUT processing active - show progress instead of just zoom
                    state = self.parent_viewer._async_processing_state
                    progress = (state['current_row'] / state['total_rows']) * 100
                    lut_name = self.parent_viewer.current_lut_name if self.parent_viewer.current_lut_name != "None" else "LUT"
                    if self.zoom_factor > 1.0:
                        self.parent_viewer.status.showMessage(f"Zoom: {zoom_percent}% - {lut_name} processing... {progress:.0f}% (Right-click drag to pan)")
                    else:
                        self.parent_viewer.status.showMessage(f"Zoom: {zoom_percent}% - {lut_name} processing... {progress:.0f}%")
                else:
                    # No LUT processing - show normal zoom status
                    if self.zoom_factor > 1.0:
                        self.parent_viewer.status.showMessage(f"Zoom: {zoom_percent}% (Right-click drag to pan)")
                    else:
                        self.parent_viewer.status.showMessage(f"Zoom: {zoom_percent}%")
        else:
            # Don't accept the event, let it propagate
            event.ignore()

    def mouseDoubleClickEvent(self, event):
        # Absorb middle-button double-clicks so they don't re-trigger
        # mousePressEvent (Qt's default behaviour), which would skip an image.
        if event.button() == Qt.MiddleButton:
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def mousePressEvent(self, event):
        # Handle middle-click for next image
        if event.button() == Qt.MiddleButton and self.parent_viewer:
            self.parent_viewer.show_next_image()
            event.accept()
            return

        # Handle right-click for panning when zoomed
        if (event.button() == Qt.RightButton and
            self.zoom_factor > 1.0 and
            self.pixmap() and
            not self.pixmap().isNull()):

            self.is_panning = True
            self.last_pan_point = event.position()
            self.setCursor(Qt.ClosedHandCursor)
            event.accept()
            return

    # Handle left-click for line drawing / free draw (only if inside image bounds)
        if (self.parent_viewer and event.button() == Qt.LeftButton and
            self.pixmap() and not self.pixmap().isNull()):

            # 💉 Color Snap takes precedence over any drawing mode
            if getattr(self.parent_viewer, 'color_snap_mode', False):
                click_pos = event.position()
                if not self._is_position_over_image(click_pos):
                    super().mousePressEvent(event)
                    return
                ox, oy = self._map_label_pos_to_original(click_pos)
                if ox is None:
                    super().mousePressEvent(event)
                    return
                color = self.parent_viewer._sample_color_at(ox, oy)
                if color is not None:
                    self.parent_viewer.commit_color_snap(color)
                event.accept()
                return

            # Check if any drawing mode is active
            is_any_drawing_mode = (self.parent_viewer.line_drawing_mode or
                                 self.parent_viewer.horizontal_line_drawing_mode or
                                 self.parent_viewer.free_line_drawing_mode or
                                 self.parent_viewer.free_draw_mode)

            if not is_any_drawing_mode:
                super().mousePressEvent(event)
                return

            # Get click position and verify it's over image (prevents accidental toolbar toggles drawing)
            click_pos = event.position()
            if not self._is_position_over_image(click_pos):
                # If we are currently drawing a free stroke and click outside, finalize the stroke
                if (self.parent_viewer.free_draw_mode and self.parent_viewer.is_drawing_free_stroke):
                    self.parent_viewer.end_free_draw_stroke()
                # Also allow this click to pass through so buttons can toggle modes
                super().mousePressEvent(event)
                return

            # PEN PRESSURE: Capture pressure information from the event
            pressure = 1.0  # Default pressure
            if self.parent_viewer and self.parent_viewer.pen_pressure_enabled:
                pressure = getattr(event, 'pressure', lambda: 1.0)()
                if callable(pressure):
                    pressure = pressure()

            # Normalize pressure to 0.1-1.0 range for better control
            pressure = max(0.1, min(1.0, pressure))

            if self.parent_viewer and self.parent_viewer.pen_pressure_enabled:
                print(f"PEN PRESSURE: Mouse press detected pressure = {pressure:.3f}")
                if pressure != 1.0:
                    print(f"PEN PRESSURE: Pressure varies from default! This means your tablet is working.")

            # Get original image for coordinate reference
            try:
                original_pixmap, error = safe_load_pixmap(self.parent_viewer.current_image)
                if error or original_pixmap.isNull():
                    return
            except Exception:
                return

            # Use the original unrotated size for coordinate transformation
            original_size = original_pixmap.size()

            # For rotated images, we need to consider the displayed dimensions
            # The displayed image might have swapped width/height due to rotation
            rotation = self.parent_viewer.rotation_angle
            if rotation == 90 or rotation == 270:
                # At 90 and 270, width and height are swapped
                display_reference_size = QSize(original_size.height(), original_size.width())
            else:
                # At 0 and 180, dimensions stay the same
                display_reference_size = original_size

            # UNIFIED coordinate conversion - use the SAME logic as in display_image
            # Calculate the base scaled size that would be used at 100% zoom
            label_size = self.size()
            base_scaled = display_reference_size.scaled(label_size, Qt.KeepAspectRatio)

            # Apply zoom factor to get the actual displayed size
            zoomed_width = int(base_scaled.width() * self.zoom_factor)
            zoomed_height = int(base_scaled.height() * self.zoom_factor)

            # Calculate position within the label (including pan offset)
            draw_x = (label_size.width() - zoomed_width) // 2 + int(self.pan_offset_x)
            draw_y = (label_size.height() - zoomed_height) // 2 + int(self.pan_offset_y)

            # Get click position relative to the zoomed image
            rel_x = click_pos.x() - draw_x
            rel_y = click_pos.y() - draw_y

            # Check if click is within the zoomed image bounds
            if (0 <= rel_x <= zoomed_width and 0 <= rel_y <= zoomed_height):
                # Convert to display coordinate space using correct scale factors for rotation
                if rotation == 90 or rotation == 270:
                    scale_x = zoomed_width / original_size.height()
                    scale_y = zoomed_height / original_size.width()
                else:
                    scale_x = zoomed_width / original_size.width()
                    scale_y = zoomed_height / original_size.height()

                # Convert to display coordinates (relative to the rotated image display space)
                display_x = rel_x / scale_x
                display_y = rel_y / scale_y

                # Transform coordinates back to original coordinate space
                flipped_h = self.parent_viewer.flipped_h
                flipped_v = self.parent_viewer.flipped_v

                # Step 1: Undo rotation transformation
                if rotation == 0:
                    unrotated_x = display_x
                    unrotated_y = display_y
                elif rotation == 90:
                    unrotated_x = display_y
                    unrotated_y = original_size.width() - display_x
                elif rotation == 180:
                    unrotated_x = original_size.width() - display_x
                    unrotated_y = original_size.height() - display_y
                elif rotation == 270:
                    unrotated_x = original_size.height() - display_y
                    unrotated_y = display_x
                else:
                    unrotated_x = display_x
                    unrotated_y = display_y

                # Step 2: Undo flip transformations to get original coordinates
                original_x = unrotated_x
                original_y = unrotated_y

                if flipped_h:
                    original_x = original_size.width() - unrotated_x
                if flipped_v:
                    original_y = original_size.height() - unrotated_y

                # Add lines using original coordinates (these will be transformed during display)
                if self.parent_viewer.line_drawing_mode:
                    self.parent_viewer.add_line(original_x)
                if self.parent_viewer.horizontal_line_drawing_mode:
                    self.parent_viewer.add_hline(original_y)
                if self.parent_viewer.free_line_drawing_mode:
                    self.parent_viewer.add_free_line_point(original_x, original_y)
                if self.parent_viewer.free_draw_mode:
                    print(f"Mouse press in free draw mode at ({original_x:.1f}, {original_y:.1f})")
                    self.parent_viewer.start_free_draw_stroke(original_x, original_y, pressure)

                    # PEN PRESSURE: Store current pressure for real-time painting
                    if self.parent_viewer and self.parent_viewer.pen_pressure_enabled:
                        self.parent_viewer._current_pressure = pressure

        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        # Handle panning (now checks for right-click)
        if self.is_panning and (event.buttons() & Qt.RightButton) and self.last_pan_point is not None:
            current_point = event.position()
            delta_x = current_point.x() - self.last_pan_point.x()
            delta_y = current_point.y() - self.last_pan_point.y()

            self.pan_offset_x += delta_x
            self.pan_offset_y += delta_y
            self.last_pan_point = current_point

            if self.parent_viewer and self.parent_viewer.current_image:
                self.parent_viewer._smart_zoom_display()

            event.accept()
            return

        # 💉 Color Snap: debounced hover preview (samples after cursor idle)
        if (self.parent_viewer and getattr(self.parent_viewer, 'color_snap_mode', False)
                and self.pixmap() and not self.pixmap().isNull()):
            pos = event.position()
            if self._is_position_over_image(pos):
                global_pos = self.mapToGlobal(pos.toPoint())
                # Hand off to viewer's debounced sampler (timer-based, ~350ms idle)
                if hasattr(self.parent_viewer, 'request_color_snap_hover_sample'):
                    self.parent_viewer.request_color_snap_hover_sample(pos, global_pos)
            else:
                prev = getattr(self.parent_viewer, '_color_snap_preview', None)
                if prev is not None and prev.isVisible():
                    prev.hide()
                # Cancel any pending sample when leaving the image
                t = getattr(self.parent_viewer, '_color_snap_hover_timer', None)
                if t is not None:
                    t.stop()
            # Don't return — let normal hover handling continue (e.g., tooltips)

        # OPTIMIZED: Handle free draw mode with ultra-fast real-time painting
        if (self.parent_viewer and self.parent_viewer.free_draw_mode and
            self.parent_viewer.is_drawing_free_stroke and event.buttons() & Qt.LeftButton and
            self.pixmap() and not self.pixmap().isNull()):
            # Abort drawing if cursor left the image bounds; finalize stroke so pen can interact with UI
            if not self._is_position_over_image(event.position()):
                self.parent_viewer.end_free_draw_stroke()
                super().mouseMoveEvent(event)
                return

            # PEN PRESSURE: Capture pressure from mouse event or stored tablet pressure
            pressure = 1.0  # Default pressure
            if self.parent_viewer and self.parent_viewer.pen_pressure_enabled:
                # First try to get pressure from mouse event
                mouse_pressure = getattr(event, 'pressure', lambda: 1.0)()
                if callable(mouse_pressure):
                    mouse_pressure = mouse_pressure()

                # If pressure is still 1.0, check if we have tablet pressure stored
                if mouse_pressure == 1.0 and hasattr(self.parent_viewer, '_tablet_pressure'):
                    pressure = self.parent_viewer._tablet_pressure
                else:
                    pressure = mouse_pressure

            # IMPROVED PRESSURE INTERPOLATION: Better range and smoothing
            pressure = max(0.05, min(1.0, pressure))  # Allow lighter minimum pressure

            # PRESSURE SMOOTHING: Apply smoothing to reduce jitter
            if hasattr(self.parent_viewer, '_last_pressure'):
                smoothing_factor = 0.3
                pressure = (smoothing_factor * pressure) + ((1 - smoothing_factor) * self.parent_viewer._last_pressure)
            self.parent_viewer._last_pressure = pressure

            # ULTRA-FAST: Use cached coordinate conversion
            if not self.parent_viewer.drawing_cache:
                return  # Cache not ready

            click_pos = event.position()
            cache = self.parent_viewer.drawing_cache

            # LIGHTNING-FAST: Pre-calculated coordinate conversion
            rel_x = click_pos.x() - cache['draw_x']
            rel_y = click_pos.y() - cache['draw_y']

            # INSTANT BOUNDS CHECK: Pre-calculated dimensions
            if (0 <= rel_x <= cache['zoomed_width'] and 0 <= rel_y <= cache['zoomed_height']):
                # PRE-COMPUTED SCALE FACTORS: No division during drawing
                rotation = cache['rotation']
                original_size = cache['original_size']

                if rotation == 90 or rotation == 270:
                    scale_x = cache['zoomed_width'] / original_size.height()
                    scale_y = cache['zoomed_height'] / original_size.width()
                else:
                    scale_x = cache['zoomed_width'] / original_size.width()
                    scale_y = cache['zoomed_height'] / original_size.height()

                display_x = rel_x / scale_x
                display_y = rel_y / scale_y

                # OPTIMIZED ROTATION: Pre-calculated transformations
                if rotation == 0:
                    unrotated_x, unrotated_y = display_x, display_y
                elif rotation == 90:
                    unrotated_x, unrotated_y = display_y, original_size.width() - display_x
                elif rotation == 180:
                    unrotated_x, unrotated_y = original_size.width() - display_x, original_size.height() - display_y
                else: # 270
                    unrotated_x, unrotated_y = original_size.height() - display_y, display_x

                # FAST FLIP TRANSFORMATIONS: Pre-calculated
                original_x, original_y = unrotated_x, unrotated_y
                if cache['flipped_h']:
                    original_x = original_size.width() - unrotated_x
                if cache['flipped_v']:
                    original_y = original_size.height() - unrotated_y

                # REAL-TIME PERFORMANCE: Add point with immediate visual feedback
                self.parent_viewer.add_free_draw_point(original_x, original_y, pressure)

                # PEN PRESSURE: Update current pressure for real-time painting
                if self.parent_viewer and self.parent_viewer.pen_pressure_enabled:
                    self.parent_viewer._current_pressure = pressure

            event.accept()
            return

        super().mouseMoveEvent(event)

    def _is_position_over_image(self, pos):
        """Check if a position is over the actual image content (not just the widget)"""
        if not self.pixmap() or self.pixmap().isNull():
            return False

        # First, check if the position is over UI elements using widget detection
        if self.parent_viewer:
            # Convert position to global coordinates
            global_pos = self.mapToGlobal(pos)

            # Convert QPointF to QPoint for widgetAt() method
            global_point = global_pos.toPoint()

            # Find which widget is actually at this position
            widget_at_pos = QApplication.widgetAt(global_point)

            # If there's a widget at this position and it's not the ImageLabel itself,
            # then we're over a UI element - return False immediately
            if widget_at_pos and widget_at_pos != self:
                return False

            # Additional specific checks for main UI areas
            main_window_pos = self.parent_viewer.mapFromGlobal(global_pos)
            main_window_point = main_window_pos.toPoint()

            # Check if clicking on the main toolbar area
            if (hasattr(self.parent_viewer, 'main_toolbar') and
                self.parent_viewer.main_toolbar.isVisible() and
                self.parent_viewer.main_toolbar.geometry().contains(main_window_point)):
                return False

            # Check if clicking on the timer toolbar area
            if (hasattr(self.parent_viewer, 'timer_toolbar') and
                self.parent_viewer.timer_toolbar.isVisible() and
                self.parent_viewer.timer_toolbar.geometry().contains(main_window_point)):
                return False

            # Check if clicking on the status bar area
            if (hasattr(self.parent_viewer, 'status') and
                self.parent_viewer.status.isVisible() and
                self.parent_viewer.status.geometry().contains(main_window_point)):
                return False

        # If no UI element detected, check if position is within actual image bounds
        label_size = self.size()

        base_scaled = self.pixmap().scaled(label_size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        zoomed_width = int(base_scaled.width() * self.zoom_factor)
        zoomed_height = int(base_scaled.height() * self.zoom_factor)

        draw_x = (label_size.width() - zoomed_width) // 2 + int(self.pan_offset_x)
        draw_y = (label_size.height() - zoomed_height) // 2 + int(self.pan_offset_y)

        rel_x = pos.x() - draw_x
        rel_y = pos.y() - draw_y

        return (0 <= rel_x <= zoomed_width and 0 <= rel_y <= zoomed_height)

    def tabletEvent(self, event):
        """Handle tablet events ONLY for pen pressure during active drawing"""
        if not (self.parent_viewer and self.parent_viewer.free_draw_mode):
            event.ignore()
            return

        if (self.parent_viewer.is_drawing_free_stroke and
            self._is_position_over_image(event.position())):

            if event.type() == QTabletEvent.TabletMove:
                pressure = event.pressure()
                if self.parent_viewer:
                    self.parent_viewer._tablet_pressure = pressure
                event.ignore()
                return

            elif event.type() == QTabletEvent.TabletPress:
                pressure = event.pressure()
                if self.parent_viewer:
                    self.parent_viewer._tablet_pressure = pressure
                event.ignore()
                return

        event.ignore()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.RightButton and self.is_panning:
            self.is_panning = False
            self.last_pan_point = None
            self.setCursor(Qt.ArrowCursor)
            event.accept()
            return

        # Handle free draw mode mouse release
        if (event.button() == Qt.LeftButton and self.parent_viewer and
            self.parent_viewer.free_draw_mode and self.parent_viewer.is_drawing_free_stroke):
            print(f"Mouse release in free draw mode")
            self.parent_viewer.end_free_draw_stroke()
            event.accept()
            return

        super().mouseReleaseEvent(event)

    def show_context_menu(self, pos):
        """Show context menu with image and zoom options"""
        if not self.parent_viewer:
            return

        # Hide context menu when zoomed in to avoid interfering with panning
        if hasattr(self, 'zoom_factor') and self.zoom_factor > 1.0:
            return

        menu = QMenu(self)

        # --- Main actions ---
        open_action = QAction("Open Folder", self)
        open_action.triggered.connect(self.parent_viewer.choose_folder)
        menu.addAction(open_action)

        menu.addSeparator()

        prev_action = QAction("Previous Image", self)
        prev_action.triggered.connect(self.parent_viewer.show_previous_image)
        menu.addAction(prev_action)

        next_action = QAction("Next Image", self)
        next_action.triggered.connect(self.parent_viewer._manual_next_image)
        menu.addAction(next_action)

        goto_action = QAction("Go to Image…  (Ctrl+G)", self)
        goto_action.triggered.connect(self.parent_viewer._go_to_image_or_page)
        menu.addAction(goto_action)

        # --- PDF View submenu (only when a PDF is open) ---
        if getattr(self.parent_viewer, '_pdf_doc', None) is not None:
            menu.addSeparator()
            pdf_menu = menu.addMenu("\U0001F4D6  PDF View")
            current_mode = getattr(
                self.parent_viewer, '_pdf_spread_mode', 'single')
            for mode_key, label in (("single", "\U0001F4C4  Single Page"),
                                    ("2page", "\U0001F4D6  2-Page Spread"),
                                    ("3page", "\U0001F4DA  3-Page Spread")):
                act = QAction(label, self)
                act.setCheckable(True)
                act.setChecked(current_mode == mode_key)
                act.triggered.connect(
                    lambda _checked=False, m=mode_key:
                        self.parent_viewer.set_pdf_spread_mode(m))
                pdf_menu.addAction(act)

        menu.addSeparator()

        # --- Zoom actions ---
        zoom_in_action = QAction("Zoom In", self)
        zoom_in_action.setShortcut("Ctrl++")
        zoom_in_action.triggered.connect(self.parent_viewer.zoom_in)
        menu.addAction(zoom_in_action)

        zoom_out_action = QAction("Zoom Out", self)
        zoom_out_action.setShortcut("Ctrl+-")
        zoom_out_action.triggered.connect(self.parent_viewer.zoom_out)
        menu.addAction(zoom_out_action)

        reset_zoom_action = QAction("Reset Zoom", self)
        reset_zoom_action.setShortcut("Ctrl+0")
        reset_zoom_action.triggered.connect(self.parent_viewer.reset_zoom)
        menu.addAction(reset_zoom_action)

        menu.addSeparator()

        # --- Transform actions ---
        flip_h_action = QAction("Flip Horizontal", self)
        flip_h_action.setShortcut("Ctrl+H")
        flip_h_action.triggered.connect(self.parent_viewer.flip_horizontal)
        menu.addAction(flip_h_action)

        flip_v_action = QAction("Flip Vertical", self)
        flip_v_action.setShortcut("Ctrl+V")
        flip_v_action.triggered.connect(self.parent_viewer.flip_vertical)
        menu.addAction(flip_v_action)

        menu.addSeparator()

        # --- View actions ---
        if self.parent_viewer.is_fullscreen:
            exit_fullscreen_action = QAction("Exit Fullscreen", self)
            exit_fullscreen_action.setShortcut("Esc")
            exit_fullscreen_action.triggered.connect(self.parent_viewer.exit_fullscreen)
            menu.addAction(exit_fullscreen_action)

            force_exit_action = QAction("Force Exit Fullscreen", self)
            force_exit_action.setShortcut("Ctrl+Esc")
            force_exit_action.triggered.connect(self.parent_viewer.force_exit_fullscreen)
            menu.addAction(force_exit_action)
        else:
            fullscreen_action = QAction("Enter Fullscreen", self)
            fullscreen_action.setShortcut("F11")
            fullscreen_action.triggered.connect(lambda: self.parent_viewer.toggle_fullscreen(True))
            menu.addAction(fullscreen_action)

        # UI visibility toggle
        ui_toggle_action = QAction("Show/Hide UI Elements", self)
        ui_toggle_action.setCheckable(True)
        ui_toggle_action.setChecked(self.parent_viewer.main_toolbar.isVisible())
        ui_toggle_action.toggled.connect(self.parent_viewer.toggle_toolbar_visibility)
        menu.addAction(ui_toggle_action)

        # Always on top toggle
        always_on_top_action = QAction("Always on Top", self)
        always_on_top_action.setCheckable(True)
        always_on_top_action.setChecked(self.parent_viewer.always_on_top)
        always_on_top_action.toggled.connect(self.parent_viewer.toggle_always_on_top)
        menu.addAction(always_on_top_action)

        menu.addSeparator()

        # --- Settings ---
        if hasattr(self.parent_viewer, 'toggle_grayscale'):
            grayscale_action = QAction("Grayscale", self)
            grayscale_action.setCheckable(True)
            grayscale_action.setChecked(self.parent_viewer.grayscale_value > 0)
            grayscale_action.toggled.connect(self.parent_viewer.toggle_grayscale)
            menu.addAction(grayscale_action)

        if hasattr(self.parent_viewer, 'toggle_contrast'):
            contrast_action = QAction("Enhanced Contrast", self)
            contrast_action.setCheckable(True)
            contrast_action.setChecked(self.parent_viewer.contrast_value != 50)
            contrast_action.toggled.connect(self.parent_viewer.toggle_contrast)
            menu.addAction(contrast_action)

        if hasattr(self.parent_viewer, 'toggle_gamma'):
            gamma_action = QAction("Enhanced Brightness", self)
            gamma_action.setCheckable(True)
            gamma_action.setChecked(self.parent_viewer.gamma_value != 50)
            gamma_action.toggled.connect(self.parent_viewer.toggle_gamma)
            menu.addAction(gamma_action)

        # Show the menu
        menu.exec(self.mapToGlobal(pos))

    def reset_zoom(self):
        """Reset zoom to 100% and clear pan"""
        self.zoom_factor = 1.0
        self.pan_offset_x = 0
        self.pan_offset_y = 0
