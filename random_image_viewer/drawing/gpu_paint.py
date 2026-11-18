"""🎮 Brush stamps painted straight into a layer image on the graphics card.

A prototype (not wired into the app yet): a layer lives as one texture on the
GPU (RGBA16F by default, premultiplied), and stamps go onto it as instanced
quads - one draw call per batch, whatever the number of stamps. Pure PySide6:
QOffscreenSurface + QOpenGLContext, QOpenGLFramebufferObject, QOpenGLTexture,
QOpenGLShaderProgram, QOpenGLBuffer / QOpenGLVertexArrayObject and
QOpenGLExtraFunctions (no PyOpenGL).

* Build-up stamps (:meth:`GpuLayer.paint_stamps`): as brushes.paint_stamps -
  each stamp the tip at the mip level the CPU would pick, turned, scaled to
  its size, at its opacity, SourceOver on premultiplied colour.
* Flat strokes (:meth:`GpuLayer.paint_flat`): as FlatStroke's core - the
  coverage is the MAX any stamp gave a pixel (GL_MAX blending into a
  one-channel buffer, where the CPU uses a grey buffer under Lighten), the
  colour is laid over itself, and the two are composed onto the layer
  (straight colour x coverage, SourceOver).
* :meth:`GpuLayer.read` / :meth:`GpuLayer.write`: a sub-rectangle back as a
  premultiplied ARGB32 QImage (undo patches, saving) and a QImage into a
  sub-rectangle (glTexSubImage2D).

Orientation: the layer's row 0 is the texture's row 0 (GL's y = 0), so a
QImage goes up and comes back with no flipping; whoever draws the texture on
screen flips v instead.

What PySide6 does not give (and the way round it):

* glGenTextures / glGenBuffers / glBufferData: not bound (pointer outputs);
  QOpenGLTexture, QOpenGLBuffer.allocate() and QOpenGLFramebufferObject do
  the same.
* glVertexAttribPointer: its last argument is a void* and a plain int offset
  is refused; QOpenGLShaderProgram.setAttributeBuffer() takes the offset.
* glReadPixels / glTexSubImage2D take any writable / readable Python buffer
  (numpy array, bytearray, memoryview of QImage.bits()) - that works.
* QOpenGLVersionFunctionsFactory.get() wants a QOpenGLVersionProfile, not the
  class; QOpenGLExtraFunctions already has what is needed here
  (glDrawArraysInstanced, glVertexAttribDivisor, glBlendEquation).
"""

import math

import numpy as np
from PySide6.QtCore import QRect
from PySide6.QtGui import (QImage, QOffscreenSurface, QOpenGLContext, QSurfaceFormat,
                           QTransform)
from PySide6.QtOpenGL import (QOpenGLBuffer, QOpenGLFramebufferObject,
                              QOpenGLFramebufferObjectFormat, QOpenGLShader,
                              QOpenGLShaderProgram, QOpenGLTexture,
                              QOpenGLVertexArrayObject)

# GL enums PySide6 does not export as names
GL_TEXTURE_2D = 0x0DE1
GL_FLOAT = 0x1406
GL_UNSIGNED_BYTE = 0x1401
GL_BGRA = 0x80E1
GL_RGBA8 = 0x8058
GL_RGBA16F = 0x881A
GL_R16F = 0x822D
GL_BLEND = 0x0BE2
GL_SCISSOR_TEST = 0x0C11
GL_ONE = 1
GL_ONE_MINUS_SRC_ALPHA = 0x0303
GL_FUNC_ADD = 0x8006
GL_MAX = 0x8008
GL_TRIANGLE_STRIP = 0x0005
GL_COLOR_BUFFER_BIT = 0x4000
GL_PACK_ALIGNMENT = 0x0D05
GL_UNPACK_ALIGNMENT = 0x0CF5
GL_PACK_ROW_LENGTH = 0x0D02
GL_UNPACK_ROW_LENGTH = 0x0CF2
GL_TEXTURE_MIN_FILTER = 0x2801
GL_TEXTURE_MAG_FILTER = 0x2800
GL_LINEAR = 0x2601
GL_DEPTH_TEST = 0x0B71

OPACITY_STEPS = 32      # as brushes.OPACITY_STEPS: flat coverage is laid in these steps

_VERT = """
#version 330 core
layout(location = 0) in vec2 corner;      // 0..1 across the tip image
layout(location = 1) in vec2 centre;      // target px
layout(location = 2) in vec4 axes;        // the tip's x and y edge, target px
layout(location = 3) in vec4 colour;      // rgb 0..1, opacity
layout(location = 4) in float lod;        // mip level (as BrushTexture.level)
uniform vec2 target;                      // target size, px
uniform vec2 origin;                      // target's top-left, layer px
out vec2 uv;
out vec4 tint;
flat out float level;
void main() {
    vec2 p = centre - origin + (corner.x - 0.5) * axes.xy + (corner.y - 0.5) * axes.zw;
    gl_Position = vec4(p / target * 2.0 - 1.0, 0.0, 1.0);   // row 0 at y = 0: no flip
    uv = corner;
    tint = colour;
    level = lod;
}
"""

# mode 0: build-up (premultiplied colour x opacity); 1: flat coverage
# (opacity in OPACITY_STEPS, red only, under GL_MAX); 2: flat colour (opaque
# colour, its own opacity from `colour_opacity`)
_FRAG = """
#version 330 core
in vec2 uv;
in vec4 tint;
flat in float level;
uniform sampler2D tip;
uniform int mode;
uniform float steps;
uniform float colour_opacity;
out vec4 frag;
void main() {
    float a = textureLod(tip, uv, level).a;
    if (mode == 1) {
        float o = steps > 0.0 ? floor(tint.a * steps + 0.5) / steps : tint.a;
        frag = vec4(a * o, 0.0, 0.0, 0.0);
    } else if (mode == 2) {
        float k = a * colour_opacity;
        frag = vec4(tint.rgb * k, k);
    } else {
        float k = a * tint.a;
        frag = vec4(tint.rgb * k, k);
    }
}
"""

_COMPOSE_VERT = """
#version 330 core
layout(location = 0) in vec2 corner;
uniform vec4 box;          // x, y, w, h: layer px
uniform vec2 target;       // layer size
out vec2 local;            // px inside the box
void main() {
    vec2 p = box.xy + corner * box.zw;
    gl_Position = vec4(p / target * 2.0 - 1.0, 0.0, 1.0);
    local = corner * box.zw;
}
"""

_COMPOSE_FRAG = """
#version 330 core
in vec2 local;
uniform sampler2D mask;
uniform sampler2D colour;
uniform vec2 scratch;      // scratch textures' size
out vec4 frag;
void main() {
    vec2 st = local / scratch;
    float m = texture(mask, st).r;
    vec4 c = texture(colour, st);
    vec3 straight = c.a > 0.0 ? c.rgb / c.a : vec3(0.0);
    frag = vec4(straight * m, m);
}
"""


class GpuPainter:
    """The offscreen OpenGL context, its shaders and the tips on the card.
    One for the app (see :func:`shared`); every GpuLayer uses it. Made in
    the GUI thread."""

    def __init__(self, share=None):
        fmt = QSurfaceFormat()
        fmt.setVersion(3, 3)
        fmt.setProfile(QSurfaceFormat.CoreProfile)
        self.context = QOpenGLContext()
        self.context.setFormat(fmt)
        # With Qt.AA_ShareOpenGLContexts set before the QApplication, every
        # QOpenGLWidget shares with the global context, so the layers'
        # textures can be drawn by the screen view as they are.
        share = share if share is not None else QOpenGLContext.globalShareContext()
        if share is not None:
            self.context.setShareContext(share)
        if not self.context.create():
            raise RuntimeError("OpenGL context not made")
        self.surface = QOffscreenSurface()
        self.surface.setFormat(self.context.format())
        self.surface.create()
        self.current()
        self.f = self.context.extraFunctions()
        self.f.initializeOpenGLFunctions()
        self.renderer = self.f.glGetString(0x1F01)
        self.stamp_program = self._program(_VERT, _FRAG)
        self.compose_program = self._program(_COMPOSE_VERT, _COMPOSE_FRAG)
        self.vao = QOpenGLVertexArrayObject()
        self.vao.create()
        self.vao.bind()
        self.quad = QOpenGLBuffer(QOpenGLBuffer.VertexBuffer)
        self.quad.create()
        self.quad.bind()
        corners = np.array([0, 0, 1, 0, 0, 1, 1, 1], np.float32).tobytes()
        self.quad.allocate(corners, len(corners))
        self.instances = QOpenGLBuffer(QOpenGLBuffer.VertexBuffer)
        self.instances.setUsagePattern(QOpenGLBuffer.StreamDraw)
        self.instances.create()
        self._instance_bytes = 0
        self._tips = {}         # id(BrushTexture) -> (texture, QOpenGLTexture, longest per level)
        self._scratch = None    # (mask fbo, colour fbo) for flat strokes, grown as needed

    def current(self):
        if QOpenGLContext.currentContext() is not self.context:
            if not self.context.makeCurrent(self.surface):
                raise RuntimeError("OpenGL context not current")

    @staticmethod
    def _program(vert, frag):
        p = QOpenGLShaderProgram()
        if not p.addShaderFromSourceCode(QOpenGLShader.Vertex, vert):
            raise RuntimeError(p.log())
        if not p.addShaderFromSourceCode(QOpenGLShader.Fragment, frag):
            raise RuntimeError(p.log())
        if not p.link():
            raise RuntimeError(p.log())
        return p

    def finish(self):
        """Wait for the card (timing, or before another context reads)."""
        self.current()
        self.f.glFinish()

    # -- tips ---------------------------------------------------------------

    def tip(self, texture):
        """The tip's mip levels as one texture: the CPU's own scaled copies
        (BrushTexture._levels) as the mip chain, so a stamp samples exactly
        what the CPU would draw."""
        entry = self._tips.get(id(texture))
        if entry is not None and entry[0] is texture:
            return entry
        levels = texture._levels
        tex = QOpenGLTexture(QOpenGLTexture.Target2D)
        tex.setFormat(QOpenGLTexture.RGBA8_UNorm)
        tex.setSize(levels[0].width(), levels[0].height())
        tex.setMipLevels(len(levels))
        tex.allocateStorage()
        for i, image in enumerate(levels):
            # (Qt's halving, max(1, w // 2), is GL's mip size rule too)
            image = image.convertToFormat(QImage.Format_ARGB32_Premultiplied)
            tex.setData(i, QOpenGLTexture.BGRA, QOpenGLTexture.UInt8, bytes(image.constBits()))
        tex.setMipMaxLevel(len(levels) - 1)
        tex.setMinMagFilters(QOpenGLTexture.LinearMipMapNearest, QOpenGLTexture.Linear)
        tex.setWrapMode(QOpenGLTexture.ClampToEdge)
        longest = np.array([max(im.width(), im.height()) for im in levels], np.float64)
        entry = (texture, tex, longest)
        self._tips[id(texture)] = entry
        return entry

    # -- stamps -------------------------------------------------------------

    def instance_data(self, texture, stamps, to_layer):
        """The stamps as one float32 array (11 a stamp) and their layer-px
        bounding box, or (None, None). Stamps too small or see-through are
        left out, as paint_stamps does."""
        _, _, longest = self.tip(texture)
        if not stamps:
            return None, None
        a = np.array([s[:5] for s in stamps], np.float64)
        rgb = np.array([(s[5] if isinstance(s[5], int) else s[5][1]) for s in stamps], np.int64)
        m = to_layer if to_layer is not None else QTransform()
        scale = math.sqrt(abs(m.determinant())) or 1.0
        x, y, size, angle, opacity = a.T
        on_screen = size * scale
        keep = (on_screen >= 0.4) & (opacity > 0)
        if not keep.any():
            return None, None
        x, y, size, angle, opacity, rgb, on_screen = (v[keep] for v in (x, y, size, angle, opacity,
                                                                       rgb, on_screen))
        # BrushTexture.level: the smallest copy still at least on_screen px
        lod = (longest[None, 1:] >= on_screen[:, None]).sum(axis=1).astype(np.float64)
        w0, h0 = texture._levels[0].width(), texture._levels[0].height()
        k = size / max(w0, h0)      # every level keeps the aspect: same k on level 0
        cx = m.m11() * x + m.m21() * y + m.dx()
        cy = m.m12() * x + m.m22() * y + m.dy()
        c, s = np.cos(angle), np.sin(angle)
        # picture-space edges R(angle) * (k*w, 0) and R(angle) * (0, k*h), then through m
        ex_x, ex_y = c * k * w0, s * k * w0
        ey_x, ey_y = -s * k * h0, c * k * h0
        ux = m.m11() * ex_x + m.m21() * ex_y
        uy = m.m12() * ex_x + m.m22() * ex_y
        vx = m.m11() * ey_x + m.m21() * ey_y
        vy = m.m12() * ey_x + m.m22() * ey_y
        out = np.empty((len(x), 11), np.float32)
        out[:, 0], out[:, 1] = cx, cy
        out[:, 2], out[:, 3], out[:, 4], out[:, 5] = ux, uy, vx, vy
        out[:, 6] = ((rgb >> 16) & 255) / 255.0
        out[:, 7] = ((rgb >> 8) & 255) / 255.0
        out[:, 8] = (rgb & 255) / 255.0
        out[:, 9] = opacity
        out[:, 10] = lod
        hx = (np.abs(ux) + np.abs(vx)) * 0.5 + 1
        hy = (np.abs(uy) + np.abs(vy)) * 0.5 + 1
        box = QRect(math.floor((cx - hx).min()), math.floor((cy - hy).min()), 0, 0)
        box.setRight(math.ceil((cx + hx).max()))
        box.setBottom(math.ceil((cy + hy).max()))
        return out, box

    def _upload_instances(self, data):
        self.vao.bind()
        self.instances.bind()
        raw = data.tobytes()
        if len(raw) > self._instance_bytes:
            self._instance_bytes = max(len(raw), 2 * self._instance_bytes)
            self.instances.allocate(self._instance_bytes)
        self.instances.write(0, raw, len(raw))

    def _bind_stamp_attributes(self):
        p, f = self.stamp_program, self.f
        self.quad.bind()
        p.enableAttributeArray(0)
        p.setAttributeBuffer(0, GL_FLOAT, 0, 2, 8)
        f.glVertexAttribDivisor(0, 0)
        self.instances.bind()
        for loc, offset, n in ((1, 0, 2), (2, 8, 4), (3, 24, 4), (4, 40, 1)):
            p.enableAttributeArray(loc)
            p.setAttributeBuffer(loc, GL_FLOAT, offset, n, 44)
            f.glVertexAttribDivisor(loc, 1)
        # (colour, opacity) is rgb at 24 and opacity at 36: one vec4 at 24

    def draw_stamps(self, tex, count, target, origin, mode, steps=0.0, colour_opacity=1.0):
        p, f = self.stamp_program, self.f
        p.bind()
        self.vao.bind()
        self._bind_stamp_attributes()
        f.glActiveTexture(0x84C0)
        tex.bind()
        p.setUniformValue1i(p.uniformLocation('tip'), 0)
        p.setUniformValue1i(p.uniformLocation('mode'), mode)
        p.setUniformValue1f(p.uniformLocation('steps'), float(steps))
        p.setUniformValue1f(p.uniformLocation('colour_opacity'), float(colour_opacity))
        p.setUniformValue(p.uniformLocation('target'), float(target[0]), float(target[1]))
        p.setUniformValue(p.uniformLocation('origin'), float(origin[0]), float(origin[1]))
        f.glDrawArraysInstanced(GL_TRIANGLE_STRIP, 0, 4, count)

    def scratch(self, w, h):
        """Mask (R16F) and colour (RGBA16F) buffers at least w x h."""
        if self._scratch is not None:
            mask, colour = self._scratch
            if mask.width() >= w and mask.height() >= h:
                return self._scratch
            w, h = max(w, mask.width()), max(h, mask.height())
        w, h = max(256, w), max(256, h)
        fm = QOpenGLFramebufferObjectFormat()
        fm.setInternalTextureFormat(GL_R16F)
        fc = QOpenGLFramebufferObjectFormat()
        fc.setInternalTextureFormat(GL_RGBA16F)
        self._scratch = (QOpenGLFramebufferObject(w, h, fm), QOpenGLFramebufferObject(w, h, fc))
        return self._scratch


_shared = None


def shared():
    """The app's one GpuPainter, made on first use."""
    global _shared
    if _shared is None:
        _shared = GpuPainter()
    return _shared


class GpuLayer:
    """A layer's image on the graphics card: *width* x *height*, premultiplied.
    *precision*: 'half' (RGBA16F - faint stamps build up without banding, as
    the CPU's 16-bit buffer) or 'byte' (RGBA8, half the memory)."""

    def __init__(self, width, height, precision='half', painter=None):
        self.gpu = painter or shared()
        self.gpu.current()
        self.width, self.height = width, height
        fmt = QOpenGLFramebufferObjectFormat()
        fmt.setInternalTextureFormat(GL_RGBA16F if precision == 'half' else GL_RGBA8)
        self.fbo = QOpenGLFramebufferObject(width, height, fmt)
        if not self.fbo.isValid():
            raise RuntimeError(f"No {width}x{height} framebuffer")
        self.clear()

    def release(self):
        """Free the card's memory now, with the right context current (left
        to Python's collector it may go while another context is current)."""
        if self.fbo is not None:
            self.gpu.current()
            self.fbo = None

    @property
    def texture_id(self):
        """The GL texture (shared contexts can draw it: row 0 at v = 0)."""
        return self.fbo.texture()

    def clear(self):
        g = self.gpu
        g.current()
        self.fbo.bind()
        g.f.glViewport(0, 0, self.width, self.height)
        g.f.glDisable(GL_SCISSOR_TEST)
        g.f.glClearColor(0, 0, 0, 0)
        g.f.glClear(GL_COLOR_BUFFER_BIT)

    # -- pixels in and out ----------------------------------------------------

    def write(self, image, at=(0, 0)):
        """*image* (any QImage) into the layer with its top-left at *at*
        (layer px) - glTexSubImage2D, no copy beyond the format change."""
        g = self.gpu
        g.current()
        if image.format() != QImage.Format_ARGB32_Premultiplied:
            image = image.convertToFormat(QImage.Format_ARGB32_Premultiplied)
        x, y = at
        r = QRect(x, y, image.width(), image.height()).intersected(QRect(0, 0, self.width, self.height))
        if r.isEmpty():
            return
        bits = image.constBits()
        bpl = image.bytesPerLine()
        offset = (r.y() - y) * bpl + (r.x() - x) * 4
        f = g.f
        f.glBindTexture(GL_TEXTURE_2D, self.fbo.texture())
        f.glPixelStorei(GL_UNPACK_ALIGNMENT, 4)
        f.glPixelStorei(GL_UNPACK_ROW_LENGTH, bpl // 4)
        f.glTexSubImage2D(GL_TEXTURE_2D, 0, r.x(), r.y(), r.width(), r.height(), GL_BGRA,
                          GL_UNSIGNED_BYTE, bits[offset:])
        f.glPixelStorei(GL_UNPACK_ROW_LENGTH, 0)
        f.glFlush()

    upload = write

    def read(self, rect=None):
        """The layer inside *rect* (QRect, layer px; all of it if None) as a
        premultiplied ARGB32 QImage."""
        g = self.gpu
        g.current()
        r = (rect or QRect(0, 0, self.width, self.height)).intersected(
            QRect(0, 0, self.width, self.height))
        out = QImage(max(1, r.width()), max(1, r.height()), QImage.Format_ARGB32_Premultiplied)
        if r.isEmpty():
            out.fill(0)
            return out
        self.fbo.bind()
        g.f.glPixelStorei(GL_PACK_ALIGNMENT, 4)
        g.f.glPixelStorei(GL_PACK_ROW_LENGTH, out.bytesPerLine() // 4)
        g.f.glReadPixels(r.x(), r.y(), r.width(), r.height(), GL_BGRA, GL_UNSIGNED_BYTE, out.bits())
        g.f.glPixelStorei(GL_PACK_ROW_LENGTH, 0)
        return out

    # -- painting -----------------------------------------------------------

    def _target(self):
        g = self.gpu
        self.fbo.bind()
        g.f.glViewport(0, 0, self.width, self.height)
        g.f.glDisable(GL_DEPTH_TEST)
        g.f.glEnable(GL_BLEND)

    def paint_stamps(self, texture, stamps, to_layer=None):
        """Build-up stamps (x, y, size, angle, opacity, rgb) in picture
        coordinates, through *to_layer* (picture -> layer px), SourceOver.
        One draw call. Returns the layer QRect touched, or None."""
        g = self.gpu
        g.current()
        data, box = g.instance_data(texture, stamps, to_layer)
        if data is None:
            return None
        _, tex, _ = g.tip(texture)
        g._upload_instances(data)
        self._target()
        g.f.glBlendEquation(GL_FUNC_ADD)
        g.f.glBlendFunc(GL_ONE, GL_ONE_MINUS_SRC_ALPHA)
        g.draw_stamps(tex, len(data), (self.width, self.height), (0, 0), mode=0)
        g.f.glFlush()       # another (shared) context may draw it next
        return box.intersected(QRect(0, 0, self.width, self.height))

    def paint_flat(self, texture, stamps, to_layer=None, colour_opacity=1.0, steps=OPACITY_STEPS):
        """A flat stroke: coverage the max of the stamps' (in *steps*),
        colour laid over itself (at *colour_opacity*), composed onto the
        layer. Returns the layer QRect touched, or None."""
        g = self.gpu
        g.current()
        data, box = g.instance_data(texture, stamps, to_layer)
        if data is None:
            return None
        box = box.intersected(QRect(0, 0, self.width, self.height))
        if box.isEmpty():
            return None
        _, tex, _ = g.tip(texture)
        g._upload_instances(data)
        mask, colour = g.scratch(box.width(), box.height())
        f = g.f
        sw, sh = mask.width(), mask.height()
        origin = (box.x(), box.y())
        f.glDisable(GL_DEPTH_TEST)
        for fbo, mode, eq in ((mask, 1, GL_MAX), (colour, 2, GL_FUNC_ADD)):
            fbo.bind()
            f.glViewport(0, 0, sw, sh)
            f.glEnable(GL_SCISSOR_TEST)
            f.glScissor(0, 0, box.width(), box.height())
            f.glClearColor(0, 0, 0, 0)
            f.glClear(GL_COLOR_BUFFER_BIT)
            f.glEnable(GL_BLEND)
            f.glBlendEquation(eq)
            f.glBlendFunc(GL_ONE, GL_ONE if mode == 1 else GL_ONE_MINUS_SRC_ALPHA)
            g.draw_stamps(tex, len(data), (sw, sh), origin, mode=mode, steps=steps,
                          colour_opacity=colour_opacity)
        f.glDisable(GL_SCISSOR_TEST)
        # compose: straight colour x coverage, SourceOver onto the layer
        self._target()
        f.glBlendEquation(GL_FUNC_ADD)
        f.glBlendFunc(GL_ONE, GL_ONE_MINUS_SRC_ALPHA)
        p = g.compose_program
        p.bind()
        g.vao.bind()
        g.quad.bind()
        for loc in (1, 2, 3, 4):
            f.glDisableVertexAttribArray(loc)
        p.enableAttributeArray(0)
        p.setAttributeBuffer(0, GL_FLOAT, 0, 2, 8)
        f.glVertexAttribDivisor(0, 0)
        f.glActiveTexture(0x84C0)
        f.glBindTexture(GL_TEXTURE_2D, mask.texture())
        f.glActiveTexture(0x84C1)
        f.glBindTexture(GL_TEXTURE_2D, colour.texture())
        f.glActiveTexture(0x84C0)
        p.setUniformValue1i(p.uniformLocation('mask'), 0)
        p.setUniformValue1i(p.uniformLocation('colour'), 1)
        p.setUniformValue(p.uniformLocation('box'), float(box.x()), float(box.y()),
                          float(box.width()), float(box.height()))
        p.setUniformValue(p.uniformLocation('target'), float(self.width), float(self.height))
        p.setUniformValue(p.uniformLocation('scratch'), float(sw), float(sh))
        f.glDrawArrays(GL_TRIANGLE_STRIP, 0, 4)
        f.glFlush()
        return box


from random_image_viewer.drawing.gpu_flat import GpuFlatStroke, flat_kit  # noqa: E402,F401
