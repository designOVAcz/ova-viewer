"""Generic floating effect-settings window.

A small frameless, draggable, always-on-top panel that hosts one effect's
controls: an Enable checkbox, an optional segmented mode selector, a stack of
uniform sliders (including the effect's Opacity) and a Reset button. It is a
pure view — all image processing stays in the main window, which this widget
drives via signals.

Modelled on :class:`~random_image_viewer.widgets.curves_window.CurvesWindow`
so every effect panel looks and behaves the same, but built from a spec so a
new effect only needs a list of slider definitions.

Signals:
    enable_toggled(bool)     — Enable checkbox changed
    value_changed(str, int)  — a slider moved: (control key, value)
    choice_changed(str)      — segmented mode selector changed
    reset_requested()        — Reset button clicked
    closed()                 — user clicked the ✕ close button
"""

from PySide6.QtWidgets import (QWidget, QHBoxLayout, QVBoxLayout, QGridLayout,
                               QLabel, QToolButton, QCheckBox, QButtonGroup,
                               QSizePolicy)
from PySide6.QtCore import Qt, Signal

from random_image_viewer.widgets.clickable_slider import ClickableSlider


class EffectWindow(QWidget):
    enable_toggled = Signal(bool)
    value_changed = Signal(str, int)
    choice_changed = Signal(str)
    reset_requested = Signal()
    closed = Signal()

    SLIDER_W = 190   # shared slider width so every row lines up
    LABEL_W = 58     # left label column width
    VALUE_W = 34     # right value column width

    def __init__(self, title, controls, choices=None, reset_tip=None, parent=None):
        """Build a panel for one effect.

        ``controls`` is a list of ``(key, label, lo, hi, default, tooltip)``
        slider definitions. ``choices`` is an optional
        ``(current, [(key, text, tooltip), ...])`` segmented selector shown
        beside the Enable checkbox.
        """
        super().__init__(
            parent,
            Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint,
        )
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setWindowTitle(title)

        self._drag_offset = None
        self._sliders = {}
        self._values = {}
        self._choice_btns = {}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._bg = QWidget(self)
        self._bg.setObjectName("effectBg")
        self._bg.setStyleSheet(
            "#effectBg {"
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

        self._header = QLabel(title)
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
        self._close_btn.setToolTip(f"Close {title} panel")
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

        # ── Enable (+ optional mode selector) ──
        top_row = QHBoxLayout()
        top_row.setContentsMargins(0, 0, 0, 0)
        top_row.setSpacing(6)

        self._enable_cb = QCheckBox("Enable")
        self._enable_cb.setStyleSheet(
            "QCheckBox { color: #d0d0d0; font-size: 9pt; }")
        self._enable_cb.toggled.connect(self.enable_toggled.emit)
        top_row.addWidget(self._enable_cb, 0)
        top_row.addStretch(1)

        if choices:
            current, options = choices
            self._choice_group = QButtonGroup(self)
            self._choice_group.setExclusive(True)
            for key, text, tip in options:
                b = QToolButton()
                b.setText(text)
                b.setCheckable(True)
                b.setFixedHeight(22)
                b.setToolTip(tip)
                b.setStyleSheet(
                    "QToolButton { color: #dddddd; background: #34363a;"
                    " border: 1px solid #4a4d50; border-radius: 3px;"
                    " font-size: 9pt; font-weight: bold; padding: 0px 6px; }"
                    "QToolButton:checked { background: #4a4d50;"
                    " border: 1px solid #7aa2ff; }"
                )
                b.clicked.connect(
                    lambda _checked=False, k=key: self.choice_changed.emit(k))
                self._choice_group.addButton(b)
                self._choice_btns[key] = b
                top_row.addWidget(b, 0)
            if current in self._choice_btns:
                self._choice_btns[current].setChecked(True)
        inner.addLayout(top_row)

        # ── Sliders (uniform widths) ──
        grid = QGridLayout()
        grid.setContentsMargins(0, 2, 0, 2)
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(6)
        for row, (key, label, lo, hi, default, tip) in enumerate(controls):
            self._add_slider_row(grid, row, key, label, lo, hi, default, tip)
        inner.addLayout(grid)

        # ── Footer: Reset ──
        footer = QHBoxLayout()
        footer.setContentsMargins(0, 0, 0, 0)
        footer.setSpacing(4)
        footer.addStretch(1)
        self._reset_btn = QToolButton()
        self._reset_btn.setText("↺ Reset")
        self._reset_btn.setToolTip(reset_tip or "Reset this effect to its defaults")
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

    def _add_slider_row(self, grid, row, key, label_text, lo, hi, val, tip):
        """Create one uniform label + slider + value row."""
        label = QLabel(label_text)
        label.setFixedWidth(self.LABEL_W)
        label.setStyleSheet("color: #c8c8c8; font-size: 9pt;")
        if tip:
            label.setToolTip(tip)
        grid.addWidget(label, row, 0)

        slider = ClickableSlider(Qt.Horizontal)
        slider.setRange(lo, hi)
        slider.setValue(val)
        slider.setFixedWidth(self.SLIDER_W)
        slider.setFixedHeight(20)
        if tip:
            slider.setToolTip(tip)
        slider.valueChanged.connect(
            lambda v, k=key: self._on_slider(k, v))
        grid.addWidget(slider, row, 1)

        value = QLabel(str(val))
        value.setFixedWidth(self.VALUE_W)
        value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        value.setStyleSheet("color: #9aa0a6; font-size: 9pt;")
        grid.addWidget(value, row, 2)

        self._sliders[key] = slider
        self._values[key] = value

    def _on_slider(self, key, value):
        label = self._values.get(key)
        if label is not None:
            label.setText(str(value))
        self.value_changed.emit(key, value)

    # ───────────── public API ─────────────
    def set_values(self, values):
        """Set sliders from a ``{key: value}`` mapping (no signals emitted)."""
        for key, v in values.items():
            slider = self._sliders.get(key)
            if slider is None:
                continue
            v = int(v)
            slider.blockSignals(True)
            slider.setValue(v)
            slider.blockSignals(False)
            self._values[key].setText(str(slider.value()))

    def set_choice(self, key):
        """Reflect the active mode selection (no signal emitted)."""
        btn = self._choice_btns.get(key)
        if btn is not None:
            btn.blockSignals(True)
            btn.setChecked(True)
            btn.blockSignals(False)

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
