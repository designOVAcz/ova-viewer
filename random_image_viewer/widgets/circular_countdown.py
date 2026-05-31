import time

from PySide6.QtWidgets import QWidget
from PySide6.QtGui import QPainter, QColor, QPen
from PySide6.QtCore import Qt, QTimer, QSize, QRect


class CircularCountdown(QWidget):
    def __init__(self, total_time=0, parent=None):
        super().__init__(parent)
        self.total_time = 0
        self.remaining_time = 0      # The actual time left
        self.displayed_time = 0      # The smooth UI value
        self.parent_viewer = None    # Reference to main viewer
        self.is_paused = False       # Pause state indicator
        self.setFixedSize(QSize(24, 24))
        self.setCursor(Qt.PointingHandCursor)  # Show it's clickable
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._on_tick)
        self._timer.start(16)  # ~60 FPS

        self._last_update = time.monotonic()

    def set_parent_viewer(self, viewer):
        self.parent_viewer = viewer

    def set_paused(self, paused):
        self.is_paused = paused
        self.update()  # Redraw with pause indicator

    def set_total_time(self, seconds):
        self.total_time = float(max(1, seconds))
        self.update()

    def set_remaining_time(self, seconds):
        self.remaining_time = float(max(0, min(self.total_time, seconds)))
        self.update()

    def _on_tick(self):
        # Interpolate displayed_time toward remaining_time
        alpha = 0.18  # Smoothing factor (smaller = smoother/slower)
        self.displayed_time += (self.remaining_time - self.displayed_time) * alpha
        if abs(self.displayed_time - self.remaining_time) < 0.01:
            self.displayed_time = self.remaining_time
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = self.rect().adjusted(4, 4, -4, -4)
        # Draw subtle background ring
        painter.setPen(QPen(QColor("#3d3e40"), 2))
        painter.setBrush(Qt.NoBrush)
        painter.drawEllipse(rect)
        # Draw smooth progress arc
        if self.total_time > 0 and self.displayed_time > 0:
            fraction = self.displayed_time / self.total_time
            angle = int(360 * 16 * fraction)
            # Use different color when paused
            color = "#ff8080" if self.is_paused else "#80b2ff"
            painter.setPen(QPen(QColor(color), 3))
            painter.drawArc(rect, 90 * 16, -angle)

        # Draw pause indicator when paused
        if self.is_paused and self.total_time > 0:
            painter.setPen(QPen(QColor("#ff8080"), 2))
            painter.setBrush(QColor("#ff8080"))
            # Draw two small vertical bars (pause symbol)
            center_x = rect.center().x()
            center_y = rect.center().y()
            bar_height = 6
            bar_width = 2
            bar1_rect = QRect(center_x - 3, center_y - bar_height//2, bar_width, bar_height)
            bar2_rect = QRect(center_x + 1, center_y - bar_height//2, bar_width, bar_height)
            painter.drawRect(bar1_rect)
            painter.drawRect(bar2_rect)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self.parent_viewer:
            # Only allow pause/resume when timer is active
            if self.parent_viewer._auto_advance_active:
                self.parent_viewer.toggle_timer_pause()
        super().mousePressEvent(event)
