"""Live view of the in-app log (see log_capture), opened from the status bar."""
import os

from PySide6.QtCore import QObject, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QTextCharFormat, QColor, QTextCursor
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QHBoxLayout, QPlainTextEdit, QPushButton,
    QVBoxLayout, QWidget,
)

from random_image_viewer import log_capture

_LEVEL_COLORS = {
    log_capture.LEVEL_INFO: None,
    log_capture.LEVEL_WARNING: "#e0a030",
    log_capture.LEVEL_ERROR: "#ff5f56",
}


class LogBridge(QObject):
    """Moves log lines from any thread onto the GUI thread."""
    lineAdded = Signal(str, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        log_capture.add_listener(self._on_line)
        self.destroyed.connect(lambda *_: log_capture.remove_listener(self._on_line))

    def _on_line(self, text, level):
        try:
            self.lineAdded.emit(text, level)
        except RuntimeError:
            log_capture.remove_listener(self._on_line)


class LogWindow(QWidget):
    def __init__(self, bridge, parent=None):
        super().__init__(parent, Qt.Window)
        self.setWindowTitle("Ova Viewer – Log")
        self.resize(900, 480)

        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.text.setMaximumBlockCount(log_capture.MAX_LINES)
        self.text.setStyleSheet(
            "QPlainTextEdit { font-family: Consolas, 'Courier New', monospace;"
            " font-size: 11px; }")

        self.problems_only = QCheckBox("Warnings && errors only")
        self.problems_only.toggled.connect(self._reload)
        self.autoscroll = QCheckBox("Auto-scroll")
        self.autoscroll.setChecked(True)

        copy_btn = QPushButton("Copy all")
        copy_btn.clicked.connect(
            lambda: QApplication.clipboard().setText(self.text.toPlainText()))
        clear_btn = QPushButton("Clear")
        clear_btn.clicked.connect(self._clear)
        folder_btn = QPushButton("Open log folder")
        folder_btn.clicked.connect(self._open_folder)
        folder_btn.setEnabled(log_capture.log_path() is not None)
        if log_capture.log_path():
            folder_btn.setToolTip(log_capture.log_path())

        bar = QHBoxLayout()
        bar.addWidget(self.problems_only)
        bar.addWidget(self.autoscroll)
        bar.addStretch(1)
        bar.addWidget(copy_btn)
        bar.addWidget(clear_btn)
        bar.addWidget(folder_btn)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.addWidget(self.text, 1)
        layout.addLayout(bar)

        bridge.lineAdded.connect(self._append)
        self._reload()

    def _append(self, line, level):
        if self.problems_only.isChecked() and level == log_capture.LEVEL_INFO:
            return
        bar = self.text.verticalScrollBar()
        at_bottom = bar.value() >= bar.maximum() - 2
        cursor = QTextCursor(self.text.document())
        cursor.movePosition(QTextCursor.End)
        fmt = QTextCharFormat()
        color = _LEVEL_COLORS.get(level)
        if color:
            fmt.setForeground(QColor(color))
        if not self.text.document().isEmpty():
            cursor.insertBlock()
        cursor.insertText(line, fmt)
        if self.autoscroll.isChecked() or at_bottom:
            bar.setValue(bar.maximum())

    def _reload(self):
        self.text.clear()
        for line, level in log_capture.snapshot():
            self._append(line, level)
        bar = self.text.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _clear(self):
        log_capture.clear()
        self.text.clear()

    def _open_folder(self):
        path = log_capture.log_path()
        if path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(path)))
