"""Floating Layers panel.

A small frameless, draggable, always-on-top palette: the layers top to bottom
(tick to show, click to select, double-click to rename), with the selected
layer's blend mode and opacity above them, and the background colour and add /
delete / reorder below.

It is a pure view: the layers live in the main window, which drives this
widget through ``set_layers`` and listens to its signals.
"""

from PySide6.QtWidgets import (QWidget, QHBoxLayout, QVBoxLayout, QLabel, QToolButton,
                               QListWidget, QListWidgetItem, QComboBox, QAbstractItemView,
                               QCheckBox)
from PySide6.QtCore import Qt, Signal

from random_image_viewer.drawing.layers import BLEND_MODES
from random_image_viewer.widgets.clickable_slider import ClickableSlider


class LayerWindow(QWidget):
    layer_selected = Signal(object)
    visibility_changed = Signal(object, bool)
    name_changed = Signal(object, str)
    opacity_changed = Signal(int)
    blend_changed = Signal(str)
    effects_changed = Signal(bool)
    impasto_changed = Signal(object)        # ⛰ settings dict, or None (flat paint)
    add_requested = Signal()
    canvas_requested = Signal()
    import_requested = Signal()             # 📥 a PNG / picture onto this layer
    remove_requested = Signal()
    move_requested = Signal(int)    # +1 up the stack, -1 down
    background_requested = Signal()
    picture_toggled = Signal(bool)          # 🖼 Picture shown / hidden
    under_canvas_toggled = Signal(bool)     # 📄 canvas under the picture on / off
    picture_opacity_changed = Signal(int)
    picture_blend_changed = Signal(str)
    closed = Signal()
    drag_finished = Signal()                # moved by hand (it leaves / rejoins the dock)

    WIDTH = 220
    ROW_H = 24
    MAX_ROWS = 8

    def __init__(self, parent=None):
        super().__init__(
            parent,
            Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint,
        )
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setWindowTitle("Layers")
        self.setFixedWidth(self.WIDTH)

        self._drag_offset = None
        self._layers = []       # bottom to top; the list shows them top first
        self._quiet = False     # suppress signals while rebuilding

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        bg = QWidget(self)
        bg.setObjectName("layerBg")
        bg.setStyleSheet(
            "#layerBg {"
            " background-color: #2b2d30;"
            " border: 1px solid #4a4d50;"
            " border-radius: 8px;"
            "}"
            "QLabel { color: #b7bcc1; }"
        )
        outer.addWidget(bg)

        inner = QVBoxLayout(bg)
        inner.setContentsMargins(10, 6, 10, 10)
        inner.setSpacing(6)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        self._header = QLabel("\U0001F4DA Layers")
        self._header.setCursor(Qt.SizeAllCursor)
        header.addWidget(self._header, 1)
        close_btn = QToolButton()
        close_btn.setText("✕")
        close_btn.setFixedSize(20, 20)
        close_btn.setToolTip("Close")
        close_btn.clicked.connect(self.closed)
        header.addWidget(close_btn)
        inner.addLayout(header)

        self._blend = QComboBox()
        self._blend.addItems(list(BLEND_MODES))
        self._blend.setToolTip("Blend mode: how this layer mixes with what is under it")
        self._blend.currentTextChanged.connect(self._on_blend)
        inner.addWidget(self._blend)

        opacity_row = QHBoxLayout()
        opacity_row.setContentsMargins(0, 0, 0, 0)
        opacity_row.addWidget(QLabel("Opacity"))
        self._opacity = ClickableSlider(Qt.Horizontal)
        self._opacity.setRange(0, 100)
        self._opacity.setToolTip("Opacity of the whole layer")
        self._opacity.valueChanged.connect(self._on_opacity)
        opacity_row.addWidget(self._opacity, 1)
        self._opacity_label = QLabel("100%")
        self._opacity_label.setFixedWidth(36)
        self._opacity_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        opacity_row.addWidget(self._opacity_label)
        inner.addLayout(opacity_row)

        self._effects = QCheckBox("✨ Effects apply to this layer")
        self._effects.setToolTip(
            "Run the view's effects (LUT, contrast, curves, value / colour groups, edges ...) "
            "over this layer's paint too, as if it were part of the picture. Off: the paint "
            "sits on top, untouched. On for new layers")
        self._effects.toggled.connect(self._on_effects)
        inner.addWidget(self._effects)

        # ⛰ Impasto: the layer's paint lit as relief, its sliders under it
        # while it is on.
        self._impasto = QCheckBox("⛰ Impasto (paint relief)")
        self._impasto.setToolTip(
            "Light this layer's paint as thick relief, like Rebelle's bristle impasto: grooves "
            "along the bristles, raised stroke edges, a wet highlight. Worked out on the GPU "
            "and kept, so painting and zooming stay quick")
        self._impasto.toggled.connect(self._on_impasto_toggled)
        inner.addWidget(self._impasto)
        self._impasto_box = QWidget()
        box = QVBoxLayout(self._impasto_box)
        box.setContentsMargins(14, 0, 0, 0)
        box.setSpacing(2)
        self._impasto_sliders = {}
        for key, label, (lo, hi), unit, tip in self._IMPASTO_SLIDERS:
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            name = QLabel(label)
            name.setFixedWidth(54)
            row.addWidget(name)
            slider = ClickableSlider(Qt.Horizontal)
            slider.setRange(lo, hi)
            slider.setToolTip(tip)
            value = QLabel("")
            value.setFixedWidth(36)
            value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            slider.valueChanged.connect(
                lambda v, k=key, u=unit, lab=value: self._on_impasto_slider(k, v, u, lab))
            row.addWidget(slider, 1)
            row.addWidget(value)
            box.addLayout(row)
            self._impasto_sliders[key] = (slider, value, unit)
        self._impasto_box.hide()
        inner.addWidget(self._impasto_box)

        self._list = QListWidget()
        self._list.setSelectionMode(QAbstractItemView.SingleSelection)
        self._list.setEditTriggers(QAbstractItemView.DoubleClicked)
        self._list.setToolTip("Tick to show or hide, click to draw on it, double-click to rename")
        self._list.currentRowChanged.connect(self._on_row)
        self._list.itemChanged.connect(self._on_item)
        inner.addWidget(self._list)

        # Under the layers, fixed: the picture, the canvas under it, and the
        # background at the very bottom - the stack reads top to bottom.
        self._picture = QCheckBox("🖼 Picture")
        self._picture.setToolTip(
            "The picture, under every layer: untick to hide it - the sketch's canvas "
            "under it, or the background, shows instead")
        self._picture.toggled.connect(self._on_picture)
        picture_row = QHBoxLayout()
        picture_row.setContentsMargins(0, 0, 0, 0)
        picture_row.addWidget(self._picture, 1)
        self._picture_blend = QComboBox()
        self._picture_blend.addItems(list(BLEND_MODES))
        self._picture_blend.setFixedWidth(96)
        self._picture_blend.setToolTip(
            "The picture's blend mode: how it mixes with the canvas under it (or the background)")
        self._picture_blend.currentTextChanged.connect(self._on_picture_blend)
        picture_row.addWidget(self._picture_blend)
        inner.addLayout(picture_row)
        picture_opacity_row = QHBoxLayout()
        picture_opacity_row.setContentsMargins(0, 0, 0, 0)
        picture_opacity_row.addWidget(QLabel("   Opacity"))
        self._picture_opacity = ClickableSlider(Qt.Horizontal)
        self._picture_opacity.setRange(0, 100)
        self._picture_opacity.setToolTip(
            "Opacity of the picture: lower it to see the canvas under it (or the background) through")
        self._picture_opacity.valueChanged.connect(self._on_picture_opacity)
        picture_opacity_row.addWidget(self._picture_opacity, 1)
        self._picture_opacity_label = QLabel("100%")
        self._picture_opacity_label.setFixedWidth(36)
        self._picture_opacity_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        picture_opacity_row.addWidget(self._picture_opacity_label)
        inner.addLayout(picture_opacity_row)
        self._under = QCheckBox("📄 Canvas")
        self._under.setToolTip(
            "The sketch's middle grey sheet under the picture, the size of it (made by "
            "New sketch): it shows where the picture is while the picture is hidden - "
            "tick 🖼 Picture to see the picture under your paint again")
        self._under.hide()      # no row until a new sketch makes the canvas
        self._under.toggled.connect(self._on_under)
        inner.addWidget(self._under)

        # Layer 0: always at the bottom, so it is a fixed row and not in the list
        background_row = QHBoxLayout()
        background_row.setContentsMargins(0, 0, 0, 0)
        self._background = QToolButton()
        self._background.setFixedSize(24, 16)
        self._background.setToolTip("Background colour, around the picture and behind it")
        self._background.clicked.connect(self.background_requested)
        background_row.addWidget(self._background)
        background_row.addWidget(QLabel("Background"), 1)
        inner.addLayout(background_row)

        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(4)
        self._add_btn = self._tool_button("+", "New layer above this one", self.add_requested.emit)
        self._canvas_btn = self._tool_button(
            "📄", "New canvas layer above this one: a sheet of paper over the picture to sketch "
                  "on. Untick it to see the picture again; Opacity makes it tracing paper; the "
                  "eraser and 🕘 History cut through it; ✨ decides whether effects reach it",
            self.canvas_requested.emit)
        self._import_btn = self._tool_button(
            "📥", "Import a PNG (or other picture) onto this layer, transparency kept. It lands "
                  "in the middle, ⬚ selected: drag to move, a corner to scale, outside to turn, "
                  "Enter applies. Ctrl+Z takes it away",
            self.import_requested.emit)
        self._remove_btn = self._tool_button(
            "−", "Delete this layer and its marks (no undo)", self.remove_requested.emit)
        self._up_btn = self._tool_button("▲", "Move up", lambda: self.move_requested.emit(1))
        self._down_btn = self._tool_button("▼", "Move down", lambda: self.move_requested.emit(-1))
        for btn in (self._add_btn, self._canvas_btn, self._import_btn, self._remove_btn,
                    self._up_btn, self._down_btn):
            buttons.addWidget(btn)
        buttons.addStretch(1)
        inner.addLayout(buttons)

    @staticmethod
    def _tool_button(text, tip, slot):
        btn = QToolButton()
        btn.setText(text)
        btn.setToolTip(tip)
        btn.setFixedSize(24, 24)
        btn.clicked.connect(lambda: slot())
        return btn

    def set_layers(self, layers, active):
        """Show *layers* (bottom to top) with *active* selected."""
        self._quiet = True
        self._layers = list(layers)
        self._list.clear()
        for layer in reversed(self._layers):
            item = QListWidgetItem(layer.name)
            item.setFlags(item.flags() | Qt.ItemIsEditable | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if layer.visible else Qt.Unchecked)
            self._list.addItem(item)
        position = self._layers.index(active)
        self._list.setCurrentRow(len(self._layers) - 1 - position)
        self._list.setFixedHeight(
            self.ROW_H * min(self.MAX_ROWS, max(3, len(self._layers))) + 6)
        self._blend.setCurrentText(active.blend)
        self._opacity.setValue(active.opacity)
        self._opacity_label.setText(f"{active.opacity}%")
        self._effects.setChecked(active.effects)
        self._show_impasto(getattr(active, 'impasto', None))
        self._up_btn.setEnabled(position < len(self._layers) - 1)
        self._down_btn.setEnabled(position > 0)
        self._quiet = False
        self.adjustSize()

    @property
    def background_button(self):
        return self._background

    def set_background(self, color):
        self._background.setStyleSheet(
            f"QToolButton {{ background-color: {color.name()}; border: 1px solid #666; }}")

    def set_picture(self, visible, under_canvas, opacity=100, blend='Normal'):
        """Show the picture layer - its tick, opacity and blend - and the
        canvas under it: its tick, or no row at all while there is none
        (*under_canvas* None)."""
        self._quiet = True
        self._picture.setChecked(bool(visible))
        self._picture_blend.setCurrentText(blend)
        self._picture_opacity.setValue(int(opacity))
        self._picture_opacity_label.setText(f"{int(opacity)}%")
        self._under.setVisible(under_canvas is not None)
        self._under.setChecked(bool(under_canvas))
        self._quiet = False
        self.adjustSize()

    def _on_picture(self, on):
        if not self._quiet:
            self.picture_toggled.emit(on)

    def _on_picture_opacity(self, value):
        self._picture_opacity_label.setText(f"{value}%")
        if not self._quiet:
            self.picture_opacity_changed.emit(value)

    def _on_picture_blend(self, mode):
        if not self._quiet:
            self.picture_blend_changed.emit(mode)

    def _on_under(self, on):
        if not self._quiet:
            self.under_canvas_toggled.emit(on)

    def _layer_at(self, row):
        return self._layers[len(self._layers) - 1 - row]

    def _on_row(self, row):
        if not self._quiet and row >= 0:
            self.layer_selected.emit(self._layer_at(row))

    def _on_item(self, item):
        if self._quiet:
            return
        layer = self._layer_at(self._list.row(item))
        visible = item.checkState() == Qt.Checked
        if visible != layer.visible:
            self.visibility_changed.emit(layer, visible)
        name = item.text().strip()
        if name and name != layer.name:
            self.name_changed.emit(layer, name)

    def _on_opacity(self, value):
        self._opacity_label.setText(f"{value}%")
        if not self._quiet:
            self.opacity_changed.emit(value)

    def _on_blend(self, mode):
        if not self._quiet:
            self.blend_changed.emit(mode)

    def _on_effects(self, on):
        if not self._quiet:
            self.effects_changed.emit(on)

    # ⛰ impasto
    _IMPASTO_SLIDERS = (
        ('depth', 'Depth', (0, 100), '%', "How high the paint stands: the strength of all the relief"),
        ('grooves', 'Grooves', (0, 100), '%',
         "Bristle grooves: relief from the fine streaks in the paint (the bristles' marks, "
         "the colour each one carried)"),
        ('edges', 'Edges', (0, 100), '%',
         "A raised rim where the paint was pushed to the stroke's edge"),
        ('edge_width', 'Edge px', (1, 40), 'px', "How wide that rim is, in picture pixels"),
        ('light', 'Light', (0, 359), '°',
         "Where the light comes from: 0° right, 90° below, 180° left, 270° above "
         "(225°: top left)"),
        ('gloss', 'Gloss', (0, 100), '%', "Wet highlight on the ridges facing the light"),
    )

    def _impasto_values(self):
        return {key: slider.value() for key, (slider, _v, _u) in self._impasto_sliders.items()}

    def _show_impasto(self, settings):
        from random_image_viewer.drawing.impasto import DEFAULTS
        values = dict(DEFAULTS, **(settings or {}))
        self._impasto.setChecked(bool(settings))
        for key, (slider, value, unit) in self._impasto_sliders.items():
            slider.setValue(int(values[key]))
            value.setText(f"{int(values[key])}{unit}")
        if self._impasto_box.isVisibleTo(self) != bool(settings):
            self._impasto_box.setVisible(bool(settings))

    def _on_impasto_toggled(self, on):
        if self._impasto_box.isVisibleTo(self) != on:
            self._impasto_box.setVisible(on)
            self.adjustSize()
        if not self._quiet:
            self.impasto_changed.emit(self._impasto_values() if on else None)

    def _on_impasto_slider(self, key, value, unit, label):
        label.setText(f"{value}{unit}")
        if not self._quiet and self._impasto.isChecked():
            self.impasto_changed.emit(self._impasto_values())

    # ── drag by the header ────────────────────────────────────────

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            self._dragged = False
            event.accept()

    def mouseMoveEvent(self, event):
        if self._drag_offset is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            self._dragged = True
            event.accept()

    def mouseReleaseEvent(self, event):
        self._drag_offset = None
        if getattr(self, '_dragged', False):
            self._dragged = False
            self.drag_finished.emit()
