"""⛰ Impasto: paint with body, lit like relief (as Rebelle's bristle impasto).

A layer's paint is turned into a height map and lit from one side, so its
strokes stand up from the canvas: ridges and grooves along the bristles, a
raised rim where the paint was pushed to a stroke's edge. The height comes
from the paint itself - no second drawing of every mark:

  * body    - where there is paint (its coverage);
  * grooves - the fine streaks in the paint (bristle gaps, the colour each
              bristle carried): the paint's brightness minus a blur of it;
  * edges   - a rim just inside each stroke's outline (Rebelle's "Edges"):
              coverage its own wider blur has not caught up with.

Lighting: a normal from the height's slopes (Sobel), diffuse light from
*light* degrees and 45° up, and a Blinn-Phong highlight for gloss. Flat
paint keeps its colour exactly; only slopes darken or catch the light.

Speed: OpenCV does the two blurs (8-bit, fast); everything else is one
fused pass per pixel - an OpenCL kernel on the app's GPU (see use_gpu), or
numpy on the CPU - and only over the part of the layer that has paint.
Callers cache the result: it is redone only when the layer's paint, the
settings or the zoom change. (Every step as its own OpenCV call, even on
the GPU, took ~300 ms for a full-screen layer: memory, not arithmetic.)
"""

import math

import numpy as np
from PySide6.QtGui import QImage

try:
    import cv2
except Exception:           # pragma: no cover
    cv2 = None

DEFAULTS = {
    'depth': 50,        # % how high the paint stands
    'grooves': 60,      # % bristle grooves / streak relief
    'edges': 35,        # % raised rim along stroke edges
    'edge_width': 6,    # picture px: how wide that rim is
    'light': 225,       # degrees the light comes from (0 right, 90 below, 225 top left)
    'gloss': 25,        # % wet highlight
}

SHINE = 40.0            # highlight tightness

_KERNEL = r"""
inline float height_at(__global const uchar4 *src, __global const uchar *ab,
                       __global const uchar *gb, int i, float kg, float ke)
{
    uchar4 p = src[i];
    float a = p.w * (1.0f / 255.0f);
    float g = (0.114f * p.x + 0.587f * p.y + 0.299f * p.z) * (1.0f / 255.0f);
    float rim = fmax(a - ab[i] * (1.0f / 255.0f), 0.0f) * a;
    return a + kg * (g - gb[i] * (1.0f / 255.0f)) + ke * rim;
}

__kernel void impasto(__global const uchar4 *src, __global const uchar *ab,
                      __global const uchar *gb, __global uchar4 *dst,
                      const int w, const int h, const float kg, const float ke,
                      const float bump, const float lx, const float ly, const float lz,
                      const float hx, const float hy, const float hz,
                      const float gloss, const float flat_spec)
{
    int x = get_global_id(0), y = get_global_id(1);
    if (x >= w || y >= h) return;
    int i = y * w + x;
    uchar4 p = src[i];
    if (p.w == 0) { dst[i] = p; return; }
    int xm = max(x - 1, 0), xp = min(x + 1, w - 1);
    int ym = max(y - 1, 0), yp = min(y + 1, h - 1);
    float h00 = height_at(src, ab, gb, ym * w + xm, kg, ke);
    float h10 = height_at(src, ab, gb, ym * w + x,  kg, ke);
    float h20 = height_at(src, ab, gb, ym * w + xp, kg, ke);
    float h01 = height_at(src, ab, gb, y  * w + xm, kg, ke);
    float h21 = height_at(src, ab, gb, y  * w + xp, kg, ke);
    float h02 = height_at(src, ab, gb, yp * w + xm, kg, ke);
    float h12 = height_at(src, ab, gb, yp * w + x,  kg, ke);
    float h22 = height_at(src, ab, gb, yp * w + xp, kg, ke);
    float gx = ((h20 + 2.0f * h21 + h22) - (h00 + 2.0f * h01 + h02)) * bump;
    float gy = ((h02 + 2.0f * h12 + h22) - (h00 + 2.0f * h10 + h20)) * bump;
    float inv = rsqrt(gx * gx + gy * gy + 1.0f);
    float nx = -gx * inv, ny = -gy * inv, nz = inv;
    float diffuse = nx * lx + ny * ly + nz * lz;
    float shade = fmax(1.0f + (diffuse - lz) / lz, 0.25f);
    float sp = fmax(nx * hx + ny * hy + nz * hz, 0.0f);
    sp = fmax(pow(sp, 40.0f) - flat_spec, 0.0f) * gloss * p.w;
    float a = (float)p.w;
    float b = fmin(p.x * shade + sp, a);
    float g = fmin(p.y * shade + sp, a);
    float r = fmin(p.z * shade + sp, a);
    dst[i] = (uchar4)((uchar)(b + 0.5f), (uchar)(g + 0.5f), (uchar)(r + 0.5f), p.w);
}
"""

_gpu = None             # the app's GPU processor (see use_gpu)
_program = None         # the kernel, built on it once
_gpu_failed = False


def use_gpu(processor):
    """Light on *processor* (GPULutProcessor - its OpenCL context and
    queue) when it is up; None for the CPU."""
    global _gpu
    _gpu = processor


def settings_key(settings):
    s = dict(DEFAULTS, **(settings or {}))
    return tuple(int(s[k]) for k in sorted(DEFAULTS))


def _array(image, write=False):
    w, h, line = image.width(), image.height(), image.bytesPerLine()
    bits = image.bits() if write else image.constBits()
    data = np.frombuffer(bits, np.uint8, count=line * h).reshape(h, line)
    return data[:, :w * 4].reshape(h, w, 4)


def _params(s, scale):
    depth = s['depth'] / 100.0
    kg = 2.2 * s['grooves'] / 100.0
    ke = 2.5 * s['edges'] / 100.0
    # Slopes per picture px: the relief is laid out in picture px (the
    # blurs go with *scale*), so its slopes on the device are times *scale*
    # - a zoom in does not flatten it; zoomed out, grooves fade under a pixel.
    bump = 6.0 * depth * scale / 8.0            # / 8: Sobel's 3x3 weights
    az, el = math.radians(s['light']), math.radians(45.0)
    lx, ly, lz = math.cos(az) * math.cos(el), math.sin(az) * math.cos(el), math.sin(el)
    hx, hy, hz = lx, ly, lz + 1.0
    hn = math.sqrt(hx * hx + hy * hy + hz * hz)
    hx, hy, hz = hx / hn, hy / hn, hz / hn
    gloss = 1.6 * s['gloss'] / 100.0
    return kg, ke, bump, (lx, ly, lz), (hx, hy, hz), gloss, hz ** SHINE


def light(image, settings=None, scale=1.0, use_gpu=True):
    """*image* (premultiplied ARGB32) lit as impasto: a new QImage, same
    size and alpha. *scale*: device px per picture px, so the relief keeps
    its size and slope at any zoom."""
    if cv2 is None or image.isNull():
        return image
    s = dict(DEFAULTS, **(settings or {}))
    src = image if image.format() == QImage.Format_ARGB32_Premultiplied else \
        image.convertToFormat(QImage.Format_ARGB32_Premultiplied)
    pixels = _array(src)
    alpha8 = np.ascontiguousarray(pixels[:, :, 3])
    x, y, w, h = cv2.boundingRect(alpha8)
    out = src.copy()
    if w == 0 or h == 0:
        return out
    scale = max(0.05, float(scale))
    edge_sigma = max(0.6, s['edge_width'] * scale)
    fine_sigma = max(0.6, 2.5 * scale)
    pad = int(math.ceil(max(edge_sigma, fine_sigma) * 3)) + 2
    H, W = alpha8.shape
    x0, y0 = max(0, x - pad), max(0, y - pad)
    x1, y1 = min(W, x + w + pad), min(H, y + h + pad)
    part = np.ascontiguousarray(pixels[y0:y1, x0:x1])
    # The two blurs (8-bit: fast) ...
    a8 = np.ascontiguousarray(part[:, :, 3])
    g8 = cv2.cvtColor(part, cv2.COLOR_BGRA2GRAY)
    ab = cv2.GaussianBlur(a8, (0, 0), edge_sigma) if s['edges'] else a8
    gb = cv2.GaussianBlur(g8, (0, 0), fine_sigma) if s['grooves'] else g8
    # ... and the rest in one pass.
    lit = None
    if use_gpu:
        lit = _light_gpu(part, ab, gb, s, scale)
    if lit is None:
        lit = _light_cpu(part, ab, gb, s, scale)
    _array(out, write=True)[y0:y1, x0:x1] = lit
    return out


def _light_gpu(part, ab, gb, s, scale):
    global _program, _gpu_failed
    gpu = _gpu
    if gpu is None or _gpu_failed or not getattr(gpu, 'gpu_enabled', False):
        return None
    try:
        import random_image_viewer.constants as constants
        cl = constants.cl
        if _program is None:
            _program = cl.Kernel(cl.Program(gpu.context, _KERNEL).build(), 'impasto')
        h, w = part.shape[:2]
        mf = cl.mem_flags
        src = cl.Buffer(gpu.context, mf.READ_ONLY | mf.COPY_HOST_PTR, hostbuf=part)
        abuf = cl.Buffer(gpu.context, mf.READ_ONLY | mf.COPY_HOST_PTR, hostbuf=np.ascontiguousarray(ab))
        gbuf = cl.Buffer(gpu.context, mf.READ_ONLY | mf.COPY_HOST_PTR, hostbuf=np.ascontiguousarray(gb))
        out = np.empty_like(part)
        dst = cl.Buffer(gpu.context, mf.WRITE_ONLY, out.nbytes)
        kg, ke, bump, (lx, ly, lz), (hx, hy, hz), gloss, flat = _params(s, scale)
        f = np.float32
        _program(gpu.queue, (w, h), None, src, abuf, gbuf, dst, np.int32(w), np.int32(h),
                 f(kg), f(ke), f(bump), f(lx), f(ly), f(lz), f(hx), f(hy), f(hz),
                 f(gloss), f(flat))
        cl.enqueue_copy(gpu.queue, out, dst)
        gpu.queue.finish()
        return out
    except Exception as e:      # a driver that will not: the CPU from now on
        print(f"⛰ Impasto on the GPU failed, using the CPU: {e}")
        _gpu_failed = True
        return None


def _light_cpu(part, ab, gb, s, scale):
    kg, ke, bump, (lx, ly, lz), (hx, hy, hz), gloss, flat = _params(s, scale)
    f32 = np.float32
    a = part[:, :, 3].astype(f32) * f32(1 / 255.0)
    g = cv2.cvtColor(part, cv2.COLOR_BGRA2GRAY).astype(f32) * f32(1 / 255.0)
    height = a.copy()
    if kg:
        height += f32(kg) * (g - gb.astype(f32) * f32(1 / 255.0))
    if ke:
        height += f32(ke) * np.maximum(a - ab.astype(f32) * f32(1 / 255.0), 0) * a
    gx = cv2.Sobel(height, cv2.CV_32F, 1, 0, ksize=3, scale=bump, borderType=cv2.BORDER_REPLICATE)
    gy = cv2.Sobel(height, cv2.CV_32F, 0, 1, ksize=3, scale=bump, borderType=cv2.BORDER_REPLICATE)
    inv = 1.0 / np.sqrt(gx * gx + gy * gy + 1.0)
    nz = inv
    nx = -gx * inv
    ny = -gy * inv
    shade = np.maximum(1.0 + (nx * f32(lx) + ny * f32(ly) + nz * f32(lz) - f32(lz)) / f32(lz), 0.25)
    out = part.copy()
    alpha = part[:, :, 3].astype(f32)
    if gloss:
        sp = np.maximum(nx * f32(hx) + ny * f32(hy) + nz * f32(hz), 0)
        sp = np.maximum(sp ** f32(SHINE) - f32(flat), 0) * f32(gloss) * alpha
    else:
        sp = 0.0
    for c in range(3):
        lit = np.minimum(part[:, :, c].astype(f32) * shade + sp, alpha)
        out[:, :, c] = (lit + 0.5).astype(np.uint8)
    return out
