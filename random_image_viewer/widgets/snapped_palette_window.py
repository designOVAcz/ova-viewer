"""Floating palette window for the Color Snap (💉) tool.

A small frameless, draggable always-on-top window that displays swatches
of colors picked or auto-extracted via the eyedropper. Persistent: only
the ✕ close button hides it (tool toggles do not affect visibility).

Swatches are grouped into rows. Each 🪄 auto-extract creates a new row;
💉 clicks append to the most-recent row.

Signals:
    swatch_clicked(str)  — left-click on a swatch (hex color)
    swatch_removed(str)  — right-click on a swatch (hex color)
    closed()             — user clicked the ✕ close button
"""

from PySide6.QtWidgets import (QWidget, QHBoxLayout, QVBoxLayout, QLabel,
                                QToolButton, QSizePolicy, QSizeGrip)
from PySide6.QtCore import Qt, Signal


class SnappedPaletteWindow(QWidget):
    swatch_clicked = Signal(str)
    swatch_removed = Signal(str)
    closed = Signal()

    SWATCH_W = 18      # minimum width for a swatch
    SWATCH_H = 24      # minimum height for a swatch
    DEFAULT_W = 260    # initial panel width
    DEFAULT_H = 140    # initial panel height
    MIN_W = 160
    MIN_H = 80

    def __init__(self, parent=None):
        super().__init__(
            parent,
            Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint,
        )
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setWindowTitle("Color Palette")

        self._drag_offset = None
        self._rows = []  # list[list[str]]

        # Root container
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._bg = QWidget(self)
        self._bg.setObjectName("snapPaletteBg")
        self._bg.setStyleSheet(
            "#snapPaletteBg {"
            " background-color: #2b2d30;"
            " border: 1px solid #4a4d50;"
            " border-radius: 8px;"
            "}"
        )
        outer.addWidget(self._bg)

        inner = QVBoxLayout(self._bg)
        inner.setContentsMargins(8, 4, 6, 8)
        inner.setSpacing(4)

        # ── Header: drag label (stretch) + ✕ close button ──
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(4)

        self._header = QLabel("💉 Palette")
        self._header.setStyleSheet(
            "QLabel { color: #c8c8c8; font-size: 10pt; font-weight: bold;"
            " padding: 2px 4px; background: transparent; }"
        )
        self._header.setCursor(Qt.SizeAllCursor)
        self._header.setToolTip("Drag to move • swatches: left-click use, right-click remove")
        header.addWidget(self._header, 1)

        self._close_btn = QToolButton()
        self._close_btn.setText("✕")
        self._close_btn.setFixedSize(18, 18)
        self._close_btn.setToolTip("Close palette panel")
        self._close_btn.setCursor(Qt.PointingHandCursor)
        self._close_btn.setStyleSheet(
            "QToolButton { color: #c8c8c8; background: transparent;"
            " border: none; font-weight: bold; font-size: 10pt; padding: 0px; }"
            "QToolButton:hover { color: #fff; background: #c54040;"
            " border-radius: 3px; }"
        )
        self._close_btn.clicked.connect(self._on_close_clicked)
        header.addWidget(self._close_btn, 0)

        inner.addLayout(header)

        # ── Rows container ──
        self._rows_container = QWidget()
        self._rows_container.setStyleSheet("background: transparent;")
        self._rows_container.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._rows_layout = QVBoxLayout(self._rows_container)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(3)
        inner.addWidget(self._rows_container, 1)

        # Empty-state placeholder
        self._empty_label = QLabel("(no colors yet — click on the image or 🪄 extract)")
        self._empty_label.setStyleSheet(
            "QLabel { color: #888; font-size: 8pt; padding: 2px 4px;"
            " background: transparent; font-style: italic; }"
        )
        inner.addWidget(self._empty_label)

        # ── Bottom: resize grip on the right ──
        bottom = QHBoxLayout()
        bottom.setContentsMargins(0, 0, 0, 0)
        bottom.setSpacing(0)
        bottom.addStretch(1)
        self._size_grip = QSizeGrip(self._bg)
        self._size_grip.setFixedSize(14, 14)
        bottom.addWidget(self._size_grip, 0, Qt.AlignRight | Qt.AlignBottom)
        inner.addLayout(bottom)

        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumSize(self.MIN_W, self.MIN_H)
        self.resize(self.DEFAULT_W, self.DEFAULT_H)
        self.set_rows([])

    # ───────────── public API ─────────────
    def set_rows(self, rows):
        """Rebuild the panel from a list of rows (each row is list[hex_str])."""
        self._rows = [list(r) for r in (rows or [])]
        # Clear existing row widgets
        while self._rows_layout.count():
            it = self._rows_layout.takeAt(0)
            w = it.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        for row_colors in self._rows:
            if not row_colors:
                continue
            row_widget = QWidget(self._rows_container)
            row_widget.setStyleSheet("background: transparent;")
            row_widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
            row_layout = QHBoxLayout(row_widget)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(3)
            for hex_color in row_colors:
                sw = QToolButton(row_widget)
                sw.setMinimumSize(self.SWATCH_W, self.SWATCH_H)
                sw.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
                sw.setToolTip(
                    f"{hex_color.upper()}  —  left-click: use as line color, right-click: remove"
                )
                sw.setStyleSheet(
                    f"QToolButton {{ background-color: {hex_color};"
                    f" border: 1px solid #444; margin: 0px; border-radius: 3px; }}"
                    f"QToolButton:hover {{ border: 1px solid #ffcc00; }}"
                )
                sw.clicked.connect(lambda checked=False, c=hex_color: self.swatch_clicked.emit(c))
                sw.setContextMenuPolicy(Qt.CustomContextMenu)
                sw.customContextMenuRequested.connect(
                    lambda _pos, c=hex_color: self.swatch_removed.emit(c)
                )
                row_layout.addWidget(sw, 1)
            # Each row stretches equally vertically within the rows container
            self._rows_layout.addWidget(row_widget, 1)
        # Empty state visibility
        non_empty = any(r for r in self._rows)
        self._empty_label.setVisible(not non_empty)
        self._rows_container.setVisible(non_empty)
        # NOTE: do not call adjustSize() — preserve the user's resized window

    def set_colors(self, colors):
        """Backward-compatible wrapper: treat the flat list as a single row."""
        self.set_rows([list(colors or [])])

    # ───────────── close button ─────────────
    def _on_close_clicked(self):
        self.closed.emit()
        self.hide()

    # ───────────── dragging ─────────────
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_offset is not None and (event.buttons() & Qt.LeftButton):
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_offset = None
            event.accept()
        else:
            super().mouseReleaseEvent(event)
