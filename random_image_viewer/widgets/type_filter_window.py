"""Floating File Types panel.

A small frameless, draggable, always-on-top palette listing every file type
present in the loaded playlist, each with a checkbox and a file count. The
types are discovered from the scanned files themselves, so the list always
reflects what a folder actually contains. Unchecking a type hides those files
while browsing; everything is checked by default.

It is a pure view: the playlist and the filtering live in the main window,
which this widget drives via signals.

Signals:
    types_changed(list)   — the hidden set changed; payload is the sorted list
                            of hidden extensions (".mp4", ".gif", …)
    closed()              — user clicked the ✕ close button
"""

from PySide6.QtWidgets import (QWidget, QHBoxLayout, QVBoxLayout, QLabel,
                               QToolButton, QCheckBox, QScrollArea, QFrame)
from PySide6.QtCore import Qt, Signal


class TypeFilterWindow(QWidget):
    types_changed = Signal(list)
    closed = Signal()

    ROW_H = 22        # per-checkbox height used to size the list
    MAX_LIST_H = 320  # grow with the type count, but never taller than this
    MIN_W = 200

    def __init__(self, parent=None):
        super().__init__(
            parent,
            Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint,
        )
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setWindowTitle("File Types")

        self._drag_offset = None
        self._boxes = {}          # ext -> QCheckBox
        self._empty_label = None  # placeholder shown when nothing is loaded
        self._quiet = False       # suppress signals while rebuilding/setting

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._bg = QWidget(self)
        self._bg.setObjectName("typeFilterBg")
        self._bg.setStyleSheet(
            "#typeFilterBg {"
            " background-color: #2b2d30;"
            " border: 1px solid #4a4d50;"
            " border-radius: 8px;"
            "}"
        )
        outer.addWidget(self._bg)

        inner = QVBoxLayout(self._bg)
        inner.setContentsMargins(10, 6, 10, 10)
        inner.setSpacing(6)

        # ── Header: drag label + ✕ close ──
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(4)

        self._header = QLabel("\U0001F5C2 File Types")
        self._header.setStyleSheet(
            "QLabel { color: #c8c8c8; font-size: 10pt; font-weight: bold;"
            " padding: 2px 4px; background: transparent; }"
        )
        self._header.setCursor(Qt.SizeAllCursor)
        self._header.setToolTip("Drag to move")
        header.addWidget(self._header, 1)

        self._close_btn = QToolButton()
        self._close_btn.setText("✕")
        self._close_btn.setFixedSize(18, 18)
        self._close_btn.setToolTip("Close file types panel")
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

        # ── All / None / Invert row ──
        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(0, 0, 0, 0)
        btn_row.setSpacing(4)
        btn_style = (
            "QToolButton { color: #d0d0d0; background: #3a3d40;"
            " border: 1px solid #4a4d50; border-radius: 3px;"
            " padding: 2px 8px; font-size: 9pt; }"
            "QToolButton:hover { background: #45484c; color: #fff; }"
        )
        for text, tip, slot in (("All", "Show every file type", self.check_all),
                                ("None", "Hide every file type", self.check_none),
                                ("Invert", "Swap shown and hidden types", self.invert)):
            b = QToolButton()
            b.setText(text)
            b.setToolTip(tip)
            b.setCursor(Qt.PointingHandCursor)
            b.setStyleSheet(btn_style)
            b.clicked.connect(slot)
            btn_row.addWidget(b)
        btn_row.addStretch(1)
        inner.addLayout(btn_row)

        # ── Scrollable checkbox list ──
        self._list_host = QWidget()
        self._list_layout = QVBoxLayout(self._list_host)
        self._list_layout.setContentsMargins(0, 0, 0, 0)
        self._list_layout.setSpacing(2)
        self._list_layout.addStretch(1)

        self._scroll = QScrollArea()
        self._scroll.setWidget(self._list_host)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setStyleSheet(
            "QScrollArea { background: transparent; }"
            "QWidget { background: transparent; }"
        )
        inner.addWidget(self._scroll, 1)

        # ── Footer summary ──
        self._summary = QLabel("")
        self._summary.setStyleSheet(
            "QLabel { color: #9a9a9a; font-size: 8pt; background: transparent; }")
        inner.addWidget(self._summary, 0)

        self.setMinimumWidth(self.MIN_W)

    # ───────────── population ─────────────
    def set_types(self, counts, hidden):
        """Rebuild the checkbox list. No signal is emitted.

        *counts* maps an extension (".mp4") to the number of files of that type
        in the playlist; *hidden* is the set of extensions to leave unchecked.
        """
        self._quiet = True
        try:
            for box in self._boxes.values():
                self._list_layout.removeWidget(box)
                box.setParent(None)
                box.deleteLater()
            self._boxes = {}
            if self._empty_label is not None:
                self._list_layout.removeWidget(self._empty_label)
                self._empty_label.setParent(None)
                self._empty_label.deleteLater()
                self._empty_label = None

            cb_style = ("QCheckBox { color: #d0d0d0; font-size: 9pt; padding: 1px 2px; }"
                        "QCheckBox:hover { color: #ffffff; }")
            for ext in sorted(counts):
                n = counts[ext]
                box = QCheckBox(f"{ext.lstrip('.').upper()}  ({n:,})")
                box.setStyleSheet(cb_style)
                box.setChecked(ext not in hidden)
                box.setToolTip(f"{n:,} {ext} file{'' if n == 1 else 's'}")
                box.toggled.connect(self._on_box_toggled)
                # Insert before the trailing stretch.
                self._list_layout.insertWidget(self._list_layout.count() - 1, box)
                self._boxes[ext] = box

            if not counts:
                self._empty_label = QLabel("no files loaded")
                self._empty_label.setStyleSheet(
                    "QLabel { color: #7a7a7a; font-size: 9pt; background: transparent; }")
                self._list_layout.insertWidget(self._list_layout.count() - 1,
                                               self._empty_label)
        finally:
            self._quiet = False

        rows = max(1, len(counts))
        self._scroll.setMinimumHeight(min(self.MAX_LIST_H, self.ROW_H * rows + 4))
        self.adjustSize()

    def set_summary(self, shown, total):
        """Update the footer count line."""
        self._summary.setText(f"showing {shown:,} of {total:,} files" if total else "")

    # ───────────── state ─────────────
    def hidden_types(self):
        """Extensions whose checkbox is currently unchecked."""
        return sorted(ext for ext, b in self._boxes.items() if not b.isChecked())

    def set_hidden(self, hidden):
        """Re-check/uncheck to match *hidden*. No signal is emitted."""
        self._quiet = True
        try:
            for ext, b in self._boxes.items():
                b.setChecked(ext not in hidden)
        finally:
            self._quiet = False

    def _on_box_toggled(self, _checked):
        if not self._quiet:
            self.types_changed.emit(self.hidden_types())

    def _set_all(self, checked):
        if not self._boxes:
            return
        self._quiet = True
        try:
            for b in self._boxes.values():
                b.setChecked(checked)
        finally:
            self._quiet = False
        self.types_changed.emit(self.hidden_types())

    def check_all(self):
        """Show every type (all boxes checked)."""
        self._set_all(True)

    def check_none(self):
        """Hide every type. The viewer keeps the current item on screen."""
        self._set_all(False)

    def invert(self):
        """Swap the shown and hidden types."""
        if not self._boxes:
            return
        self._quiet = True
        try:
            for b in self._boxes.values():
                b.setChecked(not b.isChecked())
        finally:
            self._quiet = False
        self.types_changed.emit(self.hidden_types())

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
