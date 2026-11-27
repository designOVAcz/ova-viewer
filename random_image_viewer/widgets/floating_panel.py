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

import html

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
        """Width that fits the widest group of tools on a single row.

        Dividers always start a new row, so a panel split into groups only
        needs its widest group's width, not every tool side by side (which
        left each group's row mostly empty). Without dividers this is simply
        all visible items on one row.
        """
        m = self.contentsMargins()
        widest = row = 0
        first = True
        for item in self._items:
            widget = item.widget()
            if widget is not None and widget.isHidden():
                continue
            if isinstance(widget, PanelDivider):
                widest = max(widest, row)
                row = 0
                first = True
                continue
            if not first:
                row += self._hspacing
            row += item.sizeHint().width()
            first = False
        return max(widest, row) + m.left() + m.right()

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
            if isinstance(w, PanelDivider):
                if line_height:
                    y += line_height + self._vspacing
                if not test_only:
                    item.setGeometry(QRect(QPoint(rect.x() + m.left(), y),
                                           QSize(max(1, right - (rect.x() + m.left())), 1)))
                y += 1 + self._vspacing
                x = rect.x() + m.left()
                line_height = 0
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


class PanelDivider(QWidget):
    """A full-width rule that starts a new row in the flow.

    A narrow vertical tick between tools reads as a stray mark once the flow
    wraps, so groups are separated by a rule that always spans the panel and
    always begins a fresh row instead.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(1)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.setStyleSheet("background: #5c6066;")


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
        # Panels lie inside the canvas, whose cursor (brush outline,
        # crosshair) they would inherit; buttons want the plain arrow.
        self.setCursor(Qt.ArrowCursor)
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
        self._handle.setToolTip("Drag to move · double- or middle-click to collapse/expand · drag right edge to resize")
        self._handle.setMouseTracking(True)
        outer.addWidget(self._handle)

        # Content area with wrapping flow layout
        self._content = QWidget(self)
        # A bare "background: transparent" here reaches every tool inside and
        # used to wipe out the app's :checked / :hover backgrounds, so an
        # active tool (thin glyphs like the line tools especially) looked off.
        # Say it with selectors, and give active tools a clear highlight.
        self._content.setStyleSheet(
            "QWidget { background: transparent; }"
            "QToolButton:hover, QPushButton:hover { background: rgba(255, 255, 255, 28); }"
            "QToolButton:checked, QPushButton:checked {"
            "  background: rgba(255, 255, 255, 102);"        # 40 % white
            "  border: 1px solid rgba(255, 255, 255, 140); color: #fff; }"
            "QToolButton:checked:hover, QPushButton:checked:hover {"
            "  background: rgba(255, 255, 255, 125); }")
        self._content.setMouseTracking(True)
        self._flow = FlowLayout(self._content, margin=0, hspacing=4, vspacing=4)
        outer.addWidget(self._content)

    # ── helpers ──
    def _handle_text(self):
        if not self._collapsed:
            return self._title
        # Segoe UI has no ▸, and drawing it via font fallback makes Qt scan
        # every installed font (~1.5 s at startup with collapsed panels).
        # Name the font Qt's fallback would pick so it looks the same.
        return ("<span style=\"font-family:'MS UI Gothic'\">▸</span> "
                + html.escape(self._title))

    # ── tool management ──
    def add_tool(self, widget, show=True):
        """Reparent *widget* into this panel's flow layout."""
        if widget is None:
            return
        widget.setParent(self._content)
        if show:
            widget.show()
        self._flow.addWidget(widget)

    def add_separator(self):
        """Insert a thin divider so related tools read as a group.

        It flows like any other item, so it wraps along with the tools around
        it rather than pinning the layout to a fixed shape.
        """
        line = PanelDivider(self._content)
        line.show()
        self._flow.addWidget(line)
        return line

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
            # 🧲 stuck to another panel (its key), on which side, how far along
            "stuck_to": self._stuck_key,
            "stuck_side": self._stuck_side,
            "stuck_d": self._stuck_d,
        }

    def apply_state(self, st):
        # 🧲 (resolved by key when it moves: the other panel may not be restored yet)
        self._stuck_key = st.get("stuck_to") or None
        self._stuck_side = st.get("stuck_side") or 'below'
        self._stuck_d = int(st.get("stuck_d") or 0)
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

    # ── 🧲 sticking to another panel ──
    #
    # Dragged near another panel's edge, a panel snaps to it (under, over,
    # beside it, edges or middles lined up); let go there and it sticks: it
    # follows that panel wherever it goes, folds or unfolds. Dragged away
    # and let go elsewhere, it comes loose. Chains work (A <- B <- C).

    SNAP = 14           # px: how near an edge snaps
    STICK_GAP = 4       # px between panels stuck together

    _stuck_key = None   # the persist key of the panel this one is stuck to
    _stuck_side = None  # 'below', 'above', 'left', 'right' of it
    _stuck_d = 0        # the offset along that side (x for below/above, y for left/right)
    _snap = None        # (panel, side, d) while a drag is snapped

    def _siblings(self):
        parent = self.parentWidget()
        if parent is None:
            return []
        return [w for w in parent.children()
                if isinstance(w, FloatingPanel) and w is not self]

    def stuck_to(self):
        """The panel this one follows, or None."""
        if not self._stuck_key:
            return None
        for p in self._siblings():
            if getattr(p, '_persist_key', None) == self._stuck_key:
                return p
        return None

    def _leads(self, other):
        """Does *other* follow this panel (directly or down a chain)?"""
        seen = set()
        p = other
        while p is not None and id(p) not in seen:
            seen.add(id(p))
            lead = p.stuck_to()
            if lead is self:
                return True
            p = lead
        return False

    def stick_to(self, panel, side='below', d=None):
        """Stick to *panel* on *side* (None: come loose)."""
        if panel is None or not getattr(panel, '_persist_key', None):
            self._stuck_key = None
            return
        if panel is self or self._leads(panel):
            return                      # (no loops)
        self._stuck_key = panel._persist_key
        self._stuck_side = side
        if d is None:
            d = (self.x() - panel.x()) if side in ('below', 'above') else (self.y() - panel.y())
        self._stuck_d = int(d)
        self._user_moved = True
        self.follow()

    def _stuck_pos(self, lead, side, d):
        g = self.STICK_GAP
        if side == 'below':
            return QPoint(lead.x() + d, lead.y() + lead.height() + g)
        if side == 'above':
            return QPoint(lead.x() + d, lead.y() - self.height() - g)
        if side == 'right':
            return QPoint(lead.x() + lead.width() + g, lead.y() + d)
        return QPoint(lead.x() - self.width() - g, lead.y() + d)

    def follow(self):
        """Back in place by the panel this one is stuck to. True when moved there."""
        lead = self.stuck_to()
        if lead is None or lead.isHidden() or self.isHidden():
            return False
        pos = self._stuck_pos(lead, self._stuck_side or 'below', self._stuck_d)
        parent = self.parentWidget()
        if parent is not None:      # (never pushed off the canvas)
            pos = QPoint(min(max(0, pos.x()), max(0, parent.width() - self.width())),
                         min(max(0, pos.y()), max(0, parent.height() - self.height())))
        if pos != self.pos():
            self.move(pos)
        return True

    def _followers(self):
        return [p for p in self._siblings() if p._stuck_key and p._stuck_key == getattr(self, '_persist_key', None)]

    def _move_followers(self):
        for p in self._followers():
            p.follow()

    def moveEvent(self, event):
        super().moveEvent(event)
        self._move_followers()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._move_followers()          # (folded / unfolded: the one under it moves up / down)

    def _snapped(self, pos):
        """*pos* pulled onto a nearby panel's edge: (pos, (panel, side, d) or None)."""
        w, h = self.width(), self.height()
        g, snap = self.STICK_GAP, self.SNAP
        best = None
        for p in self._siblings():
            if p.isHidden() or self._leads(p) or p is self:
                continue
            px, py, pw, ph = p.x(), p.y(), p.width(), p.height()
            cands = []
            # under / over it: horizontally overlapping
            if pos.x() < px + pw and pos.x() + w > px:
                cands.append(('below', abs(pos.y() - (py + ph + g))))
                cands.append(('above', abs(pos.y() + h + g - py)))
            # beside it: vertically overlapping
            if pos.y() < py + ph and pos.y() + h > py:
                cands.append(('right', abs(pos.x() - (px + pw + g))))
                cands.append(('left', abs(pos.x() + w + g - px)))
            for side, dist in cands:
                if dist <= snap and (best is None or dist < best[0]):
                    best = (dist, p, side)
        if best is None:
            return pos, None
        _dist, p, side = best
        # along the side: lined up with its edges or middle when near
        if side in ('below', 'above'):
            d = pos.x() - p.x()
            for want in (0, p.width() - w, (p.width() - w) // 2):
                if abs(d - want) <= snap:
                    d = want
                    break
        else:
            d = pos.y() - p.y()
            for want in (0, p.height() - h, (p.height() - h) // 2):
                if abs(d - want) <= snap:
                    d = want
                    break
        return self._stuck_pos(p, side, d), (p, side, d)

    HOME_GAP = 8        # px from the top edge at the top middle

    def _snapped_home(self, pos):
        """*pos* pulled to the canvas's middle line when near it, and then to
        the top edge too: the top middle, where 🖌 PAINT sits by default. Let
        go there, the panel stays centred however the window is resized."""
        parent = self.parentWidget()
        if parent is None:
            return pos
        mid = (parent.width() - self.width()) // 2
        if abs(pos.x() - mid) > self.SNAP:
            return pos
        y = pos.y()
        if abs(y - self.HOME_GAP) <= self.SNAP * 2:
            y = self.HOME_GAP
        return QPoint(mid, y)

    def mark_user_moved(self, moved=True):
        self._user_moved = moved

    @property
    def user_moved(self):
        return self._user_moved

    # ── hit testing ──
    def _in_handle(self, pos):
        # The whole strip above the tools counts, margins included, so a
        # slightly-off click still hits the title. Collapsed, the panel is
        # nothing but its title.
        if self._collapsed:
            return self.rect().contains(pos)
        return 0 <= pos.y() < self._content.geometry().top() and self.rect().contains(pos)

    def _on_right_edge(self, pos):
        return (not self._collapsed) and (self.width() - pos.x() <= self.RESIZE_MARGIN) \
            and 0 <= pos.x() <= self.width()

    # ── mouse ──
    # Clicks on the panel never fall through to the canvas behind it: there a
    # double-click opens the grid and a middle-click skips the picture.
    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton and self._in_handle(event.position().toPoint()):
            self.toggle_collapsed()
        event.accept()

    def mousePressEvent(self, event):
        pos = event.position().toPoint()
        if event.button() == Qt.MiddleButton:
            # Middle-click on the title is a one-click collapse/expand.
            if self._in_handle(pos):
                self.toggle_collapsed()
            event.accept()
            return
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
        event.accept()

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
            # From the global cursor, not the panel-local one: the panel is a
            # native window (above the GPU canvas), and a local position can
            # still refer to where it was before the last move - the panel
            # then flips between two places while it is dragged.
            glob = event.globalPosition().toPoint()
            new_pos = ((parent.mapFromGlobal(glob) if parent is not None else glob)
                       - self._drag_offset)
            if parent is not None:
                max_x = max(0, parent.width() - self.width())
                max_y = max(0, parent.height() - self.height())
                nx = min(max(0, new_pos.x()), max_x)
                ny = min(max(0, new_pos.y()), max_y)
                new_pos = QPoint(nx, ny)
            # 🧲 near another panel's edge: pulled onto it (Shift: no snapping)
            self._snap = None
            if not (event.modifiers() & Qt.ShiftModifier):
                new_pos, self._snap = self._snapped(new_pos)
                if self._snap is None:
                    new_pos = self._snapped_home(new_pos)
            self.move(new_pos)
            event.accept()
            return
        # Update cursor to hint at the resize affordance on the right edge.
        if self._on_right_edge(pos):
            self.setCursor(Qt.SizeHorCursor)
        else:
            self.setCursor(Qt.ArrowCursor)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        changed = False
        if self._resizing:
            self._resizing = False
            self.setCursor(Qt.ArrowCursor)
            changed = True
        if self._drag_offset is not None:
            self._drag_offset = None
            self._handle.setCursor(Qt.OpenHandCursor)
            self._user_moved = True
            # 🧲 let go snapped: stuck to that panel; anywhere else: loose
            snap, self._snap = self._snap, None
            if snap is not None:
                self.stick_to(snap[0], snap[1], snap[2])
            else:
                self._stuck_key = None
            self.moved_by_user.emit()
            changed = True
        if changed:
            # Recompute which corner this panel is docked to after the
            # user finishes moving/resizing it.
            self.update_anchor()
            # ... and those stuck to it, down the chain (where they are now
            # is kept too, should this one be hidden)
            todo, seen = self._followers(), set()
            while todo:
                p = todo.pop()
                if id(p) in seen:
                    continue
                seen.add(id(p))
                p.update_anchor()
                todo += p._followers()
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
        if pw and abs(self.x() - (pw - self.width()) // 2) <= 1:
            self._anchor_h = 'center'       # on the middle line: kept centred
        else:
            self._anchor_h = 'right' if pw and center_x > pw / 2 else 'left'
        self._anchor_v = 'bottom' if ph and center_y > ph / 2 else 'top'
        self._left_pos = self.x()
        self._top_pos = self.y()
        self._right_gap = max(0, pw - (self.x() + self.width()))
        self._bottom_gap = max(0, ph - (self.y() + self.height()))

    def dock(self, horizontal, vertical, gap_x=8, gap_y=8):
        """Pin the panel to a corner ('left'/'right', 'top'/'bottom') with the
        given gaps to the edges, as if the user had dragged it there."""
        self._anchor_h, self._anchor_v = horizontal, vertical
        self._left_pos = self._right_gap = gap_x
        self._top_pos = self._bottom_gap = gap_y
        self._user_moved = True
        self.reposition_to_anchor()

    def reposition_to_anchor(self):
        """Move the panel so it honours its current anchor, then clamp on-screen.

        For a right-anchored panel this keeps the right edge fixed (the panel
        grows/shrinks leftward on collapse/resize); for a bottom-anchored panel
        it keeps the bottom edge fixed.
        """
        parent = self.parentWidget()
        if parent is None:
            return
        if self.follow():
            return          # 🧲 stuck to another panel: it says where
        pw = parent.width()
        ph = parent.height()
        if self._anchor_h == 'right':
            x = pw - self.width() - self._right_gap
        elif self._anchor_h == 'center':
            x = (pw - self.width()) // 2
        else:
            x = self._left_pos
        if self._anchor_v == 'bottom':
            y = ph - self.height() - self._bottom_gap
        else:
            y = self._top_pos
        self.move(int(x), int(y))
        self.clamp_into_parent()

