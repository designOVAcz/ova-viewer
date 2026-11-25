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
                               QCheckBox, QStyledItemDelegate, QStyle)
from PySide6.QtCore import Qt, Signal, QEvent, QRectF, QPointF, QSize
from PySide6.QtGui import QPainter, QColor, QPen, QPainterPath

from random_image_viewer.drawing.layers import BLEND_MODES
from random_image_viewer.widgets.clickable_slider import ClickableSlider
from random_image_viewer.widgets.title_drag import on_title_row

# 👁 Visibility as an eye (as in Photoshop, Krita ...), not a tick box.
EYE_ON = QColor(214, 219, 224)
EYE_OFF = QColor(110, 115, 121)
TEXT_ON = QColor(228, 231, 234)
TEXT_OFF = QColor(125, 130, 136)
ROW_SELECTED = QColor(58, 96, 150)
ROW_HOVER = QColor(255, 255, 255, 16)


def draw_eye(p, rect, shown):
    """An eye in *rect* (QRectF): open when *shown*, else dimmed and struck through."""
    p.save()
    p.setRenderHint(QPainter.Antialiasing, True)
    colour = EYE_ON if shown else EYE_OFF
    pen = QPen(colour, 1.4)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    c = rect.center()
    w, h = min(rect.width(), 16.0), min(rect.height(), 16.0) * 0.62
    left, right = QPointF(c.x() - w / 2, c.y()), QPointF(c.x() + w / 2, c.y())
    lid = QPainterPath(left)
    lid.quadTo(QPointF(c.x(), c.y() - h), right)
    lid.quadTo(QPointF(c.x(), c.y() + h), left)
    p.drawPath(lid)
    if shown:
        p.setPen(Qt.NoPen)
        p.setBrush(colour)
        p.drawEllipse(c, 2.4, 2.4)
    else:
        p.drawLine(QPointF(c.x() - w / 2 + 1, c.y() + h * 0.9), QPointF(c.x() + w / 2 - 1, c.y() - h * 0.9))
    p.restore()


class _LayerRowDelegate(QStyledItemDelegate):
    """A layer row: 👁 eye (click it: show / hide), the layer's colour dot,
    its name - taller than a plain list row, a rounded highlight when chosen."""

    EYE_W = 26          # the eye's column (clicks there toggle, never select)

    def __init__(self, row_h, parent=None):
        super().__init__(parent)
        self.row_h = row_h

    def sizeHint(self, option, index):
        return QSize(max(1, option.rect.width()), self.row_h)

    @staticmethod
    def _shown(index):
        state = index.data(Qt.CheckStateRole)
        try:
            return Qt.CheckState(state) == Qt.Checked
        except (TypeError, ValueError):
            return state == Qt.Checked

    def _eye_rect(self, rect):
        return QRectF(rect.x() + 4, rect.y(), self.EYE_W - 4, rect.height())

    def paint(self, p, option, index):
        p.save()
        p.setRenderHint(QPainter.Antialiasing, True)
        r = QRectF(option.rect).adjusted(2, 1.5, -2, -1.5)
        if option.state & QStyle.State_Selected:
            p.setPen(Qt.NoPen)
            p.setBrush(ROW_SELECTED)
            p.drawRoundedRect(r, 5, 5)
        elif option.state & QStyle.State_MouseOver:
            p.setPen(Qt.NoPen)
            p.setBrush(ROW_HOVER)
            p.drawRoundedRect(r, 5, 5)
        shown = self._shown(index)
        draw_eye(p, self._eye_rect(QRectF(option.rect)), shown)
        x = option.rect.x() + self.EYE_W + 4
        icon = index.data(Qt.DecorationRole)
        if icon is not None and not icon.isNull():
            d = 10
            top = option.rect.y() + (option.rect.height() - d) // 2
            if not shown:
                p.setOpacity(0.45)
            icon.paint(p, x, top, d, d)
            p.setOpacity(1.0)
            x += d + 8
        p.setPen(TEXT_ON if shown else TEXT_OFF)
        font = option.font
        p.setFont(font)
        text_rect = option.rect.adjusted(x - option.rect.x(), 0, -6, 0)
        name = option.fontMetrics.elidedText(index.data(Qt.DisplayRole) or "", Qt.ElideRight,
                                             text_rect.width())
        p.drawText(text_rect, Qt.AlignVCenter | Qt.AlignLeft, name)
        p.restore()

    def editorEvent(self, event, model, option, index):
        # The eye: a click shows / hides the layer, without choosing it.
        kind = event.type()
        if kind in (QEvent.MouseButtonPress, QEvent.MouseButtonRelease, QEvent.MouseButtonDblClick):
            if (event.button() == Qt.LeftButton
                    and self._eye_rect(QRectF(option.rect)).contains(event.position())):
                if kind == QEvent.MouseButtonRelease:
                    model.setData(index, Qt.Unchecked if self._shown(index) else Qt.Checked,
                                  Qt.CheckStateRole)
                return True
        return super().editorEvent(event, model, option, index)

    def updateEditorGeometry(self, editor, option, index):
        # renaming: the name's own place, the eye and dot left as they are
        editor.setGeometry(option.rect.adjusted(self.EYE_W + 20, 3, -4, -3))


class EyeCheck(QCheckBox):
    """A show / hide toggle drawn as the layer rows' 👁 eye and its label."""

    def sizeHint(self):
        fm = self.fontMetrics()
        return QSize(26 + fm.horizontalAdvance(self.text()) + 4, max(24, fm.height() + 8))

    def minimumSizeHint(self):
        return QSize(26, 24)

    def hitButton(self, pos):
        return self.rect().contains(pos)

    def paintEvent(self, event):
        p = QPainter(self)
        shown = self.isChecked()
        draw_eye(p, QRectF(4, 0, 22, self.height()), shown)
        p.setPen(TEXT_ON if shown else TEXT_OFF)
        p.drawText(self.rect().adjusted(30, 0, 0, 0), Qt.AlignVCenter | Qt.AlignLeft, self.text())
        p.end()


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
    edges_requested = Signal()              # ⌇ the picture's edges, on transparency, onto a new layer
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
    ROW_H = 30
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
        # Always exactly as tall as what it holds: when the ⛰ sliders went
        # away it stayed tall a moment, and the spare height stretched the
        # rows above the list - the list jumped down.
        from PySide6.QtWidgets import QLayout
        outer.setSizeConstraint(QLayout.SetFixedSize)

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
        self._header.setToolTip("Drag here to move the panel")
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
        # (Placed at the bottom, under the buttons - see below.)

        self._list = QListWidget()
        self._list.setSelectionMode(QAbstractItemView.SingleSelection)
        self._list.setEditTriggers(QAbstractItemView.DoubleClicked)
        self._list.setToolTip("👁 Click the eye to show or hide, the row to draw on it, "
                              "double-click to rename")
        self._list.setItemDelegate(_LayerRowDelegate(self.ROW_H, self._list))
        self._list.setUniformItemSizes(True)
        self._list.setMouseTracking(True)
        self._list.viewport().setAttribute(Qt.WA_Hover, True)
        self._list.setStyleSheet(
            "QListWidget { background: #232528; border: 1px solid #3a3d41; border-radius: 6px;"
            " padding: 2px; outline: 0; }")
        self._list.currentRowChanged.connect(self._on_row)
        self._list.itemChanged.connect(self._on_item)
        inner.addWidget(self._list)

        # Under the layers, fixed: the picture, the canvas under it, and the
        # background at the very bottom - the stack reads top to bottom.
        self._picture = EyeCheck("🖼 Picture")
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
        self._under = EyeCheck("📄 Canvas")
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
        self._edges_btn = self._tool_button(
            "⌇", "Edges of the picture onto a new layer - just the lines, on transparency. In ◧ "
                 "Studio it lies over the canvas on the left, matched to the picture: set its "
                 "blend (Screen, Difference ...) and opacity to check your painting against the "
                 "reference - the picture on the right stays as it is. Sensitivity and colour "
                 "as the ✨ Edges effect's",
            self.edges_requested.emit)
        self._remove_btn = self._tool_button(
            "−", "Delete this layer and its marks (no undo)", self.remove_requested.emit)
        self._up_btn = self._tool_button("▲", "Move up", lambda: self.move_requested.emit(1))
        self._down_btn = self._tool_button("▼", "Move down", lambda: self.move_requested.emit(-1))
        for btn in (self._add_btn, self._canvas_btn, self._import_btn, self._edges_btn,
                    self._remove_btn, self._up_btn, self._down_btn):
            buttons.addWidget(btn)
        buttons.addStretch(1)
        inner.addLayout(buttons)

        # ⛰ Impasto at the very bottom: its sliders come and go with the
        # layer chosen, and above the list they made the list jump each time.
        rule = QWidget()
        rule.setFixedHeight(1)
        rule.setStyleSheet("background: #4a4d50;")
        inner.addWidget(rule)
        inner.addWidget(self._impasto)
        inner.addWidget(self._impasto_box)

    @staticmethod
    def _dot(colour):
        from PySide6.QtGui import QIcon, QPainter, QPixmap
        pix = QPixmap(12, 12)
        pix.fill(Qt.transparent)
        p = QPainter(pix)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        p.setBrush(colour)
        p.drawEllipse(1, 1, 10, 10)
        p.end()
        return QIcon(pix)

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
            tag = getattr(layer, 'tag', None)
            if tag is not None:
                item.setIcon(self._dot(tag))     # the same dot as on the ✎ badge
            item.setFlags(item.flags() | Qt.ItemIsEditable | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if layer.visible else Qt.Unchecked)
            self._list.addItem(item)
        position = self._layers.index(active)
        self._list.setCurrentRow(len(self._layers) - 1 - position)
        self._list.setFixedHeight(
            self.ROW_H * min(self.MAX_ROWS, max(3, len(self._layers))) + 8)
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
            # by the title row alone: a slider just missed moved the whole panel
            self._drag_offset = (event.globalPosition().toPoint() - self.frameGeometry().topLeft()
                                 if on_title_row(self, self._header, event) else None)
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
