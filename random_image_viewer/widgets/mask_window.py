"""🎭 Colour mask: its palette (see viewer/mask_tool and drawing/color_mask).
A pure view - the main window keeps the mask.

Signals:
    source_changed(str)         'picture' | 'layer'
    seed_selected(int)          a colour chip clicked: the sliders below edit it
    seed_removed(int)           a chip's ✕ (right-click)
    seed_flipped(int)           a chip's + / - swapped (right-click)
    threshold_changed(int)      the chosen colour's threshold
    contiguous_toggled(bool)    the chosen colour: touching the click only
    grow_changed(int)           px, the whole mask (negative shrinks)
    feather_changed(int)        px, the whole mask
    invert_toggled(bool)
    preview_changed(str)        'tint' | 'checker'
    action(str)                 'remove' | 'cut' | 'copy' | 'clear'
    closed()                    ✕
"""

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel,
                               QToolButton, QMenu, QCheckBox)

from random_image_viewer.widgets.clickable_slider import ClickableSlider
from random_image_viewer.widgets.title_drag import on_title_row

_TEXT = "QLabel { color: #b7bcc1; font-size: 9pt; background: transparent; }"
_BUTTON = ("QToolButton { color: #d0d0d0; font-size: 9pt; padding: 3px 8px; border: 1px solid #4a4d50;"
           " border-radius: 4px; background: #232528; min-height: 0; }"
           "QToolButton:hover { border-color: #7aa2ff; }"
           "QToolButton:checked { color: #fff; background: #2f3a48; border-color: #7aa2ff; }"
           "QToolButton:disabled { color: #6b6f74; border-color: #3a3d41; }")
_CHECK = "QCheckBox { color: #b7bcc1; font-size: 9pt; background: transparent; }"


class MaskWindow(QWidget):
    source_changed = Signal(str)
    seed_selected = Signal(int)
    seed_removed = Signal(int)
    seed_flipped = Signal(int)
    threshold_changed = Signal(int)
    contiguous_toggled = Signal(bool)
    grow_changed = Signal(int)
    feather_changed = Signal(int)
    invert_toggled = Signal(bool)
    preview_changed = Signal(str)
    action = Signal(str)
    closed = Signal()

    CHIPS_PER_ROW = 8

    def __init__(self, parent=None):
        super().__init__(parent, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setWindowTitle("Colour mask")
        self._drag_offset = None
        self._chips = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        bg = QWidget(self)
        bg.setObjectName("maskBg")
        bg.setStyleSheet("#maskBg { background-color: #2b2d30; border: 1px solid #4a4d50;"
                         " border-radius: 8px; }")
        outer.addWidget(bg)
        inner = QVBoxLayout(bg)
        inner.setContentsMargins(10, 6, 10, 10)
        inner.setSpacing(6)

        header = QHBoxLayout()
        self._header = QLabel("🎭 Colour mask")
        self._header.setStyleSheet("QLabel { color: #c8c8c8; font-size: 10pt; font-weight: bold;"
                                   " padding: 2px 4px; background: transparent; }")
        self._header.setCursor(Qt.SizeAllCursor)
        self._header.setToolTip("Drag to move")
        header.addWidget(self._header, 1)
        close = QToolButton()
        close.setText("✕")
        close.setFixedSize(18, 18)
        close.setToolTip("Close (W) - the mask goes")
        close.setStyleSheet(
            "QToolButton { color: #c8c8c8; background: transparent; border: none;"
            " font-weight: bold; font-size: 10pt; padding: 0px; }"
            "QToolButton:hover { color: #fff; background: #c54040; border-radius: 3px; }")
        close.clicked.connect(self._close)
        header.addWidget(close)
        inner.addLayout(header)

        # From what: the picture, or the layer drawn on
        row = QHBoxLayout()
        row.setSpacing(4)
        label = QLabel("From")
        label.setStyleSheet(_TEXT)
        row.addWidget(label)
        self._src = {}
        for key, text, tip in (('picture', "🖼 Picture", "Mask the picture's colours"),
                               ('layer', "✎ Layer", "Mask the colours of the layer drawn on (📚)")):
            b = QToolButton()
            b.setText(text)
            b.setCheckable(True)
            b.setToolTip(tip)
            b.setStyleSheet(_BUTTON)
            b.clicked.connect(lambda _c=False, k=key: self._pick_source(k))
            row.addWidget(b)
            self._src[key] = b
        row.addStretch(1)
        inner.addLayout(row)

        hint = QLabel("Click: a colour · Shift+click: add one · Alt+click: take one away\n"
                      "Drag sideways after the click: its threshold")
        hint.setStyleSheet("QLabel { color: #8d9298; font-size: 8pt; background: transparent; }")
        inner.addWidget(hint)

        self._chip_grid = QGridLayout()
        self._chip_grid.setSpacing(4)
        self._chip_grid.setAlignment(Qt.AlignLeft)
        inner.addLayout(self._chip_grid)
        self._no_chips = QLabel("No colour picked yet")
        self._no_chips.setStyleSheet("QLabel { color: #6b6f74; font-size: 8pt; background: transparent; }")
        inner.addWidget(self._no_chips)

        # The chosen colour's own
        self._threshold, self._threshold_value = self._slider(inner, "Threshold", 0, 100,
                                                              "How far a colour may be from the one picked"
                                                              " (CIELAB distance) and still be in",
                                                              self.threshold_changed)
        self._contiguous = QCheckBox("Contiguous - only what touches the click")
        self._contiguous.setStyleSheet(_CHECK)
        self._contiguous.toggled.connect(self.contiguous_toggled.emit)
        inner.addWidget(self._contiguous)

        sep = QWidget()
        sep.setFixedHeight(1)
        sep.setStyleSheet("background: #3c3f43;")
        inner.addWidget(sep)

        # The whole mask
        self._grow, self._grow_value = self._slider(inner, "Grow", -30, 30,
                                                    "The whole mask bigger (or smaller, below 0) by so many px"
                                                    " of the picture", self.grow_changed, unit=" px")
        self._feather, self._feather_value = self._slider(inner, "Feather", 0, 60,
                                                          "A soft edge round the whole mask, so many px wide",
                                                          self.feather_changed, unit=" px")
        row = QHBoxLayout()
        self._invert = QCheckBox("Invert")
        self._invert.setStyleSheet(_CHECK)
        self._invert.setToolTip("Everything but the colours picked: pick the background, invert,"
                                " and the subject is in the mask")
        self._invert.toggled.connect(self.invert_toggled.emit)
        row.addWidget(self._invert)
        row.addStretch(1)
        label = QLabel("Show")
        label.setStyleSheet(_TEXT)
        row.addWidget(label)
        self._preview = {}
        for key, text, tip in (('tint', "Red", "The mask in red over the picture - what is selected"),
                               ('checker', "Removed", "The mask as a grey checkerboard: what ⌫ Remove "
                                                      "would take away")):
            b = QToolButton()
            b.setText(text)
            b.setCheckable(True)
            b.setToolTip(tip)
            b.setStyleSheet(_BUTTON)
            b.clicked.connect(lambda _c=False, k=key: self._pick_preview(k))
            row.addWidget(b)
            self._preview[key] = b
        inner.addLayout(row)

        row = QHBoxLayout()
        row.setSpacing(4)
        self._actions = {}
        for key, text, tip in (
                ('remove', "⌫ Remove", "Take what is in the mask away. From the picture: a cut-out layer"
                                       " of the rest is made and the picture hidden (show it again in 📚)"),
                ('cut', "✂ Cut to layer", "What is in the mask onto a new layer of its own - and taken away"
                                          " where it was"),
                ('copy', "⧉ Copy to layer", "What is in the mask onto a new layer; where it was stays as it is"),
                ('clear', "Clear", "Every colour picked goes - a new mask")):
            b = QToolButton()
            b.setText(text)
            b.setToolTip(tip)
            b.setStyleSheet(_BUTTON)
            b.clicked.connect(lambda _c=False, k=key: self.action.emit(k))
            row.addWidget(b)
            self._actions[key] = b
        inner.addLayout(row)

    def _slider(self, layout, name, lo, hi, tip, signal, unit=""):
        row = QHBoxLayout()
        label = QLabel(name)
        label.setStyleSheet(_TEXT)
        label.setFixedWidth(62)
        row.addWidget(label)
        slider = ClickableSlider(Qt.Horizontal)
        slider.setRange(lo, hi)
        slider.setToolTip(tip)
        value = QLabel("")
        value.setFixedWidth(42)
        value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        value.setStyleSheet(_TEXT)

        def changed(v):
            value.setText(f"{v}{unit}")
            signal.emit(v)
        slider.valueChanged.connect(changed)
        row.addWidget(slider, 1)
        row.addWidget(value)
        layout.addLayout(row)
        slider._unit = unit
        return slider, value

    # -- from the main window ------------------------------------------------

    def set_state(self, state):
        """*state*: {'source', 'seeds': [(rgb, op)], 'chosen', 'threshold',
        'contiguous', 'grow', 'feather', 'invert', 'preview', 'has_mask',
        'can_layer'}."""
        for key, b in self._src.items():
            b.setChecked(key == state['source'])
        self._src['layer'].setEnabled(state.get('can_layer', True))
        for key, b in self._preview.items():
            b.setChecked(key == state['preview'])
        self._set_chips(state['seeds'], state['chosen'])
        chosen = state['chosen'] is not None
        for slider, label, v in ((self._threshold, self._threshold_value, state['threshold']),
                                 (self._grow, self._grow_value, state['grow']),
                                 (self._feather, self._feather_value, state['feather'])):
            slider.blockSignals(True)
            slider.setValue(int(v))
            slider.blockSignals(False)
            label.setText(f"{int(v)}{slider._unit}")
        for box, v in ((self._contiguous, state['contiguous']), (self._invert, state['invert'])):
            box.blockSignals(True)
            box.setChecked(bool(v))
            box.blockSignals(False)
        self._threshold.setEnabled(chosen)
        self._contiguous.setEnabled(chosen)
        for key in ('remove', 'cut', 'copy'):
            self._actions[key].setEnabled(bool(state['has_mask']))
        self._actions['clear'].setEnabled(bool(state['seeds']))

    def _set_chips(self, seeds, chosen):
        for chip in self._chips:
            self._chip_grid.removeWidget(chip)
            chip.hide()                 # (gone now, not when the event loop gets to it)
            chip.deleteLater()
        self._chips = []
        for i, (rgb, op) in enumerate(seeds):
            chip = QToolButton()
            chip.setFixedSize(26, 22)
            chip.setText("−" if op == '-' else "+")
            r, g, b = rgb
            ink = "#000" if (0.299 * r + 0.587 * g + 0.114 * b) > 140 else "#fff"
            ring = "#ffffff" if i == chosen else "#3c3f43"
            chip.setStyleSheet(
                f"QToolButton {{ background: {QColor(r, g, b).name()}; color: {ink}; font-weight: bold;"
                f" border: 2px solid {ring}; border-radius: 4px; padding: 0; min-height: 0; }}")
            chip.setToolTip(("Takes away" if op == '-' else "Adds") +
                            f" rgb({r}, {g}, {b}) - click to set its threshold, right-click for more")
            chip.clicked.connect(lambda _c=False, n=i: self.seed_selected.emit(n))
            chip.setContextMenuPolicy(Qt.CustomContextMenu)
            chip.customContextMenuRequested.connect(lambda _p, n=i, c=chip: self._chip_menu(n, c))
            self._chip_grid.addWidget(chip, i // self.CHIPS_PER_ROW, i % self.CHIPS_PER_ROW)
            self._chips.append(chip)
        self._no_chips.setVisible(not seeds)
        self.adjustSize()

    def _chip_menu(self, n, chip):
        menu = QMenu(self)
        flip = menu.addAction("Add instead / take away instead")
        drop = menu.addAction("✕ Remove this colour")
        got = menu.exec(chip.mapToGlobal(chip.rect().bottomLeft()))
        if got is flip:
            self.seed_flipped.emit(n)
        elif got is drop:
            self.seed_removed.emit(n)

    def _pick_source(self, key):
        for k, b in self._src.items():
            b.setChecked(k == key)
        self.source_changed.emit(key)

    def _pick_preview(self, key):
        for k, b in self._preview.items():
            b.setChecked(k == key)
        self.preview_changed.emit(key)

    def _close(self):
        self.hide()
        self.closed.emit()

    # -- moved by the title row ----------------------------------------------

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_offset = (event.globalPosition().toPoint() - self.frameGeometry().topLeft()
                                 if on_title_row(self, self._header, event) else None)
            event.accept()

    def mouseMoveEvent(self, event):
        if self._drag_offset is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()

    def mouseReleaseEvent(self, event):
        self._drag_offset = None
