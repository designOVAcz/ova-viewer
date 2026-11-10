"""Floating Brush panel.

The brush tips (Round, then the imported PNG textures) as a grid of swatches,
with how the textured tip is laid down below: spacing, rotation, pressure and
colour jitter. Same frameless, draggable palette as the Layers panel.

A pure view: the settings live in the main window, which fills this widget
through ``set_textures`` / ``set_settings`` and listens to its signals.
"""

from PySide6.QtWidgets import (QWidget, QHBoxLayout, QVBoxLayout, QLabel, QToolButton,
                               QListWidget, QListWidgetItem, QComboBox, QCheckBox,
                               QAbstractItemView, QListView)
from PySide6.QtCore import Qt, Signal, QSize
from PySide6.QtGui import QIcon, QPixmap, QPainter, QColor

from random_image_viewer.widgets.clickable_slider import ClickableSlider

SWATCH = 40


class BrushWindow(QWidget):
    texture_chosen = Signal(str)            # '' = the round brush
    setting_changed = Signal(str, object)
    size_changed = Signal(int, bool)        # slider position, and whether the drag is over
    folder_requested = Signal()
    files_requested = Signal()
    remove_requested = Signal(str)
    reset_requested = Signal()
    closed = Signal()
    drag_finished = Signal()                # moved by hand (it leaves / rejoins the dock)

    WIDTH = 250

    # key, label, range, unit, tooltip
    _SLIDERS = (
        ('spray_spread', 'Spread', (1, 20), '×',
         "Spray: the cloud is the brush size times this - up to 20x (400 px -> 8000 px)"),
        ('spray_density', 'Specks', (1, 3000), '', "Spray: specks per stamp - few for a sparse dusting"),
        ('spray_speck', 'Speck', (1, 60), 'px', "Spray: speck size as drawn (screen px); they vary down to about half"),
        ('rake_tines', 'Tines', (2, 40), '', "Rake: how many tines in the row (the row is the brush size wide)"),
        ('rake_tine', 'Tine', (5, 100), '%', "Rake: each tine's thickness, as a share of the gap between "
                                             "them - 100 % and they touch"),
        ('rake_wobble', 'Wobble', (0, 100), '%', "Rake: how much the tines differ in size, place and "
                                                 "strength, like a worn rake - 0 % lays ruled lines"),
        ('spacing', 'Spacing', (1, 200), '%', "Gap between stamps, as a share of their size"),
        ('angle', 'Angle', (0, 359), '°', "Turns every stamp by this much"),
        ('angle_jitter', 'Angle ±', (0, 180), '°', "Random turn per stamp, either way"),
        ('scatter', 'Scatter', (0, 300), '%',
         "Throws each stamp off the stroke at random, up to this share of its size (airbrush, dust)"),
        ('size_jitter', 'Size ±', (0, 100), '%', "Each stamp up to this much smaller, at random"),
        ('hue_jitter', 'Hue ±', (0, 100), '%', "Colour jitter: hue (100 % = anywhere on the wheel)"),
        ('sat_jitter', 'Sat ±', (0, 100), '%', "Colour jitter: saturation"),
        ('val_jitter', 'Value ±', (0, 100), '%', "Colour jitter: brightness"),
    )

    def __init__(self, parent=None):
        super().__init__(
            parent,
            Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint,
        )
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setWindowTitle("Brush")
        self.setFixedWidth(self.WIDTH)

        self._drag_offset = None
        self._quiet = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        bg = QWidget(self)
        bg.setObjectName("brushBg")
        bg.setStyleSheet(
            "#brushBg {"
            " background-color: #2b2d30;"
            " border: 1px solid #4a4d50;"
            " border-radius: 8px;"
            "}"
            "QLabel, QCheckBox { color: #b7bcc1; }"
        )
        outer.addWidget(bg)
        inner = QVBoxLayout(bg)
        inner.setContentsMargins(10, 6, 10, 10)
        inner.setSpacing(5)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        title = QLabel("\U0001F58C Brush")
        title.setCursor(Qt.SizeAllCursor)
        header.addWidget(title, 1)
        reset_btn = QToolButton()
        reset_btn.setText("↺")
        reset_btn.setFixedSize(20, 20)
        reset_btn.setToolTip("Reset the brush settings to the defaults "
                             "(keeps the tip, mode, size and colour)")
        reset_btn.clicked.connect(self.reset_requested)
        header.addWidget(reset_btn)
        close_btn = QToolButton()
        close_btn.setText("✕")
        close_btn.setFixedSize(20, 20)
        close_btn.setToolTip("Close")
        close_btn.clicked.connect(self.closed)
        header.addWidget(close_btn)
        inner.addLayout(header)

        mode_row = QHBoxLayout()
        mode_row.setContentsMargins(0, 0, 0, 0)
        self._mode = QComboBox()
        self._mode.addItem("🖍 Paint", 'paint')
        self._mode.addItem("💧 Blend", 'blend')
        self._mode.addItem("👆 Smudge", 'smudge')
        self._mode.addItem("🕘 History", 'history')
        self._mode.addItem("🎯 Pick blend", 'pick')
        self._mode.addItem("🧽 Erase", 'erase')
        self._mode.setToolTip("Paint: the brush colour. Blend: mixed with what is under it. "
                              "Smudge: drags the colours under the pen along. Pick blend: Blend "
                              "with the colour under the pen where the stroke starts. Erase: the "
                              "brush rubs out what is drawn on this layer (keys 1-6)")
        self._mode.currentIndexChanged.connect(self._on_mode)
        mode_row.addWidget(self._mode, 1)
        inner.addLayout(mode_row)

        # Brush size, the toolbar slider's twin (same curved track: the main
        # window turns positions into px and keeps the two in step).
        size_row = QHBoxLayout()
        size_row.setContentsMargins(0, 0, 0, 0)
        size_name = QLabel("Size")
        size_name.setFixedWidth(52)
        size_row.addWidget(size_name)
        self._size = ClickableSlider(Qt.Horizontal)
        self._size.setRange(0, 1000)
        self._size.setToolTip("Brush size (1-400 px), the same as the toolbar's")
        self._size_value = QLabel("")
        self._size_value.setFixedWidth(36)
        self._size_value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._size.valueChanged.connect(
            lambda pos: self._quiet or self.size_changed.emit(pos, not self._size.isSliderDown()))
        self._size.sliderReleased.connect(lambda: self.size_changed.emit(self._size.value(), True))
        size_row.addWidget(self._size, 1)
        size_row.addWidget(self._size_value)
        inner.addLayout(size_row)

        mix_row = QHBoxLayout()
        mix_row.setContentsMargins(0, 0, 0, 0)
        self._mix_name = QLabel("Mix")
        self._mix_name.setFixedWidth(52)
        mix_row.addWidget(self._mix_name)
        self._mix = ClickableSlider(Qt.Horizontal)
        self._mix.setRange(0, 100)
        self._mix_value = QLabel("")
        self._mix_value.setFixedWidth(36)
        self._mix_value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._mix.valueChanged.connect(
            lambda v: self._on_slider('mix', v, '%', self._mix_value))
        mix_row.addWidget(self._mix, 1)
        mix_row.addWidget(self._mix_value)
        inner.addLayout(mix_row)
        self._mode_value = 'paint'

        self._list = QListWidget()
        self._list.setViewMode(QListView.IconMode)
        self._list.setIconSize(QSize(SWATCH, SWATCH))
        self._list.setGridSize(QSize(SWATCH + 6, SWATCH + 6))
        self._list.setResizeMode(QListView.Adjust)
        self._list.setMovement(QListView.Static)
        self._list.setSelectionMode(QAbstractItemView.SingleSelection)
        self._list.setStyleSheet("QListWidget { background: #1f2123; border: 1px solid #3a3d40; }")
        self._list.setFixedHeight((SWATCH + 6) * 3 + 6)
        self._list.setToolTip("Brush tip: Round, or a PNG texture")
        self._list.currentItemChanged.connect(self._on_item)
        inner.addWidget(self._list)

        files = QHBoxLayout()
        files.setContentsMargins(0, 0, 0, 0)
        files.setSpacing(4)
        files.addWidget(self._tool_button("📁", "Use every PNG in a folder", self.folder_requested.emit))
        files.addWidget(self._tool_button("+", "Add PNG textures", self.files_requested.emit))
        self._remove_btn = self._tool_button(
            "−", "Take this texture off the list (the file stays)", self._on_remove)
        files.addWidget(self._remove_btn)
        self._name = QLabel("")
        self._name.setStyleSheet("color: #8a8f95; font-size: 10px;")
        files.addWidget(self._name, 1)
        inner.addLayout(files)

        self._sample = QComboBox()
        self._sample.addItem("Sample: this layer and below", 'below')
        self._sample.addItem("Sample: this layer only", 'layer')
        self._sample.addItem("Sample: everything on screen", 'all')
        self._sample.setToolTip(
            "What Blend and Smudge pick their colour up from. This layer and below: the "
            "picture (with its effects) and the layers up to this one - never the ones above. "
            "This layer only: just its own paint; where it is empty a smudge lays nothing. "
            "Everything on screen: all layers, above included")
        self._sample.currentIndexChanged.connect(
            lambda i: self._emit('sample', self._sample.itemData(i)))
        inner.addWidget(self._sample)

        self._pigment = QComboBox()
        self._pigment.addItem("Mix: like paint (pigment)", True)
        self._pigment.addItem("Mix: like light", False)
        self._pigment.setToolTip(
            "How Blend and Smudge mix colours. Like paint: blue and yellow make green, "
            "red and blue a purple, white lightens without washing out. Like light: "
            "a plain average - blue and yellow make grey")
        self._pigment.currentIndexChanged.connect(
            lambda i: self._emit('pigment', self._pigment.itemData(i)))
        inner.addWidget(self._pigment)

        self._paint_mode = QComboBox()
        self._paint_mode.addItem("Paint: flat strokes (no build-up)", True)
        self._paint_mode.addItem("Paint: stamps build up", False)
        self._paint_mode.setToolTip(
            "Flat: a stroke is as strong as its strongest stamp, so the tip's gaps run "
            "along it as streaks (HeavyPaint). Build up: every stamp adds ink")
        self._paint_mode.currentIndexChanged.connect(
            lambda i: self._emit('flat', self._paint_mode.itemData(i)))
        inner.addWidget(self._paint_mode)

        self._angle_mode = QComboBox()
        self._angle_mode.addItem("Rotate: pen tilt, else stroke direction", 'auto')
        self._angle_mode.addItem("Rotate: stroke direction (ignore tilt)", 'direction')
        self._angle_mode.addItem("Rotate: fixed angle", 'fixed')
        self._angle_mode.setToolTip(
            "What turns each stamp. Pen tilt: the tip keeps the pen's lean whichever way you "
            "draw, so a flat tip drawn along its long side leaves a thin line, as a real one "
            "would. Stroke direction: it turns with the stroke and always lies across it")
        self._angle_mode.currentIndexChanged.connect(
            lambda i: self._emit('angle_mode', self._angle_mode.itemData(i)))
        inner.addWidget(self._angle_mode)

        self._sliders = {}
        self._slider_rows = {}
        for key, label, (lo, hi), unit, tip in self._SLIDERS:
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            name = QLabel(label)
            name.setFixedWidth(52)
            row.addWidget(name)
            slider = ClickableSlider(Qt.Horizontal)
            slider.setRange(lo, hi)
            slider.setToolTip(tip)
            value = QLabel("")
            value.setFixedWidth(36)
            value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            slider.valueChanged.connect(
                lambda v, k=key, u=unit, lab=value: self._on_slider(k, v, u, lab))
            row.addWidget(slider, 1)
            row.addWidget(value)
            inner.addLayout(row)
            self._sliders[key] = (slider, value, unit)
            self._slider_rows[key] = (name, slider, value)

        self._jitter_mode = QComboBox()
        self._jitter_mode.addItem("Colour jitter: once per stroke", False)
        self._jitter_mode.addItem("Colour jitter: every stamp", True)
        self._jitter_mode.addItem("Colour jitter: across the tip (streaks)", 'streaks')
        self._jitter_mode.addItem("Colour jitter: every speck (particles)", 'particles')
        self._jitter_mode.setToolTip(
            "One shifted colour per stroke, a new one every stamp, or streaks: every bristle "
            "across the tip its own colour, running on along the stroke. With streaks, Blend and "
            "Smudge mix and drag the colours under each streak on their own. Particles: every "
            "speck of the tip its own colour (the Dust tip, a grainy texture)")
        self._jitter_mode.currentIndexChanged.connect(
            lambda i: self._emit('jitter_per_stamp', self._jitter_mode.itemData(i)))
        inner.addWidget(self._jitter_mode)

        self._jitter_marks = QCheckBox("Jitter lines && shapes too")
        self._jitter_marks.setToolTip(
            "Ruler lines, straight lines, polylines and filled shapes get a colour jittered by "
            "Hue / Sat / Value ± as well - a new one for each line or shape")
        self._jitter_marks.toggled.connect(lambda on: self._emit('jitter_marks', on))
        inner.addWidget(self._jitter_marks)

        checks = QHBoxLayout()
        checks.setContentsMargins(0, 0, 0, 0)
        checks.addWidget(QLabel("Pressure:"))
        self._size_pressure = QCheckBox("size")
        self._size_pressure.setToolTip("Lighter pressure, smaller stamps (needs 🎨 pen pressure on)")
        self._size_pressure.toggled.connect(lambda on: self._emit('size_pressure', on))
        checks.addWidget(self._size_pressure)
        self._opacity_pressure = QCheckBox("opacity")
        self._opacity_pressure.setToolTip("Lighter pressure, fainter stamps (needs 🎨 pen pressure on)")
        self._opacity_pressure.toggled.connect(lambda on: self._emit('opacity_pressure', on))
        checks.addWidget(self._opacity_pressure)
        checks.addStretch(1)
        inner.addLayout(checks)

        self._texture_widgets = [self._paint_mode, self._angle_mode, self._jitter_mode,
                                 self._size_pressure, self._opacity_pressure]

    @staticmethod
    def _tool_button(text, tip, slot):
        btn = QToolButton()
        btn.setText(text)
        btn.setToolTip(tip)
        btn.setFixedSize(24, 24)
        btn.clicked.connect(lambda: slot())
        return btn

    @staticmethod
    def _round_icon():
        pix = QPixmap(SWATCH, SWATCH)
        pix.fill(Qt.transparent)
        p = QPainter(pix)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(230, 230, 230))
        p.drawEllipse(8, 8, SWATCH - 16, SWATCH - 16)
        p.end()
        return QIcon(pix)

    def show_size(self, position, px):
        """Put the size slider at *position*, reading *px* - without echoing
        it back as a change."""
        quiet, self._quiet = self._quiet, True
        if not self._size.isSliderDown():
            self._size.setValue(int(position))
        self._size_value.setText(f"{int(px)}px")
        self._quiet = quiet

    def set_textures(self, textures, current):
        """*textures*: [(path, name, QImage thumbnail)]; *current*: the path in
        use, '' for Round."""
        self._quiet = True
        self._list.clear()
        item = QListWidgetItem(self._round_icon(), "")
        item.setData(Qt.UserRole, '')
        item.setToolTip("Round (the plain brush)")
        self._list.addItem(item)
        chosen = item
        for path, name, thumb in textures:
            item = QListWidgetItem(QIcon(QPixmap.fromImage(thumb)), "")
            item.setData(Qt.UserRole, path)
            item.setToolTip(f"{name}\n{path}")
            self._list.addItem(item)
            if path == current:
                chosen = item
        self._list.setCurrentItem(chosen)
        self._quiet = False
        self._show_current(chosen)

    def _on_mode(self, index):
        self._mode_value = self._mode.itemData(index)
        self._show_mix()
        self._show_current(self._list.currentItem())
        self._emit('mode', self._mode_value)

    def _show_mix(self):
        mode = self._mode_value
        self._mix.setEnabled(mode not in ('paint', 'erase'))
        if hasattr(self, '_sample'):
            self._sample.setEnabled(mode in ('blend', 'smudge', 'pick'))
            self._pigment.setEnabled(mode in ('blend', 'smudge', 'pick'))
        self._mix_name.setText({'smudge': "Carry", 'history': "Tint"}.get(mode, "Mix"))
        self._mix.setToolTip({
            'smudge': "How far the picked-up colour is dragged",
            'blend': "Blender brush: it drags the pixels under it along and mixes them. "
                     "Low: more of the brush colour goes in and it drags less far. "
                     "100 %: only moves what is there, the furthest",
            'pick': "Blend with the colour picked under the pen at the first touch. "
                    "Low: more of that colour goes in and it drags less far. "
                    "100 %: only moves what is there, the furthest",
            'history': "How much of the brush colour goes into the picture painted back "
                       "(0 %: the picture itself)",
        }.get(mode, "Blend / Smudge / History only"))

    def set_settings(self, settings):
        self._quiet = True
        self._mode_value = settings['mode']
        self._mode.setCurrentIndex(self._mode.findData(settings['mode']))
        self._mix.setValue(int(settings['mix']))
        self._mix_value.setText(f"{int(settings['mix'])}%")
        self._show_mix()
        for key, (slider, value, unit) in self._sliders.items():
            slider.setValue(int(settings[key]))
            value.setText(f"{int(settings[key])}{unit}")
        self._paint_mode.setCurrentIndex(self._paint_mode.findData(settings['flat']))
        self._sample.setCurrentIndex(max(0, self._sample.findData(settings.get('sample', 'below'))))
        self._pigment.setCurrentIndex(max(0, self._pigment.findData(bool(settings.get('pigment', True)))))
        self._angle_mode.setCurrentIndex(self._angle_mode.findData(settings['angle_mode']))
        self._jitter_mode.setCurrentIndex(self._jitter_mode.findData(settings['jitter_per_stamp']))
        self._jitter_marks.setChecked(bool(settings.get('jitter_marks', False)))
        self._size_pressure.setChecked(settings['size_pressure'])
        self._opacity_pressure.setChecked(settings['opacity_pressure'])
        self._quiet = False
        self._show_current(self._list.currentItem())

    def _show_current(self, item):
        path = item.data(Qt.UserRole) if item is not None else ''
        self._remove_btn.setEnabled(bool(path) and not path.startswith(':'))   # built-ins stay
        self._name.setText(item.toolTip().split('\n')[0] if item is not None else "")
        # The settings below shape stamps: a texture, or Round when it blends
        # or smudges (painting Round is the plain pen).
        stamped = bool(path) or self._mode_value != 'paint'
        for widget in self._texture_widgets:
            widget.setEnabled(stamped)
        for key, (slider, _value, _unit) in self._sliders.items():
            # The colour jitter also serves lines and shapes (Jitter lines &
            # shapes too), so it stays usable with the plain round pen.
            slider.setEnabled(stamped or key in ('hue_jitter', 'sat_jitter', 'val_jitter'))
        # The Spray tip's own sliders show only with it.
        spray = bool(path) and path.startswith(':spray')
        rake = bool(path) and path.startswith(':rake')       # ... and the Rake's with it
        changed = False
        for key, widgets in self._slider_rows.items():
            for prefix, shown in (('spray_', spray), ('rake_', rake)):
                if key.startswith(prefix) and widgets[0].isVisibleTo(self) != shown:
                    for widget in widgets:
                        widget.setVisible(shown)
                    changed = True
        if changed:
            self.adjustSize()

    def _on_item(self, item, _previous):
        if item is None:
            return
        self._show_current(item)
        if not self._quiet:
            self.texture_chosen.emit(item.data(Qt.UserRole))

    def _on_remove(self):
        item = self._list.currentItem()
        if item is not None and item.data(Qt.UserRole):
            self.remove_requested.emit(item.data(Qt.UserRole))

    def _on_slider(self, key, value, unit, label):
        label.setText(f"{value}{unit}")
        self._emit(key, value)

    def _emit(self, key, value):
        if not self._quiet:
            self.setting_changed.emit(key, value)

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
