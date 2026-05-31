"""Splash screen with dynamic status text shown during startup."""

import os
import sys

from PySide6.QtWidgets import QSplashScreen, QApplication
from PySide6.QtGui import QPainter, QColor, QFont, QPixmap
from PySide6.QtCore import Qt, QRect


def _find_splash_image():
    """Locate ova_viewer.png in the project root or PyInstaller bundle."""
    if getattr(sys, 'frozen', False):
        base = sys._MEIPASS
    else:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(base, "ova_viewer.png")
    if os.path.exists(path):
        return path
    return None


class DynamicSplash(QSplashScreen):
    """A splash screen that displays module loading progress."""

    WIDTH = 420
    HEIGHT = 300

    def __init__(self):
        # Try to load ova_viewer.png as background
        img_path = _find_splash_image()
        if img_path:
            src = QPixmap(img_path)
            pixmap = src.scaled(self.WIDTH, self.HEIGHT,
                                Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self.WIDTH = pixmap.width()
            self.HEIGHT = pixmap.height()
        else:
            pixmap = QPixmap(self.WIDTH, self.HEIGHT)
            pixmap.fill(QColor(45, 45, 45))

        super().__init__(pixmap)
        self._has_image = img_path is not None
        self.setWindowFlag(Qt.WindowStaysOnTopHint)
        self.setWindowFlag(Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self._status = "Starting Ova Viewer\u2026"

    def set_status(self, text: str):
        """Update the status line and repaint immediately."""
        self._status = text
        self.repaint()
        QApplication.processEvents()

    # ------------------------------------------------------------------
    def drawContents(self, painter: QPainter):
        """Custom paint: simple status text near the bottom of the splash."""
        painter.setRenderHint(QPainter.Antialiasing)

        if not self._has_image:
            painter.fillRect(self.rect(), QColor(45, 45, 45))

        # Simple, single-line status centered on the image — no outlines, no shadows.
        status_font = QFont("Segoe UI", 9)
        painter.setFont(status_font)
        painter.setPen(QColor(255, 255, 255))
        painter.drawText(QRect(0, 0, self.WIDTH, self.HEIGHT),
                         Qt.AlignHCenter | Qt.AlignVCenter, self._status)


def show_splash(app: QApplication) -> DynamicSplash:
    """Create, show, and return the dynamic splash screen."""
    splash = DynamicSplash()
    splash.show()
    app.processEvents()
    return splash


def close_splash(splash: DynamicSplash | None = None):
    """Close the Qt splash screen."""
    if splash is not None:
        splash.close()
    if splash is not None:
        splash.close()
