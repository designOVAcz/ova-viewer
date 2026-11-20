"""🎮 The canvas drawn on the graphics card while the view moves.

Zooming or panning, the picture and each layer's own image (see
RandomImageViewer._layer_canvas) are drawn here through OpenGL: the images
go to the graphics card once (again only when they change) and every step
of a zoom or pan is just drawing them at the new size - a fraction of a
millisecond, whole, with nothing to wait for, as in a paint program. The
plain canvas takes over again once the view rests or a stroke starts.

It is a window of its own, frameless, laid exactly over the canvas and
transparent to input - the mouse, wheel and pen go straight through to the
canvas under it. (As a widget inside the main window, its OpenGL made Qt
draw the whole window through OpenGL, and every dab of a stroke cost a
full-window composition: painting ran at half speed.) The tool panels lying
on the canvas are drawn over it as they look (grabbed once per zoom).
"""

import math

from PySide6.QtCore import QEvent, QPoint, QRect, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QRegion
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtWidgets import QWidget


class GpuView(QOpenGLWidget):
    passes_input = True     # the canvas under it gets the mouse and pen (see ImageLabel)

    def __init__(self, window, canvas, embedded=False):
        # *embedded*: the canvas itself, a child widget of it inside the main
        # window (the GPU canvas while painting) - the pen goes straight to
        # the canvas, its panels lie over it as they are, and Qt draws a
        # frame per screen refresh. Else a window of its own laid over the
        # canvas (zoom / pan on the plain canvas): no Qt parent - made with
        # the main window as parent it left every stroke on the plain canvas
        # after it about twice as slow; it belongs to the main window through
        # Windows only (owner, see _own).
        self.embedded = embedded
        if embedded:
            super().__init__(canvas)
        else:
            super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowTransparentForInput
                             | Qt.WindowDoesNotAcceptFocus | Qt.NoDropShadowWindowHint)
        self._owner = window
        self._owned = False
        self._canvas = canvas
        # The last frame kept between paints: a pen move redraws only the
        # piece it changed (see update_area) - drawn whole, every move sent
        # the whole stroke image to the graphics card again (~15 ms).
        self.setUpdateBehavior(QOpenGLWidget.PartialUpdate)
        self._full = True       # the next paint draws everything
        self._textures = {}     # image serial -> (QOpenGLTexture, size): layers on the card
        self._used_textures = set()
        self._area = None       # ... else only this (canvas coords)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setFocusPolicy(Qt.NoFocus)
        self._background = QColor(0, 0, 0)
        self._items = []        # (image, transform, opacity, composition mode or None, fill)
        self._overlay = None    # callable(painter): lines drawn over it all (guides)
        self._panels = []       # (pixmap, QPoint): tool panels on the canvas, as they look
        self._painted = set()   # cacheKeys of the images drawn last: on the graphics card already
        self.failed = False
        self.active = False     # showing (opaque) - else there but see-through, preloading
        # Drawn over the scene each frame: the stroke being drawn, the grid
        # (see RandomImageViewer._gpu_overlay) - the canvas as the label
        # would show it, when this is the display.
        self.on_top = None
        # The panels lying on the canvas show through holes in this window,
        # live (a hover, a slider) - see refresh_holes.
        self._holes = None
        self._watched = set()
        self._hole_timer = QTimer(self)
        self._hole_timer.setSingleShot(True)
        self._hole_timer.timeout.connect(self.refresh_holes)
        canvas.installEventFilter(self)
        if not embedded:
            window.installEventFilter(self)
        else:
            self.hide()

    def eventFilter(self, obj, event):
        t = event.type()
        if obj is self._owner:
            if t in (QEvent.Move, QEvent.Resize, QEvent.WindowStateChange) and self.isVisible():
                self.place()
        elif obj is self._canvas:
            if self.embedded:
                if t == QEvent.Resize:
                    self.place()
                return False
            if t in (QEvent.Move, QEvent.Resize) and self.isVisible():
                self.place()
            elif t in (QEvent.ChildAdded, QEvent.ChildRemoved):
                self._hole_timer.start(0)
            elif t == QEvent.Hide and self.isVisible():
                # The canvas gave way (the thumbnail grid, a video): so does this.
                hidden = getattr(self._owner, '_gpu_canvas_hidden', None)
                if hidden is not None:
                    hidden()
        elif t in (QEvent.Move, QEvent.Resize, QEvent.Show, QEvent.Hide):
            self._hole_timer.start(0)
        return False

    def refresh_holes(self):
        """Holes where the tool panels lie on the canvas, so they show
        (and answer) as they are - drawn as pictures over this window,
        they froze until the next zoom. (Embedded, the panels are its
        siblings above it: nothing to cut.)"""
        if self.embedded:
            return
        region = QRegion(self.rect())
        for child in self._canvas.children():
            if not isinstance(child, QWidget) or child.isWindow():
                continue
            if child not in self._watched:
                child.installEventFilter(self)
                self._watched.add(child)
            if child.isVisible():
                region = region.subtracted(QRegion(child.geometry()))
        if region != self._holes:
            self._holes = region
            if region == QRegion(self.rect()):
                self.clearMask()
            else:
                self.setMask(region)

    # Off the screen until used: shown over the canvas only once it has
    # drawn the view (a window shown first was black while the images went
    # to the graphics card). Not hidden by window opacity: that made it a
    # layered window, and Windows draws OpenGL in one through a slow copy -
    # painting on it lagged far behind the pen.
    _AWAY = QPoint(-32000, -32000)

    def ensure_shown(self):
        """There (see-through until active, see activate)."""
        if self.embedded:
            return              # shown by activate, under the canvas's panels
        if not self.isVisible():
            if not self.active:
                self.setWindowOpacity(0.0)      # see-through until used (see activate)
            self._own()
            self.place()
            self.show()

    def nativeEvent(self, event_type, message):
        """Windows asks what is under the pointer (WM_NCHITTEST): nothing
        here - the mouse and the pen go on to the canvas under it (the main
        window, same thread). Qt lets input through with a layered window,
        which made the drawing here slow (see _AWAY)."""
        try:
            if event_type == b"windows_generic_MSG" or event_type == "windows_generic_MSG":
                import ctypes.wintypes
                msg = ctypes.wintypes.MSG.from_address(int(message))
                if msg.message == 0x0084:           # WM_NCHITTEST
                    return True, -1                 # HTTRANSPARENT
        except Exception:
            pass
        return super().nativeEvent(event_type, message)

    def _unlayer(self):
        """No WS_EX_LAYERED on it (see _AWAY)."""
        try:
            import ctypes
            u32 = ctypes.windll.user32
            u32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
            u32.SetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
            hwnd = ctypes.c_void_p(int(self.winId()))
            ex = u32.GetWindowLongPtrW(hwnd, -20)           # GWL_EXSTYLE
            if ex & 0x80000:                                # WS_EX_LAYERED
                u32.SetWindowLongPtrW(hwnd, -20, ex & ~0x80000)
        except Exception:
            pass

    def _own(self):
        """The main window as this window's owner, window to window only
        (a transient parent, not a widget parent): above it, minimised
        with it."""
        if self._owned:
            return
        try:
            self.winId()
            mine, theirs = self.windowHandle(), self._owner.windowHandle()
            if mine is not None and theirs is not None:
                mine.setTransientParent(theirs)
                self._owned = True
        except Exception as e:
            print(f"GPU view owner not set: {e}")

    def ready_for(self, items):
        """Every image in *items* is on the graphics card already (drawn
        before): showing it now shows them at once, not black."""
        for image, *_rest in items:
            if image is not None and image.cacheKey() not in self._painted:
                return False
        return True

    def activate(self):
        """Seen from now on - drawn with this view first: made opaque with
        an update still waiting, it showed what it drew last (the view
        before, for a frame - a blink)."""
        if not self.active:
            self.active = True
            self._keep()
            if self.embedded:
                self.place()
                self.show()
                self.lower()        # the panels lying on the canvas stay over it
                return
            self.repaint()          # drawn first, then seen
            self.setWindowOpacity(1.0)

    def put_away_soon(self, ms=50):
        """Away once the canvas under it is on screen: hidden at once, the
        canvas from before the zoom showed for a frame - a blink. A zoom
        again meanwhile keeps it (see set_scene)."""
        from PySide6.QtCore import QTimer
        timer = getattr(self, '_away_timer', None)
        if timer is None:
            timer = self._away_timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(self.put_away)
        timer.start(ms)

    def _keep(self):
        timer = getattr(self, '_away_timer', None)
        if timer is not None:
            timer.stop()

    def put_away(self):
        """Gone until the next zoom - hidden, not just see-through: a window
        kept over the canvas slowed every dab of painting. Shown again, it
        draws once see-through before it is used (see ready_for)."""
        self.active = False
        self._panels = []
        self._painted = set()
        if self.isVisible():
            if not self.embedded:
                self.setWindowOpacity(0.0)
            self.hide()

    def place(self):
        """Exactly over the canvas, on screen."""
        if self.embedded:
            if self.geometry() != self._canvas.rect():
                self.setGeometry(self._canvas.rect())
            return
        top_left = self._canvas.mapToGlobal(QPoint(0, 0))
        rect = QRect(top_left, self._canvas.size())
        if self.geometry() != rect:
            resized = self.size() != rect.size()
            self.setGeometry(rect)
            if resized:
                self._holes = None
                self.refresh_holes()

    def grab_panels(self, panels):
        """The tool panels lying on the canvas, as pictures to draw on top."""
        self._panels = []
        for panel in panels:
            try:
                if panel.isVisible():
                    self._panels.append((panel.grab(), panel.mapTo(self._canvas, QPoint(0, 0))))
            except RuntimeError:
                pass

    def set_scene(self, background, items, overlay=None):
        """*items*: (QImage | QPixmap | None, QTransform, opacity, mode, fill)
        drawn in order - an image through its transform, or (image None)
        fill = (QRectF, QColor) through it; *overlay* draws on top,
        untransformed (canvas coordinates)."""
        self._keep()
        self._background = QColor(background)
        self._items = list(items)
        self._overlay = overlay
        self.place()
        self._full = True
        self.update()

    def update_area(self, rect):
        """Only *rect* (canvas coords) changed: the next paint redraws
        just that, over the frame kept from before."""
        if not self._full:
            self._area = QRect(rect) if self._area is None else self._area.united(rect)
        self.update()

    def resizeGL(self, w, h):
        self._full = True

    def paintGL(self):
        p = QPainter(self)
        area = None if self._full else self._area
        if self._full or area is None:
            area = None
            self._full = False
        self._area = None
        self._area_dev = None
        try:
            if area is not None:
                # The piece snapped to whole device pixels, and that one
                # rectangle used for both clips - QPainter's (picture,
                # background) and the scissor (layer and stroke textures):
                # rounded each its own way at 150 % scaling, a 1 px strip at
                # the edges got only one of them - thin lines in the stroke.
                dpr = self.devicePixelRatioF()
                x0, y0 = math.floor(area.x() * dpr), math.floor(area.y() * dpr)
                x1 = math.ceil((area.x() + area.width()) * dpr)
                y1 = math.ceil((area.y() + area.height()) * dpr)
                self._area_dev = QRect(x0, y0, x1 - x0, y1 - y0)
                area = QRectF(x0 / dpr, y0 / dpr, (x1 - x0) / dpr, (y1 - y0) / dpr)
                p.setClipRect(area)
            p.fillRect(self.rect(), self._background)
            p.setRenderHint(QPainter.SmoothPixmapTransform, True)
            self._painted = {image.cacheKey() for image, *_r in self._items if image is not None}
            for image, transform, opacity, mode, fill in self._items:
                p.save()
                p.setTransform(transform)
                p.setOpacity(opacity)
                if mode is not None:
                    p.setCompositionMode(mode)
                if image is None:
                    if fill is not None:
                        rect, colour = fill
                        p.fillRect(QRectF(rect), colour)
                elif (mode is not None and mode != QPainter.CompositionMode_SourceOver
                        and not hasattr(image, 'toImage')
                        and self._blend_layer(p, image, fill if isinstance(fill, dict) else None,
                                              transform, opacity, mode, area)):
                    pass        # a layer in Multiply, Screen ... (see _blend_layer)
                elif (isinstance(fill, dict) and mode is None and not self._textures_failed
                        and self._draw_layer_texture(p, image, fill, transform, opacity, area)):
                    pass        # a layer's own image, kept on the graphics card (see _layer_texture)
                elif hasattr(image, 'toImage'):
                    p.drawPixmap(0, 0, image)
                else:
                    p.drawImage(0, 0, image)
                p.restore()
            if self._overlay is not None:
                self._overlay(p)
            if self.on_top is not None:
                p.resetTransform()
                p.setOpacity(1.0)
                p.setCompositionMode(QPainter.CompositionMode_SourceOver)
                self.on_top(p, area)
            if self.active:
                for pixmap, at in self._panels:
                    p.drawPixmap(at, pixmap)
        except Exception as e:          # a driver that will not: the plain path from now on
            print(f"🎮 GPU view failed, drawing on the CPU: {e}")
            self.failed = True
        finally:
            p.end()
        self._drop_unused_textures()

    # -- layers kept on the graphics card --------------------------------
    #
    # A layer's image is a texture of its own here, sent once and then only
    # where it changed (the main window notes each change: see
    # RandomImageViewer._canvas_note). Drawn through QPainter, every change
    # sent the whole image again - ~70 MB, ~20 ms at every pen-up. With
    # mipmaps, so a zoomed-out view is smooth rather than grainy.

    _textures_failed = False

    def _layer_texture(self, image, note):
        """*image*'s texture, brought up to date (see _canvas_note)."""
        from PySide6.QtOpenGL import QOpenGLTexture
        GL_TEXTURE_2D, GL_BGRA, GL_UNSIGNED_BYTE = 0x0DE1, 0x80E1, 0x1401
        GL_UNPACK_ROW_LENGTH, GL_UNPACK_ALIGNMENT = 0x0CF2, 0x0CF5
        if not hasattr(self, '_textures'):
            self._textures = {}
        if image.format() != image.Format.Format_ARGB32_Premultiplied:
            return None
        serial = image.cacheKey() >> 32
        w, h = image.width(), image.height()
        kept = self._textures.get(serial)
        f = self.context().functions()
        full = (kept is None or kept[1] != (w, h) or note.get('all')
                or note.get('key') != image.cacheKey())
        if full:
            if kept is not None:
                kept[0].destroy()
            tex = QOpenGLTexture(QOpenGLTexture.Target2D)
            tex.setFormat(QOpenGLTexture.RGBA8_UNorm)
            tex.setSize(w, h)
            mip = note.get('mip', True)        # the live stroke (drawn 1:1) needs none
            tex.setMipLevels(tex.maximumMipLevels() if mip else 1)
            tex.allocateStorage(QOpenGLTexture.BGRA, QOpenGLTexture.UInt8)
            tex.setMinMagFilters(QOpenGLTexture.LinearMipMapLinear if mip else QOpenGLTexture.Linear,
                                 QOpenGLTexture.Linear)
            tex.setWrapMode(QOpenGLTexture.ClampToEdge)
            rects = [QRect(0, 0, w, h)]
            self._textures[serial] = kept = (tex, (w, h))
        else:
            tex = kept[0]
            rects = [r.intersected(QRect(0, 0, w, h)) for r in note.get('rects', [])]
            rects = [r for r in rects if not r.isEmpty()]
        if rects:
            f.glBindTexture(GL_TEXTURE_2D, tex.textureId())
            f.glPixelStorei(GL_UNPACK_ALIGNMENT, 4)
            for r in rects:
                # Just the rectangle, as a small image of its own: handed a
                # slice of the whole image, the binding copied all the rest
                # of it (megabytes) for every upload.
                piece = image if r == image.rect() else image.copy(r)
                f.glPixelStorei(GL_UNPACK_ROW_LENGTH, piece.bytesPerLine() // 4)
                f.glTexSubImage2D(GL_TEXTURE_2D, 0, r.x(), r.y(), r.width(), r.height(),
                                  GL_BGRA, GL_UNSIGNED_BYTE, piece.constBits())
            f.glPixelStorei(GL_UNPACK_ROW_LENGTH, 0)
            if note.get('mip', True):
                tex.generateMipMaps()
        note['key'] = image.cacheKey()
        note['rects'] = []
        note['all'] = False
        self._used_textures.add(serial)
        return tex

    def _draw_layer_texture(self, p, image, note, transform, opacity, area):
        """Draw a layer's texture through *transform* (image px -> canvas
        coords) at *opacity*, premultiplied, clipped to *area*. False when
        it cannot (then QPainter draws it the plain way)."""
        try:
            from PySide6.QtGui import QMatrix4x4, QTransform
            from PySide6.QtOpenGL import QOpenGLTextureBlitter
            GL_BLEND, GL_SCISSOR_TEST = 0x0BE2, 0x0C11
            GL_ONE, GL_ONE_MINUS_SRC_ALPHA, GL_CONSTANT_ALPHA = 1, 0x0303, 0x8003
            p.beginNativePainting()
            try:
                tex = self._layer_texture(image, note)
                if tex is None:
                    return False
                self._blit(tex.textureId(), image.width(), image.height(), transform, opacity, area)
                return True
            finally:
                p.endNativePainting()
        except Exception as e:
            print(f"🎮 Layer textures off (drawn the plain way): {e}")
            self._textures_failed = True
            return False

    def draw_texture_id(self, p, texture_id, w, h, transform, opacity, area):
        """A texture of another context (shared - the GPU stroke's preview)
        drawn like a layer's (see _blit). False when it cannot."""
        try:
            p.beginNativePainting()
            try:
                self._blit(texture_id, w, h, transform, opacity, area)
                return True
            finally:
                p.endNativePainting()
        except Exception as e:
            print(f"🎮 GPU stroke not drawn: {e}")
            return False

    def _blit(self, texture_id, w, h, transform, opacity, area):
        """Texture *texture_id* (w x h, row 0 = top, premultiplied) through
        *transform* (texture px -> canvas coords) at *opacity*, clipped to
        *area*. Native painting must be on."""
        from PySide6.QtGui import QMatrix4x4, QTransform
        from PySide6.QtOpenGL import QOpenGLTextureBlitter
        GL_BLEND, GL_SCISSOR_TEST = 0x0BE2, 0x0C11
        GL_ONE, GL_ONE_MINUS_SRC_ALPHA, GL_CONSTANT_ALPHA = 1, 0x0303, 0x8003
        if True:
            if True:
                if getattr(self, '_blitter', None) is None:
                    self._blitter = QOpenGLTextureBlitter()
                    self._blitter.create()
                f = self.context().functions()
                dpr = self.devicePixelRatioF()
                W, H = self.width(), self.height()
                f.glViewport(0, 0, int(round(W * dpr)), int(round(H * dpr)))
                if area is not None:
                    dev = getattr(self, '_area_dev', None)
                    if dev is None:
                        dev = QRect(math.floor(area.x() * dpr), math.floor(area.y() * dpr),
                                    math.ceil(area.width() * dpr), math.ceil(area.height() * dpr))
                    f.glEnable(GL_SCISSOR_TEST)
                    f.glScissor(dev.x(), int(round(H * dpr)) - dev.y() - dev.height(), dev.width(), dev.height())
                # The blitter's quad (-1..1) -> image px -> canvas -> clip space.
                quad = QTransform(w / 2.0, 0, 0, h / 2.0, w / 2.0, h / 2.0)
                clip = QTransform(2.0 / W, 0, 0, -2.0 / H, -1.0, 1.0)
                matrix = QMatrix4x4(quad * transform * clip)
                f.glEnable(GL_BLEND)
                # Premultiplied, at the layer's opacity: colour * opacity
                # (the constant), what is under kept by 1 - alpha * opacity
                # (the blitter scales alpha by its opacity).
                f.glBlendColor(0.0, 0.0, 0.0, float(opacity))
                f.glBlendFuncSeparate(GL_CONSTANT_ALPHA, GL_ONE_MINUS_SRC_ALPHA, GL_ONE, GL_ONE_MINUS_SRC_ALPHA)
                b = self._blitter
                b.bind()
                b.setOpacity(float(opacity))
                b.blit(texture_id, matrix, QOpenGLTextureBlitter.OriginBottomLeft)
                b.release()
                if area is not None:
                    f.glDisable(GL_SCISSOR_TEST)

    # -- layer blend modes, on the graphics card --------------------------
    #
    # QPainter on OpenGL knows SourceOver and little else: a layer in
    # Multiply, Screen, Overlay ... was not drawn at all. Here a shader does
    # what the CPU's QPainter does: what is drawn so far is copied to a
    # texture, and the layer goes on with the W3C compositing formula
    #   out = (1 - Sa) D + (1 - Da) S + Sa Da B(Sc, Dc)     (premultiplied)
    # B being the mode's blend of the unpremultiplied colours.

    _BLEND_IDS = None
    _blend_failed = False

    @classmethod
    def _blend_id(cls, mode):
        if cls._BLEND_IDS is None:
            M = QPainter
            cls._BLEND_IDS = {
                M.CompositionMode_Multiply: 1, M.CompositionMode_Screen: 2,
                M.CompositionMode_Overlay: 3, M.CompositionMode_SoftLight: 4,
                M.CompositionMode_HardLight: 5, M.CompositionMode_Darken: 6,
                M.CompositionMode_Lighten: 7, M.CompositionMode_ColorDodge: 8,
                M.CompositionMode_ColorBurn: 9, M.CompositionMode_Difference: 10,
                M.CompositionMode_Exclusion: 11, M.CompositionMode_Plus: 12}
        return cls._BLEND_IDS.get(mode)

    _BLEND_FRAG = """
        uniform sampler2D src;
        uniform sampler2D dst;
        uniform vec2 dstSize;
        uniform float opacity;
        uniform int mode;
        float soft(float s, float d) {
            if (s <= 0.5) return d - (1.0 - 2.0 * s) * d * (1.0 - d);
            float e = d <= 0.25 ? ((16.0 * d - 12.0) * d + 4.0) * d : sqrt(d);
            return d + (2.0 * s - 1.0) * (e - d);
        }
        float hard(float s, float d) {
            return s <= 0.5 ? 2.0 * s * d : 1.0 - 2.0 * (1.0 - s) * (1.0 - d);
        }
        float dodge(float s, float d) {
            if (d <= 0.0) return 0.0;
            if (s >= 1.0) return 1.0;
            return min(1.0, d / (1.0 - s));
        }
        float burn(float s, float d) {
            if (d >= 1.0) return 1.0;
            if (s <= 0.0) return 0.0;
            return 1.0 - min(1.0, (1.0 - d) / s);
        }
        float one(float s, float d) {
            if (mode == 1) return s * d;
            if (mode == 2) return s + d - s * d;
            if (mode == 3) return hard(d, s);
            if (mode == 4) return soft(s, d);
            if (mode == 5) return hard(s, d);
            if (mode == 6) return min(s, d);
            if (mode == 7) return max(s, d);
            if (mode == 8) return dodge(s, d);
            if (mode == 9) return burn(s, d);
            if (mode == 10) return abs(s - d);
            if (mode == 11) return s + d - 2.0 * s * d;
            return min(1.0, s + d);
        }
        vec4 blended(vec2 tc, vec2 frag) {
            vec4 s = TEX(src, tc) * opacity;
            vec4 d = TEX(dst, frag / dstSize);
            vec3 sc = s.a > 0.0 ? s.rgb / s.a : vec3(0.0);
            vec3 dc = d.a > 0.0 ? d.rgb / d.a : vec3(0.0);
            vec3 b = vec3(one(sc.r, dc.r), one(sc.g, dc.g), one(sc.b, dc.b));
            return vec4((1.0 - s.a) * d.rgb + (1.0 - d.a) * s.rgb + s.a * d.a * b,
                        s.a + d.a - s.a * d.a);
        }
    """

    def _blend_program(self):
        """The blend shader for this context (made once), or None."""
        prog = getattr(self, '_blend_prog', None)
        if prog is not None:
            return prog
        from PySide6.QtGui import QSurfaceFormat
        from PySide6.QtOpenGL import QOpenGLShaderProgram, QOpenGLShader
        ctx = self.context()
        fmt = ctx.format()
        if ctx.isOpenGLES():
            head_v = head_f = "#version 100\nprecision highp float;\n"
            vin, vout, fin, tex, outv = "attribute", "varying", "varying", "texture2D", "gl_FragColor"
            decl = ""
        elif fmt.profile() == QSurfaceFormat.CoreProfile:
            head_v = head_f = "#version 330 core\n"
            vin, vout, fin, tex, outv = "in", "out", "in", "texture", "fragColour"
            decl = "out vec4 fragColour;\n"
        else:
            head_v = head_f = "#version 120\n"
            vin, vout, fin, tex, outv = "attribute", "varying", "varying", "texture2D", "gl_FragColor"
            decl = ""
        vert = (head_v + f"{vin} vec2 pos;\nuniform mat4 matrix;\n{vout} vec2 tc;\n"
                "void main() { tc = pos; gl_Position = matrix * vec4(pos, 0.0, 1.0); }\n")
        frag = (head_f + f"#define TEX {tex}\n{fin} vec2 tc;\n{decl}" + self._BLEND_FRAG
                + f"void main() {{ {outv} = blended(tc, gl_FragCoord.xy); }}\n")
        prog = QOpenGLShaderProgram()
        if not (prog.addShaderFromSourceCode(QOpenGLShader.Vertex, vert)
                and prog.addShaderFromSourceCode(QOpenGLShader.Fragment, frag)
                and prog.link()):
            raise RuntimeError("blend shader: " + prog.log())
        from PySide6.QtOpenGL import QOpenGLBuffer, QOpenGLVertexArrayObject
        import struct
        vao = QOpenGLVertexArrayObject()
        vao.create()
        vao.bind()
        buf = QOpenGLBuffer(QOpenGLBuffer.VertexBuffer)
        buf.create()
        buf.bind()
        data = struct.pack('8f', 0, 0, 1, 0, 0, 1, 1, 1)
        buf.allocate(data, len(data))
        prog.bind()
        loc = prog.attributeLocation("pos")
        prog.enableAttributeArray(loc)
        prog.setAttributeBuffer(loc, 0x1406, 0, 2)          # GL_FLOAT
        prog.release()
        buf.release()
        vao.release()
        self._blend_prog, self._blend_vao, self._blend_buf = prog, vao, buf
        return prog

    def _blend_layer(self, p, image, note, transform, opacity, mode, area):
        """Draw a layer's image in blend mode *mode* (a QPainter
        CompositionMode) over what is drawn so far. False when it cannot."""
        code = self._blend_id(mode)
        if code is None or self._blend_failed:
            return False
        from PySide6.QtGui import QMatrix4x4, QTransform
        from PySide6.QtOpenGL import QOpenGLTexture
        GL_TEXTURE_2D, GL_TEXTURE0, GL_BLEND, GL_SCISSOR_TEST = 0x0DE1, 0x84C0, 0x0BE2, 0x0C11
        GL_TRIANGLE_STRIP = 0x0005
        try:
            p.beginNativePainting()
            try:
                if note is None:
                    # (an image of its own each time - an impasto-lit layer):
                    # sent whole when it is a new one, else kept
                    notes = self.__dict__.setdefault('_blend_notes', {})
                    note = notes.setdefault(image.cacheKey() >> 32, {'key': None, 'rects': [], 'all': True})
                if image.format() != image.Format.Format_ARGB32_Premultiplied:
                    image = image.convertToFormat(image.Format.Format_ARGB32_Premultiplied)
                tex = self._layer_texture(image, note)
                if tex is None:
                    return False
                prog = self._blend_program()
                f = self.context().functions()
                dpr = self.devicePixelRatioF()
                W, H = self.width(), self.height()
                dw, dh = int(round(W * dpr)), int(round(H * dpr))
                # What is drawn so far, as a texture (the part being drawn).
                dst = getattr(self, '_blend_dst', None)
                if dst is None or (dst.width(), dst.height()) != (dw, dh):
                    if dst is not None:
                        dst.destroy()
                    dst = self._blend_dst = QOpenGLTexture(QOpenGLTexture.Target2D)
                    dst.setFormat(QOpenGLTexture.RGBA8_UNorm)
                    dst.setSize(dw, dh)
                    dst.setMipLevels(1)
                    dst.allocateStorage(QOpenGLTexture.RGBA, QOpenGLTexture.UInt8)
                    dst.setMinMagFilters(QOpenGLTexture.Nearest, QOpenGLTexture.Nearest)
                    dst.setWrapMode(QOpenGLTexture.ClampToEdge)
                dev = getattr(self, '_area_dev', None) if area is not None else None
                box = dev if dev is not None else QRect(0, 0, dw, dh)
                gx, gy = box.x(), dh - box.y() - box.height()       # GL: from the bottom
                f.glViewport(0, 0, dw, dh)
                f.glActiveTexture(GL_TEXTURE0 + 1)
                f.glBindTexture(GL_TEXTURE_2D, dst.textureId())
                f.glCopyTexSubImage2D(GL_TEXTURE_2D, 0, gx, gy, gx, gy, box.width(), box.height())
                f.glActiveTexture(GL_TEXTURE0)
                f.glBindTexture(GL_TEXTURE_2D, tex.textureId())
                if dev is not None:
                    f.glEnable(GL_SCISSOR_TEST)
                    f.glScissor(gx, gy, box.width(), box.height())
                f.glDisable(GL_BLEND)           # the shader writes the result itself
                # unit quad -> image px -> canvas -> clip space
                quad = QTransform(image.width(), 0, 0, image.height(), 0, 0)
                clip = QTransform(2.0 / W, 0, 0, -2.0 / H, -1.0, 1.0)
                prog.bind()
                prog.setUniformValue("matrix", QMatrix4x4(quad * transform * clip))
                prog.setUniformValue1i("src", 0)
                prog.setUniformValue1i("dst", 1)
                prog.setUniformValue("dstSize", float(dw), float(dh))
                prog.setUniformValue1f("opacity", float(opacity))
                prog.setUniformValue1i("mode", int(code))
                self._blend_vao.bind()
                f.glDrawArrays(GL_TRIANGLE_STRIP, 0, 4)
                self._blend_vao.release()
                prog.release()
                if dev is not None:
                    f.glDisable(GL_SCISSOR_TEST)
                f.glActiveTexture(GL_TEXTURE0 + 1)
                f.glBindTexture(GL_TEXTURE_2D, 0)
                f.glActiveTexture(GL_TEXTURE0)
                return True
            finally:
                p.endNativePainting()
        except Exception as e:
            print(f"\U0001F3AE Layer blend modes off on the GPU: {e}")
            self._blend_failed = True
            return False

    def _drop_unused_textures(self):
        """Textures of images no longer drawn: freed (their memory)."""
        used = getattr(self, '_used_textures', None)
        textures = getattr(self, '_textures', None)
        if textures:
            gone = [s for s in textures if s not in (used or ())]
            if gone:
                # Freed with this window's own context current - switching
                # it away (doneCurrent) while the native window was still
                # finishing its frame crashed it.
                from PySide6.QtGui import QOpenGLContext
                ours = QOpenGLContext.currentContext() is self.context()
                if not ours:
                    self.makeCurrent()
                for s in gone:
                    textures.pop(s)[0].destroy()
                if not ours:
                    self.doneCurrent()
        self._used_textures = set()


# -- 🎮 the GPU canvas: a native OpenGL window inside the canvas ---------------
#
# The canvas drawn on the graphics card while painting. A native child window
# of the canvas (QOpenGLWindow in a window container): Windows composes it
# directly and the main window stays an ordinary one - 0.07 ms an update, Qt
# showing a frame per screen refresh. The two tried before were both slow: an
# OpenGL widget inside the main window made Qt draw the whole window through
# OpenGL (~55 ms a pen move), and a window of its own laid over the canvas
# needed a layered window / hit-test tricks and fell behind the pen.
#
# The mouse, pen, wheel and touch reach this window first: they are handed
# to the canvas (ImageLabel) as they are - its coordinates are the canvas's.
# The panels lying on the canvas become native too, above it.

from PySide6.QtGui import QOpenGLContext  # noqa: E402
from PySide6.QtOpenGL import QOpenGLWindow  # noqa: E402


class GpuCanvasWindow(QOpenGLWindow):
    passes_input = True
    embedded = True

    # the scene drawing, shared with GpuView
    set_scene = GpuView.set_scene
    update_area = GpuView.update_area
    paintGL = GpuView.paintGL
    _layer_texture = GpuView._layer_texture
    _draw_layer_texture = GpuView._draw_layer_texture
    _drop_unused_textures = GpuView._drop_unused_textures
    draw_texture_id = GpuView.draw_texture_id
    _blit = GpuView._blit
    _blend_id = GpuView._blend_id
    _BLEND_FRAG = GpuView._BLEND_FRAG
    _blend_program = GpuView._blend_program
    _blend_layer = GpuView._blend_layer
    _blend_failed = False
    _keep = GpuView._keep
    ready_for = GpuView.ready_for
    _textures_failed = False

    _INPUT = None

    def __init__(self, window, canvas):
        super().__init__(QOpenGLWindow.PartialUpdateBlit)
        self._owner = window
        self._canvas = canvas
        self._full = True
        self._area = None
        self._textures = {}
        self._used_textures = set()
        self._background = QColor(0, 0, 0)
        self._items = []
        self._overlay = None
        self._panels = []
        self._painted = set()
        self.failed = False
        self.active = False
        self.on_top = None
        self.setFlag(Qt.WindowDoesNotAcceptFocus, True)
        from PySide6.QtWidgets import QWidget as _W
        self.box = _W.createWindowContainer(self, canvas)
        self.box.setAttribute(Qt.WA_NativeWindow, True)
        self.box.setFocusPolicy(Qt.NoFocus)
        self.box.passes_input = True        # not a panel over the canvas (see ImageLabel._ui_at)
        self.box.hide()
        canvas.installEventFilter(self)
        self._natives = set()

    # -- what GpuView's drawing asks of it
    def rect(self):
        return QRect(0, 0, self.width(), self.height())

    def devicePixelRatioF(self):
        return self.devicePixelRatio()

    def place(self):
        if self.box.geometry() != self._canvas.rect():
            self.box.setGeometry(self._canvas.rect())
            self._full = True

    def refresh_holes(self):
        self._panels_over()

    def grab_panels(self, panels):
        self._panels = []

    def ensure_shown(self):
        pass

    def isVisible(self):
        return self.box.isVisible()

    def activate(self):
        if not self.active:
            self.active = True
            self.place()
            self.box.show()
            self.box.lower()
            self._panels_over()
            self._full = True
            self.requestUpdate()

    def put_away_soon(self, ms=50):
        self.put_away()

    def put_away(self):
        self.active = False
        self._painted = set()
        if self.box.isVisible():
            self.box.hide()

    def _panels_over(self):
        """The panels lying on the canvas: native windows above this one
        (a plain widget under a native window is hidden by it)."""
        for child in self._canvas.children():
            if (child is self.box or not isinstance(child, QWidget) or child.isWindow()
                    or not child.isVisible()):
                continue
            if child not in self._natives:
                child.setAttribute(Qt.WA_NativeWindow, True)
                self._natives.add(child)
            child.raise_()

    def eventFilter(self, obj, event):
        t = event.type()
        if obj is self._canvas:
            if t == QEvent.Resize:
                self.place()
            elif t == QEvent.CursorChange:
                self.setCursor(self._canvas.cursor())       # the brush ring over the GPU canvas too
            elif t == QEvent.ChildAdded and self.active:
                QTimer.singleShot(0, self._panels_over)
        return False

    # -- input: on to the canvas, as it is
    def event(self, e):
        types = self._input_types()
        t = e.type()
        if t in types:
            from PySide6.QtWidgets import QApplication
            QApplication.sendEvent(self._canvas, e)
            if t == QEvent.MouseButtonRelease and e.button() == Qt.RightButton:
                # the canvas's right-click menu (it asks for it on this event)
                from PySide6.QtGui import QContextMenuEvent
                pos = e.position().toPoint()
                QApplication.sendEvent(self._canvas, QContextMenuEvent(
                    QContextMenuEvent.Mouse, pos, self._canvas.mapToGlobal(pos), e.modifiers()))
            if t in (QEvent.TabletPress, QEvent.TabletMove, QEvent.TabletRelease):
                return e.isAccepted()       # not taken: Qt makes the mouse event, handed on too
            return True
        return super().event(e)

    @classmethod
    def _input_types(cls):
        if cls._INPUT is None:
            cls._INPUT = {QEvent.MouseButtonPress, QEvent.MouseButtonRelease, QEvent.MouseMove,
                          QEvent.MouseButtonDblClick, QEvent.Wheel, QEvent.TabletPress,
                          QEvent.TabletMove, QEvent.TabletRelease, QEvent.TabletEnterProximity,
                          QEvent.TabletLeaveProximity, QEvent.TouchBegin, QEvent.TouchUpdate,
                          QEvent.TouchEnd, QEvent.TouchCancel, QEvent.Enter, QEvent.Leave,
                          QEvent.NativeGesture}
        return cls._INPUT
