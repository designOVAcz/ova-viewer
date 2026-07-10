"""Floating Curves (classical RGB levels) window.

A small frameless, draggable, always-on-top panel that hosts the tone-curve
controls: an Enable checkbox, a Master/R/G/B channel selector and three
equally sized sliders (Black point, White point, Midtone). It is a pure view:
all image processing stays in the main window, which this widget drives via
signals.

Signals:
    enable_toggled(bool)  — Enable checkbox changed
    channel_changed(str)  — active channel changed ("master"/"r"/"g"/"b")
    black_changed(int)    — black-point slider moved (0-254)
    white_changed(int)    — white-point slider moved (1-255)
    gamma_changed(int)    — midtone slider moved (-100..100)
    reset_requested()     — Reset button clicked
    closed()              — user clicked the ✕ close button
"""

from PySide6.QtWidgets import (QWidget, QHBoxLayout, QVBoxLayout, QGridLayout,
                               QLabel, QToolButton, QCheckBox, QButtonGroup,
                               QSizePolicy)
from PySide6.QtCore import Qt, Signal

from random_image_viewer.widgets.clickable_slider import ClickableSlider


class CurvesWindow(QWidget):
    enable_toggled = Signal(bool)
    channel_changed = Signal(str)
    black_changed = Signal(int)
    white_changed = Signal(int)
    gamma_changed = Signal(int)
    opacity_changed = Signal(int)
    reset_requested = Signal()
    closed = Signal()

    SLIDER_W = 190   # shared slider width so all three look identical
    LABEL_W = 42     # left label column width
    VALUE_W = 34     # right value column width

    _CHANNELS = (("master", "RGB", "#dddddd"), ("r", "R", "#ff6b6b"),
                 ("g", "G", "#6bd66b"), ("b", "B", "#6ba8ff"))

    def __init__(self, parent=None):
        super().__init__(
            parent,
            Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint,
        )
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setWindowTitle("Curves")

        self._drag_offset = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._bg = QWidget(self)
        self._bg.setObjectName("curvesBg")
        self._bg.setStyleSheet(
            "#curvesBg {"
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

        self._header = QLabel("📈 Curves")
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
        self._close_btn.setToolTip("Close curves panel")
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

        # ── Enable + channel selector row ──
        top_row = QHBoxLayout()
        top_row.setContentsMargins(0, 0, 0, 0)
        top_row.setSpacing(6)

        self._enable_cb = QCheckBox("Enable")
        self._enable_cb.setStyleSheet(
            "QCheckBox { color: #d0d0d0; font-size: 9pt; }")
        self._enable_cb.toggled.connect(self.enable_toggled.emit)
        top_row.addWidget(self._enable_cb, 0)

        top_row.addStretch(1)

        self._channel_group = QButtonGroup(self)
        self._channel_group.setExclusive(True)
        self._channel_btns = {}
        for key, text, color in self._CHANNELS:
            b = QToolButton()
            b.setText(text)
            b.setCheckable(True)
            b.setFixedSize(30, 22)
            b.setToolTip(f"Edit {text} channel")
            b.setStyleSheet(
                "QToolButton { color: %s; background: #34363a; border: 1px solid #4a4d50;"
                " border-radius: 3px; font-size: 9pt; font-weight: bold; }"
                "QToolButton:checked { background: #4a4d50; border: 1px solid #7aa2ff; }"
                % color
            )
            b.clicked.connect(lambda _checked=False, k=key: self.channel_changed.emit(k))
            self._channel_group.addButton(b)
            self._channel_btns[key] = b
            top_row.addWidget(b, 0)
        self._channel_btns["master"].setChecked(True)
        inner.addLayout(top_row)

        # ── Sliders grid (uniform widths) ──
        grid = QGridLayout()
        grid.setContentsMargins(0, 2, 0, 2)
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(6)

        self._black_slider, self._black_value = self._add_slider_row(
            grid, 0, "Black", 0, 254, 0, self._on_black)
        self._white_slider, self._white_value = self._add_slider_row(
            grid, 1, "White", 1, 255, 255, self._on_white)
        self._gamma_slider, self._gamma_value = self._add_slider_row(
            grid, 2, "Mid", -100, 100, 0, self._on_gamma)
        self._opacity_slider, self._opacity_value = self._add_slider_row(
            grid, 3, "Opacity", 0, 100, 100, self._on_opacity)
        inner.addLayout(grid)

        # ── Footer: Reset ──
        footer = QHBoxLayout()
        footer.setContentsMargins(0, 0, 0, 0)
        footer.setSpacing(4)
        footer.addStretch(1)
        self._reset_btn = QToolButton()
        self._reset_btn.setText("↺ Reset")
        self._reset_btn.setToolTip("Reset all curve channels to neutral")
        self._reset_btn.setCursor(Qt.PointingHandCursor)
        self._reset_btn.setStyleSheet(
            "QToolButton { color: #d0d0d0; background: #34363a; border: 1px solid #4a4d50;"
            " border-radius: 3px; padding: 2px 8px; font-size: 9pt; }"
            "QToolButton:hover { background: #4a4d50; }"
        )
        self._reset_btn.clicked.connect(self.reset_requested.emit)
        footer.addWidget(self._reset_btn, 0)
        inner.addLayout(footer)

        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

    def _add_slider_row(self, grid, row, label_text, lo, hi, val, on_change):
        """Create one uniform label + slider + value row; return (slider, value_label)."""
        label = QLabel(label_text)
        label.setFixedWidth(self.LABEL_W)
        label.setStyleSheet("color: #c8c8c8; font-size: 9pt;")
        grid.addWidget(label, row, 0)

        slider = ClickableSlider(Qt.Horizontal)
        slider.setRange(lo, hi)
        slider.setValue(val)
        slider.setFixedWidth(self.SLIDER_W)
        slider.setFixedHeight(20)
        slider.valueChanged.connect(on_change)
        grid.addWidget(slider, row, 1)

        value = QLabel(str(val))
        value.setFixedWidth(self.VALUE_W)
        value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        value.setStyleSheet("color: #9aa0a6; font-size: 9pt;")
        grid.addWidget(value, row, 2)
        return slider, value

    # ───────────── slider callbacks ─────────────
    def _on_black(self, v):
        self._black_value.setText(str(v))
        self.black_changed.emit(v)

    def _on_white(self, v):
        self._white_value.setText(str(v))
        self.white_changed.emit(v)

    def _on_gamma(self, v):
        self._gamma_value.setText(str(v))
        self.gamma_changed.emit(v)

    def _on_opacity(self, v):
        self._opacity_value.setText(str(v))
        self.opacity_changed.emit(v)

    # ───────────── public API ─────────────
    def set_channel(self, channel):
        """Reflect the active channel selection (no signal emitted)."""
        btn = self._channel_btns.get(channel)
        if btn is not None:
            btn.blockSignals(True)
            btn.setChecked(True)
            btn.blockSignals(False)

    def set_values(self, black, white, gamma):
        """Set the three sliders to the given values (no signals emitted)."""
        for slider, value, v in (
            (self._black_slider, self._black_value, int(black)),
            (self._white_slider, self._white_value, int(white)),
            (self._gamma_slider, self._gamma_value, int(gamma)),
        ):
            slider.blockSignals(True)
            slider.setValue(v)
            slider.blockSignals(False)
            value.setText(str(v))

    def set_opacity(self, opacity):
        """Set the opacity slider (0-100, no signal emitted)."""
        v = int(opacity)
        self._opacity_slider.blockSignals(True)
        self._opacity_slider.setValue(v)
        self._opacity_slider.blockSignals(False)
        self._opacity_value.setText(str(v))

    def set_enabled_state(self, enabled):
        """Reflect the effect's enabled state in the checkbox (no signal)."""
        self._enable_cb.blockSignals(True)
        self._enable_cb.setChecked(bool(enabled))
        self._enable_cb.blockSignals(False)

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
