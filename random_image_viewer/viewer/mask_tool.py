"""🎭 Colour mask tool (W): pick colours on the picture or a layer, and the
mask made of them is removed, cut or copied onto a layer of its own - for
taking a background away.

The mask is worked out on the sheet's pixels (see drawing/color_mask): live
on a copy of at most a few megapixels, again at the picture's full size when
it is used. What it makes are ordinary 📸 image marks, so Ctrl+Z, 📚 Layers,
saving and 💾 Save layers (transparent PNGs) all take them as they are:
  - removed from a layer: a 🧊 cut (see _paint_snapshot) on that layer;
  - removed from the picture: the picture cannot change, so a cut-out layer
    of the rest is made and the picture hidden;
  - cut / copied: a new layer with what is in the mask.

A mixin of RandomImageViewer: everything here is the viewer's own state.
"""

import math
import os

from PySide6.QtCore import Qt, QTimer, QRect, QPointF
from PySide6.QtGui import QPainter, QTransform, QImage, QImageReader

from random_image_viewer.drawing import color_mask

PREVIEW_PIXELS = 3_000_000      # the live mask is worked out on this many at most
APPLY_PIXELS = 50_000_000       # ... and when used, on the picture's own up to this


class MaskToolMixin:
    mask_mode = False
    _mask = None            # the colours picked and the mask's settings, see _new_mask
    _mask_src = None        # the pixels the live mask is made from (cached)
    _mask_parts = None      # seed key -> its similarity (cached)
    _mask_view = None       # {'mask', 'overlay', 'rect', 'k'}: the live mask and its look
    _mask_drag = None       # (x at the click, threshold then): a drag sets the threshold
    _mask_window = None

    # -- on / off --------------------------------------------------------------

    def toggle_mask_tool(self, checked):
        if checked:
            if not self.current_image or not self._export_parts():
                self.status.showMessage("🎭 Open a picture first", 3000)
                self._sync_mask_button(False)
                return
            self._deactivate_tools(keep='mask')
            self.mask_mode = True
            if self._mask is None:
                self._mask = self._new_mask()
            win = self._ensure_mask_window()
            if not win.isVisible():
                win.adjustSize()
                btn = getattr(self, 'mask_tool_btn', None)
                if btn is not None and not getattr(self, '_mask_window_placed', False):
                    at = btn.mapToGlobal(btn.rect().bottomLeft())
                    win.move(at.x() - win.width() // 2, at.y() + 6)
                    self._mask_window_placed = True
                win.show()
                win.raise_()
            self._mask_state_to_window()
            self.status.showMessage(
                "🎭 Colour mask: click a colour (Shift+click adds, Alt+click takes away, drag sideways "
                "for its threshold) - then ⌫ Remove, ✂ Cut or ⧉ Copy to a layer")
        else:
            self._disable_mask_silent()
        self.apply_tool_cursor()

    def _disable_mask_silent(self):
        if not self.mask_mode and self._mask is None:
            return
        self.mask_mode = False
        self._drop_mask()
        win = self._mask_window
        if win is not None and win.isVisible():
            win.hide()
        self._sync_mask_button(False)

    def _sync_mask_button(self, on):
        btn = getattr(self, 'mask_tool_btn', None)
        if btn is not None and btn.isChecked() != on:
            btn.blockSignals(True)
            btn.setChecked(on)
            btn.blockSignals(False)

    def _drop_mask(self):
        """The mask goes (the tool off, another picture)."""
        self._mask = None
        self._mask_src = None
        self._mask_parts = None
        self._mask_view = None
        self._mask_drag = None
        if hasattr(self, 'image_label'):
            self._mask_repaint()

    def _mask_repaint(self):
        """The mask shown anew: on the plain canvas, and on the GPU canvas
        over it where that is what shows (it draws the mask itself, see
        _gpu_tool_overlay - repainting the plain one alone changed nothing
        there)."""
        self.image_label.update()
        if self.__dict__.get('_gpu_display'):
            try:
                self._live_update(None)
            except Exception as e:
                print(f"Mask overlay repaint failed: {e}")

    def _mask_new_picture(self):
        """Another picture: the tool stays in hand, with a new mask."""
        if not self.mask_mode:
            self._drop_mask()
            return
        self._drop_mask()
        self._mask = self._new_mask()
        self._mask_state_to_window()

    def _new_mask(self):
        s = self._settings

        def num(key, default):
            try:
                return int(s.value(key, default))
            except (TypeError, ValueError):
                return default
        return {'source': 'picture', 'seeds': [], 'chosen': None,
                'threshold': max(0, min(100, num("mask_threshold", 18))),
                'contiguous': str(s.value("mask_contiguous", "true")).lower() == "true",
                'grow': 0, 'feather': max(0, min(60, num("mask_feather", 1))),
                'invert': False, 'preview': 'tint'}

    def _ensure_mask_window(self):
        win = self._mask_window
        if win is None:
            from random_image_viewer.widgets.mask_window import MaskWindow
            win = self._mask_window = MaskWindow(self)
            win.source_changed.connect(self._mask_set_source)
            win.seed_selected.connect(self._mask_choose)
            win.seed_removed.connect(self._mask_remove_seed)
            win.seed_flipped.connect(self._mask_flip_seed)
            win.threshold_changed.connect(self._mask_set_threshold)
            win.contiguous_toggled.connect(self._mask_set_contiguous)
            win.grow_changed.connect(lambda v: self._mask_set('grow', v))
            win.feather_changed.connect(self._mask_set_feather)
            win.invert_toggled.connect(lambda v: self._mask_set('invert', v))
            win.preview_changed.connect(lambda v: self._mask_set('preview', v))
            win.action.connect(self._mask_action)
            win.closed.connect(lambda: self._sync_mask_button(False) or self._disable_mask_silent()
                               or self.apply_tool_cursor())
        return win

    def _mask_state_to_window(self):
        win, m = self._mask_window, self._mask
        if win is None or m is None:
            return
        chosen = m['chosen']
        seed = m['seeds'][chosen] if chosen is not None else None
        win.set_state({
            'source': m['source'],
            'seeds': [(s['rgb'], s['op']) for s in m['seeds']],
            'chosen': chosen,
            'threshold': seed['tol'] if seed else m['threshold'],
            'contiguous': seed['contig'] if seed else m['contiguous'],
            'grow': m['grow'], 'feather': m['feather'], 'invert': m['invert'],
            'preview': m['preview'],
            'has_mask': bool(m['seeds']),
            'can_layer': True,
        })

    # -- the panel ---------------------------------------------------------------

    def _mask_set_source(self, source):
        m = self._mask
        if m is None or m['source'] == source:
            return
        m['source'] = source
        m['seeds'], m['chosen'] = [], None     # (colours picked on the other one mean nothing here)
        self._mask_src = self._mask_parts = self._mask_view = None
        if source == 'layer':
            self.status.showMessage(f"🎭 Masking {self.active_layer.name} - pick its colours", 3000)
        self._mask_changed()

    def _mask_choose(self, n):
        m = self._mask
        if m is not None and 0 <= n < len(m['seeds']):
            m['chosen'] = n
            self._mask_state_to_window()

    def _mask_remove_seed(self, n):
        m = self._mask
        if m is not None and 0 <= n < len(m['seeds']):
            del m['seeds'][n]
            m['chosen'] = (len(m['seeds']) - 1) if m['seeds'] else None
            self._mask_changed()

    def _mask_flip_seed(self, n):
        m = self._mask
        if m is not None and 0 <= n < len(m['seeds']):
            seed = m['seeds'][n]
            seed['op'] = '+' if seed['op'] == '-' else '-'
            self._mask_changed()

    def _mask_set_threshold(self, v):
        m = self._mask
        if m is None:
            return
        m['threshold'] = int(v)
        self._settings.setValue("mask_threshold", int(v))
        if m['chosen'] is not None:
            m['seeds'][m['chosen']]['tol'] = int(v)
        self._mask_changed(window=False)

    def _mask_set_contiguous(self, on):
        m = self._mask
        if m is None:
            return
        m['contiguous'] = bool(on)
        self._settings.setValue("mask_contiguous", "true" if on else "false")
        if m['chosen'] is not None:
            m['seeds'][m['chosen']]['contig'] = bool(on)
        self._mask_changed(window=False)

    def _mask_set_feather(self, v):
        self._settings.setValue("mask_feather", int(v))
        self._mask_set('feather', v)

    def _mask_set(self, key, value):
        if self._mask is not None:
            self._mask[key] = value
            self._mask_changed(window=False)

    def _mask_changed(self, window=True):
        """Worked out again a moment later (a slider dragged: once per pause)."""
        if window:
            self._mask_state_to_window()
        timer = self.__dict__.get('_mask_timer')
        if timer is None:
            timer = self._mask_timer = QTimer(self)
            timer.setSingleShot(True)
            timer.setInterval(25)
            timer.timeout.connect(self._mask_update)
        timer.start()

    # -- the pixels ----------------------------------------------------------------

    def _mask_rect(self):
        """The sheet the mask covers (picture coordinates)."""
        parts = self._export_parts()
        return parts.get('both' if self.studio_mode else 'picture')

    def _mask_render(self, source, rect, k):
        """*rect* of the sheet at *k* times: the picture alone, or the layer
        drawn on alone (at full opacity: its opacity is the layer's, not the
        pixels')."""
        layers = self.layers
        shown = [(layer.visible, layer.opacity) for layer in layers]
        picture_shown = self.image_visible
        try:
            if source == 'picture':
                for layer in layers:
                    layer.visible = False
                self.image_visible = True
                return self._render_sheet_part(rect, k, paper=False, grey=False, picture=True, sheet=False)
            active = self.active_layer
            for layer in layers:
                layer.visible = layer is active
            active.opacity = 100            # (an export run: the layer's screen image is not touched)
            return self._render_sheet_part(rect, k, paper=True, grey=False, picture=False, sheet=False)
        finally:
            for layer, (visible, opacity) in zip(layers, shown):
                layer.visible, layer.opacity = visible, opacity
            self.image_visible = picture_shown

    def _mask_source_key(self, source):
        layer = self.active_layer
        return (self.current_image, source, self.studio_mode,
                layer.signature() if source == 'layer' else None)

    def _mask_pixels(self, source, rect, k):
        image = self._mask_render(source, rect, k)
        lab, alpha = color_mask.to_arrays(image)
        return {'image': image, 'lab': lab, 'alpha': alpha, 'rect': QRect(rect), 'k': k,
                'key': self._mask_source_key(source)}

    def _mask_live_pixels(self):
        m = self._mask
        rect = self._mask_rect()
        if m is None or rect is None or rect.isEmpty():
            return None
        key = self._mask_source_key(m['source'])
        src = self._mask_src
        if src is None or src['key'] != key or src['rect'] != rect:
            k = min(1.0, math.sqrt(PREVIEW_PIXELS / float(rect.width() * rect.height())))
            src = self._mask_src = self._mask_pixels(m['source'], rect, k)
            self._mask_parts = {}
        return src

    def _mask_from(self, src, cache=None):
        """The finished mask (float32, *src*'s pixels) from the colours picked."""
        m = self._mask
        rect, k = src['rect'], src['k']
        radius = max(1, int(round(2 * max(1.0, k))))
        parts = []
        for seed in m['seeds']:
            px, py = (seed['x'] - rect.x()) * k, (seed['y'] - rect.y()) * k
            key = (round(seed['x'], 2), round(seed['y'], 2), seed['tol'], seed['contig'])
            sim = cache.get(key) if cache is not None else None
            if sim is None:
                sim = color_mask.similarity(src['lab'], src['alpha'], px, py,
                                            seed['tol'], seed['contig'], radius)
                if cache is not None:
                    cache[key] = sim
            parts.append((seed['op'], sim))
        mask = color_mask.combine(src['alpha'].shape, parts)
        return color_mask.finish(mask, m['invert'], m['grow'] * k, m['feather'] * k)

    def _mask_update(self):
        m = self._mask
        if m is None:
            return
        if not m['seeds']:
            self._mask_view = None
            self._mask_repaint()
            return
        src = self._mask_live_pixels()
        if src is None:
            return
        if self._mask_parts is None:
            self._mask_parts = {}
        if len(self._mask_parts) > 64:
            self._mask_parts.clear()
        mask = self._mask_from(src, self._mask_parts)
        overlay = (color_mask.checker_image(mask) if m['preview'] == 'checker'
                   else color_mask.tint_image(mask))
        self._mask_view = {'mask': mask, 'overlay': overlay, 'rect': src['rect'], 'k': src['k']}
        share = float(mask.mean()) * 100
        self.status.showMessage(f"🎭 Mask: {share:.0f} % of the {'picture' if m['source'] == 'picture' else self.active_layer.name}"
                                + (" (inverted)" if m['invert'] else ""), 3000)
        self._mask_repaint()

    def paint_mask_overlay(self, painter):
        view = self._mask_view
        if not self.mask_mode or view is None:
            return
        _v, to_label = self._sel_view()
        if to_label is None:
            return
        rect, k = view['rect'], view['k']
        painter.save()
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        painter.setTransform(QTransform(1.0 / k, 0, 0, 1.0 / k, rect.x(), rect.y()) * to_label)
        painter.drawImage(0, 0, view['overlay'])
        painter.restore()

    # -- the canvas ----------------------------------------------------------------

    def mask_press(self, pos, modifiers):
        m = self._mask
        if m is None:
            return
        at = self._to_picture(pos)
        rect = self._mask_rect()
        if at is None or rect is None or not rect.contains(int(at.x()), int(at.y())):
            self.status.showMessage("🎭 Click on the picture", 2000)
            return
        src = self._mask_live_pixels()
        if src is None:
            return
        if modifiers & Qt.AltModifier:
            op = '-'
        elif modifiers & Qt.ShiftModifier:
            op = '+'
        else:
            op = '+'
            m['seeds'] = []         # a plain click: a new mask
        k = src['k']
        lab, _empty = color_mask.seed_colour(src['lab'], src['alpha'],
                                             (at.x() - src['rect'].x()) * k, (at.y() - src['rect'].y()) * k)
        m['seeds'].append({'x': at.x(), 'y': at.y(), 'op': op, 'tol': m['threshold'],
                           'contig': m['contiguous'], 'rgb': color_mask.lab_to_rgb(lab)})
        m['chosen'] = len(m['seeds']) - 1
        self._mask_drag = (pos.x(), m['threshold'])
        self._mask_changed()

    def mask_move(self, pos, buttons):
        drag, m = self._mask_drag, self._mask
        if drag is None or m is None or not buttons & Qt.LeftButton or m['chosen'] is None:
            return
        x0, tol0 = drag
        tol = int(max(0, min(100, round(tol0 + (pos.x() - x0) / 4.0))))
        if tol != m['seeds'][m['chosen']]['tol']:
            m['seeds'][m['chosen']]['tol'] = tol
            m['threshold'] = tol
            self._mask_changed()

    def mask_release(self):
        if self._mask_drag is not None and self._mask is not None:
            self._settings.setValue("mask_threshold", int(self._mask['threshold']))
        self._mask_drag = None

    # -- using it ----------------------------------------------------------------

    def _mask_full_k(self, rect):
        """The scale the mask is used at: the picture's own size (it may be
        held smaller in memory), within what can be made."""
        k = 1.0
        base = self.pixmap_cache.get(self.current_image)
        try:
            if base is not None and os.path.isfile(self.current_image):
                full = QImageReader(self.current_image).size()
                if full.isValid():
                    k = max(1.0, max(full.width(), full.height()) / float(max(base.width(), base.height())))
        except Exception:
            k = 1.0
        while k > 1.0 and (not self._export_fits(rect, k) or rect.width() * rect.height() * k * k > APPLY_PIXELS):
            k = max(1.0, k * 0.8)
        return k

    def _mask_action(self, kind):
        m = self._mask
        if m is None:
            return
        if kind == 'clear':
            m['seeds'], m['chosen'] = [], None
            self._mask_view = None
            self._mask_changed()
            return
        if not m['seeds']:
            self.status.showMessage("🎭 Pick a colour first", 2500)
            return
        rect = self._mask_rect()
        if rect is None:
            return
        self.commit_selection()
        source = m['source']
        k = self._mask_full_k(rect)
        with self._working("🎭 Applying the mask"):
            src = self._mask_pixels(source, rect, k)
            mask = self._mask_from(src)
        if float(mask.max()) <= 0.0:
            self.status.showMessage("🎭 The mask is empty - nothing to do", 3000)
            return
        corners = [(float(rect.x()), float(rect.y())), (float(rect.right() + 1), float(rect.y())),
                   (float(rect.x()), float(rect.bottom() + 1))]
        layer = self.active_layer
        made = []
        if source == 'picture':
            if kind in ('remove', 'cut'):
                self._mask_new_layer("Cut-out", color_mask.masked(src['image'], 1.0 - mask), corners)
                made.append(self.active_layer.name)
            if kind in ('cut', 'copy'):
                self._mask_new_layer("Masked", color_mask.masked(src['image'], mask), corners)
                made.append(self.active_layer.name)
            if kind in ('remove', 'cut') and self.image_visible:
                self.toggle_image_visibility(False)
        else:
            if kind in ('remove', 'cut'):
                empty = QImage(src['image'].size(), QImage.Format_ARGB32_Premultiplied)
                empty.fill(Qt.transparent)
                layer.marks['drawn_images'].append({'image': empty, 'cut': color_mask.alpha_image(mask),
                                                    'corners': corners})
                layer.marks['_undo_stack'].append('image')
                layer.raster = None
            if kind in ('cut', 'copy'):
                self._mask_new_layer("Masked", color_mask.masked(src['image'], mask), corners,
                                     like=layer)
                made.append(self.active_layer.name)
        # Used: a new mask next, of what is there now (a picture's cut-out:
        # its layer, so a second pass takes more from it)
        if source == 'picture' and kind in ('remove', 'cut'):
            self.select_layer(self.layers[self.layers.index(self.active_layer) - (1 if kind == 'cut' else 0)])
            m['source'] = 'layer'
        m['seeds'], m['chosen'] = [], None
        self._mask_src = self._mask_parts = self._mask_view = None
        self._refresh_layer_window()
        self._redraw_layers()
        self._mask_state_to_window()
        what = {'remove': "⌫ Removed", 'cut': "✂ Cut", 'copy': "⧉ Copied"}[kind]
        where = f" to {', '.join(made)}" if made else f" from {layer.name}"
        tail = (" - the picture is hidden (show it in 📚); 💾 Save layers keeps it see-through"
                if source == 'picture' and kind in ('remove', 'cut') else "")
        self.status.showMessage(f"🎭 {what}{where}{tail} · Ctrl+Z undoes", 8000)

    def _mask_new_layer(self, name, image, corners, like=None):
        self.add_layer()
        layer = self.active_layer
        layer.name = f"{name} {self._layer_count}"
        if like is not None:
            layer.opacity, layer.blend = like.opacity, like.blend
        image.setDevicePixelRatio(1.0)
        self.drawn_images.append({'image': image, 'corners': corners})
        self._undo_stack.append('image')
        layer.raster = None
