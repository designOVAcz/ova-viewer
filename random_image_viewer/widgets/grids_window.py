"""▦ Grids: the composition grids as a palette of tiles (see
drawing/comp_grids). Any number on at once, each in its own colour;
right-click a tile to turn the ones that have a direction (spiral,
triangles). A pure view - the main window keeps which are on.

Signals:
    grid_toggled(str, bool)      a tile clicked on / off
    orient_changed(str, int)     a tile turned (right-click)
    opacity_changed(int)         0..100
    clear_requested()            every grid off (now only from ▦'s right-click menu)
    shown_toggled(bool)          On / Off: all the chosen ones shown or hidden, kept chosen
    closed()                     ✕
    drag_finished()              moved by its title row
"""

from PySide6.QtCore import Qt, Signal, QRectF, QSize
from PySide6.QtGui import QIcon, QPainter, QPixmap, QColor
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel,
                               QToolButton)

from random_image_viewer.drawing import comp_grids
from random_image_viewer.widgets.clickable_slider import ClickableSlider
from random_image_viewer.widgets.title_drag import on_title_row


class GridsWindow(QWidget):
    grid_toggled = Signal(str, bool)
    orient_changed = Signal(str, int)
    opacity_changed = Signal(int)
    clear_requested = Signal()
    shown_toggled = Signal(bool)    # all the chosen grids shown / hidden (kept chosen)
    closed = Signal()
    drag_finished = Signal()

    TILE = QSize(78, 50)
    COLUMNS = 3

    def __init__(self, parent=None):
        super().__init__(parent, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setWindowTitle("Grids")
        self._drag_offset = None
        self._dragged = False
        self._orient = {key: 0 for key in comp_grids.GRIDS}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        bg = QWidget(self)
        bg.setObjectName("gridsBg")
        bg.setStyleSheet("#gridsBg { background-color: #2b2d30; border: 1px solid #4a4d50;"
                         " border-radius: 8px; }")
        outer.addWidget(bg)
        inner = QVBoxLayout(bg)
        inner.setContentsMargins(10, 6, 10, 10)
        inner.setSpacing(6)

        header = QHBoxLayout()
        header.setSpacing(4)
        self._header = QLabel("▦ Grids")
        self._header.setStyleSheet("QLabel { color: #c8c8c8; font-size: 10pt; font-weight: bold;"
                                   " padding: 2px 4px; background: transparent; }")
        self._header.setCursor(Qt.SizeAllCursor)
        self._header.setToolTip("Drag to move")
        header.addWidget(self._header, 1)
        # On / off for all of them at once - the ones chosen stay chosen (it
        # used to be "None", which forgot them: picked again one by one)
        self._shown = QToolButton()
        self._shown.setCheckable(True)
        self._shown.setChecked(True)
        self._shown.setToolTip("Show or hide the grids - the ones chosen below stay chosen")
        self._shown.setStyleSheet(
            "QToolButton { color: #c8c8c8; font-size: 9pt; padding: 0 8px; border: 1px solid #4a4d50;"
            " border-radius: 4px; background: #232528; min-height: 0; }"
            "QToolButton:checked { color: #fff; background: #2f3a48; border-color: #7aa2ff; }")
        self._shown.toggled.connect(self._on_shown)
        self._show_label(True)
        header.addWidget(self._shown)
        close = QToolButton()
        close.setText("✕")
        close.setFixedSize(18, 18)
        close.setToolTip("Close the grids palette (the grids stay on)")
        close.setStyleSheet(
            "QToolButton { color: #c8c8c8; background: transparent; border: none;"
            " font-weight: bold; font-size: 10pt; padding: 0px; }"
            "QToolButton:hover { color: #fff; background: #c54040; border-radius: 3px; }")
        close.clicked.connect(self._close)
        header.addWidget(close)
        inner.addLayout(header)

        tiles = QGridLayout()
        tiles.setSpacing(6)
        self._tiles = {}
        for i, (key, (name, _make, turns, _c, about)) in enumerate(comp_grids.GRIDS.items()):
            b = QToolButton()
            b.setCheckable(True)
            b.setFixedSize(self.TILE + QSize(8, 8))
            b.setIconSize(self.TILE)
            b.setToolTip(f"{name}\n{about}" + ("\nRight-click: turn it" if turns > 1 else ""))
            b.setStyleSheet(
                "QToolButton { background: #232528; border: 1px solid #3c3f43; border-radius: 4px;"
                " padding: 0; min-height: 0; }"
                "QToolButton:hover { border-color: #6b7078; }"
                "QToolButton:checked { background: #2f3a48; border: 1px solid #7aa2ff; }")
            b.toggled.connect(lambda on, k=key: self.grid_toggled.emit(k, on))
            b.setContextMenuPolicy(Qt.CustomContextMenu)
            b.customContextMenuRequested.connect(lambda _p, k=key: self._turn(k))
            tiles.addWidget(b, i // self.COLUMNS, i % self.COLUMNS)
            self._tiles[key] = b
            self._paint_tile(key)
        inner.addLayout(tiles)

        row = QHBoxLayout()
        label = QLabel("Opacity")
        label.setStyleSheet("QLabel { color: #b7bcc1; font-size: 9pt; }")
        row.addWidget(label)
        self._opacity = ClickableSlider(Qt.Horizontal)
        self._opacity.setRange(10, 100)
        self._opacity.setToolTip("How strongly the grid lines show")
        self._opacity_value = QLabel("")
        self._opacity_value.setFixedWidth(36)
        self._opacity_value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._opacity_value.setStyleSheet("QLabel { color: #b7bcc1; font-size: 9pt; }")
        self._opacity.valueChanged.connect(self._on_opacity)
        row.addWidget(self._opacity, 1)
        row.addWidget(self._opacity_value)
        inner.addLayout(row)

    def _paint_tile(self, key):
        """The tile's picture: the grid itself on a small landscape sheet."""
        size = self.TILE
        dpr = max(1.0, self.devicePixelRatioF())
        pix = QPixmap(round(size.width() * dpr), round(size.height() * dpr))
        pix.setDevicePixelRatio(dpr)
        pix.fill(QColor("#1d1f22"))
        p = QPainter(pix)
        sheet = QRectF(4, 6, size.width() - 8, size.height() - 12)
        p.fillRect(sheet, QColor("#3a3d41"))
        comp_grids.paint_grids(p, sheet, [(key, self._orient[key])], opacity=1.0, width=1.0)
        p.end()
        self._tiles[key].setIcon(QIcon(pix))

    def _turn(self, key):
        turns = comp_grids.orientations(key)
        if turns <= 1:
            return
        # (two turns: mirrored left-right; four: and top-bottom)
        order = [0, 1] if turns == 2 else [0, 1, 3, 2]
        self._orient[key] = order[(order.index(self._orient[key]) + 1) % len(order)]
        self._paint_tile(key)
        self.orient_changed.emit(key, self._orient[key])

    def _on_opacity(self, v):
        self._opacity_value.setText(f"{v}%")
        self.opacity_changed.emit(v)

    def _show_label(self, on):
        self._shown.setText("👁 On" if on else "Off")

    def _on_shown(self, on):
        self._show_label(on)
        self._dim_tiles(not on)
        self.shown_toggled.emit(bool(on))

    def _dim_tiles(self, dim):
        """Hidden: the chosen tiles shown faded - still chosen, not drawn."""
        for b in self._tiles.values():
            effect = b.graphicsEffect()
            if dim:
                if effect is None:
                    from PySide6.QtWidgets import QGraphicsOpacityEffect
                    effect = QGraphicsOpacityEffect(b)
                    b.setGraphicsEffect(effect)
                effect.setOpacity(0.45)
            elif effect is not None:
                effect.setOpacity(1.0)

    def set_state(self, grids, opacity, shown=True):
        """Show *grids* ({key: orient}) as on, the rest off; *opacity* 0..100;
        *shown*: the on / off for all of them."""
        if self._shown.isChecked() != bool(shown):
            self._shown.blockSignals(True)
            self._shown.setChecked(bool(shown))
            self._shown.blockSignals(False)
            self._show_label(bool(shown))
            self._dim_tiles(not shown)
        for key, b in self._tiles.items():
            on = key in grids
            if key in grids and grids[key] != self._orient[key]:
                self._orient[key] = grids[key]
                self._paint_tile(key)
            if b.isChecked() != on:
                b.blockSignals(True)
                b.setChecked(on)
                b.blockSignals(False)
        self._opacity.blockSignals(True)
        self._opacity.setValue(int(opacity))
        self._opacity.blockSignals(False)
        self._opacity_value.setText(f"{int(opacity)}%")

    def orient(self, key):
        return self._orient.get(key, 0)

    def _close(self):
        self.hide()
        self.closed.emit()

    # -- moved by the title row ----------------------------------------------

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
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
        if self._dragged:
            self._dragged = False
            self.drag_finished.emit()
