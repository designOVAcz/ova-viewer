"""Floating, draggable tool panels (HeavyPaint-style) overlaid on the canvas.

This module provides two building blocks used by the main window to replace the
classic top toolbars:

* :class:`FlowLayout` — a wrapping layout that arranges child widgets left to
  right, wrapping to a new line when it runs out of horizontal space. Hidden
  widgets (``isHidden()``) are skipped so context-only controls reserve no
  space.
* :class:`FloatingPanel` — a slim, rounded, semi-transparent container with a
  thin drag handle. It holds tool widgets in a :class:`FlowLayout` and can be
  dragged around within its parent (the image canvas), resized horizontally by
  dragging its right edge (icons re-flow / wrap), and collapsed to just its
  title bar by double-clicking the title (minimise). It emits ``changed``
  whenever geometry or collapsed state changes so the owner can persist layout.
"""

from PySide6.QtWidgets import QLayout, QWidget, QSizePolicy, QLabel, QVBoxLayout
from PySide6.QtCore import Qt, QRect, QSize, QPoint, Signal


class FlowLayout(QLayout):
    """A layout that lays widgets out horizontally and wraps to new rows."""

    def __init__(self, parent=None, margin=0, hspacing=4, vspacing=4):
        super().__init__(parent)
        self._items = []
        self._hspacing = hspacing
        self._vspacing = vspacing
        self.setContentsMargins(margin, margin, margin, margin)

    def __del__(self):
        while self.count():
            self.takeAt(0)

    # ── QLayout plumbing ──
    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index):
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self):
        return Qt.Orientations(Qt.Orientation(0))

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._do_layout(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for item in self._items:
            w = item.widget()
            if w is not None and w.isHidden():
                continue
            size = size.expandedTo(item.minimumSize())
        m = self.contentsMargins()
        size += QSize(m.left() + m.right(), m.top() + m.bottom())
        return size

    def natural_row_width(self):
        """Width needed to lay all visible items out in a single row."""
        m = self.contentsMargins()
        w = m.left() + m.right()
        first = True
        for item in self._items:
            widget = item.widget()
            if widget is not None and widget.isHidden():
                continue
            if not first:
                w += self._hspacing
            w += item.sizeHint().width()
            first = False
        return w

    def _do_layout(self, rect, test_only):
        m = self.contentsMargins()
        x = rect.x() + m.left()
        y = rect.y() + m.top()
        line_height = 0
        right = rect.right() - m.right()

        for item in self._items:
            w = item.widget()
            if w is not None and w.isHidden():
                continue
            hint = item.sizeHint()
            item_w = hint.width()
            item_h = hint.height()
            next_x = x + item_w
            if next_x - 1 > right and line_height > 0:
                # wrap to next row
                x = rect.x() + m.left()
                y = y + line_height + self._vspacing
                next_x = x + item_w
                line_height = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), QSize(item_w, item_h)))
            x = next_x + self._hspacing
            line_height = max(line_height, item_h)

        return y + line_height + m.bottom() - rect.y()


class FloatingPanel(QWidget):
    """A draggable, resizable, collapsible tool container."""

    moved_by_user = Signal()
    changed = Signal()

    RESIZE_MARGIN = 7
    MIN_WIDTH = 46
    MAX_WIDTH = 4000

    def __init__(self, title, parent=None):
        super().__init__(parent)
        self.setObjectName("FloatingPanel")
        self._title = title
        self._user_moved = False
        self._collapsed = False
        self._drag_offset = None
        self._resizing = False
        self._resize_start = None
        self._resize_start_w = 0
        self._content_width = None  # user/programmatic wrap width for the flow

        # Corner-anchoring: a panel docked on the right/bottom half of the
        # canvas keeps a constant pixel gap to that edge so collapsing and
        # window resizing keep it visually pinned to its corner (instead of
        # always anchoring to the top-left).
        self._anchor_h = 'left'   # 'left' | 'right'
        self._anchor_v = 'top'    # 'top' | 'bottom'
        self._right_gap = 0       # px from panel right edge to parent right edge
        self._bottom_gap = 0      # px from panel bottom edge to parent bottom edge
        self._left_pos = 0        # px from parent left edge to panel left edge
        self._top_pos = 0         # px from parent top edge to panel top edge

        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setMouseTracking(True)
        self.setStyleSheet(
            "#FloatingPanel {"
            "  background-color: rgba(30, 32, 35, 210);"
            "  border: 1px solid rgba(90, 95, 100, 190);"
            "  border-radius: 7px;"
            "}"
        )

        outer = QVBoxLayout(self)
        outer.setContentsMargins(5, 3, 5, 5)
        outer.setSpacing(2)

        # Thin drag handle / title bar. Double-click collapses/expands.
        self._handle = QLabel(self._handle_text(), self)
        self._handle.setFixedHeight(13)
        self._handle.setAlignment(Qt.AlignCenter)
        self._handle.setStyleSheet(
            "QLabel {"
            "  color: #b8bcc0;"
            "  font-size: 8px;"
            "  font-weight: bold;"
            "  letter-spacing: 1px;"
            "  background: transparent;"
            "}"
        )
        self._handle.setCursor(Qt.OpenHandCursor)
        self._handle.setToolTip("Drag to move · double-click to collapse/expand · drag right edge to resize")
        self._handle.setMouseTracking(True)
        outer.addWidget(self._handle)

        # Content area with wrapping flow layout
        self._content = QWidget(self)
        self._content.setStyleSheet("background: transparent;")
        self._content.setMouseTracking(True)
        self._flow = FlowLayout(self._content, margin=0, hspacing=4, vspacing=4)
        outer.addWidget(self._content)

    # ── helpers ──
    def _handle_text(self):
        arrow = "▸ " if self._collapsed else ""
        return arrow + self._title

    # ── tool management ──
    def add_tool(self, widget, show=True):
        """Reparent *widget* into this panel's flow layout."""
        if widget is None:
            return
        widget.setParent(self._content)
        if show:
            widget.show()
        self._flow.addWidget(widget)

    def _apply_content_width(self, width):
        """Constrain the flow to *width* px and size the content to fit."""
        width = max(self.MIN_WIDTH, int(width))
        self._content_width = width
        self._content.setFixedWidth(width)
        h = self._flow.heightForWidth(width)
        self._content.setFixedHeight(max(1, h))

    def finalize(self):
        """Size content to a single horizontal row, then show the panel."""
        self._flow.invalidate()
        natural = self._flow.natural_row_width()
        self._apply_content_width(natural)
        self._sync_size()
        self.show()
        self.raise_()

    def relayout(self):
        """Re-run the flow layout keeping the current content width."""
        self._flow.invalidate()
        width = self._content_width or self._flow.natural_row_width()
        self._apply_content_width(width)
        self._sync_size()

    def _sync_size(self):
        """Resize the panel to fit the handle + (optional) content."""
        self._handle.setText(self._handle_text())
        if self._collapsed:
            self._content.hide()
        else:
            self._content.show()
        self.adjustSize()

    # ── collapse / expand ──
    def set_collapsed(self, collapsed, emit=True):
        collapsed = bool(collapsed)
        if collapsed == self._collapsed:
            return
        self._collapsed = collapsed
        self._sync_size()
        # Honour the panel's corner anchor so a right/bottom-docked panel keeps
        # its right/bottom edge fixed instead of snapping to the top-left.
        self.reposition_to_anchor()
        if emit:
            self.changed.emit()

    def toggle_collapsed(self):
        self.set_collapsed(not self._collapsed)

    @property
    def collapsed(self):
        return self._collapsed

    # ── persisted state ──
    def state(self):
        parent = self.parentWidget()
        pw = parent.width() if parent is not None else 0
        ph = parent.height() if parent is not None else 0
        return {
            "x": self.x(),
            "y": self.y(),
            # Fractional position (0..1) so a moved panel scales to any window
            # size on restore, instead of clamping into a smaller canvas.
            "xf": (self.x() / pw) if pw else 0.0,
            "yf": (self.y() / ph) if ph else 0.0,
            "w": self._content_width or self._flow.natural_row_width(),
            "collapsed": self._collapsed,
            "moved": self._user_moved,
            # Corner-anchor persistence so right/bottom-docked panels restore
            # to their corner on any window size.
            "anchor_h": self._anchor_h,
            "anchor_v": self._anchor_v,
            "right_gap": self._right_gap,
            "bottom_gap": self._bottom_gap,
        }

    def apply_state(self, st):
        try:
            if st.get("w"):
                self._apply_content_width(int(st["w"]))
            self._collapsed = bool(st.get("collapsed", False))
            self._sync_size()
            if st.get("moved"):
                self._user_moved = True
                parent = self.parentWidget()
                pw = parent.width() if parent is not None else 0
                ph = parent.height() if parent is not None else 0
                if "anchor_h" in st and "anchor_v" in st:
                    # Corner-anchored restore: keep the saved gap to the
                    # right/bottom edge so the panel returns to its corner.
                    self._anchor_h = st.get("anchor_h", 'left')
                    self._anchor_v = st.get("anchor_v", 'top')
                    self._right_gap = int(st.get("right_gap", 0))
                    self._bottom_gap = int(st.get("bottom_gap", 0))
                    self._left_pos = int(st.get("x", self.x()))
                    self._top_pos = int(st.get("y", self.y()))
                    self.reposition_to_anchor()
                else:
                    # Legacy fractional restore (pre-anchor saved layouts).
                    if pw and "xf" in st and "yf" in st:
                        x = int(float(st["xf"]) * pw)
                        y = int(float(st["yf"]) * ph)
                    else:
                        x = int(st.get("x", self.x()))
                        y = int(st.get("y", self.y()))
                    self.move(x, y)
                    self.clamp_into_parent()
                    self.update_anchor()
        except Exception:
            pass

    def mark_user_moved(self, moved=True):
        self._user_moved = moved

    @property
    def user_moved(self):
        return self._user_moved

    # ── hit testing ──
    def _in_handle(self, pos):
        return self._handle.geometry().contains(pos)

    def _on_right_edge(self, pos):
        return (not self._collapsed) and (self.width() - pos.x() <= self.RESIZE_MARGIN) \
            and 0 <= pos.x() <= self.width()

    # ── mouse ──
    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton and self._in_handle(event.position().toPoint()):
            self.toggle_collapsed()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def mousePressEvent(self, event):
        pos = event.position().toPoint()
        if event.button() == Qt.LeftButton and self._on_right_edge(pos):
            self._resizing = True
            self._resize_start = event.globalPosition().toPoint()
            self._resize_start_w = self._content_width or self._content.width()
            self.raise_()
            event.accept()
            return
        if event.button() == Qt.LeftButton and self._in_handle(pos):
            self._drag_offset = pos
            self._handle.setCursor(Qt.ClosedHandCursor)
            self.raise_()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        pos = event.position().toPoint()
        if self._resizing:
            delta = event.globalPosition().toPoint().x() - self._resize_start.x()
            new_w = max(self.MIN_WIDTH, min(self.MAX_WIDTH, self._resize_start_w + delta))
            self._apply_content_width(new_w)
            self._sync_size()
            self.clamp_into_parent()
            event.accept()
            return
        if self._drag_offset is not None:
            parent = self.parentWidget()
            new_pos = self.mapToParent(pos - self._drag_offset)
            if parent is not None:
                max_x = max(0, parent.width() - self.width())
                max_y = max(0, parent.height() - self.height())
                nx = min(max(0, new_pos.x()), max_x)
                ny = min(max(0, new_pos.y()), max_y)
                new_pos = QPoint(nx, ny)
            self.move(new_pos)
            event.accept()
            return
        # Update cursor to hint at the resize affordance on the right edge.
        if self._on_right_edge(pos):
            self.setCursor(Qt.SizeHorCursor)
        else:
            self.unsetCursor()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        changed = False
        if self._resizing:
            self._resizing = False
            self.unsetCursor()
            changed = True
        if self._drag_offset is not None:
            self._drag_offset = None
            self._handle.setCursor(Qt.OpenHandCursor)
            self._user_moved = True
            self.moved_by_user.emit()
            changed = True
        if changed:
            # Recompute which corner this panel is docked to after the
            # user finishes moving/resizing it.
            self.update_anchor()
            self.changed.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def clamp_into_parent(self):
        """Keep the panel fully within its parent's bounds."""
        parent = self.parentWidget()
        if parent is None:
            return
        max_x = max(0, parent.width() - self.width())
        max_y = max(0, parent.height() - self.height())
        nx = min(max(0, self.x()), max_x)
        ny = min(max(0, self.y()), max_y)
        if nx != self.x() or ny != self.y():
            self.move(nx, ny)

    # ── corner anchoring ──
    def update_anchor(self):
        """Recompute the anchor edges and gaps from the current geometry.

        Called after the user finishes dragging or resizing so a panel that
        ended up on the right/bottom half of the canvas becomes pinned to that
        corner (constant pixel gap), while one on the left/top half stays
        anchored top-left.
        """
        parent = self.parentWidget()
        if parent is None:
            return
        pw = parent.width()
        ph = parent.height()
        center_x = self.x() + self.width() / 2
        center_y = self.y() + self.height() / 2
        self._anchor_h = 'right' if pw and center_x > pw / 2 else 'left'
        self._anchor_v = 'bottom' if ph and center_y > ph / 2 else 'top'
        self._left_pos = self.x()
        self._top_pos = self.y()
        self._right_gap = max(0, pw - (self.x() + self.width()))
        self._bottom_gap = max(0, ph - (self.y() + self.height()))

    def reposition_to_anchor(self):
        """Move the panel so it honours its current anchor, then clamp on-screen.

        For a right-anchored panel this keeps the right edge fixed (the panel
        grows/shrinks leftward on collapse/resize); for a bottom-anchored panel
        it keeps the bottom edge fixed.
        """
        parent = self.parentWidget()
        if parent is None:
            return
        pw = parent.width()
        ph = parent.height()
        if self._anchor_h == 'right':
            x = pw - self.width() - self._right_gap
        else:
            x = self._left_pos
        if self._anchor_v == 'bottom':
            y = ph - self.height() - self._bottom_gap
        else:
            y = self._top_pos
        self.move(int(x), int(y))
        self.clamp_into_parent()

