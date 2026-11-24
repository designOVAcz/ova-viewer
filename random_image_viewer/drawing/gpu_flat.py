"""🖌 Flat strokes on the graphics card: brushes.FlatStroke, draw for draw.

GpuFlatStroke runs FlatStroke's own geometry (head, swept body pieces cut to
their mitre joints, tail; the same motion frames) in Python, but every
QPainter.drawImage becomes one instanced quad, all of an add() in one draw
call per buffer. The images are not drawn on the CPU at all: the white
masks they come from (the tip's mip copies and BrushTexture.motion_frame's
'profile' / 'front' / 'back') sit in one R8 atlas, and the shader makes each
texel what the CPU's cached variant holds:

* grey (coverage): the mask x the stamp's opacity step, as Qt's const-alpha
  byte multiply - texture.grey / frame_variant(step=...);
* colour: a palette colour x the mask (premultiplied), the colour picked per
  texel by an index map for streaks / particles (_streaked's bands and
  scatter, made exactly as there) - texture.coloured / frame_variant(rgb=).

The texels are then filtered as Qt's raster engine filters a transformed
image under SmoothPixmapTransform (bilinear on 8-bit texels, 4- or 8-bit
sub-pixel weights picked by qdrawhelper's rules), the coverage kept as the
max (GL_MAX on an R8 buffer: the CPU's grey RGB32 under Lighten), the colour
laid SourceOver on an RGBA8 buffer (the CPU's ARGB32 premultiplied), the
body's colour at Qt's const alpha for 0.35. compose: the colour
un-premultiplied, made opaque and cut by the coverage, as FlatStroke.compose.

The buffers cover the whole layer (one stroke at a time per GpuPainter); the
tail and the body's waiting piece, which move with the pen, are drawn on a
small copy of the buffers around them whenever the stroke is shown
(preview) or laid (commit), never into the buffers - as compose() does on
the CPU.

Usage (see GpuFlatStroke):

    stroke = GpuFlatStroke(layer)              # layer: gpu_paint.GpuLayer
    stroke.add(texture, new_stamps, view)      # every pen event
    stroke.preview()                           # every frame; then draw
                                               # layer.texture_id and over it
                                               # stroke.overlay_texture_id
    undo = layer.read(stroke.bounds())         # before laying it
    stroke.commit()
"""

import math

import numpy as np
from PySide6.QtCore import QRect
from PySide6.QtGui import QImage, QPainter
from PySide6.QtOpenGL import (QOpenGLBuffer, QOpenGLFramebufferObject,
                              QOpenGLFramebufferObjectFormat, QOpenGLTexture,
                              QOpenGLVertexArrayObject)

from random_image_viewer.drawing import gpu_paint as _gp
from random_image_viewer.drawing.gpu_paint import (GL_BGRA, GL_BLEND, GL_COLOR_BUFFER_BIT,
                                                   GL_DEPTH_TEST, GL_FLOAT, GL_FUNC_ADD, GL_MAX,
                                                   GL_ONE, GL_ONE_MINUS_SRC_ALPHA,
                                                   GL_PACK_ALIGNMENT, GL_PACK_ROW_LENGTH,
                                                   GL_RGBA8, GL_SCISSOR_TEST, GL_TEXTURE_2D,
                                                   GL_TRIANGLE_STRIP, GL_UNPACK_ALIGNMENT,
                                                   GL_UNSIGNED_BYTE, OPACITY_STEPS)

GL_R8 = 0x8229
GL_RED = 0x1903
GL_RGBA = 0x1908
GL_NEAREST = 0x2600
GL_READ_FRAMEBUFFER = 0x8CA8
GL_DRAW_FRAMEBUFFER = 0x8CA9

ATLAS_SIZE = 4096       # the R8 atlas of tip / motion-frame masks and index maps
PALETTE_W = 64          # colours a palette row holds (STREAKS is 32)
PALETTE_ROWS = 1024
STRIDE = 32             # floats an instance

_FLAT_VERT = """
#version 330 core
layout(location = 0) in vec4 c01;       // corners 0 and 1 (layer px; strip order)
layout(location = 1) in vec4 c23;       // corners 2 and 3
layout(location = 2) in vec4 inv;       // device -> image: du/dx, dv/dx, du/dy, dv/dy
layout(location = 3) in vec4 anchor;    // image u, v at device point (x, y)
layout(location = 4) in vec4 src;       // the mask in the atlas: x, y, w, h
layout(location = 5) in vec4 idx;       // index map in the atlas: x, y, w, h (w = 0: none)
layout(location = 6) in vec4 params;    // grey alpha, colour alpha, palette row, weight bits
layout(location = 7) in vec4 taper;     // body piece: width at its start, end (share of the widest)
uniform vec2 target;
uniform vec2 origin;
flat out vec2 vTaper;
flat out vec4 vInv;
flat out vec4 vAnchor;
flat out ivec4 vSrc;
flat out ivec4 vIdx;
flat out ivec4 vParams;
void main() {
    vec2 p = gl_VertexID == 0 ? c01.xy : gl_VertexID == 1 ? c01.zw : gl_VertexID == 2 ? c23.xy : c23.zw;
    gl_Position = vec4((p - origin) / target * 2.0 - 1.0, 0.0, 1.0);
    vInv = inv;
    vTaper = taper.xy;
    vAnchor = anchor;
    vSrc = ivec4(src + 0.5);
    vIdx = ivec4(idx + 0.5);
    vParams = ivec4(params + 0.5);
}
"""

_FLAT_FRAG = """
#version 330 core
flat in vec2 vTaper;
flat in vec4 vInv;
flat in vec4 vAnchor;
flat in ivec4 vSrc;
flat in ivec4 vIdx;
flat in ivec4 vParams;
uniform sampler2D atlas;
uniform sampler2D palette;
uniform int pass;           // 0: grey coverage (R), 1: premultiplied colour
uniform vec2 origin;
out vec4 frag;

int bmul(int x, int a) {    // Qt's BYTE_MUL
    int t = x * a + 128;
    return (t + (t >> 8)) >> 8;
}

int fetch8(ivec2 p) { return int(texelFetch(atlas, p, 0).r * 255.0 + 0.5); }

ivec4 texel(ivec2 t) {
    int a = fetch8(vSrc.xy + t);
    if (pass == 0)
        return ivec4(vParams.x >= 255 ? a : bmul(a, vParams.x), 0, 0, 0);
    int i = 0;
    if (vIdx.z > 0)
        i = fetch8(vIdx.xy + min(t, vIdx.zw - 1));
    ivec3 c = ivec3(texelFetch(palette, ivec2(i, vParams.z), 0).rgb * 255.0 + 0.5);
    return ivec4(bmul(c.r, a), bmul(c.g, a), bmul(c.b, a), a);
}

void main() {
    vec2 rel = gl_FragCoord.xy + origin - vAnchor.zw;
    vec2 uv = vAnchor.xy + vec2(vInv.x * rel.x + vInv.z * rel.y, vInv.y * rel.x + vInv.w * rel.y);
    if (vTaper.x != 1.0 || vTaper.y != 1.0) {
        // a body piece narrowing / widening along its way (a trapezoid, as
        // FlatStroke's quadToQuad): the profile squeezed across to the width here
        float r = mix(vTaper.x, vTaper.y, clamp(uv.x / float(vSrc.z), 0.0, 1.0));
        if (r <= 0.0)
            discard;
        float mid = float(vSrc.w) * 0.5;
        uv.y = (uv.y - mid) / r + mid;
    }
    if (uv.x < 0.0 || uv.y < 0.0 || uv.x >= float(vSrc.z) || uv.y >= float(vSrc.w))
        discard;
    int bits = vParams.w;
    int W = 1 << bits;
    vec2 f = uv - 0.5;
    vec2 fl = floor(f);
    ivec2 d = clamp(ivec2(floor((f - fl) * float(W))), ivec2(0), ivec2(W - 1));
    ivec2 p1 = ivec2(fl);
    ivec2 hi = vSrc.zw - 1;
    ivec2 a = clamp(p1, ivec2(0), hi);
    ivec2 b = clamp(p1 + 1, ivec2(0), hi);
    ivec4 tl = texel(a);
    ivec4 tr = texel(ivec2(b.x, a.y));
    ivec4 bl = texel(ivec2(a.x, b.y));
    ivec4 br = texel(b);
    ivec4 v = (tl * ((W - d.x) * (W - d.y)) + tr * (d.x * (W - d.y))
               + bl * ((W - d.x) * d.y) + br * (d.x * d.y)) >> (2 * bits);
    if (pass == 0) {
        frag = vec4(float(v.x) / 255.0, 0.0, 0.0, 1.0);
        return;
    }
    int ca = vParams.y;
    if (ca < 255)
        v = ivec4(bmul(v.r, ca), bmul(v.g, ca), bmul(v.b, ca), bmul(v.a, ca));
    frag = vec4(v) / 255.0;
}
"""

_COMPOSE_VERT = """
#version 330 core
uniform vec4 box;          // x, y, w, h: layer px
uniform vec2 target;       // target size
uniform vec2 origin;       // target's top-left, layer px
void main() {
    vec2 c = vec2(gl_VertexID & 1, gl_VertexID >> 1);
    vec2 p = box.xy + c * box.zw - origin;
    gl_Position = vec4(p / target * 2.0 - 1.0, 0.0, 1.0);
}
"""

_COMPOSE_FRAG = """
#version 330 core
uniform sampler2D mask;
uniform sampler2D colour;
uniform sampler2D tmask;
uniform sampler2D tcolour;
uniform vec4 tbox;          // the tail's copy of the buffers: layer px x, y, w, h (w = 0: none)
uniform vec2 origin;
out vec4 frag;

int bmul(int x, int a) {
    int t = x * a + 128;
    return (t + (t >> 8)) >> 8;
}

void main() {
    ivec2 P = ivec2(floor(gl_FragCoord.xy + origin));
    ivec4 tb = ivec4(tbox + 0.5);
    ivec2 q = P - tb.xy;
    vec4 m;
    vec4 c;
    if (tb.z > 0 && q.x >= 0 && q.y >= 0 && q.x < tb.z && q.y < tb.w) {
        m = texelFetch(tmask, q, 0);
        c = texelFetch(tcolour, q, 0);
    } else {
        m = texelFetch(mask, P, 0);
        c = texelFetch(colour, P, 0);
    }
    int m8 = int(m.r * 255.0 + 0.5);
    if (m8 == 0) {
        frag = vec4(0.0);
        return;
    }
    ivec4 c8 = ivec4(c * 255.0 + 0.5);
    ivec3 s = ivec3(0);
    if (c8.a >= 255)
        s = c8.rgb;
    else if (c8.a > 0)
        s = min(ivec3(255), (c8.rgb * 255 + c8.a / 2) / c8.a);
    frag = vec4(float(bmul(s.r, m8)), float(bmul(s.g, m8)), float(bmul(s.b, m8)), float(m8)) / 255.0;
}
"""


def _qt_alpha(opacity):
    """Qt's const alpha (0..255) for a QPainter opacity, as the raster engine
    turns it: qRound(opacity * 256), then (a * 255) >> 8."""
    return (int(math.floor(opacity * 256 + 0.5)) * 255) >> 8


BODY_ALPHA = _qt_alpha(0.35)    # brushes.FlatStroke.BODY_COLOUR_MIX
_GREY_ALPHA = [255 if s >= OPACITY_STEPS else _qt_alpha(s / OPACITY_STEPS)
               for s in range(OPACITY_STEPS + 1)]
BODY_STEPS = 255        # brushes.BODY_STEPS: the swept body's opacity, full 8-bit


def _affine(m):
    """A QTransform (or None) as (m11, m12, m21, m22, dx, dy)."""
    if m is None:
        return (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    return (m.m11(), m.m12(), m.m21(), m.m22(), m.dx(), m.dy())


def _mul(a, b):
    """*a* then *b* (Qt's a * b)."""
    return (a[0] * b[0] + a[1] * b[2], a[0] * b[1] + a[1] * b[3],
            a[2] * b[0] + a[3] * b[2], a[2] * b[1] + a[3] * b[3],
            a[4] * b[0] + a[5] * b[2] + b[4], a[4] * b[1] + a[5] * b[3] + b[5])


def _place(x, y, size, angle, w, h, tip_long):
    """Image (w x h) px -> picture: centred on (x, y), turned, size/tip_long
    (brushes._stamp_matrix and FlatStroke._frame_matrix)."""
    k = size / tip_long
    c, s = math.cos(angle) * k, math.sin(angle) * k
    hw, hh = w / 2.0, h / 2.0
    return (c, s, -s, c, x - c * hw + s * hh, y - s * hw - c * hh)


def _band_map(axis, n, w, h):
    """_streaked's bands: *n* colours across axis *axis* of a w x h image, as
    Qt scales the n-pixel stripe (nearest). (1, w) or (h, 1) uint8 indices."""
    mw, mh = (w, 1) if axis == 0 else (1, h)
    sw, sh = (n, 1) if axis == 0 else (1, n)
    v = np.arange(n, dtype=np.uint8)
    px = np.empty((n, 4), np.uint8)
    px[:, 0], px[:, 1], px[:, 2], px[:, 3] = v, v, v, 255
    stripe = QImage(px.tobytes(), sw, sh, sw * 4, QImage.Format_ARGB32_Premultiplied).copy()
    image = QImage(mw, mh, QImage.Format_ARGB32_Premultiplied)
    image.fill(0)
    p = QPainter(image)
    p.setCompositionMode(QPainter.CompositionMode_Source)
    p.drawImage(QRect(0, 0, mw, mh), stripe)
    p.end()
    a = np.frombuffer(image.constBits(), np.uint8).reshape(mh, image.bytesPerLine())
    return a[:, :mw * 4].reshape(mh, mw, 4)[..., 0].copy()


def _scatter_map(number, n, w, h):
    """_streaked's particles: the colour index of each pixel of a w x h
    image, for map *number* (the same scatter: the same random numbers)."""
    cell = max(1, round(max(w, h) / 160))
    rng = np.random.default_rng(number * 7919 + w * 31 + h)
    idx = rng.integers(0, n, (-(-h // cell), -(-w // cell)))
    idx = np.repeat(np.repeat(idx, cell, 0), cell, 1)[:h, :w]
    return idx.astype(np.uint8)


def _alpha_of(image):
    w, h = image.width(), image.height()
    a = np.frombuffer(image.constBits(), np.uint8).reshape(h, image.bytesPerLine())
    return a[:, :w * 4].reshape(h, w, 4)[..., 3].copy()


class _FlatKit:
    """What flat strokes share on the card: shaders, the mask atlas, the
    palette, and the stroke's buffers (mask R8 + colour RGBA8 over the whole
    layer, a small copy of them for the tail, and the preview overlay)."""

    def __init__(self, gpu):
        self.gpu = gpu
        gpu.current()
        f = gpu.f
        self.program = gpu._program(_FLAT_VERT, _FLAT_FRAG)
        self.compose_program = gpu._program(_COMPOSE_VERT, _COMPOSE_FRAG)
        self.vao = QOpenGLVertexArrayObject()
        self.vao.create()
        self.vao.bind()
        self.instances = QOpenGLBuffer(QOpenGLBuffer.VertexBuffer)
        self.instances.setUsagePattern(QOpenGLBuffer.StreamDraw)
        self.instances.create()
        self.instances.bind()
        self._bytes = 256 * STRIDE * 4
        self.instances.allocate(self._bytes)
        p = self.program
        for loc in range(8):
            p.enableAttributeArray(loc)
            p.setAttributeBuffer(loc, GL_FLOAT, loc * 16, 4, STRIDE * 4)
            f.glVertexAttribDivisor(loc, 1)
        self.vao.release()
        self.empty_vao = QOpenGLVertexArrayObject()     # the compose quad (gl_VertexID)
        self.empty_vao.create()
        self.u = {n: p.uniformLocation(n) for n in ('atlas', 'palette', 'pass', 'origin', 'target')}
        c = self.compose_program
        self.cu = {n: c.uniformLocation(n) for n in ('mask', 'colour', 'tmask', 'tcolour', 'tbox',
                                                     'origin', 'box', 'target')}
        self.atlas = self._texture(QOpenGLTexture.R8_UNorm, ATLAS_SIZE, ATLAS_SIZE)
        self.palette = self._texture(QOpenGLTexture.RGBA8_UNorm, PALETTE_W, PALETTE_ROWS)
        self._entries = {}      # key -> (x, y, w, h) in the atlas
        self._keep = {}         # id(texture) -> texture (so its id stays its own)
        self._shelf = [0, 0, 0]  # x, y, height of the shelf being filled
        self._rows = {}         # colours tuple -> palette row
        self._slots = {}        # 0xRRGGBB -> (row, column): single colours, packed from the last row up
        self.buffers = None     # (mask, colour) over the layer
        self.overlay = None
        self.temp = None        # (mask, colour) for the tail
        self.last_bounds = None  # the last stroke's bounds (cleared before the next)
        self.busy = None        # the stroke using the buffers now (a live one: lay others on the CPU)

    @staticmethod
    def _texture(fmt, w, h):
        t = QOpenGLTexture(QOpenGLTexture.Target2D)
        t.setFormat(fmt)
        t.setSize(w, h)
        t.setMipLevels(1)
        t.allocateStorage()
        t.setMinMagFilters(QOpenGLTexture.Nearest, QOpenGLTexture.Nearest)
        t.setWrapMode(QOpenGLTexture.ClampToEdge)
        return t

    # -- buffers --------------------------------------------------------------

    @staticmethod
    def _fbo(w, h, internal):
        fmt = QOpenGLFramebufferObjectFormat()
        fmt.setInternalTextureFormat(internal)
        fbo = QOpenGLFramebufferObject(w, h, fmt)
        if not fbo.isValid():
            raise RuntimeError(f"No {w}x{h} framebuffer")
        return fbo

    def layer_buffers(self, w, h):
        """Mask, colour (and the overlay), at least w x h, made / grown here."""
        if self.buffers is None or self.buffers[0].width() < w or self.buffers[0].height() < h:
            if self.buffers is not None:
                w, h = max(w, self.buffers[0].width()), max(h, self.buffers[0].height())
            self.buffers = self.overlay = None
            self.buffers = (self._fbo(w, h, GL_R8), self._fbo(w, h, GL_RGBA8))
            self.overlay = self._fbo(w, h, GL_RGBA8)
            for fbo in self.buffers + (self.overlay,):
                self.clear(fbo, None)
            self.last_bounds = None
        return self.buffers

    def temp_buffers(self, w, h):
        if self.temp is None or self.temp[0].width() < w or self.temp[0].height() < h:
            if self.temp is not None:
                w, h = max(w, self.temp[0].width()), max(h, self.temp[0].height())
            w, h = max(256, w), max(256, h)
            self.temp = (self._fbo(w, h, GL_R8), self._fbo(w, h, GL_RGBA8))
        return self.temp

    def clear(self, fbo, rect):
        f = self.gpu.f
        fbo.bind()
        f.glViewport(0, 0, fbo.width(), fbo.height())
        if rect is not None:
            f.glEnable(GL_SCISSOR_TEST)
            f.glScissor(rect.x(), rect.y(), rect.width(), rect.height())
        f.glClearColor(0, 0, 0, 0)
        f.glClear(GL_COLOR_BUFFER_BIT)
        f.glDisable(GL_SCISSOR_TEST)

    def copy(self, source, rect, target):
        """*rect* of *source* into *target* at (0, 0) (glBlitFramebuffer)."""
        f = self.gpu.f
        f.glBindFramebuffer(GL_READ_FRAMEBUFFER, source.handle())
        f.glBindFramebuffer(GL_DRAW_FRAMEBUFFER, target.handle())
        x, y, w, h = rect.x(), rect.y(), rect.width(), rect.height()
        f.glBlitFramebuffer(x, y, x + w, y + h, 0, 0, w, h, GL_COLOR_BUFFER_BIT, GL_NEAREST)
        f.glBindFramebuffer(GL_READ_FRAMEBUFFER, 0)
        f.glBindFramebuffer(GL_DRAW_FRAMEBUFFER, 0)

    # -- the atlas and the palette -------------------------------------------

    def reset_atlas(self):
        self._entries.clear()
        self._keep.clear()
        self._shelf = [0, 0, 0]
        self._rows.clear()
        self._slots.clear()

    def entry(self, key, make):
        """(x, y, w, h) of *key* in the atlas; *make*() -> (h, w) uint8 array,
        uploaded on first use. MemoryError when the atlas is full."""
        e = self._entries.get(key)
        if e is not None:
            return e
        a = np.ascontiguousarray(make(), np.uint8)
        h, w = a.shape
        sx, sy, sh = self._shelf
        if sx + w > ATLAS_SIZE:
            sx, sy, sh = 0, sy + sh, 0
        if sy + h > ATLAS_SIZE or w > ATLAS_SIZE:
            raise MemoryError("atlas full")
        f = self.gpu.f
        f.glBindTexture(GL_TEXTURE_2D, self.atlas.textureId())
        f.glPixelStorei(GL_UNPACK_ALIGNMENT, 1)
        f.glTexSubImage2D(GL_TEXTURE_2D, 0, sx, sy, w, h, GL_RED, GL_UNSIGNED_BYTE, a.tobytes())
        f.glPixelStorei(GL_UNPACK_ALIGNMENT, 4)
        e = (sx, sy, w, h)
        self._entries[key] = e
        self._shelf = [sx + w, sy, max(sh, h)]
        return e

    def row(self, colours):
        """The palette row holding *colours* (0xRRGGBB ints)."""
        r = self._rows.get(colours)
        if r is not None:
            return r
        if len(self._rows) >= PALETTE_ROWS - (len(self._slots) + PALETTE_W - 1) // PALETTE_W:
            raise MemoryError("palette full")
        r = len(self._rows)
        c = np.array(colours[:PALETTE_W], np.uint32)
        px = np.empty((len(c), 4), np.uint8)
        px[:, 0], px[:, 1], px[:, 2], px[:, 3] = (c >> 16) & 255, (c >> 8) & 255, c & 255, 255
        f = self.gpu.f
        f.glBindTexture(GL_TEXTURE_2D, self.palette.textureId())
        f.glPixelStorei(GL_UNPACK_ALIGNMENT, 1)
        f.glTexSubImage2D(GL_TEXTURE_2D, 0, 0, r, len(c), 1, GL_RGBA, GL_UNSIGNED_BYTE, px.tobytes())
        f.glPixelStorei(GL_UNPACK_ALIGNMENT, 4)
        self._rows[colours] = r
        return r

    def slot(self, rgb):
        """(row, column) of the single colour *rgb* in the palette: packed
        PALETTE_W to a row from the last row up (🖌 bristles, each hair its
        own colour - a row each ran out at 1024 colours)."""
        s = self._slots.get(rgb)
        if s is not None:
            return s
        n = len(self._slots)
        row, col = PALETTE_ROWS - 1 - n // PALETTE_W, n % PALETTE_W
        if row < len(self._rows):
            raise MemoryError("palette full")
        px = bytes(((rgb >> 16) & 255, (rgb >> 8) & 255, rgb & 255, 255))
        f = self.gpu.f
        f.glBindTexture(GL_TEXTURE_2D, self.palette.textureId())
        f.glPixelStorei(GL_UNPACK_ALIGNMENT, 1)
        f.glTexSubImage2D(GL_TEXTURE_2D, 0, col, row, 1, 1, GL_RGBA, GL_UNSIGNED_BYTE, px)
        f.glPixelStorei(GL_UNPACK_ALIGNMENT, 4)
        self._slots[rgb] = (row, col)
        return row, col

    # -- drawing --------------------------------------------------------------

    def draw(self, data, targets, size, origin, passes=(0, 1)):
        """The (n, STRIDE) float32 instances into *targets* (mask fbo, colour
        fbo), whose top-left is layer px *origin*, *size* their size.
        *passes*: which of the two to draw (0 coverage, 1 colour) - the
        targets given are those passes' buffers, in that order."""
        f = self.gpu.f
        raw = data.tobytes()
        p = self.program
        p.bind()
        self.vao.bind()
        self.instances.bind()
        if len(raw) > self._bytes:
            self._bytes = max(len(raw), 2 * self._bytes)
            self.instances.allocate(self._bytes)
        self.instances.write(0, raw, len(raw))
        f.glActiveTexture(0x84C0)
        f.glBindTexture(GL_TEXTURE_2D, self.atlas.textureId())
        f.glActiveTexture(0x84C1)
        f.glBindTexture(GL_TEXTURE_2D, self.palette.textureId())
        f.glActiveTexture(0x84C0)
        u = self.u
        p.setUniformValue1i(u['atlas'], 0)
        p.setUniformValue1i(u['palette'], 1)
        p.setUniformValue(u['origin'], float(origin[0]), float(origin[1]))
        p.setUniformValue(u['target'], float(size[0]), float(size[1]))
        f.glDisable(GL_DEPTH_TEST)
        f.glDisable(GL_SCISSOR_TEST)
        f.glEnable(GL_BLEND)
        n = len(data)
        for i, fbo in zip(passes, targets):
            fbo.bind()
            f.glViewport(0, 0, size[0], size[1])
            if i == 0:
                f.glBlendEquation(GL_MAX)
                f.glBlendFunc(GL_ONE, GL_ONE)
            else:
                f.glBlendEquation(GL_FUNC_ADD)
                f.glBlendFunc(GL_ONE, GL_ONE_MINUS_SRC_ALPHA)
            p.setUniformValue1i(u['pass'], i)
            f.glDrawArraysInstanced(GL_TRIANGLE_STRIP, 0, 4, n)
        f.glBlendEquation(GL_FUNC_ADD)
        self.vao.release()

    def compose(self, target, rect, tbox, blend, size):
        """The stroke inside *rect* (layer px) into *target* (layer-sized):
        mask + colour (the tail's copy inside *tbox*) as FlatStroke.compose.
        *blend*: SourceOver onto what is there (commit), else replace it."""
        f = self.gpu.f
        mask, colour = self.buffers
        tm, tc = self.temp if self.temp is not None else self.buffers
        target.bind()
        f.glViewport(0, 0, size[0], size[1])
        f.glDisable(GL_DEPTH_TEST)
        f.glDisable(GL_SCISSOR_TEST)
        if blend:
            f.glEnable(GL_BLEND)
            f.glBlendEquation(GL_FUNC_ADD)
            f.glBlendFunc(GL_ONE, GL_ONE_MINUS_SRC_ALPHA)
        else:
            f.glDisable(GL_BLEND)
        p = self.compose_program
        p.bind()
        self.empty_vao.bind()
        for unit, tex in enumerate((mask.texture(), colour.texture(), tm.texture(), tc.texture())):
            f.glActiveTexture(0x84C0 + unit)
            f.glBindTexture(GL_TEXTURE_2D, tex)
        f.glActiveTexture(0x84C0)
        cu = self.cu
        for unit, name in enumerate(('mask', 'colour', 'tmask', 'tcolour')):
            p.setUniformValue1i(cu[name], unit)
        if tbox is None or tbox.isEmpty():
            p.setUniformValue(cu['tbox'], 0.0, 0.0, 0.0, 0.0)
        else:
            p.setUniformValue(cu['tbox'], float(tbox.x()), float(tbox.y()),
                              float(tbox.width()), float(tbox.height()))
        p.setUniformValue(cu['origin'], 0.0, 0.0)
        p.setUniformValue(cu['target'], float(size[0]), float(size[1]))
        p.setUniformValue(cu['box'], float(rect.x()), float(rect.y()),
                          float(rect.width()), float(rect.height()))
        f.glDrawArrays(GL_TRIANGLE_STRIP, 0, 4)
        f.glEnable(GL_BLEND)
        self.empty_vao.release()


def flat_kit(gpu=None):
    """The GpuPainter's flat-stroke machinery, made on first use."""
    gpu = gpu or _gp.shared()
    kit = getattr(gpu, '_flat_kit', None)
    if kit is None:
        kit = gpu._flat_kit = _FlatKit(gpu)
    return kit


class GpuFlatStroke:
    """A flat stroke (brushes.FlatStroke) painted on the card, for *layer* (a
    gpu_paint.GpuLayer). One at a time per GpuPainter (they share buffers).

        stroke = GpuFlatStroke(layer)
        stroke.add(texture, stamps, view)  # per pen event: StampTrail's new stamps
        stroke.preview()                   # per frame: refresh the overlay
        ... draw layer.texture_id, then stroke.overlay_texture_id over it
        before = layer.read(stroke.bounds())   # undo patch
        stroke.commit()                    # onto the layer; the stroke is done

    *view*: QTransform picture -> layer px (None: the same). The look is
    FlatStroke's: head, swept body, tail, coverage the max, colour laid over
    itself, OPACITY_STEPS, streak / particle colours."""

    FLARE = 1.5
    TURN_EASE = 0.35
    REVERSE_COS = 0.5

    def __init__(self, layer):
        self.layer = layer
        self.gpu = layer.gpu
        self.gpu.current()
        self.kit = k = flat_kit(self.gpu)
        k.busy = self           # (the buffers are this stroke's until it ends - see _end)
        k.layer_buffers(layer.width, layer.height)
        if k.last_bounds is not None:
            for fbo in k.buffers + (k.overlay,):
                k.clear(fbo, k.last_bounds)
            k.last_bounds = None
        self._records = []      # draws waiting for the card (see _flush)
        self._first = None
        self._prev = None
        self._dir = None
        self._reach = None
        self._width = None
        self._tail = None
        self._pend = None
        self._restart = False
        self.head = None
        self._bounds = None     # layer px laid so far (QRect)
        self._dirty = None      # changed since the last preview()
        self._tbox = None       # the tail's copy, at the last preview
        self._levels = {}       # id(texture) -> [(w, h)] per level
        self.done = False

    # -- the app's side -------------------------------------------------------

    @property
    def overlay_texture_id(self):
        """The preview: premultiplied RGBA8, as big as the layer (or bigger -
        see overlay_size), row 0 = the layer's top; the stroke as commit()
        would lay it, transparent elsewhere."""
        return self.kit.overlay.texture()

    @property
    def overlay_size(self):
        return self.kit.overlay.width(), self.kit.overlay.height()

    def bounds(self):
        """Layer px the stroke covers so far, tail included (QRect)."""
        b = self._bounds
        t = self._tail_records()
        tb = self._records_box(t) if t else None
        if tb is not None:
            b = tb if b is None else b.united(tb)
        if b is None:
            return QRect()
        return b.intersected(QRect(0, 0, self.layer.width, self.layer.height))

    def add(self, texture, stamps, view=None):
        """Lay *stamps* (x, y, size, angle, opacity, rgb), picture coords,
        through *view* (picture -> layer px). Returns the layer QRect laid
        (not counting the tail, see preview), or None."""
        self.gpu.current()
        self._lay_stamps(texture, stamps, view)
        return self._flush()

    def preview(self):
        """The overlay brought up to date (the tail and the body's waiting
        piece included). Returns the layer QRect redrawn, or None."""
        g = self.gpu
        g.current()
        self._flush()
        tbox = self._draw_tail_copy()
        rect = self._dirty
        for r in (tbox, self._tbox):
            if r is not None and not r.isEmpty():
                rect = r if rect is None else rect.united(r)
        self._tbox = tbox
        self._dirty = None
        if rect is None:
            return None
        rect = rect.intersected(QRect(0, 0, self.layer.width, self.layer.height))
        if rect.isEmpty():
            return None
        k = self.kit
        k.compose(k.overlay, rect, tbox, False, self.overlay_size)
        # Finished, not just sent: the canvas window draws this texture from
        # its own (shared) context, and with glFlush alone it sampled it half
        # written - the stroke torn by bare 1 px rows, a ladder of grey lines
        # through it while the pen was down (any GPU brush; the bristles,
        # dense and white, showed it plainly).
        g.f.glFinish()
        return rect

    def commit(self):
        """Lay the stroke on the layer (SourceOver), tail and all. Returns the
        layer QRect changed, or None (read it from the layer *before*, for
        undo: layer.read(stroke.bounds()))."""
        g = self.gpu
        g.current()
        self._flush()
        tbox = self._draw_tail_copy()
        rect = self.bounds()
        if not rect.isEmpty():
            self.kit.compose(self.layer.fbo, rect, tbox, True, (self.layer.width, self.layer.height))
        g.f.glFlush()
        self._end(rect)
        return rect if not rect.isEmpty() else None

    def cancel(self):
        """Drop the stroke (the overlay is cleared when the next one starts)."""
        self.gpu.current()
        self._end(self.bounds())

    def _end(self, rect):
        b = rect if not rect.isEmpty() else None
        if self._tbox is not None:
            b = self._tbox if b is None else b.united(self._tbox)
        self.kit.last_bounds = b
        self.done = True
        if self.kit.busy is self:
            self.kit.busy = None

    def read_overlay(self, rect):
        """The preview inside *rect* as a premultiplied ARGB32 QImage."""
        out = QImage(rect.width(), rect.height(), QImage.Format_ARGB32_Premultiplied)
        self.gpu.current()
        self.kit.overlay.bind()
        f = self.gpu.f
        f.glPixelStorei(GL_PACK_ALIGNMENT, 4)
        f.glPixelStorei(GL_PACK_ROW_LENGTH, out.bytesPerLine() // 4)
        f.glReadPixels(rect.x(), rect.y(), rect.width(), rect.height(), GL_BGRA, GL_UNSIGNED_BYTE,
                       out.bits())
        f.glPixelStorei(GL_PACK_ROW_LENGTH, 0)
        return out

    # -- FlatStroke's geometry (see brushes.FlatStroke for the why) -----------

    def _dims(self, texture):
        d = self._levels.get(id(texture))
        if d is None:
            d = self._levels[id(texture)] = [(im.width(), im.height()) for im in texture._levels]
        return d

    @staticmethod
    def _turn(stamp, direction):
        return math.degrees(stamp[3] - math.atan2(direction[1], direction[0]))

    def _body(self, texture, level, stamp, tip_long, scale):
        """FlatStroke._body, line for line."""
        self.head = None
        if self._first is None:
            self._first = self._prev = stamp
            return []
        prev = self._prev
        dx, dy = stamp[0] - prev[0], stamp[1] - prev[1]
        moved = math.hypot(dx, dy)
        if moved < max(0.25, (0.04 * stamp[2]) if self._dir is None else 0.25):
            return []
        self._prev = stamp
        step = (dx / moved, dy / moved)
        if self._dir is None:
            self._dir = step
        else:
            e = self.TURN_EASE
            nx, ny = self._dir[0] + (step[0] - self._dir[0]) * e, self._dir[1] + (step[1] - self._dir[1]) * e
            n = math.hypot(nx, ny)
            self._dir = (nx / n, ny / n) if n > 1e-6 else step
        d = self._dir
        turn = self._turn(stamp, d)
        frame = texture.motion_frame(level, turn)
        k = stamp[2] / tip_long
        width = frame['h'] * k
        if self._reach is None:
            first = self._first
            f_frame = texture.motion_frame(level, self._turn(first, d))
            # The body grows from the first stamp's middle, inside the tip
            # laid there whole (holes closed, see motion_frame 'whole'). From
            # its front edge, with the tip's front filled out to it, the
            # start was a square slab that stood out as the stroke curved.
            self._reach = (first[0], first[1])
            self._width = f_frame['h'] * first[2] / tip_long
            self.head = (first, self._turn(first, d), d)
            # The reach starts at the first stamp's front edge, half a tip
            # ahead: the next few stamps lie behind it. That is the pen
            # still crossing the first stamp, not turning back - taken for
            # a turn, every stroke restarted at its second stamp, its head
            # the raw tip (a speckled tip's holes as a dark blob at the
            # start) and never the filled 'front'.
            self._in_head = True
        tx, ty = stamp[0], stamp[1]
        rx, ry = self._reach
        vx, vy = tx - rx, ty - ry
        far = math.hypot(vx, vy)
        if (not getattr(self, '_in_head', False) and far >= 0.25
                and vx * d[0] + vy * d[1] < self.REVERSE_COS * far):
            self._dir = step
            frame = texture.motion_frame(level, self._turn(stamp, step))
            self._reach = (tx, ty)
            self._width = frame['h'] * k
            self.head = (stamp, self._turn(stamp, step), step)
            self._restart = True
            return []
        if (tx - rx) * d[0] + (ty - ry) * d[1] < 0.25:
            return []
        self._reach = (tx, ty)
        self._in_head = False
        span = math.hypot(tx - rx, ty - ry)
        was = self._width
        width = min(max(width, was - self.FLARE * span), was + self.FLARE * span)
        self._width = width
        pieces = max(1, min(32, math.ceil(span * scale / 6.0)))
        fh = frame['h']
        out = []
        for i in range(pieces):
            a, b = i / pieces, (i + 1) / pieces
            out.append({'s': (rx + (tx - rx) * a, ry + (ty - ry) * a),
                        'e': (rx + (tx - rx) * b, ry + (ty - ry) * b),
                        'w0': was + (width - was) * a, 'w1': was + (width - was) * b,
                        'd': d, 'turn': turn, 'fh': fh})
        return out

    @staticmethod
    def _bisector(d1, d2):
        bx, by = d1[0] + d2[0], d1[1] + d2[1]
        n = math.hypot(bx, by)
        return (bx / n, by / n) if n > 1e-6 else d2

    def _push_piece(self, piece, sink):
        pend = self._pend
        if pend is not None:
            joint = self._bisector(pend['d'], piece['d'])
            pend['end_cut'] = joint
            piece['start_cut'] = joint
            self._piece_record(pend, sink, BODY_ALPHA)
        else:
            piece['start_cut'] = None
        self._pend = piece

    def _flush_piece(self, sink):
        pend, self._pend = self._pend, None
        if pend is not None:
            pend['end_cut'] = None
            self._piece_record(pend, sink, BODY_ALPHA)

    def _piece_record(self, piece, sink, colour_alpha, end_cut=Ellipsis):
        """FlatStroke._piece_strip + _draw_piece: the 1-px profile stretched
        over the piece, cut to its joints by a clip quad. *end_cut* given:
        that instead of the piece's own (None: square-ended, as compose)."""
        d = piece['d']
        ax, ay = -d[1], d[0]
        (sx, sy), (ex, ey) = piece['s'], piece['e']
        half = max(piece['w0'], piece['w1']) / 2
        view, scale = piece['view']
        px = 1.0 / max(scale, 1e-6)
        start_cut = piece.get('start_cut')
        end_cut = piece.get('end_cut') if end_cut is Ellipsis else end_cut

        def reach(cut):
            if cut is None:
                return 0.0
            c = abs(cut[0] * d[0] + cut[1] * d[1])
            s = math.sqrt(max(0.0, 1.0 - c * c))
            return half * s / max(c, 0.3)

        back = reach(start_cut) + px
        fwd = reach(end_cut) + px
        bx, by = sx - d[0] * back, sy - d[1] * back
        fx, fy = ex + d[0] * fwd, ey + d[1] * fwd
        # a trapezoid from its start's width to its end's (FlatStroke.piece_widths):
        # the strip at the widest, the shader squeezes the profile to each place's
        from random_image_viewer.drawing.brushes import FlatStroke
        wb, wf = FlatStroke.piece_widths(piece, back, fwd)
        wd = max(wb, wf, 1e-6)
        taper = (wb / wd, wf / wd)
        h = piece['fh']
        m = (fx - bx, fy - by, ax * wd / h, ay * wd / h, bx - ax * wd / 2, by - ay * wd / 2)
        side = half * 1.6 + px

        def line(centre, cut, shift):
            if cut is None:
                cx, cy = -d[1], d[0]
            else:
                cx, cy = -cut[1], cut[0]
            k = side / max(abs(cx * ax + cy * ay), 0.3)
            ox, oy = centre[0] + d[0] * shift, centre[1] + d[1] * shift
            return (ox - cx * k, oy - cy * k), (ox + cx * k, oy + cy * k)

        s0, s1 = line(piece['s'], start_cut, -px)
        e0, e1 = line(piece['e'], end_cut, px)
        if (e0[0] - s0[0]) * d[0] + (e0[1] - s0[1]) * d[1] <= 0 \
                or (e1[0] - s1[0]) * d[0] + (e1[1] - s1[1]) * d[1] <= 0:
            s0 = (bx - ax * side * 2 - d[0] * half, by - ay * side * 2 - d[1] * half)
            s1 = (bx + ax * side * 2 - d[0] * half, by + ay * side * 2 - d[1] * half)
        v = view
        clip = [(v[0] * x + v[2] * y + v[4], v[1] * x + v[3] * y + v[5]) for x, y in (s0, e0, s1, e1)]
        level, turn, step, rgb, texture = piece['lay']
        sink.append((_mul(m, view), 1, h, texture, ('mf', level, turn, 'profile'),
                     step, colour_alpha, ('frame', rgb), clip, taper))

    def _lay_stamps(self, texture, stamps, view):
        v = _affine(view)
        scale = math.sqrt(abs(v[0] * v[3] - v[1] * v[2]))
        dims = self._dims(texture)
        sink = self._records
        stamp_max = getattr(texture, 'stamp_max', False)
        fs = texture.FRAME_STEP
        for stamp in stamps:
            on_screen = stamp[2] * scale
            step = round(stamp[4] * OPACITY_STEPS)
            if on_screen < 0.4 or step <= 0:
                continue
            level = texture.level(on_screen)
            step = min(OPACITY_STEPS, step)
            lw, lh = dims[level]
            tip_long = max(lw, lh)
            if stamp_max:
                sink.append((_mul(_place(stamp[0], stamp[1], stamp[2], stamp[3], lw, lh, tip_long), v),
                             lw, lh, texture, ('tip', level), step, 255, ('tip', stamp[5]), None))
                continue
            is_first = self._first is None
            self._restart = False
            pieces = self._body(texture, level, stamp, tip_long, scale)
            if is_first:
                sink.append((_mul(_place(stamp[0], stamp[1], stamp[2], stamp[3], lw, lh, tip_long), v),
                             lw, lh, texture, ('tip', level), step, 255, ('tip', stamp[5]), None))
                continue
            if self.head is not None:
                first, turn, d = self.head
                if self._restart:
                    self._flush_piece(sink)
                    frame = texture.motion_frame(level, turn)
                    tq = int(round(turn / fs) * fs) % 360
                    m = _place(first[0], first[1], first[2], math.atan2(d[1], d[0]),
                               frame['w'], frame['h'], tip_long)
                    sink.append((_mul(m, v), frame['w'], frame['h'], texture, ('mf', level, tq, 'whole'),
                                 step, 255, ('frame-end', first[5]), None))
                else:
                    frame = texture.motion_frame(level, turn)
                    tq = int(round(turn / fs) * fs) % 360
                    m = _place(first[0], first[1], first[2], math.atan2(d[1], d[0]),
                               frame['w'], frame['h'], tip_long)
                    sink.append((_mul(m, v), frame['w'], frame['h'], texture, ('mf', level, tq, 'whole'),
                                 step, 255, ('frame-end', first[5]), None))
            for piece in pieces:
                # the body at full 8-bit opacity (brushes.BODY_STEPS), as FlatStroke
                fine = max(1, min(BODY_STEPS, round(stamp[4] * BODY_STEPS)))
                piece['lay'] = (level, int(round(piece['turn'] / fs) * fs) % 360,
                                ('alpha', _qt_alpha(fine / BODY_STEPS)), stamp[5], texture)
                piece['view'] = (v, scale)
                self._push_piece(piece, sink)
            if self._reach is not None:
                self._tail = (texture, level, step, stamp, v, self._dir)

    def _tail_records(self):
        """FlatStroke.compose's extras: the body's waiting piece (square-ended,
        its colour at full strength) and _draw_tail's tip and 'back' fill."""
        out = []
        if self._pend is not None:
            self._piece_record(self._pend, out, 255, end_cut=None)
        if self._tail is not None:
            texture, level, step, stamp, v, d = self._tail
            lw, lh = self._dims(texture)[level]
            tip_long = max(lw, lh)
            turn = self._turn(stamp, d)
            frame = texture.motion_frame(level, turn)
            fs = texture.FRAME_STEP
            tq = int(round(turn / fs) * fs) % 360
            m = _place(stamp[0], stamp[1], stamp[2], math.atan2(d[1], d[0]), frame['w'], frame['h'],
                       tip_long)
            # the tip itself, its holes closed, streaks along the stroke (BrushTexture.motion_frame)
            out.append((_mul(m, v), frame['w'], frame['h'], texture, ('mf', level, tq, 'whole'),
                        step, 255, ('frame-end', stamp[5]), None))
            rx, ry = self._reach
            big = 4 * (frame['w'] + frame['h']) * stamp[2] / tip_long + 4
            ax, ay = d
            lx, ly = -ay, ax
            corners = [(rx + lx * big, ry + ly * big), (rx - lx * big, ry - ly * big),
                       (rx + lx * big + ax * big, ry + ly * big + ay * big),
                       (rx - lx * big + ax * big, ry - ly * big + ay * big)]
            clip = [(v[0] * x + v[2] * y + v[4], v[1] * x + v[3] * y + v[5]) for x, y in corners]
            m = _place(stamp[0], stamp[1], stamp[2], math.atan2(d[1], d[0]), frame['w'], frame['h'],
                       tip_long)
            out.append((_mul(m, v), frame['w'], frame['h'], texture, ('mf', level, tq, 'back'),
                        step, 255, ('frame-end', stamp[5]), clip))
        return out

    # -- records -> instances on the card -------------------------------------
    # a record: (image -> layer px affine, w, h, texture, mask key, opacity
    # step, colour alpha, colour spec, clip quad in layer px or None)

    @staticmethod
    def _records_box(records):
        x0 = y0 = math.inf
        x1 = y1 = -math.inf
        for rec in records:
            m, w, h, clip = rec[0], rec[1], rec[2], rec[8]
            pts = [(m[4], m[5]), (m[0] * w + m[4], m[1] * w + m[5]),
                   (m[2] * h + m[4], m[3] * h + m[5]),
                   (m[0] * w + m[2] * h + m[4], m[1] * w + m[3] * h + m[5])]
            ax0, ax1 = min(p[0] for p in pts), max(p[0] for p in pts)
            ay0, ay1 = min(p[1] for p in pts), max(p[1] for p in pts)
            if clip is not None:
                # drawn where both the strip and the clip are
                ax0, ax1 = max(ax0, min(p[0] for p in clip)), min(ax1, max(p[0] for p in clip))
                ay0, ay1 = max(ay0, min(p[1] for p in clip)), min(ay1, max(p[1] for p in clip))
            x0, y0, x1, y1 = min(x0, ax0), min(y0, ay0), max(x1, ax1), max(y1, ay1)
        if x0 > x1 or y0 > y1:
            return None
        bx, by = math.floor(x0) - 1, math.floor(y0) - 1
        return QRect(bx, by, math.ceil(x1) + 2 - bx, math.ceil(y1) + 2 - by)

    def _colour_spec(self, spec, w, h):
        """(palette row, index map entry or None) for a colour spec."""
        kind, rgb = spec
        k = self.kit
        if isinstance(rgb, int):
            return k.row((int(rgb),)), None
        axis, colours = rgb[0], tuple(rgb[1:])
        if kind == 'frame' and axis < 0:
            # particles in the body: their average (as frame_variant)
            from random_image_viewer.drawing.brushes import particles_mean
            return k.row((particles_mean(rgb),)), None
        n = len(colours)
        if kind == 'frame' or (kind == 'frame-end' and axis >= 0):
            axis = 1                    # frame_variant: bands run along the motion
        # ('frame-end': the front and the tail - particles keep their speckle
        # there, as frame_variant does)
        if axis < 0:
            num = -1 - axis
            e = k.entry(('scatter', num, n, w, h), lambda: _scatter_map(num, n, w, h))
        elif axis == 0:
            e = k.entry(('band', 0, n, w), lambda: _band_map(0, n, w, h))
        else:
            e = k.entry(('band', 1, n, h), lambda: _band_map(1, n, w, h))
        return k.row(colours), e

    def _source(self, texture, key):
        k = self.kit
        full = (id(texture),) + key
        e = k._entries.get(full)
        if e is not None:
            return e
        k._keep[id(texture)] = texture
        if key[0] == 'tip':
            return k.entry(full, lambda: _alpha_of(texture._levels[key[1]]))
        _, level, turn, part = key
        return k.entry(full, lambda: _alpha_of(texture.motion_frame(level, turn)[part]))

    def _instances(self, records):
        rows = []
        for rec in records:
            m, w, h, texture, key, step, colour_alpha, spec, clip = rec[:9]
            taper = rec[9] if len(rec) > 9 else (1.0, 1.0)
            det = m[0] * m[3] - m[1] * m[2]
            if abs(det) < 1e-12:
                continue
            sx, sy, _, _ = self._source(texture, key)
            row, ie = self._colour_spec(spec, w, h)
            if clip is None:
                c0 = (m[4], m[5])
                c = (m[4], m[5], m[0] * w + m[4], m[1] * w + m[5], m[2] * h + m[4], m[3] * h + m[5],
                     m[0] * w + m[2] * h + m[4], m[1] * w + m[3] * h + m[5])
            else:
                c0 = clip[0]
                c = clip[0] + clip[1] + clip[2] + clip[3]
            i11, i12, i21, i22 = m[3] / det, -m[1] / det, -m[2] / det, m[0] / det
            rx, ry = c0[0] - m[4], c0[1] - m[5]
            # Qt's bilinear sub-pixel precision (qdrawhelper's fast paths:
            # 8 bits scaling up, 4 bits scaling down or turned)
            fdx, fdy = int(i11 * 65536), int(i12 * 65536)
            if fdy == 0:
                bits = 8 if (abs(fdx) <= 2 * 65536 or abs(i22) < 0.125) else 4
            else:
                bits = 8 if (abs(i11) < 0.125 or abs(i22) < 0.125) else 4
            ix, iy, iw, ih = ie if ie is not None else (0, 0, 0, 0)
            rows.append(c + (i11, i12, i21, i22, i11 * rx + i21 * ry, i12 * rx + i22 * ry, c0[0], c0[1],
                             sx, sy, w, h, ix, iy, iw, ih,
                             step[1] if isinstance(step, tuple) else _GREY_ALPHA[step],
                             colour_alpha, row, bits,
                             taper[0], taper[1], 0.0, 0.0))
        return np.array(rows, np.float32).reshape(-1, STRIDE)

    def _resolve(self, records):
        try:
            return self._instances(records)
        except MemoryError:
            # (only between draws: the records keep keys, not atlas places)
            self.kit.reset_atlas()
            return self._instances(records)

    def _flush(self):
        """The waiting draws onto the buffers; their layer QRect, or None."""
        records = self._records
        if not records:
            return None
        self._records = []
        data = self._resolve(records)
        if not len(data):
            return None
        k = self.kit
        k.draw(data, k.buffers, (k.buffers[0].width(), k.buffers[0].height()), (0, 0))
        box = self._records_box(records)
        if box is None:
            return None
        box = box.intersected(QRect(0, 0, self.layer.width, self.layer.height))
        if box.isEmpty():
            return None
        self._bounds = box if self._bounds is None else self._bounds.united(box)
        self._dirty = box if self._dirty is None else self._dirty.united(box)
        return box

    def _draw_tail_copy(self):
        """The buffers inside the tail's box copied, the waiting piece and the
        tail drawn on the copy (never into the buffers). Its layer QRect, or
        None."""
        records = self._tail_records()
        if not records:
            return None
        box = self._records_box(records)
        if box is None:
            return None
        box = box.intersected(QRect(0, 0, self.layer.width, self.layer.height))
        if box.isEmpty():
            return None
        k = self.kit
        data = self._resolve(records)
        tm, tc = k.temp_buffers(box.width(), box.height())
        k.copy(k.buffers[0], box, tm)
        k.copy(k.buffers[1], box, tc)
        k.draw(data, (tm, tc), (tm.width(), tm.height()), (box.x(), box.y()))
        self._bounds = box if self._bounds is None else self._bounds.united(box)
        return box


# -- 🖌 bristles on the card ----------------------------------------------------

BRISTLE_PH = 32         # a bristle's cross-section, texels across


def _bristle_profile(soft):
    """A hair's cross-section (BRISTLE_PH x 1): solid in the core, a faint
    halo out to the edge for a soft hair - as BristleStroke's CPU drawing
    lays a core line over a 35 % halo 1 + 2 soft as wide."""
    core = 1.0 / (1.0 + 2.0 * soft)
    t = np.abs((np.arange(BRISTLE_PH) + 0.5) / BRISTLE_PH - 0.5) * 2.0     # 0 middle .. 1 edge
    edge = 1.5 / BRISTLE_PH                                               # (about a texel of softness)
    inner = np.clip((core - t) / edge + 0.5, 0.0, 1.0)
    outer = np.clip((1.0 - t) / edge + 0.5, 0.0, 1.0) * (0.35 if soft > 0 else 0.0)
    a = np.maximum(inner, outer)
    return (a * 255.0 + 0.5).astype(np.uint8).reshape(BRISTLE_PH, 1)


class GpuBristleStroke(GpuFlatStroke):
    """🖌 The Hair brush's bristles laid on the card: every hair's line a
    one-texel strip (its cross-section) stretched along it - one instance a
    segment, all of a pen event in one draw, as HeavyPaint lays its
    bristles in WebGL. The bristles themselves (where each hair goes, how
    wide, how strong, its colour) are brushes.BristleStroke.segments, the
    same as on the CPU; only the drawing is here. Coverage under GL_MAX,
    colour laid over itself in the order laid - as FlatStroke / its CPU
    drawing. preview / bounds / read_overlay / commit / cancel: as
    GpuFlatStroke (no tail: a bristle has no tip to show)."""

    def __init__(self, layer, model):
        super().__init__(layer)
        self.model = model          # brushes.BristleStroke(rect, buffers=False)

    def _tail_records(self):
        return []

    def add(self, texture, stamps, view=None):
        from PySide6.QtGui import QTransform
        self.gpu.current()
        segs = self.model.segments(texture, stamps, view if view is not None else QTransform())
        self.model.last_segments = segs
        return self.add_segments(segs)

    def add_segments(self, segs):
        """*segs* (as BristleStroke.segments gives them) laid: the box touched."""
        self.gpu.current()
        if not segs:
            return None
        # Each strip end to end with the next, overlapping by a hair (3/4 px),
        # coverage and colour alike. Reaching half its width past its ends,
        # the overlap showed at every joint: a carried colour (Wet) laid twice
        # there, hairs of different colours on one track (particles - the
        # bundle's hairs trail each other) taking turns, a dashed ladder along
        # the strands; with the colour drawn shorter than the coverage, the
        # bare coverage composed dark. (The wedge a strip leaves outside a
        # curve is a fraction of a pixel: the brush turns a few degrees a step.)
        try:
            data, box = self._bristle_instances(segs, reach=False)
        except MemoryError:
            self.kit.reset_atlas()          # (between draws: rebuilt from the keys)
            data, box = self._bristle_instances(segs, reach=False)
        if data is None:
            return None
        k = self.kit
        k.draw(data, k.buffers, (k.buffers[0].width(), k.buffers[0].height()), (0, 0))
        box = box.intersected(QRect(0, 0, self.layer.width, self.layer.height))
        if box.isEmpty():
            return None
        self._bounds = box if self._bounds is None else self._bounds.united(box)
        self._dirty = box if self._dirty is None else self._dirty.united(box)
        return box

    def _bristle_instances(self, segs, reach=True):
        """The segments as instance rows (see _FLAT_VERT), built in numpy.
        *reach*: each strip half its width past both ends (False: end to end)."""
        k = self.kit
        soft = float(self.model._hairs['soft'])
        q = round(soft * 20) / 20
        sx, sy, _w, ph = k.entry(('bristle-profile', q), lambda: _bristle_profile(q))
        p0 = np.concatenate([s[0] for s in segs])
        p1 = np.concatenate([s[1] for s in segs])
        width = np.concatenate([s[2] for s in segs])
        alpha = np.concatenate([s[3] for s in segs])
        colours = np.concatenate([s[4] for s in segs])
        keep = alpha > 0.01
        if not keep.any():
            return None, None
        p0, p1, width, alpha, colours = p0[keep], p1[keep], width[keep], alpha[keep], colours[keep]
        # the strip: the segment, reaching half its width past both ends (the
        # next one overlaps it: one line), as wide as the hair with its halo
        full = width * (1.0 + 2.0 * soft) if soft > 0 else width
        d = p1 - p0
        ln = np.hypot(d[:, 0], d[:, 1])
        tiny = ln < 1e-6
        ln[tiny] = 1.0
        ux, uy = d[:, 0] / ln, d[:, 1] / ln
        ux[tiny], uy[tiny] = 1.0, 0.0
        # (end to end the joints left hairline gaps without colour under the
        # coverage - dark dots along the hair: a hair's breadth over instead)
        half = width * 0.5 if reach else np.minimum(width * 0.5, 0.75)
        bx, by = p0[:, 0] - ux * half, p0[:, 1] - uy * half
        fx, fy = p1[:, 0] + ux * half, p1[:, 1] + uy * half
        ax, ay = -uy, ux                                        # across
        m0, m1 = fx - bx, fy - by
        m2, m3 = ax * full / ph, ay * full / ph
        m4, m5 = bx - ax * full / 2, by - ay * full / 2
        det = m0 * m3 - m1 * m2
        ok = np.abs(det) > 1e-12
        if not ok.all():
            m0, m1, m2, m3, m4, m5, det = (a[ok] for a in (m0, m1, m2, m3, m4, m5, det))
            alpha, colours = alpha[ok], colours[ok]
            if not len(alpha):
                return None, None
        n = len(alpha)
        rows = np.empty((n, STRIDE), np.float32)
        rows[:, 0], rows[:, 1] = m4, m5
        rows[:, 2], rows[:, 3] = m0 + m4, m1 + m5
        rows[:, 4], rows[:, 5] = m2 * ph + m4, m3 * ph + m5
        rows[:, 6], rows[:, 7] = m0 + m2 * ph + m4, m1 + m3 * ph + m5
        rows[:, 8], rows[:, 9] = m3 / det, -m1 / det            # device -> image
        rows[:, 10], rows[:, 11] = -m2 / det, m0 / det
        rows[:, 12], rows[:, 13] = 0.0, 0.0                     # image (0, 0) at the corner ...
        rows[:, 14], rows[:, 15] = m4, m5                       # ... here
        rows[:, 16], rows[:, 17], rows[:, 18], rows[:, 19] = sx, sy, 1, ph
        # each colour: its palette slot; the column through a 1 x 1 index map
        slot_of = {}
        for c in np.unique(colours):
            row, col = k.slot(int(c))
            ix, iy, _, _ = k.entry(('const', col), lambda col=col: np.full((1, 1), col, np.uint8))
            slot_of[int(c)] = (row, ix, iy)
        info = np.array([slot_of[int(c)] for c in colours], np.float32).reshape(-1, 3)
        rows[:, 20], rows[:, 21] = info[:, 1], info[:, 2]
        rows[:, 22], rows[:, 23] = 1, 1
        rows[:, 24] = np.clip(np.rint(alpha * 255.0), 1, 255)   # coverage
        # the colour laid whole - or, carried (Wet), partly over what the hair
        # laid before it (brushes.BristleStroke.CARRY_MIX)
        rows[:, 25] = round(255 * self.model.CARRY_MIX) if self.model.carries else 255
        rows[:, 26] = info[:, 0]
        rows[:, 27] = 8                                         # bilinear weight bits
        rows[:, 28], rows[:, 29], rows[:, 30], rows[:, 31] = 1.0, 1.0, 0.0, 0.0
        xs = np.concatenate([rows[:, 0], rows[:, 2], rows[:, 4], rows[:, 6]])
        ys = np.concatenate([rows[:, 1], rows[:, 3], rows[:, 5], rows[:, 7]])
        x0, y0 = math.floor(float(xs.min())) - 1, math.floor(float(ys.min())) - 1
        box = QRect(x0, y0, math.ceil(float(xs.max())) + 2 - x0, math.ceil(float(ys.max())) + 2 - y0)
        return rows, box
