"""Pigment mixing: colours mixed like paint, not like light.

Mixing light (a plain average of RGB) turns blue and yellow grey; paint
turns them green, because each pigment *takes away* light. This follows the
Kubelka-Munk model the way spectral.js does: a colour becomes a reflectance
curve over the visible range (built from white, cyan, magenta, yellow, red,
green and blue curves), the curves mix by their absorption / scattering
(K/S), and the result is turned back into RGB. Whatever the curves cannot
express goes along as a residual, so a colour mixed with itself - or at 0 %
and 100 % - comes back exactly.

In K/S terms dark colours are very strong - a touch of black would blacken
everything - so each side's share is weighted by its lightness (square root),
as paint roughly behaves. The weight is linear in the share, so many small
mixes (a blender's per-stamp pickup) add up like one big one.

Plain numpy on a handful of numbers per pixel. Patches mix like light at full
size and add how paint differs from that worked out at half size (a smooth
shift, so the detail stays sharp), skipping pixels whose two colours are
nearly the same (there paint and light mix alike).
"""

from functools import lru_cache

import numpy as np

BANDS = 10                       # samples over 400 - 700 nm
FLOOR = 0.02                     # least a primary reflects anywhere
SAME = 12                        # summed |RGB| difference below which light mixing is used

_lam = np.linspace(400.0, 700.0, BANDS)


def _up(c, w):
    return 1.0 / (1.0 + np.exp(-(_lam - c) / w))


def _down(c, w):
    return 1.0 - _up(c, w)


def _peak(c, w):
    return np.exp(-0.5 * ((_lam - c) / w) ** 2)


# Reflectance of the primaries: white cyan magenta yellow red green blue.
_PRIMARIES = np.stack([
    np.ones(BANDS),
    _down(575, 16),
    _down(495, 16) + _up(590, 14),
    _up(495, 14),
    _up(595, 14),
    _up(480, 15) * _down(585, 15),
    _down(505, 18),
]).clip(FLOOR, 1.0).astype(np.float32)

# Reflectance -> linear RGB: three broad sensitivities, flat white -> 1, 1, 1.
_SENSE = np.stack([_peak(605, 35), _peak(540, 30), _peak(455, 25)])
_SENSE = (_SENSE / _SENSE.sum(1, keepdims=True)).astype(np.float32).T.copy()   # BANDS x 3

_v = np.arange(256) / 255.0
_TO_LINEAR = np.where(_v <= 0.04045, _v / 12.92, ((_v + 0.055) / 1.055) ** 2.4).astype(np.float32)
_q = np.linspace(0.0, 1.0, 4096)
_TO_SRGB = np.round(np.where(_q <= 0.0031308, _q * 12.92,
                             1.055 * _q ** (1 / 2.4) - 0.055) * 255).astype(np.uint8)
_LUMA = np.array([0.2126, 0.7152, 0.0722], np.float32)


def _latent(rgb):
    """uint8 (n, 3) RGB -> K/S (n, BANDS), residual (n, 3), strength (n,)."""
    lin = _TO_LINEAR[rgb]
    r, g, b = lin[:, 0], lin[:, 1], lin[:, 2]
    w = np.minimum(np.minimum(r, g), b)
    r, g, b = r - w, g - w, b - w
    c, m, y = np.minimum(g, b), np.minimum(r, b), np.minimum(r, g)
    weights = np.stack([w, c, m, y, r - m - y, g - c - y, b - c - m], 1).clip(0.0, None)
    refl = np.clip(weights @ _PRIMARIES, FLOOR * 0.5, 1.0)
    ks = (1.0 - refl) ** 2 / (2.0 * refl)
    residual = lin - refl @ _SENSE
    strength = np.sqrt(np.maximum(lin @ _LUMA, 0.002))
    return ks, residual, strength


def mix_arrays(a, b, t):
    """uint8 (n, 3) RGB *a* and *b* mixed like paint, *t* (scalar or (n,))
    of the way to *b*. Returns uint8 (n, 3)."""
    t = np.broadcast_to(np.asarray(t, np.float32), (len(a),))
    ka, ra, sa = _latent(a)
    kb, rb, sb = _latent(b)
    wa = (1.0 - t) * sa
    wb = t * sb
    share = (wb / np.maximum(wa + wb, 1e-12))[:, None]
    ks = ka + (kb - ka) * share
    refl = 1.0 + ks - np.sqrt(ks * ks + 2.0 * ks)
    lin = refl @ _SENSE + ra + (rb - ra) * share
    out = _TO_SRGB[np.clip(lin * 4095.0 + 0.5, 0, 4095).astype(np.int32)]
    # The ends exactly (no rounding on the way through).
    out[t <= 0.0] = a[t <= 0.0]
    out[t >= 1.0] = b[t >= 1.0]
    return out


def _split(c):
    return (c >> 16) & 255, (c >> 8) & 255, c & 255


@lru_cache(maxsize=65536)
def _mix_cached(a, b, t):
    out = mix_arrays(np.array([_split(a)], np.uint8), np.array([_split(b)], np.uint8), t / 4096.0)[0]
    return (int(out[0]) << 16) | (int(out[1]) << 8) | int(out[2])


def mix(a, b, t):
    """0xRRGGBB *a* and *b* mixed like paint, *t* of the way to *b*."""
    if t <= 0.0 or a == b:
        return a
    if t >= 1.0:
        return b
    return _mix_cached(a, b, round(t * 4096))


def mix_many(a, b, t):
    """Lists of 0xRRGGBB *a* and *b* mixed like paint, *t* (one share for
    all, or one each) of the way to *b* - one array pass for the lot instead
    of a call each (a smudge's streaks mix 32 colours per stamp)."""
    n = len(a)
    if n == 0:
        return list(a)
    t = np.broadcast_to(np.asarray(t, np.float32), (n,))
    if (t <= 0.0).all():
        return list(a)
    if (t >= 1.0).all():
        return list(b)
    ca, cb = np.array(a, np.uint32), np.array(b, np.uint32)
    todo = ca != cb
    if not todo.any():
        return list(a)
    sub_a, sub_b = ca[todo], cb[todo]
    pa = np.stack([(sub_a >> 16) & 255, (sub_a >> 8) & 255, sub_a & 255], 1).astype(np.uint8)
    pb = np.stack([(sub_b >> 16) & 255, (sub_b >> 8) & 255, sub_b & 255], 1).astype(np.uint8)
    m = mix_arrays(pa, pb, t[todo]).astype(np.uint32)
    out = ca.copy()
    out[todo] = (m[:, 0] << 16) | (m[:, 1] << 8) | m[:, 2]
    return out.tolist()


def _straight(bgra, alpha):
    """Premultiplied BGRA uint8 (n, 4) -> straight RGB float (n, 3)."""
    rgb = bgra[:, 2::-1].astype(np.float32)
    return rgb * (255.0 / np.maximum(alpha, 1.0))[:, None]


SHIFT_CELLS = 4096               # the paint shift is worked out on at most this many cells


def _shrink(values, h, w, f):
    """(h * w, k) per pixel -> (ceil(h/f) * ceil(w/f), k) block means."""
    v = values.reshape(h, w, -1)
    ph, pw = -h % f, -w % f
    if ph or pw:
        v = np.pad(v, ((0, ph), (0, pw), (0, 0)), mode='edge')
    # Summed strided slices: much quicker than mean() over reshaped axes.
    total = v[0::f, 0::f].copy()
    for i in range(f):
        for j in range(f):
            if i or j:
                total += v[i::f, j::f]
    total *= 1.0 / (f * f)
    return total.reshape(-1, total.shape[2])


def _mix_straight(dst, src, share, both, h, w):
    """Straight RGB float *dst* (h * w, 3) towards *src* by *share*: light
    mixing, plus how paint differs from it where *both* are there - that
    worked out on a coarser grid (half size, or smaller for a big patch:
    a smooth shift, so the detail stays sharp)."""
    out = dst + (src - dst) * share[:, None]
    half = h >= 8 and w >= 8
    f = 1
    if half:
        f = 2
        while -(-h // f) * -(-w // f) > SHIFT_CELLS:
            f += 1
        packed = np.empty((len(dst), 8), np.float32)       # one pass for all four
        packed[:, 0:3], packed[:, 3:6], packed[:, 6], packed[:, 7] = dst, src, share, both
        small = _shrink(packed, h, w, f)
        d, s, t, on = small[:, 0:3], small[:, 3:6], small[:, 6], small[:, 7] >= 0.75
    else:
        d, s, t, on = dst, src, share, both
    pick = on & (t > 0.0) & (t < 1.0) & (np.abs(d - s).sum(1) > SAME)
    if not pick.any():
        return out
    a = np.clip(d[pick] + 0.5, 0, 255).astype(np.uint8)
    b = np.clip(s[pick] + 0.5, 0, 255).astype(np.uint8)
    shift = np.zeros_like(d)
    shift[pick] = mix_arrays(a, b, t[pick]).astype(np.float32) - (d[pick] + (s[pick] - d[pick]) * t[pick, None])
    if half:
        hh, hw = -(-h // f), -(-w // f)
        shift = shift.reshape(hh, hw, 3).repeat(f, 0).repeat(f, 1)[:h, :w].reshape(-1, 3)
        shift[~both] = 0.0
    return out + shift


def _pack(rgb, alpha, shape):
    out = np.empty((len(alpha), 4), np.uint8)
    scale = alpha / 255.0
    out[:, 2::-1] = np.clip(rgb * scale[:, None] + 0.5, 0, 255).astype(np.uint8)
    out[:, 3] = np.clip(alpha + 0.5, 0, 255).astype(np.uint8)
    return out.reshape(shape)


_gpu = None             # the app's GPU processor (see use_gpu)
GPU_MIN_PIXELS = 1024   # smaller patches: the CPU is as quick


def use_gpu(processor):
    """Mix patches on *processor* (GPULutProcessor) when it is up: the
    whole paint model per pixel, no coarse shift needed."""
    global _gpu
    _gpu = processor


def over(dst, src, opacity, rgb=None, amount=0.0):
    """Premultiplied BGRA uint8 arrays (h, w, 4): *src* laid over *dst* at
    *opacity* as SourceOver would, the colours mixed like paint. With *rgb*
    (0xRRGGBB) and *amount*, the result is then tinted towards it as tint()
    does - in the same pass, without unpacking the pixels twice. On the GPU
    when there is one (see use_gpu)."""
    gpu = _gpu
    if (gpu is not None and getattr(gpu, 'gpu_enabled', False)
            and not getattr(gpu, '_pigment_failed', False)
            and dst.shape[0] * dst.shape[1] >= GPU_MIN_PIXELS):
        out = gpu.pigment_over_gpu(dst, src, opacity, _PRIMARIES, _SENSE, rgb, amount)
        if out is not None:
            return out
    shape = dst.shape
    d = dst.reshape(-1, 4)
    s = src.reshape(-1, 4)
    da = d[:, 3].astype(np.float32)
    sa = s[:, 3].astype(np.float32) * opacity
    out_a = sa + da * (1.0 - sa / 255.0)
    share = np.where(out_a > 0, sa / np.maximum(out_a, 1e-6), 0.0).astype(np.float32)
    out = _mix_straight(_straight(d, da), _straight(s, s[:, 3].astype(np.float32)),
                        share, (da > 0) & (sa > 0), shape[0], shape[1])
    if rgb is not None and amount > 0.0:
        colour = np.array(_split(rgb), np.float32)
        out = _mix_straight(out, np.broadcast_to(colour, out.shape),
                            np.full(len(d), amount, np.float32), out_a > 0, shape[0], shape[1])
    return _pack(out, out_a, shape)


def tint(dst, rgb, amount):
    """Premultiplied BGRA uint8 (h, w, 4): its colours mixed like paint
    *amount* of the way to 0xRRGGBB *rgb*; transparent stays transparent."""
    shape = dst.shape
    d = dst.reshape(-1, 4)
    da = d[:, 3].astype(np.float32)
    colour = np.array(_split(rgb), np.float32)
    share = np.full(len(d), amount, np.float32)
    out = _mix_straight(_straight(d, da), np.broadcast_to(colour, (len(d), 3)), share, da > 0,
                        shape[0], shape[1])
    return _pack(out, da, shape)
