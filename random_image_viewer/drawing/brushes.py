"""Textured brushes: a PNG stamped along the stroke.

A texture is a mask: ink where the PNG is bright and opaque, so both white-on-
transparent tips and grey-on-black ones work as they are. Each stamp is turned
by the pen's tilt (or, without tilt, the direction of the stroke), sized and
faded by pressure, and can have its colour jittered.

A stroke's stamps are worked out once, while it is drawn, in picture
coordinates (:class:`StampTrail`), and kept with the stroke. The live preview
and every later repaint draw that same list, so the stroke never changes once
the pen lifts - not with zoom, not with the random jitter.

The brush can also Blend (each stamp mixes its colour with what is on screen
under it) or Smudge (no colour of its own: it picks up what is under the pen
and drags it along). The colours are sampled while drawing and kept in the
stamps like any other, so those strokes stay put too.
"""

import colorsys
import math
import os
import random
import time

import numpy as np
from PySide6.QtCore import Qt, QLineF, QPointF, QRect, QRectF
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QTransform

from . import pigment

try:
    import cv2 as _cv2      # 🌊 Smear's per-pixel blend, several times numpy's speed
except Exception:           # pragma: no cover - numpy does it then
    _cv2 = None

TEXTURE_MAX = 256       # px: larger PNGs are scaled down once, on load
LEVEL_MIN = 16          # smallest of the scaled copies, see BrushTexture.level
PALETTE_SIZE = 24       # jittered colours a stroke picks from, per stamp
TILT_MIN = 8            # degrees: a pen held straighter than this has no lean

DEFAULT_SETTINGS = {
    'texture': '',              # PNG path; '' is the round brush
    'spacing': 20,              # % of the stamp size between stamps
    'size_pressure': True,
    'opacity_pressure': True,
    'angle_mode': 'auto',       # 'auto': tilt, else stroke direction;
                                #   'direction': stroke direction only; 'fixed'
    'angle': 0,                 # degrees, added to either
    'angle_jitter': 0,          # degrees either way, per stamp
    'hue_jitter': 0,            # % (100 = anywhere on the wheel)
    'sat_jitter': 0,            # %
    'val_jitter': 0,            # %
    'jitter_marks': False,      # lines and shapes get a jittered colour too (one each)
    'lines_paint': True,        # new straight lines / polylines are paint (brushes smudge,
                                #   blend and cover them); off: guides drawn over the paint
    'gradient': 'linear',       # 🌈 the gradient tool: 'linear' or 'radial'
    'gradient_to': '',          # 🌈 ... from the pen colour to this ('#rrggbb'), '' = transparent
    'line_gradient': False,     # 🌈 lines and polylines take the gradient, along their way
    'shape_gradient': False,    # 🌈 shapes fill with the gradient instead of one colour
    'shape_stars': False,       # ✨ shapes fill with a starfield (tiny random specks) instead
    'line_stars': False,        # ✨ lines and polylines paint a starfield along their way
    'stars_amount': 40,         # ✨ 1..100: how many (20·v² per megapixel of picture)
    'stars_size': 1,            # ✨ the smallest star, real screen px as drawn (few medium, rare ones 5x)
    'gradient_mid': 50,         # 🌈 % along it where the colours are half mixed (gradient_bias)
    'jitter_per_stamp': False,  # True: every stamp its own colour; 'streaks':
                                #   every streak across the tip its own colour;
                                #   'particles': every speck its own colour
    'scatter': 0,               # %: stamps thrown off the stroke, up to this share of their size
    'size_jitter': 0,           # %: stamps up to this much smaller, at random
    'flat': True,               # stamps don't build up within a stroke (see FlatStroke)
    'mode': 'paint',            # one of MODES
    'brush_blend': 'Normal',    # paint / history: the stroke blended with what is under
                                #   it (a layers.BLEND_MODES name), kept as those pixels
    'sample': 'below',          # blend / smudge read: 'below' (this layer and the
                                #   ones under it), 'layer' (this one only), 'all'
    'mix': 50,                  # %: blend - share of the colour under the pen;
                                #    smudge - how far the picked-up colour carries
    'pigment': True,            # blend / smudge mix colours like paint (blue + yellow
                                #   = green), not like light (= grey); see pigment.py
    'spray_spread': 1,          # Spray tip: its cloud is the brush size times this
    'spray_density': 150,       # Spray tip: specks per stamp
    'spray_speck': 2,           # Spray tip: speck size, screen px as drawn
    'rake_tines': 8,            # Rake tip: tines in the row
    'rake_tine': 45,            # Rake tip: each tine's thickness, % of the gap between them
    'rake_wobble': 25,          # Rake tip: % the tines differ in size, place and strength
    'soft_softness': 60,        # Soft tip: % of the radius that fades out (100: all feather)
    'hair_count': 48,           # 🖌 Hair brush: hairs across the tip
    'hair_vary': 50,            # 🖌 ... % they differ in thickness / strength (gaps: streaks)
    'hair_ragged': 40,          # 🖌 ... % they differ in length and start (frayed ends)
    'hair_soft': 30,            # 🖌 ... % soft hair edges
    'hair_seed': 17,            # 🖌 ... which brush (another seed: other hairs)
    'hair_width': 8,            # 🖌 ... each hair's thickness, ‰ of the brush size (BristleStroke)
    'hair_flat': 35,            # 🖌 ... 0: a round bundle, 100: a flat row of hairs
    'hair_shape': 40,           # 🖌 ... each hair's tip: 0 round, 100 a long flat dab (ribbons)
    'hair_wet': 0,              # 🖌 ... % each hair picks up the paint it passes and smears it on
    'smear_grab': 70,           # Smear: % how firmly hairs grab paint that stands out
    'smear_edge': 25,           # Smear: % how much it must stand out to be grabbed
    'smear_blur': 20,           # Smear: % how much the hairs also blend what they pass
    'push_ice': 40,             # Push: % how unevenly the pixels move (cracks the paint like ice)
    'smoothing': 25,            # %: the pen's way steadied (1€ filter; see StampTrail._smoothed)
    'pressure_curve': 0,        # -50..50: < 0 a light touch counts more, > 0 a firmer press needed
}

MODES = ('paint', 'blend', 'smudge', 'history', 'pick', 'erase', 'smear', 'push')
# 'smear': drags the very pixels under it into streaks (see DragStroke)
# 'push': liquify - moves the pixels under it along, crisp, cracking like ice (PushStroke)
# Modes that work on the pixels under the pen and are kept as those pixels
PIXEL_MODES = ('blend', 'pick', 'smear', 'push', 'smudge')
# 'erase': the stroke - tip, size, pressure, opacity - rubs paint out of the
# layer it is drawn on instead of laying any (see main_window: Stroke.erase)
# Modes that blend like Blend ('pick': Blend with the colour picked under the
# pen where the stroke starts, as the eyedropper would - see main_window)
BLENDING = ('blend', 'pick')
# Each mode keeps its own slider (Mix / Carry / Tint): what suits one makes
# no sense in another - Carry 100 % made Blend invisible.
MODE_MIX = {'paint': 50, 'blend': 50, 'smudge': 70, 'history': 0, 'pick': 50, 'erase': 0,
            'smear': 60, 'push': 85}
BLEND_SHOW_MAX = 0.65   # Blend at Mix 100 %: at most this much of what is under shows through
OPACITY_STEPS = 32      # flat strokes: stamp opacities are rounded to these
# ... but the swept body (a one-pixel profile, cheap to keep per opacity) at
# full 8-bit: in 32 steps, pressure easing off left a band at every step -
# the stroke's end a stack of plates.
BODY_STEPS = 255
ROUND = ':round'        # the built-in soft round tip, for Round in Blend / Smudge
BRISTLES = ':bristles'  # the built-in bristle tip (a row of hairs)
DUST = ':dust'          # the built-in airbrush / particle tip (a cloud of specks)
SPRAY = ':spray'        # the procedural spray: specks drawn as dots, sharp at any size
RAKE = ':rake'          # a rake: tines in a row, dragged into parallel lines (see rake_path)
SOFT = ':soft'          # a feathered round: solid middle, edge fading by Softness (see soft_path)
HAIRS = ':hairs'        # 🖌 a procedural hair brush: uneven hairs, frayed ends (see hairs_path)
SPRAY_SPREAD_MAX = 60      # (20 was too little to cover a large canvas)
PARTICLE_COLOURS = 16   # 'particles' jitter: colours a stamp's specks are spread over
PARTICLE_SETS = 16      # ... and the different colourings a stroke picks from
SPECKLED = 1000         # a particles tuple numbered from here: a flat stroke speckled
                        # all along (see FlatStroke._specks). Lower: painted before
                        # that, the body in the colours' average - they keep their look
SPECK_TILE = 512        # cells a side of that speckle, repeated
STREAKS = 32            # colour bands across the tip, with 'streaks' jitter
STREAK_CACHE_BYTES = 48 << 20


class Sampled(int):
    """A colour read from under the pen (0xRRGGBB) with *alpha*: how much
    paint was there, 0..1 - sparse specks on an empty layer are a little."""

    def __new__(cls, rgb, alpha=1.0):
        obj = super().__new__(cls, rgb)
        obj.alpha = alpha
        return obj


def _alpha(colour):
    """How much paint a sampled colour stands for (1 for a plain one)."""
    return getattr(colour, 'alpha', 1.0) if colour is not None else 0.0


SAMPLE_MIN_ALPHA = 3    # of 255: less than this under the pen counts as nothing


def picked_up(coverage):
    """How much paint a smudge takes from a spot *coverage* (0..1) covered.
    Sparse specks are opaque dots: a real smudge drags each into a streak of
    its own, far more visible than their average coverage - 4 % of specks
    would smear invisibly thin. The square root keeps solid paint at 1 and
    lifts thin coverage (4 % -> 20 %)."""
    return math.sqrt(min(1.0, max(0.0, coverage)))


class _ByteCache:
    """Images by key, oldest dropped once they add up to *limit* bytes."""

    def __init__(self, limit):
        from collections import OrderedDict
        self._items, self._bytes, self._limit = OrderedDict(), 0, limit

    def get(self, key):
        image = self._items.get(key)
        if image is not None:
            self._items.move_to_end(key)
        return image

    def put(self, key, image):
        self._items[key] = image
        self._bytes += image.sizeInBytes()
        while self._bytes > self._limit and len(self._items) > 1:
            _old, dropped = self._items.popitem(last=False)
            self._bytes -= dropped.sizeInBytes()


_streak_cache = _ByteCache(STREAK_CACHE_BYTES)


def _streaked(source, rgb):
    """*source* (a white mask) coloured in bands: *rgb* is (axis, c0, c1 ...),
    the bands running across the image's x (axis 0) or y (axis 1)."""
    import numpy as np
    axis, colours = rgb[0], rgb[1:]
    n = len(colours)
    c = np.array(colours, np.uint32)
    bgra = np.empty((n, 4), np.uint8)
    bgra[:, 0], bgra[:, 1], bgra[:, 2], bgra[:, 3] = c & 255, (c >> 8) & 255, (c >> 16) & 255, 255
    if axis < 0:
        # Particles: every speck its own colour - the colours scattered over
        # the tip in little cells (about a speck each), a fixed scatter per
        # map number (-1 - axis), so a stroke redraws the same.
        w, h = source.width(), source.height()
        cell = max(1, round(max(w, h) / 160))
        rng = np.random.default_rng((-1 - axis) * 7919 + w * 31 + h)
        idx = rng.integers(0, n, (-(-h // cell), -(-w // cell)))
        idx = np.repeat(np.repeat(idx, cell, 0), cell, 1)[:h, :w]
        pixels = np.ascontiguousarray(bgra[idx])
        image = QImage(pixels.data, w, h, w * 4, QImage.Format_ARGB32_Premultiplied).copy()
        p = QPainter(image)
        p.setCompositionMode(QPainter.CompositionMode_DestinationIn)
        p.drawImage(0, 0, source)
        p.end()
        return image
    sw, sh = (n, 1) if axis == 0 else (1, n)
    stripe = QImage(bgra.tobytes(), sw, sh, sw * 4, QImage.Format_ARGB32_Premultiplied).copy()
    image = QImage(source.size(), QImage.Format_ARGB32_Premultiplied)
    p = QPainter(image)
    p.setCompositionMode(QPainter.CompositionMode_Source)
    p.drawImage(QRect(0, 0, source.width(), source.height()), stripe)   # crisp bands
    p.setCompositionMode(QPainter.CompositionMode_DestinationIn)
    p.drawImage(0, 0, source)
    p.end()
    return image


def is_speckled(rgb):
    """Is *rgb* a particles colour tuple of a stroke speckled all along?"""
    return isinstance(rgb, tuple) and rgb[0] <= -1 - SPECKLED


_speck_tiles = {}


def _speck_tile(rgb):
    """The speckle for particles tuple *rgb*: a pixel a cell, each one of
    its colours - the same every time for the same tuple."""
    image = _speck_tiles.get(rgb)
    if image is None:
        colours = np.array(rgb[1:], np.uint32) | np.uint32(0xFF000000)
        rng = np.random.default_rng([-rgb[0], *rgb[1:]])
        cells = colours[rng.integers(0, len(colours), (SPECK_TILE, SPECK_TILE), dtype=np.uint8)]
        image = QImage(cells.data, SPECK_TILE, SPECK_TILE, SPECK_TILE * 4, QImage.Format_RGB32).copy()
        if len(_speck_tiles) >= 12:
            _speck_tiles.pop(next(iter(_speck_tiles)))
        _speck_tiles[rgb] = image
    return image


def particles_mean(rgb):
    """A particles colour tuple (axis < 0, c0, c1 ...) as one 0xRRGGBB: the
    average of its specks."""
    colours = rgb[1:]
    n = max(1, len(colours))
    r = sum((c >> 16) & 255 for c in colours) / n
    g = sum((c >> 8) & 255 for c in colours) / n
    b = sum(c & 255 for c in colours) / n
    return (round(r) << 16) | (round(g) << 8) | round(b)


def _mix(a, b, t):
    """0xRRGGBB colours *a* and *b* mixed, *t* of the way to *b*."""
    out = 0
    for shift in (16, 8, 0):
        ca, cb = (a >> shift) & 255, (b >> shift) & 255
        out |= round(ca + (cb - ca) * t) << shift
    return out


# -- colour jitter, as a painter means it --------------------------------
#
# Hue / Sat / Value ± work in OKLCH (Björn Ottosson's OKLab, as lightness,
# chroma and hue), a colour space made so equal steps look equal:
#   Value ±  lighter / darker - its place on a grey scale - at the same
#            intensity and hue (the same step looks the same on yellow and blue)
#   Sat ±    more intense, or greyer: towards the grey of the SAME value,
#            never lighter or darker (HSV's saturation went towards white)
#   Hue ±    round the wheel, value and intensity kept
# A colour pushed past what the screen can show loses intensity, not value.

def _srgb_to_linear(c):
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def _linear_to_srgb(c):
    c = np.clip(c, 0.0, 1.0)
    return np.where(c <= 0.0031308, c * 12.92, 1.055 * c ** (1 / 2.4) - 0.055)


def _rgb_to_oklab(rgb):
    """(..., 3) sRGB 0..1 -> (..., 3) OKLab (L 0..1)."""
    lin = _srgb_to_linear(rgb)
    lms = lin @ np.array([[0.4122214708, 0.2119034982, 0.0883024619],
                          [0.5363325363, 0.6806995451, 0.2817188376],
                          [0.0514459929, 0.1073969566, 0.6299787005]])
    lms = np.cbrt(lms)
    return lms @ np.array([[0.2104542553, 1.9779984951, 0.0259040371],
                           [0.7936177850, -2.4285922050, 0.7827717662],
                           [-0.0040720468, 0.4505937099, -0.8086757660]])


def _oklab_to_linear(lab):
    lms = lab @ np.array([[1.0, 1.0, 1.0],
                          [0.3963377774, -0.1055613458, -0.0894841775],
                          [0.2158037573, -0.0638541728, -1.2914855480]])
    lms = lms ** 3
    return lms @ np.array([[4.0767416621, -1.2684380046, -0.0041960863],
                           [-3.3077115913, 2.6097574011, -0.7034186147],
                           [0.2309699292, -0.3413193965, 1.7076147010]])


def jitter_rgb(rgb, hj, sj, vj, uh, us, uv):
    """sRGB colours *rgb* ((..., 3), 0..1) jittered by Hue / Sat / Value ±
    (*hj*, *sj*, *vj*: 0..1) with random numbers *uh*, *us*, *uv* in [-1, 1]
    (each the shape of rgb[..., 0]), in OKLCH - see above. sRGB back."""
    lab = _rgb_to_oklab(np.asarray(rgb, np.float64))
    L, a, b = lab[..., 0], lab[..., 1], lab[..., 2]
    C, h = np.hypot(a, b), np.arctan2(b, a)
    h = h + np.asarray(uh) * hj * math.pi            # 100 %: anywhere on the wheel
    C = C * np.clip(1.0 + np.asarray(us) * sj, 0.0, None)   # 100 %: grey .. twice as intense
    L = np.clip(L + np.asarray(uv) * vj, 0.0, 1.0)          # 100 %: anywhere black .. white
    ca, sa = np.cos(h), np.sin(h)
    # Past what sRGB holds: the most intensity that fits, value and hue kept
    # (a few halvings - the colours are made in batches).
    k_lo = np.zeros_like(C)
    k_hi = np.ones_like(C)

    def fits(k):
        lin = _oklab_to_linear(np.stack([L, C * k * ca, C * k * sa], -1))
        return np.all((lin >= -1e-4) & (lin <= 1 + 1e-4), -1)
    ok = fits(k_hi)
    if not ok.all():
        for _ in range(12):
            mid = (k_lo + k_hi) / 2
            good = fits(mid)
            k_lo = np.where(good, mid, k_lo)
            k_hi = np.where(good, k_hi, mid)
        k = np.where(ok, 1.0, k_lo)
    else:
        k = k_hi
    lin = _oklab_to_linear(np.stack([L, C * k * ca, C * k * sa], -1))
    return _linear_to_srgb(lin)


def gradient_lut(rgb0, rgb1, n=1024):
    """🌈 (n, 3) sRGB 0..1: from *rgb0* to *rgb1* (sRGB 0..1 triples) in
    OKLab steps - the middle looks halfway to the eye, with no grey dip (as
    red to green has mixed in sRGB) or dark band."""
    a = _rgb_to_oklab(np.array([rgb0], np.float64))[0]
    b = _rgb_to_oklab(np.array([rgb1], np.float64))[0]
    t = np.linspace(0.0, 1.0, n)[:, None]
    return _linear_to_srgb(_oklab_to_linear(a + (b - a) * t))


def gradient_bias(t, mid):
    """🌈 *t* (0..1, a float or numpy array) bent so the half-way mix falls
    at *mid* (0..1) instead of 0.5: t ** (ln ½ / ln mid). The ends stay put;
    mid 0.5 leaves it as it is."""
    mid = min(0.98, max(0.02, float(mid)))
    if abs(mid - 0.5) < 1e-6:
        return t
    return t ** (math.log(0.5) / math.log(mid))


def jitter_color(color, settings, rng=random):
    """*color* (a QColor) shifted at random by the Hue / Sat / Value ±
    settings, its alpha kept: one jittered colour, as a line or shape gets."""
    hj, sj, vj = (settings['hue_jitter'] / 100.0, settings['sat_jitter'] / 100.0,
                  settings['val_jitter'] / 100.0)
    out = QColor(color)
    if not (hj or sj or vj):
        return out
    uh, us, uv = rng.uniform(-1, 1), rng.uniform(-1, 1), rng.uniform(-1, 1)
    r, g, b = jitter_rgb([color.redF(), color.greenF(), color.blueF()], hj, sj, vj, uh, us, uv)
    out.setRgbF(float(r), float(g), float(b), color.alphaF())
    return out


def _cell_noise(ix, iy, seed, channel):
    """Per-cell random numbers in [-1, 1): a hash of the cell's (ix, iy), the
    mark's *seed* and *channel*, so a cell has the same colour however much
    of the mark is drawn, at any zoom."""
    with np.errstate(over='ignore'):
        x = (np.asarray(ix, np.int64).view(np.uint64) * np.uint64(0x9E3779B97F4A7C15)
             ^ np.asarray(iy, np.int64).view(np.uint64) * np.uint64(0xC2B2AE3D27D4EB4F)
             ^ np.uint64((int(seed) * 0x165667B19E3779F9 + channel * 0x27D4EB2F165667C5) & (2 ** 64 - 1)))
        x ^= x >> np.uint64(30)
        x *= np.uint64(0xBF58476D1CE4E5B9)
        x ^= x >> np.uint64(27)
        x *= np.uint64(0x94D049BB133111EB)
        x ^= x >> np.uint64(31)
    return (x >> np.uint64(11)).astype(np.float64) / float(2 ** 52) - 1.0


def jitter_cells(color, jitter, ix, iy):
    """Colours (uint32 0xFFRRGGBB array, the shape of *ix*) for the cells
    (ix, iy) of a line or shape jittered per cell: *color* shifted by the
    Hue / Sat / Value ± kept in *jitter* ({'h', 's', 'v', 'seed'})."""
    seed = jitter.get('seed', 0)
    ix, iy = np.asarray(ix), np.asarray(iy)
    table = _jitter_table(color.rgb() & 0xFFFFFF, jitter.get('h', 0), jitter.get('s', 0), jitter.get('v', 0))
    n = _JITTER_LEVELS

    def level(channel):
        q = ((_cell_noise(ix, iy, seed, channel) + 1.0) * (n / 2.0)).astype(np.int64)
        return np.minimum(q, n - 1)
    return table[(level(1) * n + level(2)) * n + level(3)]


_JITTER_LEVELS = 20         # steps per Hue / Sat / Value: 8000 colours a mark's cells pick from
_jitter_tables = {}


def _jitter_table(rgb, h, s, v):
    """Every colour a jittered line or shape can take: *rgb* (0xRRGGBB)
    jittered at _JITTER_LEVELS steps of each random number (uint32 ARGB, in
    the order jitter_cells indexes). Made once per colour and amounts - the
    OKLCH sums, gamut included, would cost per cell otherwise."""
    key = (rgb, h, s, v)
    table = _jitter_tables.get(key)
    if table is None:
        n = _JITTER_LEVELS
        steps = (np.arange(n) + 0.5) / n * 2.0 - 1.0
        uh, us, uv = (g.ravel() for g in np.meshgrid(steps, steps, steps, indexing='ij'))
        base = np.broadcast_to(np.array([((rgb >> 16) & 255) / 255.0, ((rgb >> 8) & 255) / 255.0,
                                         (rgb & 255) / 255.0]), (n ** 3, 3))
        out = np.round(jitter_rgb(base, h / 100.0, s / 100.0, v / 100.0, uh, us, uv) * 255).astype(np.uint32)
        table = (np.uint32(0xFF000000) | (out[:, 0] << np.uint32(16)) | (out[:, 1] << np.uint32(8))
                 | out[:, 2])
        if len(_jitter_tables) > 16:
            _jitter_tables.pop(next(iter(_jitter_tables)))
        _jitter_tables[key] = table
    return table


def pressure_size(pressure):
    """Stamp size factor for a pressure, the round brush's curve (20-150 %)."""
    return 0.2 + 1.3 * max(0.0, pressure) ** 0.8


def pressure_opacity(pressure):
    return 0.15 + 0.85 * max(0.0, pressure)


class BrushTexture:
    """A PNG as a white mask with scaled copies, and its tinted versions."""

    def __init__(self, path, image=None, name=None):
        if image is None:
            image = QImage(path)
        if image.isNull():
            raise ValueError(f"Not an image: {path}")
        self.path = path
        self.name = name or os.path.splitext(os.path.basename(path))[0]
        if max(image.width(), image.height()) > TEXTURE_MAX:
            image = image.scaled(TEXTURE_MAX, TEXTURE_MAX, Qt.KeepAspectRatio,
                                 Qt.SmoothTransformation)
        mask = self._to_mask(image)
        # Smooth scaling only blends neighbouring pixels, so a big texture
        # drawn tiny would sparkle; each stamp uses the copy nearest its size.
        self._levels = [mask]
        while max(mask.width(), mask.height()) > LEVEL_MIN:
            mask = mask.scaled(max(1, mask.width() // 2), max(1, mask.height() // 2),
                               Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
            self._levels.append(mask)
        self._tints = {}

    @staticmethod
    def _to_mask(image):
        """White, with alpha = the PNG's alpha x its brightness."""
        import numpy as np
        image = image.convertToFormat(QImage.Format_ARGB32)
        w, h = image.width(), image.height()
        a = np.frombuffer(image.constBits(), np.uint8).reshape(h, image.bytesPerLine())
        a = a[:, :w * 4].reshape(h, w, 4).astype(np.float32)    # B G R A
        lum = a[..., 2] * 0.299 + a[..., 1] * 0.587 + a[..., 0] * 0.114
        shown = a[..., 3] > 8
        if shown.any() and not (a[..., 3] > 250).all() and lum[shown].max() < 24:
            # Dark paint on transparent (many tip packs): the shape is in
            # the alpha alone - read as brightness it would be empty.
            lum = np.full_like(lum, 255.0)
        alpha = (a[..., 3] * lum / 255.0).clip(0, 255).astype(np.uint8)
        out = np.empty((h, w, 4), np.uint8)
        out[..., :3] = alpha[..., None]     # premultiplied white
        out[..., 3] = alpha
        # Copied in the same expression, while the bytes it reads still exist:
        # a QImage made on bytes does not keep them alive.
        data = out.tobytes()
        return QImage(data, w, h, w * 4, QImage.Format_ARGB32_Premultiplied).copy()

    @property
    def size(self):
        return self._levels[0].size()

    def level(self, longest):
        """Index of the smallest copy still at least *longest* px."""
        i = 0
        while (i + 1 < len(self._levels)
               and max(self._levels[i + 1].width(), self._levels[i + 1].height()) >= longest):
            i += 1
        return i

    def tinted(self, level, rgb):
        """The mask at *level* in colour *rgb* (0xRRGGBB), cached."""
        key = (level, rgb)
        image = self._tints.get(key)
        if image is None:
            if len(self._tints) > 512:
                self._tints.clear()
            mask = self._levels[level]
            image = QImage(mask.size(), QImage.Format_ARGB32_Premultiplied)
            image.fill(QColor((rgb >> 16) & 255, (rgb >> 8) & 255, rgb & 255))
            p = QPainter(image)
            p.setCompositionMode(QPainter.CompositionMode_DestinationIn)
            p.drawImage(0, 0, mask)
            p.end()
            self._tints[key] = image
        return image

    def grey(self, level, step):
        """The mask at *level* as opaque grey on black, at opacity
        step/OPACITY_STEPS: what a flat stroke takes the maximum of."""
        key = ('grey', level, step)
        image = self._tints.get(key)
        if image is None:
            image = self._variant('grey', self._levels[level], opacity=step / OPACITY_STEPS)
            self._tints[key] = image
        return image

    def profile(self, level, keep=None):
        """The mask at *level* squeezed to one pixel, keeping image axis *keep*
        (0: x, 1: y; None: the long side): what the tip leaves when dragged
        across the other one. Each line is as strong as the tip's ink along
        it, on average (so the dragged body matches the tip's own look at the
        stroke's start), a little weaker where the line is sparse - a grainy
        tip leaves grainy streaks instead of filling in, and the gaps of a
        row of bristles stay open."""
        mask = self._levels[level]
        w, h = mask.width(), mask.height()
        if keep is None:
            keep = 0 if w >= h else 1
        key = ('profile', level, keep)
        image = self._tints.get(key)
        if image is None:
            import numpy as np
            a = np.frombuffer(mask.constBits(), np.uint8).reshape(h, mask.bytesPerLine())
            a = a[:, :w * 4].reshape(h, w, 4)[..., 3].astype(np.float32)
            axis = 0 if keep == 0 else 1
            lit = a > 8
            count = lit.sum(axis=axis).astype(np.float32)
            ink = np.where(lit, a, 0).sum(axis=axis) / np.maximum(count, 1)
            fill = count / max(1.0, count.max())
            line = ink * fill ** 0.25
            # A dragged tip lays paint down solid: the strongest streak is as
            # strong as the tip's brightest pixel (full ink at 100 % opacity),
            # the weaker ones lifted towards it, keeping their order - grain
            # without the stroke reading as see-through. (Left at the plain
            # average, a soft tip's body came out at 40-60 % even at 100 %.)
            top = line.max()
            if top > 0:
                line = (line / top) ** 0.5 * a.max()
            v = line.clip(0, 255).astype(np.uint8)
            out = np.repeat(v[:, None], 4, axis=1)      # premultiplied white
            pw, ph = (w, 1) if keep == 0 else (1, h)
            image = QImage(np.ascontiguousarray(out).tobytes(), pw, ph, pw * 4,
                           QImage.Format_ARGB32_Premultiplied).copy()
            self._tints[key] = image
        return image

    def _variant(self, kind, source, colour=None, opacity=1.0):
        """*source* as opaque grey on black at *opacity*, or tinted *colour*."""
        if kind == 'grey':
            image = QImage(source.size(), QImage.Format_RGB32)
            image.fill(Qt.black)
            p = QPainter(image)
            p.setOpacity(opacity)
            p.drawImage(0, 0, source)
            p.end()
            return image
        tint = QImage(source.size(), QImage.Format_ARGB32_Premultiplied)
        tint.fill(colour)
        p = QPainter(tint)
        p.setCompositionMode(QPainter.CompositionMode_DestinationIn)
        p.drawImage(0, 0, source)
        p.end()
        return tint

    def front(self, level, keep, sign, back=False):
        """The tip at *level* with, on each line along axis *keep*, the stretch
        from its leading ink to the leading edge (the side *sign* of the other
        axis) filled at that line's swept strength: the stroke's first stamp
        as the drag starts, so the swept body grows out of the tip's own front
        edge - ragged like the texture - with no gap and no box over it.
        *back*: the stroke's end instead - each line filled from the trailing
        edge up to its leading ink, so the body stops in the tip's own ragged
        front at the last stamp."""
        key = ('front', level, keep, sign, back)
        image = self._tints.get(key)
        if image is None:
            import numpy as np
            mask = self._levels[level]
            w, h = mask.width(), mask.height()
            a = np.frombuffer(mask.constBits(), np.uint8).reshape(h, mask.bytesPerLine())
            a = a[:, :w * 4].reshape(h, w, 4)[..., 3]
            prof = self.profile(level, keep)
            n = w if keep == 0 else h
            v = np.frombuffer(prof.constBits(), np.uint8)[::4][:n]
            work = a if keep == 0 else a.T                  # (squeezed, lines)
            lit = work > 8
            rows = np.arange(work.shape[0])[:, None]
            if sign > 0:
                first = work.shape[0] - 1 - np.argmax(lit[::-1], axis=0)
                fill = (rows <= first[None, :]) if back else (rows >= first[None, :])
            else:
                first = np.argmax(lit, axis=0)
                fill = (rows >= first[None, :]) if back else (rows <= first[None, :])
            fill &= lit.any(axis=0)[None, :]
            out = np.maximum(work, np.where(fill, v[None, :], 0)).astype(np.uint8)
            if keep != 0:
                out = out.T
            out = np.ascontiguousarray(out)
            px = np.repeat(out[..., None], 4, axis=2)       # premultiplied white
            image = QImage(px.tobytes(), w, h, w * 4, QImage.Format_ARGB32_Premultiplied).copy()
            self._tints[key] = image
        return image

    def front_variant(self, level, keep, sign, step=None, rgb=None, back=False):
        """front() as grey at opacity *step*, or coloured *rgb*."""
        source = self.front(level, keep, sign, back)
        if rgb is None:
            key = ('front-grey', level, keep, sign, step, back)
            image = self._tints.get(key)
            if image is None:
                image = self._variant('grey', source, opacity=step / OPACITY_STEPS)
                self._tints[key] = image
            return image
        if isinstance(rgb, int):
            key = ('front-tint', level, keep, sign, rgb, back)
            image = self._tints.get(key)
            if image is None:
                image = self._variant('tint', source,
                                      QColor((rgb >> 16) & 255, (rgb >> 8) & 255, rgb & 255))
                self._tints[key] = image
            return image
        if rgb[0] >= 0:
            rgb = (keep,) + tuple(rgb[1:])  # banded along the dragged side, as the body
        # (particles: speckled, as the tip - see frame_variant)
        key = (id(self), 'front', level, keep, sign, rgb, back)
        image = _streak_cache.get(key)
        if image is None:
            image = _streaked(source, rgb)
            _streak_cache.put(key, image)
        return image

    def swept(self, level, step=None, rgb=None, keep=None):
        """The profile (along *keep*) as grey at opacity *step*, or tinted *rgb*."""
        key = ('swept', level, step, rgb, keep)
        image = self._tints.get(key)
        if image is None:
            if rgb is None:
                image = self._variant('grey', self.profile(level, keep), opacity=step / OPACITY_STEPS)
            else:
                image = self._variant('tint', self.profile(level, keep),
                                      QColor((rgb >> 16) & 255, (rgb >> 8) & 255, rgb & 255))
            self._tints[key] = image
        return image

    FRAME_STEP = 3      # degrees: the tip's turn to the motion, in steps (cached)

    def motion_frame(self, level, turn):
        """The tip at *level* turned by *turn* degrees, so the stroke runs
        along +x: dict of its size and three masks (white, premultiplied),
        cached per FRAME_STEP of turn -
        'profile': 1 x h, each row the tip's ink along the motion (as in
            profile(): the line's average ink, the densest made full, weaker
            ones lifted - grain without see-through);
        'front': the turned tip with each row filled on from its leading ink
            to the frame's leading edge - the stroke's start as it drags;
        'back': each row filled from the trailing edge up to its leading
            ink - the stroke's end, the tip's own ragged front;
        'whole': the turned tip with each row filled between its trailing
            and leading ink - the tip's ragged outline, its holes closed
            (the tail under the pen). The raw tip there let a speckled tip's
            holes show the canvas: a dark speckled blob where the stroke
            started and ended, and, its streaks laid along the tip's own
            axis, bands across the stroke when pen tilt turns the tip.
        Working in the motion's frame, the stroke's width is the tip's real
        footprint across the motion whatever its turn, so it changes
        smoothly round a curve instead of flipping between the tip's sides."""
        turn = int(round(turn / self.FRAME_STEP) * self.FRAME_STEP) % 360
        key = ('mframe', level, turn)
        frame = self._tints.get(key)
        if frame is not None:
            return frame
        import numpy as np
        tip = self._levels[level]
        w, h = tip.width(), tip.height()
        a = math.radians(turn)
        fw = int(math.ceil(abs(w * math.cos(a)) + abs(h * math.sin(a)))) + 2
        fh = int(math.ceil(abs(w * math.sin(a)) + abs(h * math.cos(a)))) + 2
        turned = QImage(fw, fh, QImage.Format_ARGB32_Premultiplied)
        turned.fill(Qt.transparent)
        p = QPainter(turned)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        p.translate(fw / 2.0, fh / 2.0)
        p.rotate(turn)
        p.translate(-w / 2.0, -h / 2.0)
        p.drawImage(0, 0, tip)
        p.end()
        arr = np.frombuffer(turned.constBits(), np.uint8).reshape(fh, turned.bytesPerLine())
        alpha = arr[:, :fw * 4].reshape(fh, fw, 4)[..., 3].astype(np.float32)
        lit = alpha > 8
        count = lit.sum(axis=1).astype(np.float32)
        ink = np.where(lit, alpha, 0).sum(axis=1) / np.maximum(count, 1)
        fill = count / max(1.0, count.max())
        line = ink * fill ** 0.25
        top = line.max()
        if top > 0:
            line = (line / top) ** 0.5 * alpha.max()
        v = line.clip(0, 255).astype(np.float32)
        has = lit.any(axis=1)
        cols = np.arange(fw)[None, :]
        lead = np.where(has, fw - 1 - np.argmax(lit[:, ::-1], axis=1), -1)[:, None]
        trail = np.where(has, np.argmax(lit, axis=1), fw)[:, None]

        def image(values2d):
            out = np.ascontiguousarray(values2d.clip(0, 255).astype(np.uint8))
            px = np.repeat(out[..., None], 4, axis=-1)
            hh, ww = out.shape
            return QImage(np.ascontiguousarray(px).tobytes(), ww, hh, ww * 4,
                          QImage.Format_ARGB32_Premultiplied).copy()
        rows = np.where(has, v, 0)[:, None]
        # (front: from the trailing ink on, so the first stamp's holes close
        # once the drag starts - see 'whole')
        front = np.maximum(alpha, np.where((cols >= trail) & has[:, None], rows, 0))
        back = np.maximum(alpha, np.where((cols <= lead) & has[:, None], rows, 0))
        whole = np.maximum(alpha, np.where((cols >= trail) & (cols <= lead) & has[:, None], rows, 0))
        frame = {'w': fw, 'h': fh, 'profile': image(v[:, None]),
                 'front': image(front), 'back': image(back), 'whole': image(whole)}
        self._tints[key] = frame
        return frame

    def frame_variant(self, level, turn, part, step=None, rgb=None, steps=OPACITY_STEPS):
        """A motion_frame() mask as grey at opacity *step* / *steps*, or
        coloured *rgb* (one colour, or streaks - banded across the motion)."""
        frame = self.motion_frame(level, turn)
        turn = int(round(turn / self.FRAME_STEP) * self.FRAME_STEP) % 360
        source = frame[part]
        if rgb is None:
            key = ('mf-grey', level, turn, part, step, steps)
            image = self._tints.get(key)
            if image is None:
                if len(self._tints) > 8192:     # (profiles at 255 opacities: kept in bounds)
                    self._tints.clear()
                image = self._variant('grey', source, opacity=step / steps)
                self._tints[key] = image
            return image
        if isinstance(rgb, int):
            key = ('mf-tint', level, turn, part, rgb)
            image = self._tints.get(key)
            if image is None:
                image = self._variant('tint', source,
                                      QColor((rgb >> 16) & 255, (rgb >> 8) & 255, rgb & 255))
                self._tints[key] = image
            return image
        if rgb[0] < 0 and part == 'profile':
            # particles in the body: their average - a one-pixel profile
            # cannot hold specks, and banded each short piece where the
            # stroke narrows (the pen lifting) became a striped block
            return self.frame_variant(level, turn, part, rgb=particles_mean(rgb))
        if not (rgb[0] < 0 and part != 'profile'):
            rgb = (1,) + tuple(rgb[1:])     # bands run along the motion
        # (particles keep their speckle in the front and the tail - the tip's
        # own shape, as every stamp has it: banded, each stroke ended in a
        # striped block. Only the one-pixel profile, stretched along the
        # stroke, needs bands.)
        key = (id(self), 'mf', level, turn, part, rgb)
        image = _streak_cache.get(key)
        if image is None:
            image = _streaked(source, rgb)
            _streak_cache.put(key, image)
        return image

    def coloured(self, level, rgb, keep=None):
        """The stamp in *rgb* - one 0xRRGGBB, or a streaked tuple (see
        StampTrail) - or, with *keep* (an axis), its swept profile."""
        if isinstance(rgb, int):
            return self.swept(level, rgb=rgb, keep=keep) if keep is not None else self.tinted(level, rgb)
        if keep is not None and rgb[0] < 0:
            # particles over a swept profile: their average (see frame_variant)
            return self.swept(level, rgb=particles_mean(rgb), keep=keep)
        if keep is not None:
            # The bands run along the side being dragged. Laid along the other
            # one, they were squeezed into the one-pixel profile with it - a
            # single colour for the whole stroke.
            rgb = (keep,) + tuple(rgb[1:])
        key = (id(self), level, keep, rgb)
        image = _streak_cache.get(key)
        if image is None:
            image = _streaked(self.profile(level, keep) if keep is not None else self._levels[level], rgb)
            _streak_cache.put(key, image)
        return image

    def streak_axis(self, angle_offset=0.0):
        """(axis, share): the image axis the streaks are laid across - the
        side that lies across the stroke under the turn *angle_offset*
        (degrees) the stamps get on top of the stroke direction, so the
        streaks run along it - and its length as a share of the longest side."""
        w, h = self.size.width(), self.size.height()
        a = math.radians(angle_offset)
        axis = 1 if abs(math.cos(a)) >= abs(math.sin(a)) else 0
        return axis, (w if axis == 0 else h) / max(w, h)

    def outline(self, size, turn, dpr=1.0):
        """The tip's outline as a cursor image: the edge of where it paints,
        *size* px on its longest side, under *turn* (a linear QTransform:
        rotation, maybe a mirror). A light line with a dark rim, so it shows on
        any picture. The stamp's centre is the image's centre. None if the
        tip is too faint or small to outline."""
        if getattr(self, 'bristle_engine', False):
            return self._bristle_outline(size, turn, dpr)
        import numpy as np
        mask = self._levels[self.level(size * dpr)]
        w, h = mask.width(), mask.height()
        k = size * dpr / max(w, h)
        m = QTransform.fromTranslate(-w / 2.0, -h / 2.0) * QTransform.fromScale(k, k) * turn
        reach = m.mapRect(QRectF(0, 0, w, h))
        half_w = math.ceil(max(-reach.left(), reach.right())) + 3
        half_h = math.ceil(max(-reach.top(), reach.bottom())) + 3
        W, H = 2 * half_w, 2 * half_h
        image = QImage(W, H, QImage.Format_ARGB32_Premultiplied)
        image.fill(Qt.transparent)
        p = QPainter(image)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        p.setTransform(m * QTransform.fromTranslate(half_w, half_h))
        p.drawImage(0, 0, mask)
        p.end()
        # A silhouette, not every speck and hole of a grainy tip: blur by
        # scaling down and back up, so near bits merge before the threshold.
        f = max(2, int(size * dpr / 24))

        def grow(b):
            g = b.copy()
            g[1:] |= b[:-1]
            g[:-1] |= b[1:]
            g[:, 1:] |= b[:, :-1]
            g[:, :-1] |= b[:, 1:]
            return g

        def alpha(img):
            # A copy, never a view: *img* may be a temporary (a scaled image
            # made just for this call), freed as soon as this returns - a
            # view into its pixels then read freed memory and crashed the app
            # (in numpy) when the cursor was rebuilt after a stroke.
            w_, h_ = img.width(), img.height()
            a = np.frombuffer(img.constBits(), np.uint8).reshape(h_, img.bytesPerLine())
            return a[:, :w_ * 4].reshape(h_, w_, 4)[..., 3].copy()

        small = image.scaled(max(1, W // f), max(1, H // f), Qt.IgnoreAspectRatio,
                             Qt.SmoothTransformation)
        level = alpha(small).copy()
        # Holes: what is not ink and cannot be reached from the border. Found
        # on the small copy (a few dozen steps) and filled there, so scaling
        # it back up smoothly cannot open them again.
        solid = level > 40
        outside = np.zeros_like(solid)
        outside[0, :] = outside[-1, :] = True
        outside[:, 0] = outside[:, -1] = True
        outside &= ~solid
        while True:
            more = grow(outside) & ~solid
            if (more == outside).all():
                break
            outside = more
        level[~solid & ~outside] = 255
        sh, sw = level.shape
        filled = np.repeat(level[..., None], 4, axis=2)
        small = QImage(filled.tobytes(), sw, sh, sw * 4, QImage.Format_ARGB32_Premultiplied).copy()
        inside = alpha(small.scaled(W, H, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)) > 40
        inside[0, :] = inside[-1, :] = inside[:, 0] = inside[:, -1] = False     # padding

        edge = inside & grow(~inside)           # inside, next to outside
        if not edge.any():
            return None
        rim = grow(edge) & ~edge
        out = np.zeros((H, W, 4), np.uint8)
        out[rim] = (0, 0, 0, 170)               # premultiplied BGRA
        out[edge] = (255, 255, 255, 255)
        cursor = QImage(out.tobytes(), W, H, W * 4, QImage.Format_ARGB32_Premultiplied).copy()
        cursor.setDevicePixelRatio(dpr)
        return cursor

    def _bristle_outline(self, size, turn, dpr=1.0):
        """🖌 The bristles as the cursor (as HeavyPaint shows its bristle
        brushes): every hair a short stick where it sits in the bundle - the
        same spread, Flatness, Bristle shape and random turn the strokes are
        laid with (BristleStroke._make), at full pressure - turned with the
        brush. A light stick over a dark one, so it shows on any picture."""
        hairs = getattr(self, '_cursor_hairs', None)
        if hairs is None:
            hairs = self._cursor_hairs = BristleStroke._make(getattr(self, 'hair_params', None) or {})
        k = size * dpr
        u, v = hairs['u'], hairs['v']
        # a stick as long as the hair's flat tip, never shorter than a few
        # px (a hair under a pixel wide would not show at all)
        half = np.maximum(2.0 * dpr, 0.5 * hairs['thick'] * hairs['long'] * k)
        half = np.minimum(half, 0.12 * k + 2.0 * dpr)
        m = QTransform(turn.m11(), turn.m12(), turn.m21(), turn.m22(), 0.0, 0.0)
        cx = m.m11() * v * k + m.m21() * u * k              # in the tip's frame: along = x, across = y
        cy = m.m12() * v * k + m.m22() * u * k
        dx = m.m11() * np.cos(hairs['axis']) + m.m21() * np.sin(hairs['axis'])
        dy = m.m12() * np.cos(hairs['axis']) + m.m22() * np.sin(hairs['axis'])
        dl = np.hypot(dx, dy)
        dl[dl < 1e-9] = 1.0
        dx, dy = dx / dl * half, dy / dl * half
        reach = float(np.max(np.hypot(cx, cy) + half)) + 4.0 * dpr
        W = 2 * int(math.ceil(reach))
        image = QImage(W, W, QImage.Format_ARGB32_Premultiplied)
        image.fill(Qt.transparent)
        p = QPainter(image)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.translate(W / 2.0, W / 2.0)
        lines = [QLineF(float(x - ex), float(y - ey), float(x + ex), float(y + ey))
                 for x, y, ex, ey in zip(cx, cy, dx, dy)]
        p.setPen(QPen(QColor(0, 0, 0, 170), 3.0 * dpr, Qt.SolidLine, Qt.RoundCap))
        p.drawLines(lines)
        p.setPen(QPen(QColor(255, 255, 255, 235), 1.0 * dpr, Qt.SolidLine, Qt.RoundCap))
        p.drawLines(lines)
        p.end()
        image.setDevicePixelRatio(dpr)
        return image

    def thumbnail(self, edge, color=QColor(230, 230, 230)):
        """The texture fitted into an *edge* px square, for the brush panel."""
        image = self.tinted(0, color.rgb() & 0xFFFFFF).scaled(
            edge, edge, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        if self.path == DUST or getattr(self, 'procedural', False):
            # Fine specks scaled down to an icon came out a faint grey haze,
            # easy to miss: laid a few times over, a bright spray.
            bright = QImage(image.size(), QImage.Format_ARGB32_Premultiplied)
            bright.fill(Qt.transparent)
            p = QPainter(bright)
            for _ in range(4):
                p.drawImage(0, 0, image)
            p.end()
            image = bright
        return image


_textures = {}


def _round_image(edge=128):
    """A soft-edged white disc: the Round tip as a stamp."""
    from PySide6.QtGui import QRadialGradient
    image = QImage(edge, edge, QImage.Format_ARGB32)
    image.fill(Qt.transparent)
    g = QRadialGradient(edge / 2.0, edge / 2.0, edge / 2.0)
    g.setColorAt(0.0, QColor(255, 255, 255, 255))
    g.setColorAt(0.8, QColor(255, 255, 255, 255))
    g.setColorAt(1.0, QColor(255, 255, 255, 0))
    p = QPainter(image)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setPen(Qt.NoPen)
    p.setBrush(g)
    p.drawEllipse(0, 0, edge, edge)
    p.end()
    return image


def _bristle_image(width=48, height=256, hairs=110, seed=7):
    """A brush of hairs, upright (long along y, so it lies across a stroke the
    stamps follow): soft dots of mixed size and strength, thick in the middle
    and thinning out to the sides - swept along, a dense body of paint with
    streaks in it and a ragged edge."""
    from PySide6.QtGui import QRadialGradient
    rng = random.Random(seed)
    image = QImage(width, height, QImage.Format_ARGB32)
    image.fill(Qt.transparent)
    p = QPainter(image)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setPen(Qt.NoPen)
    for _ in range(hairs):
        y = height / 2 + max(-0.46, min(0.46, rng.gauss(0, 0.2))) * height
        r = rng.uniform(3.0, 9.0)
        x = width / 2 + rng.uniform(-0.2, 0.2) * width
        g = QRadialGradient(x, y, r)
        g.setColorAt(0.0, QColor(255, 255, 255, rng.randint(120, 255)))
        g.setColorAt(0.6, QColor(255, 255, 255, 140))
        g.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.setBrush(g)
        p.drawEllipse(QRectF(x - r, y - r, 2 * r, 2 * r))
    p.end()
    return image


def _dust_image(edge=256, specks=1200, seed=11):
    """An airbrush / particle tip: a cloud of small hard specks, thick in the
    middle and thinning out to a soft edge - stamped close with a random turn
    (and Scatter), a spray of dust; with 'particles' jitter, each speck its
    own colour."""
    rng = random.Random(seed)
    image = QImage(edge, edge, QImage.Format_ARGB32)
    image.fill(Qt.transparent)
    p = QPainter(image)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setPen(Qt.NoPen)
    c = edge / 2.0
    for _ in range(specks):
        # Denser towards the middle: a bell-shaped spray, cut at the rim.
        r = min(0.97, abs(rng.gauss(0, 0.5))) * c
        a = rng.uniform(0, 2 * math.pi)
        x, y = c + math.cos(a) * r, c + math.sin(a) * r
        size = rng.uniform(0.8, 2.6) * (1.0 - 0.35 * r / c)
        p.setBrush(QColor(255, 255, 255, rng.randint(150, 255)))
        p.drawEllipse(QRectF(x - size, y - size, 2 * size, 2 * size))
    p.end()
    return image


def _spray_image(edge=256):
    """The Spray tip's icon - and its stand-in where a stroke is laid as a
    texture (Blend, History): fewer, bigger specks than Dust."""
    return _dust_image(edge, specks=260, seed=5)


def _rake_geometry(tines, tine):
    """(height of the image, diameter of a tine) for a rake image."""
    height = max(256, min(1024, tines * 40))
    pitch = height / tines
    return height, max(3.0, pitch * tine / 100.0)


def _rake_image(tines=8, tine=45, wobble=25, seed=3):
    """A rake: *tines* soft round tips in a row, upright (the row lies across
    the stroke, as the stamps turn with it), each *tine* % of the gap
    between them thick. *wobble* %: they differ a little in size, place and
    strength, as worn tines do - 0 makes ruled lines. Dragged flat it lays
    parallel lines; in Smudge each tine combs its own colour along."""
    rng = random.Random(seed)
    height, d = _rake_geometry(tines, tine)
    pitch = height / tines
    width = max(4, int(math.ceil(d * 1.3)))
    image = QImage(width, height, QImage.Format_ARGB32)
    image.fill(Qt.transparent)
    from PySide6.QtGui import QRadialGradient
    p = QPainter(image)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setPen(Qt.NoPen)
    w = wobble / 100.0
    for i in range(tines):
        y = (i + 0.5) * pitch + rng.uniform(-0.5, 0.5) * w * max(0.0, pitch - d)
        r = d / 2.0 * (1.0 - rng.uniform(0.0, 0.45) * w)
        a = int(255 * (1.0 - rng.uniform(0.0, 0.4) * w))
        g = QRadialGradient(width / 2.0, y, r)
        g.setColorAt(0.0, QColor(255, 255, 255, a))
        g.setColorAt(0.7, QColor(255, 255, 255, a))
        g.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.setBrush(g)
        p.drawEllipse(QRectF(width / 2.0 - r, y - r, 2 * r, 2 * r))
    p.end()
    return image


def _hairs_image(hairs=48, vary=50, ragged=40, soft=30, seed=17):
    """🖌 A procedural hair brush (as HeavyPaint's bristle brushes): *hairs*
    hairs side by side across the stroke (the image is upright: across = y,
    along the stroke = x). Each hair a short streak of its own thickness and
    strength (*vary* %: from a tidy row to uneven, some hairs thin, faint or
    missing - the gaps that run along a flat stroke as streaks), its own
    length and start (*ragged* %: a frayed beginning and end) and edge
    (*soft* %). The outer hairs thinner and weaker, a few strays past them.
    Laid with numpy - a few ms - and sharp at any size, as Rake."""
    rng = np.random.default_rng(seed)
    H, W = 512, 288                     # across, along (long: room for frayed ends)
    hairs = max(4, min(400, int(hairs)))
    v, rg, sf = vary / 100.0, ragged / 100.0, soft / 100.0
    pitch = H * 0.92 / hairs            # the row fills ~92 % of the width, strays outside
    ys = np.arange(H, dtype=np.float32)[:, None]
    xs = np.arange(W, dtype=np.float32)[None, :]
    alpha = np.zeros((H, W), np.float32)
    centres = (H * 0.04 + (np.arange(hairs) + 0.5) * pitch
               + rng.uniform(-0.35, 0.35, hairs) * pitch * v)
    # a few strays past the edges
    strays = int(round(hairs * 0.08 * (0.3 + v)))
    if strays:
        side = rng.choice([-1.0, 1.0], strays)
        out = rng.uniform(0.0, H * 0.05, strays)
        centres = np.concatenate([centres, np.where(side < 0, H * 0.04 - out, H * 0.96 + out)])
    for i, cy in enumerate(centres):
        edge = abs(cy - H / 2) / (H / 2)                   # 0 middle .. 1 rim
        stray = i >= hairs
        thin = 1.0 - 0.55 * max(0.0, edge - 0.6) / 0.4 if not stray else 0.45
        width = pitch * (0.55 + 0.6 * rng.random() * (0.3 + v)) * thin
        strength = 1.0 - v * 0.75 * rng.random() ** 1.6
        if stray or edge > 0.8:
            strength *= rng.uniform(0.35, 0.8)
        if rng.random() < 0.12 * v:
            strength *= 0.25                                # a hair that hardly holds paint
        # along: its own start and length (frayed ends)
        # (skewed: most hairs near the full length, a few much shorter or
        # starting late - the long frayed hairs at a stroke's start and end)
        start = (rng.random() ** 1.8) * 0.65 * rg * W
        length = W * (1.0 - (rng.random() ** 1.8) * 0.7 * rg) - start
        x0, x1 = start, start + max(W * 0.18, length)
        half = max(0.6, width / 2.0)
        # only the rows it covers (the whole image per hair: ~140 ms a tip)
        r0, r1 = max(0, int(cy - half) - 1), min(H, int(cy + half) + 2)
        if r1 <= r0:
            continue
        # across: a rounded profile, edge softness by *soft*
        d = np.abs(ys[r0:r1] - cy) / half
        across = np.clip((1.0 - d) / max(0.08, sf * 0.9 + 0.08), 0.0, 1.0)
        across = across * across * (3 - 2 * across)
        fade = max(1.5, W * 0.06)
        along = np.clip(np.minimum(xs - x0, x1 - xs) / fade, 0.0, 1.0)
        np.maximum(alpha[r0:r1], strength * across * along, out=alpha[r0:r1])
    a8 = (np.clip(alpha, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)
    bgra = np.empty((H, W, 4), np.uint8)
    bgra[..., :3] = 255
    bgra[..., 3] = a8
    return QImage(bgra.data, W, H, W * 4, QImage.Format_ARGB32).copy()


def hairs_path(settings):
    """The Hair brush with its settings, as a path a stroke keeps (the same
    hairs at any zoom): ':hairs?n=<hairs>&v=<vary %>&r=<ragged %>&s=<soft %>&k=<seed>
    &w=<hair width ‰>&e=2'. e=2: laid as bristles (BristleStroke); a path
    without it is an older stroke, laid as the tip dragged, as it was made."""
    n = max(4, min(400, int(settings.get('hair_count', 48))))
    v = max(0, min(100, int(settings.get('hair_vary', 50))))
    r = max(0, min(100, int(settings.get('hair_ragged', 40))))
    s = max(0, min(100, int(settings.get('hair_soft', 30))))
    k = max(0, min(999, int(settings.get('hair_seed', 17))))
    w = max(1, min(200, int(settings.get('hair_width', 8))))
    f = max(0, min(100, int(settings.get('hair_flat', 35))))
    h = max(0, min(100, int(settings.get('hair_shape', 40))))
    x = max(0, min(100, int(settings.get('hair_wet', 0))))
    return f"{HAIRS}?n={n}&v={v}&r={r}&s={s}&k={k}&w={w}&f={f}&h={h}&x={x}&e=2"


def _hairs_tip(path):
    """A Hair brush for *path* (see hairs_path, or HAIRS for the defaults)."""
    values = {'n': DEFAULT_SETTINGS['hair_count'], 'v': DEFAULT_SETTINGS['hair_vary'],
              'r': DEFAULT_SETTINGS['hair_ragged'], 's': DEFAULT_SETTINGS['hair_soft'],
              'k': DEFAULT_SETTINGS['hair_seed']}
    if '?' in path:
        for part in path.split('?', 1)[1].split('&'):
            try:
                key, value = part.split('=', 1)
                values[key] = int(value)
            except ValueError:
                pass
    tip = BrushTexture(path, _hairs_image(values['n'], values['v'], values['r'], values['s'],
                                          seed=values['k']), "Hair brush (procedural)")
    tip.hair_params = dict(values)
    tip.bristle_engine = int(values.get('e', 0)) >= 2      # see hairs_path
    return tip


def rake_path(settings):
    """The Rake tip with its settings, as a path a stroke keeps (it lays the
    same tines again at any zoom): ':rake?t=<tines>&w=<tine %>&j=<wobble %>'."""
    tines = max(2, min(40, int(settings.get('rake_tines', 8))))
    tine = max(5, min(100, int(settings.get('rake_tine', 45))))
    wobble = max(0, min(100, int(settings.get('rake_wobble', 25))))
    return f"{RAKE}?t={tines}&w={tine}&j={wobble}"


def _rake_tip(path):
    """A Rake tip for *path* (see rake_path, or RAKE for the defaults)."""
    values = {'t': DEFAULT_SETTINGS['rake_tines'], 'w': DEFAULT_SETTINGS['rake_tine'],
              'j': DEFAULT_SETTINGS['rake_wobble']}
    if '?' in path:
        for part in path.split('?', 1)[1].split('&'):
            try:
                key, value = part.split('=', 1)
                values[key] = int(value)
            except ValueError:
                pass
    tines = max(2, min(40, values['t']))
    tine = max(5, min(100, values['w']))
    tip = BrushTexture(path, _rake_image(tines, tine, max(0, min(100, values['j']))), "Rake")
    height, d = _rake_geometry(tines, tine)
    tip.rake_tine = d / height          # a tine's width as a share of the stamp (spacing follows it)
    return tip


def _soft_image(softness=60, edge=256):
    """A feathered round: solid out to (1 - *softness* %) of the radius,
    then fading smoothly (a cosine, so neither the core nor the rim shows a
    step) to nothing at the edge. 0 %: a hard disc; 100 %: all feather."""
    soft = max(0.0, min(1.0, softness / 100.0))
    c = (edge - 1) / 2.0
    yy, xx = np.mgrid[0:edge, 0:edge].astype(np.float32)
    r = np.sqrt((xx - c) ** 2 + (yy - c) ** 2) / (edge / 2.0)
    core = 1.0 - soft
    if soft <= 0.0:
        # A hard disc, with a one-pixel antialiased rim.
        alpha = np.clip((1.0 - r) * (edge / 2.0), 0.0, 1.0)
    else:
        t = np.clip((r - core) / soft, 0.0, 1.0)
        alpha = 0.5 * (1.0 + np.cos(np.pi * t))
        alpha[r >= 1.0] = 0.0
    a8 = (alpha * 255.0 + 0.5).astype(np.uint8)
    bgra = np.empty((edge, edge, 4), np.uint8)
    bgra[..., 0] = bgra[..., 1] = bgra[..., 2] = 255
    bgra[..., 3] = a8
    return QImage(bgra.data, edge, edge, edge * 4, QImage.Format_ARGB32).copy()


def soft_path(settings):
    """The Soft tip with its Softness, as a path a stroke keeps (so it lays
    the same feather again later): ':soft?s=<softness %>'."""
    soft = max(0, min(100, int(settings.get('soft_softness', DEFAULT_SETTINGS['soft_softness']))))
    return f"{SOFT}?s={soft}"


def _soft_tip(path):
    """A Soft tip for *path* (see soft_path, or SOFT for the default)."""
    soft = DEFAULT_SETTINGS['soft_softness']
    if '?' in path:
        try:
            soft = int(path.split('?', 1)[1].split('=', 1)[1])
        except (IndexError, ValueError):
            pass
    soft = max(0, min(100, soft))
    tip = BrushTexture(path, _soft_image(soft), f"Soft round ({soft} % feather)")
    tip.stamp_max = True        # flat strokes: each stamp laid as is (see FlatStroke.add)
    return tip


_BUILT_IN = {ROUND: (_round_image, "Round"), BRISTLES: (_bristle_image, "Bristles"),
             DUST: (_dust_image, "Dust (airbrush)"), SPRAY: (_spray_image, "Spray (procedural)")}


def spray_path(settings, size_px):
    """The Spray tip with its settings, as a path a stroke keeps (so it lays
    the same specks again at any zoom): ':spray?n=<specks>&s=<speck size as
    a share of the cloud's width>'."""
    spread = max(1, min(SPRAY_SPREAD_MAX, int(settings.get('spray_spread', 1))))
    width = max(1.0, float(size_px) * spread)
    n = max(1, int(settings.get('spray_density', 150)))
    return f"{SPRAY}?n={n}&s={float(settings.get('spray_speck', 2)) / width:.6g}"


def _spray_tip(path):
    """A Spray tip for *path* (see spray_path): the built-in one's images
    with its own speck count and size."""
    import copy
    base = load_texture(SPRAY)
    tip = copy.copy(base)
    tip.path = path
    try:
        for part in path.split('?', 1)[1].split('&'):
            key, value = part.split('=', 1)
            if key == 'n':
                tip.specks = max(1, min(20000, int(value)))
            elif key == 's':
                tip.speck = max(1e-6, min(1.0, float(value)))
    except (IndexError, ValueError):
        pass
    return tip


def load_texture(path):
    """The texture for *path*, loaded once per session (None if unreadable).
    ROUND, BRISTLES, DUST and SPRAY (with its settings, see spray_path) are
    the built-in tips."""
    if path and path.startswith(SPRAY + '?'):
        if path not in _textures:
            _textures[path] = _spray_tip(path)
        return _textures[path]
    if path and (path == RAKE or path.startswith(RAKE + '?')):
        if path not in _textures:
            _textures[path] = _rake_tip(path)
        return _textures[path]
    if path and (path == SOFT or path.startswith(SOFT + '?')):
        if path not in _textures:
            _textures[path] = _soft_tip(path)
        return _textures[path]
    if path and (path == HAIRS or path.startswith(HAIRS + '?')):
        if path not in _textures:
            _textures[path] = _hairs_tip(path)
        return _textures[path]
    if path in _BUILT_IN:
        if path not in _textures:
            make, name = _BUILT_IN[path]
            tip = BrushTexture(path, make(), name)
            if path == SPRAY:
                tip.procedural, tip.specks, tip.speck = True, 150, 0.02
            _textures[path] = tip
        if path not in _textures:
            make, name = _BUILT_IN[path]
            _textures[path] = BrushTexture(path, make(), name)
        return _textures[path]
    key = os.path.normcase(os.path.abspath(path))
    if key not in _textures:
        try:
            _textures[key] = BrushTexture(path)
        except (ValueError, OSError):
            _textures[key] = None
    return _textures[key]


def textures_in_folder(folder):
    """PNG paths in *folder*, sorted by name."""
    try:
        names = sorted(n for n in os.listdir(folder) if n.lower().endswith('.png'))
    except OSError:
        return []
    return [os.path.join(folder, n) for n in names]


class StampTrail:
    """Stamps laid along a stroke as its points come in.

    Positions, sizes and angles are in picture coordinates. *to_picture* is
    the view's QTransform from screen to picture at the time of drawing; it
    turns the pen's on-screen tilt into a picture direction and the brush's
    screen size into picture pixels, so the stroke stays put under zoom,
    rotation and flips afterwards.

    *sampler*: f(x, y, size) -> 0xRRGGBB of what is on screen under a stamp
    (picture coords), or None; Blend and Smudge read colours through it.
    """

    def __init__(self, settings, texture, color, size_px, to_picture, pressure_on, seed=None,
                 sampler=None):
        self.settings = dict(settings)
        if texture is not None and (texture.path == DUST or getattr(texture, 'procedural', False)):
            # Specks pile up like spray; dragged flat they turned into streaks.
            self.settings['flat'] = False
        if getattr(texture, 'bristle_engine', False):
            self.settings['flat'] = True    # 🖌 bristles are laid as one stroke (BristleStroke)
        self._sampler = sampler if self.settings['mode'] == 'smudge' else None
        self._carried = None    # smudge: the colour picked up so far
        self._load = 1.0        # smudge: how much of it is left (empty spots use it up)
        self._found_share = 1.0 # smudge streaks: share of the tip over paint, last stamp
        self.texture = texture
        self.stamps = []        # (x, y, size, angle, opacity, rgb)
        self._to_picture = to_picture
        self._unit = math.sqrt(abs(to_picture.determinant())) or 1.0   # picture px per screen px
        self._size = size_px * self._unit
        if getattr(texture, 'procedural', False):
            # Spray: the cloud is the brush size times Spread (up to 20x).
            self._size *= max(1, min(SPRAY_SPREAD_MAX, int(self.settings.get('spray_spread', 1))))
        self._pressure_on = pressure_on
        self._rng = random.Random(seed)
        self._alpha = color.alphaF()
        self._palette = self._jitter_palette(color)
        self._stroke_rgb = self._rng.choice(self._palette)
        # 'streaks': colour varies across the tip and runs on along the stroke,
        # like loaded bristles - bands of a few streaks each, picked once.
        self._streaks = None
        if self.settings['jitter_per_stamp'] == 'streaks':
            self._streak_axis, self._streak_share = texture.streak_axis(
                self.settings['angle'] if self.settings['angle_mode'] in ('auto', 'direction') else 0)
            colours = []
            while len(colours) < STREAKS:
                colours += [self._rng.choice(self._palette)] * self._rng.randint(1, 4)
            self._streaks = colours[:STREAKS]
            self._carried_streaks = [None] * STREAKS
        tine = getattr(texture, 'rake_tine', None)
        if tine:
            # 🪮 Rake: stamps close enough that each tine draws a line, not
            # a row of dots - a share of the tine's own width.
            self.settings['spacing'] = min(self.settings['spacing'], max(0.5, tine * 100 * 0.4))
            if self._streaks is None and self._sampler is not None:
                # Smudge: every tine combs its own colour along (the streaks
                # machinery: bands across the tip, each carrying what it met).
                self._streak_axis, self._streak_share = texture.streak_axis(
                    self.settings['angle'] if self.settings['angle_mode'] in ('auto', 'direction') else 0)
                self._streaks = [self._stroke_rgb] * STREAKS
                self._carried_streaks = [None] * STREAKS
        # 'particles': every speck its own colour. A handful of colourings per
        # stroke, each stamp one of them (turned at random it looks new), so
        # the stamps are cached instead of made one by one.
        self._particles = None
        self.speckled = False
        if self.settings['jitter_per_stamp'] == 'particles' and len(set(self._palette)) > 1:
            # (a tip dragged flat: speckled all along, see FlatStroke._specks.
            # Not the soft round - each of its stamps is laid as it is.)
            # (🖌 bristles: no specks - each hair takes a colour of its own
            # from them, see BristleStroke._hair_colours; speckled sent the
            # stroke to the CPU, ~30x slower)
            self.speckled = ((self.settings['flat'] or self.settings['mode'] == 'history')
                             and self.settings['mode'] in ('paint', 'history')
                             and not getattr(texture, 'stamp_max', False)
                             and not getattr(texture, 'bristle_engine', False))
            number = SPECKLED if self.speckled else 0
            self._particles = [(-1 - number - i,) + tuple(self._rng.choice(self._palette)
                                                          for _ in range(PARTICLE_COLOURS))
                               for i in range(PARTICLE_SETS)]
        self._last = None       # (x, y, pressure) of the previous point
        self._pending = None    # first point, waiting for a real pressure
        self._filter = None     # Smoothing's 1€ filter state (see _smoothed)
        self._curve_pts = []    # the last (up to 4) smoothed points, for the curve
        self._curve_n = 0       # how many came this stroke
        self._raw_last = None   # the pen's own last point
        self._travel = 0.0      # distance walked since the last stamp
        self._direction = None  # smoothed unit vector in picture space
        self._tilt = None       # smoothed lean, picture space
        self.angle = None       # the last stamp's turn, picture space

    def _jitter_palette(self, color):
        s = self.settings
        hj, sj, vj = s['hue_jitter'] / 100.0, s['sat_jitter'] / 100.0, s['val_jitter'] / 100.0
        base = color.rgb() & 0xFFFFFF
        if not (hj or sj or vj):
            return [base]
        rng = self._rng
        u = np.array([[rng.uniform(-1, 1) for _ in range(3)] for _ in range(PALETTE_SIZE)])
        base_rgb = np.broadcast_to(np.array([color.redF(), color.greenF(), color.blueF()]),
                                   (PALETTE_SIZE, 3))
        out = np.round(jitter_rgb(base_rgb, hj, sj, vj, u[:, 0], u[:, 1], u[:, 2]) * 255).astype(int)
        return [int((r << 16) | (g << 8) | b) for r, g, b in out]     # (OKLCH, see jitter_rgb)

    def _picture_direction(self, sx, sy):
        """A screen direction as a picture one (unit vector), or None."""
        m = self._to_picture
        dx, dy = m.m11() * sx + m.m21() * sy, m.m12() * sx + m.m22() * sy
        n = math.hypot(dx, dy)
        return (dx / n, dy / n) if n else None

    @staticmethod
    def _blend(old, new, k):
        if old is None:
            return new
        x, y = old[0] + (new[0] - old[0]) * k, old[1] + (new[1] - old[1]) * k
        n = math.hypot(x, y)
        return (x / n, y / n) if n else new

    def add(self, x, y, pressure=None, tilt=None):
        """Feed the next point (picture coords). *pressure* None: not known
        yet (the first point of a stroke). *tilt*: the pen's (xTilt, yTilt) in
        degrees, or None. Returns the stamps it made.

        The pen's points are smoothed (Smoothing: a 1€ filter - steady
        when the pen moves slowly, hardly any lag when it moves fast) and
        joined by curves through them (centripetal Catmull-Rom) instead of
        straight pieces - a quick curve showed its corners. The curve to a
        point is laid when the next one comes (one pen event later);
        finish() lays the rest, up to where the pen lifted."""
        if not self._pressure_on:
            pressure = 1.0
        elif pressure is not None:
            pressure = self._pressure_curve(pressure)
        if tilt is not None and math.hypot(*tilt) >= TILT_MIN:
            lean = self._picture_direction(tilt[0], tilt[1])
            if lean is not None:
                self._tilt = self._blend(self._tilt, lean, 0.35)
        if pressure is None:
            self._pending = (x, y)
            return []
        self._raw_last = (x, y, pressure)
        new = []
        for px, py, pp in self._through(x, y, pressure):
            new += self._walk(px, py, pp)
        return new

    # -- the pen's way: pressure curve, smoothing, curves ----------------

    def _pressure_curve(self, pressure):
        """Pressure through the brush's Firmness: below 0 a light touch
        counts for more (soft), above 0 it takes a firmer press."""
        firm = self.settings.get('pressure_curve', 0)
        pressure = max(0.0, min(1.0, pressure))
        return pressure ** (2.0 ** (firm / 50.0)) if firm else pressure

    def _smoothed(self, x, y):
        """(x, y) through the 1€ filter (Casiez et al. 2012), in screen px
        and seconds: the cutoff rises with the pen's speed."""
        s = max(0.0, min(1.0, self.settings.get('smoothing', 0) / 100.0))
        if s <= 0.0:
            return x, y
        now = time.perf_counter()
        sx, sy = x / self._unit, y / self._unit
        f = self._filter
        if f is None:
            self._filter = [sx, sy, 0.0, 0.0, now]
            return x, y
        fx, fy, dx, dy, t = f
        dt = max(1.0 / 240.0, now - t)          # bursts of events: not a near-zero step
        min_cutoff = 10.0 ** (1.0 - 2.0 * s)    # Hz: ~10 (light) .. 0.1 (heavy)
        beta = 0.04 * (1.0 - s) ** 2 + 0.0015

        def alpha(cutoff):
            tau = 1.0 / (2.0 * math.pi * cutoff)
            return 1.0 / (1.0 + tau / dt)

        a_d = alpha(15.0)       # the speed itself followed quickly: a fast stroke opens the filter at once
        dx += (((sx - fx) / dt) - dx) * a_d
        dy += (((sy - fy) / dt) - dy) * a_d
        a = alpha(min_cutoff + beta * math.hypot(dx, dy))
        fx += (sx - fx) * a
        fy += (sy - fy) * a
        self._filter = [fx, fy, dx, dy, now]
        return fx * self._unit, fy * self._unit

    def _through(self, x, y, pressure):
        """The points to walk to for this pen point: the curve from two
        points back to the last one (see add)."""
        x, y = self._smoothed(x, y)
        pts = self._curve_pts
        pts.append((x, y, pressure))
        del pts[:-4]
        self._curve_n += 1
        if self._curve_n == 1:
            return [(x, y, pressure)]
        if self._curve_n == 2:
            return []
        a = pts[-4] if len(pts) >= 4 else pts[-3]
        return self._spline(a, pts[-3], pts[-2], pts[-1])

    def _spline(self, a, b, c, d):
        """Points along the centripetal Catmull-Rom curve from *b* to *c*
        (*a*, *d* its neighbours), every ~1.5 screen px, *c* last."""
        length = math.hypot(c[0] - b[0], c[1] - b[1]) / self._unit
        if length < 1e-6:
            return [c]
        n = max(1, min(64, int(math.ceil(length / 1.5))))
        if n == 1:
            return [c]

        def knot(t, p, q):
            return t + max(math.hypot(q[0] - p[0], q[1] - p[1]), 1e-4) ** 0.5

        t0 = 0.0
        t1 = knot(t0, a, b)
        t2 = knot(t1, b, c)
        t3 = knot(t2, c, d)

        def lerp(p, q, ta, tb, t):
            u = (t - ta) / (tb - ta)
            return (p[0] + (q[0] - p[0]) * u, p[1] + (q[1] - p[1]) * u)

        out = []
        for k in range(1, n):
            t = t1 + (t2 - t1) * k / n
            a1 = lerp(a, b, t0, t1, t)
            a2 = lerp(b, c, t1, t2, t)
            a3 = lerp(c, d, t2, t3, t)
            b1 = lerp(a1, a2, t0, t2, t)
            b2 = lerp(a2, a3, t1, t3, t)
            px, py = lerp(b1, b2, t1, t2, t)
            out.append((px, py, b[2] + (c[2] - b[2]) * k / n))
        out.append(c)
        return out

    def _walk(self, x, y, pressure):
        """Lay stamps along the straight way from the last point to (x, y)."""
        new = []
        first = False
        if self._pending is not None:
            # The first point gets this point's pressure, and its stamp waits
            # until there is a direction to turn it by.
            px, py = self._pending
            self._pending = None
            self._last = (px, py, pressure)
            first = True
        if self._last is None:
            self._last = (x, y, pressure)
            new.append(self._stamp(x, y, pressure))
            return new
        x0, y0, p0 = self._last
        dist = math.hypot(x - x0, y - y0)
        if dist > 0:
            self._direction = self._blend(self._direction, ((x - x0) / dist, (y - y0) / dist), 0.35)
        if first:
            new.append(self._stamp(x0, y0, p0))
        if dist > 0:
            walked = 0.0
            while True:
                t = walked / dist if dist else 0.0
                p = p0 + (pressure - p0) * t
                step = self._stamp_size(p) * self.settings['spacing'] / 100.0
                if self.settings['mode'] in BLENDING:
                    # Each Blend stamp moves pixels about: a few % of the tip
                    # apart is as smooth, and keeps it quick.
                    step = max(step, self._stamp_size(p) * 0.03)
                elif self.settings['mode'] in ('smear', 'push'):
                    # A smear moves the pixels a step at a time: further
                    # apart, every speck is laid again in a dotted row
                    # instead of a streak. Big tips may step a little more.
                    step = min(step, max(self._unit * 1.5, self._stamp_size(p) * 0.008))
                if self._streaks is not None and self._sampler is not None:
                    # Streaks picking up colour change it stamp by stamp:
                    # close stamps, so it flows instead of stepping in bands.
                    step = min(step, self._stamp_size(p) * 0.06)
                if self.settings['mode'] == 'smudge' and self._sampler is None:
                    # 👆 the pixel smudge (SmudgeStroke): a patch laid every few %
                    step = self._stamp_size(p) * smudge_spacing(self.settings) / 100.0
                step = max(self._unit * 0.5, step)
                if walked + (step - self._travel) > dist:
                    break
                walked += step - self._travel
                self._travel = 0.0
                t = walked / dist
                new.append(self._stamp(x0 + (x - x0) * t, y0 + (y - y0) * t,
                                       p0 + (pressure - p0) * t))
            self._travel += dist - walked
        self._last = (x, y, pressure)
        return new

    def finish(self, pressure=1.0):
        """The pen lifted: the curve's last piece, up to where it lifted. A
        tap that never moved still leaves one stamp, at *pressure*."""
        if self._pending is not None:
            self.add(*self._pending, pressure=pressure)
        elif self._curve_n >= 2:
            pts = self._curve_pts
            a = pts[-3] if len(pts) >= 3 else pts[-2]
            for px, py, pp in self._spline(a, pts[-2], pts[-1], pts[-1]):
                self._walk(px, py, pp)
            raw = self._raw_last
            if raw is not None and math.hypot(raw[0] - pts[-1][0], raw[1] - pts[-1][1]) > 0.5 * self._unit:
                self._walk(*raw)        # smoothing lagged: on to the pen's own last place
        self._curve_n = 0
        return self.stamps

    # (No taper in / out: a stroke's start and end are as the pen paints
    # them - its pressure. Tapers were tried and taken out: the stepped fade
    # they laid over the flat stroke looked worse than the plain tip.)

    def _stamp_size(self, pressure):
        if self._pressure_on and self.settings['size_pressure']:
            return self._size * pressure_size(pressure)
        return self._size

    def _stamp(self, x, y, pressure):
        s = self.settings
        if s['angle_mode'] == 'auto' and (self._tilt or self._direction):
            dx, dy = self._tilt or self._direction
            angle = math.atan2(dy, dx)
        elif s['angle_mode'] == 'direction' and self._direction:
            # Crosswise to the stroke whichever way it goes, the pen's lean
            # ignored: a flat tip never ends up dragged edgeways.
            dx, dy = self._direction
            angle = math.atan2(dy, dx)
        else:
            # Fixed: the stamp as it was on screen (the view's turn undone).
            dx, dy = self._picture_direction(1.0, 0.0) or (1.0, 0.0)
            angle = math.atan2(dy, dx)
        angle += math.radians(s['angle'])
        self.angle = angle      # before the jitter: what the cursor shows
        if s['angle_jitter']:
            angle += math.radians(self._rng.uniform(-1, 1) * s['angle_jitter'])
        opacity = self._alpha
        if self._pressure_on and s['opacity_pressure']:
            opacity *= pressure_opacity(pressure)
        rgb = self._rng.choice(self._palette) if s['jitter_per_stamp'] is True else self._stroke_rgb
        if self._particles is not None:
            rgb = self._rng.choice(self._particles)
        size = self._stamp_size(pressure)
        if s.get('size_jitter'):
            size *= 1.0 - self._rng.random() * s['size_jitter'] / 100.0
        if s.get('scatter'):
            # Thrown off the stroke, anywhere within Scatter % of the size.
            turn = self._rng.uniform(0.0, 2.0 * math.pi)
            reach = math.sqrt(self._rng.random()) * s['scatter'] / 100.0 * size
            x, y = x + math.cos(turn) * reach, y + math.sin(turn) * reach
        if self._streaks is not None:
            rgb = self._streaked_colours(x, y, size, angle)
        elif self._sampler is not None:
            here = self._sampler(x, y, size)
            if here is not None:
                k = self._carry()
                if s['mode'] in BLENDING:
                    # Wet: the brush takes on some of each colour it passes
                    # and carries it on (the per-pixel mix with what is under
                    # it happens when the stroke is laid, see FlatStroke).
                    self._wet = rgb if getattr(self, '_wet', None) is None else self._wet
                    self._wet = self._mix(self._wet, int(here), self._pickup() * _alpha(here))
                    rgb = self._wet
                else:
                    # Smudge: lay what was picked up, then pick up some of
                    # what is here; *k* is how much of the old colour stays.
                    # A spot with little paint (sparse specks) gives a little:
                    # its colour, and the load, in proportion.
                    a = _alpha(here)
                    if self._carried is None:
                        self._carried, self._load = int(here), a
                    rgb = self._carried
                    opacity *= self._load
                    old, new = self._load * k, (1.0 - k) * a
                    if old + new > 0.0:
                        self._carried = self._mix(int(here), self._carried, old / (old + new))
                    self._load = old + new                      # topped up here
            elif s['mode'] == 'smudge' and self._carried is not None:
                # An empty spot (a layer sampled on its own): lay what is
                # left and use it up, so the smear thins out and ends.
                rgb = self._carried
                opacity *= self._load
                self._load *= self._carry()
            elif s['mode'] == 'smudge':
                opacity = 0.0           # nothing here to pick up, nothing to lay
        if (self._streaks is not None and self._sampler is not None and s['mode'] == 'smudge'
                and not getattr(self, '_streaks_empty', False)):
            opacity *= self._load
            self._load = self._load * self._carry() + (1.0 - self._carry()) * self._found_share
        if self._streaks is not None and getattr(self, '_streaks_empty', False):
            opacity = 0.0
        if s['mode'] == 'smudge' and self._load < 0.01:
            opacity = 0.0
        stamp = (x, y, size, angle, opacity, rgb)
        if getattr(self.texture, 'bristle_engine', False):
            # bristles splay and touch by the pen's pressure (BristleStroke),
            # with or without Pressure: size / opacity
            stamp += (round(float(pressure), 4),)
        self.stamps.append(stamp)
        return stamp

    def _mix(self, a, b, t):
        """Colours *a* and *b* mixed *t* of the way to *b*: like paint or like light."""
        return pigment.mix(a, b, t) if self.settings.get('pigment', True) else _mix(a, b, t)

    def _mix_many(self, a, b, t):
        """Lists of colours mixed pairwise, *t* of the way to *b* - in one go."""
        if self.settings.get('pigment', True):
            return pigment.mix_many(a, b, t)
        ts = t if isinstance(t, (list, tuple)) else [t] * len(a)
        return [_mix(p, q, s) for p, q, s in zip(a, b, ts)]

    CARRY_MAX = 0.96    # Carry 100 %: the paint still thins out (half in ~3.4 tip lengths)

    def _carry(self):
        """Smudge: the share of the carried colour that stays per stamp.
        Carry is set as it feels at Spacing 20 and kept per distance, so a
        tighter spacing does not drag the paint further; scaled below 1 so
        even Carry 100 % fades into what it passes (it never repaints)."""
        k = self.CARRY_MAX * min(1.0, max(0.0, self.settings['mix'] / 100.0))
        if k <= 0.0:
            return 0.0
        return k ** (max(0.5, self.settings['spacing']) / 20.0)

    def _pickup(self):
        """Blend: the share of the colour under a stamp the wet brush takes
        on, per stamp, whatever the spacing. Mix sets how wet: the brush's
        own colour halves every 1 / Mix tip lengths (Mix 50 %: every two),
        so a low Mix keeps it longer, a high one mixes fast."""
        wet = max(0.02, self.settings['mix'] / 100.0)
        return 1.0 - 0.5 ** (max(0.005, self.settings['spacing'] / 100.0) * wet)

    def _streaked_colours(self, x, y, size, angle):
        """The stamp's colour as (axis, c0 ... cN): the stroke's streak
        colours, each blended with - or, smudging, dragging - what is under
        its own spot across the tip, so a smudge pulls the colours under the
        whole width along side by side."""
        colours = self._streaks
        s = self.settings
        if self._sampler is not None:
            if self._streak_axis == 0:
                vx, vy = math.cos(angle), math.sin(angle)
            else:
                vx, vy = -math.sin(angle), math.cos(angle)
            length = size * self._streak_share
            k = self._carry()
            carried = self._carried_streaks
            offsets = [((i + 0.5) / STREAKS - 0.5) * length for i in range(len(colours))]
            points = [(x + vx * t, y + vy * t) for t in offsets]
            many = getattr(self._sampler, 'many', None)
            if many is not None:
                found = many(points, length / STREAKS)
            else:
                found = [self._sampler(px, py, length / STREAKS) for px, py in points]
            # All the streaks' mixes in one go (see _mix_many): per streak
            # they were most of a smudge's time.
            out = []
            idx, mix_a, mix_b, shares = [], [], [], []
            fresh = all(c is None for c in carried)
            # Share of the tip over paint (sparse specks count a little): the
            # rest uses the load up (see add)
            self._found_share = sum(_alpha(c) for c in found) / max(1, len(found))
            if s['mode'] == 'smudge' and fresh and self._found_share > 0.0:
                self._load = self._found_share      # first pickup: as much as was there
            kept = self._load * k
            for i, own in enumerate(colours):
                here = found[i]
                if s['mode'] in BLENDING:
                    if carried[i] is None:
                        carried[i] = own
                    if here is not None:
                        idx.append(i); mix_a.append(carried[i]); mix_b.append(int(here))
                        shares.append(self._pickup() * _alpha(here))
                else:
                    if carried[i] is None and here is not None:
                        carried[i] = int(here)
                    # A streak that found nothing yet lays what the others
                    # picked up (filled in below), never the brush colour.
                    c = carried[i]
                    out.append(c)
                    if here is not None:
                        new = (1.0 - k) * _alpha(here)
                        idx.append(i); mix_a.append(int(here)); mix_b.append(c)
                        shares.append(kept / (kept + new) if kept + new > 0.0 else 0.0)
            if idx:
                mixed = self._mix_many(mix_a, mix_b, shares)
                for i, c in zip(idx, mixed):
                    carried[i] = c
            if s['mode'] == 'smudge':
                have = [c for c in out if c is not None]
                if have:
                    fill = have[len(have) // 2]
                    out = [c if c is not None else fill for c in out]
                else:
                    out = list(colours)     # nothing anywhere: no stamp (see _streaks_empty)
            if s['mode'] in BLENDING:
                out = list(carried)
            colours = out
            # Smudging where nothing has been picked up yet (an empty spot of
            # a layer sampled on its own): no colour to drag, no stamp.
            self._streaks_empty = s['mode'] == 'smudge' and all(c is None for c in carried)
        return (self._streak_axis,) + tuple(colours)


def _stamp_matrix(stamp, image, view):
    x, y, size, angle = stamp[:4]
    k = size / max(image.width(), image.height())
    m = QTransform()
    m.translate(x, y)
    m.rotateRadians(angle)
    m.scale(k, k)
    m.translate(-image.width() / 2.0, -image.height() / 2.0)
    return m * view


class FlatStroke:
    """One stroke painted the HeavyPaint way: the tip dragged like paint.

    Coverage is the *maximum* anything gave a pixel (a grey buffer under
    Lighten), so going over a spot again within the stroke changes nothing.
    Colour is laid over itself (one colour, unless jittered), and the
    coverage then cuts it to shape. *rect*: the device area it covers.

    Everything is worked out in the motion's frame: the tip turned so the
    stroke runs along +x (BrushTexture.motion_frame). Its width across the
    stroke is then the tip's real footprint whatever its own turn, and
    changes smoothly as the stroke curves - a tip that keeps its turn (pen
    tilt, a fixed angle) used to be dragged by one side or the other, and
    round a curve the stroke flipped between the two in steps.

    A stroke has three parts:

    * the head - the first stamp as it is, with each line filled on from its
      leading ink to the leading edge once the drag starts ('front'), so the
      body grows out of the tip's ragged front;
    * the body - the tip's profile across the motion swept along, so its
      grain and gaps run on as streaks, its width following the tip's size
      as pressure changes it. Laid up to the middle of the stamp under the
      pen, never past it;
    * the tail - the stamp under the pen, each line filled from the body up
      to its leading ink ('back'). Not written into the buffers but added
      when composing, so it moves with the pen; when the pen lifts it is
      the stroke's end, the tip's own ragged edge.
    """

    FLARE = 1.5     # how fast the body may widen / narrow, per length gone
    TURN_EASE = 0.35    # how quickly the motion's direction follows the pen
    REVERSE_COS = 0.5   # the pen this far off the body's way (60°): start afresh (see _body)
    BODY_COLOUR_MIX = 0.35  # share of each body piece's colour laid over what is there

    def __init__(self, rect, source=None):
        self.rect = rect
        # History: (picture, view, tint) - the colour is the picture itself,
        # drawn through *view* (picture -> device), with the stamps' own
        # colour mixed in by *tint* (0..1). See compose().
        self.source = source
        self.mask = QImage(rect.size(), QImage.Format_RGB32)
        self.mask.fill(Qt.black)
        self.colour = QImage(rect.size(), QImage.Format_ARGB32_Premultiplied)
        self.colour.fill(Qt.transparent)
        self._first = None      # the stroke's first stamp
        self._prev = None       # the last stamp that moved it on
        self._dir = None        # the motion's direction (unit vector, eased)
        self._reach = None      # picture point the body has been laid up to
        self._width = None      # the body's width across the motion, last laid
        self._tail = None       # what compose() adds, see _draw_tail
        self._tail_box = None   # device box of the tail last laid
        self._pend = None       # the body's last piece, drawn when the next is known
        self._pend_view = None  # (view, scale) it was made under
        self._restart = False   # the head just set starts the stroke afresh (turned back)
        self.head = None
        self._specks = None     # (QBrush, cells -> picture -> device): see _speck_field

    def _speck_field(self, texture, stamp, view):
        """Particles: the colour is a speckle of its own under the whole
        stroke, the coverage cut out of it. Through the tip it could not
        be: the swept body is a profile one pixel long, stretched along the
        stroke, so only the first and last stamp had specks and the middle
        came out as bands. Cells a speck big (as on the tip), hung on the
        first stamp - its place, turn and size - so the stroke lays the same
        specks live, burnt in, at any zoom, moved or turned."""
        from PySide6.QtGui import QBrush
        x, y, size, angle = stamp[:4]
        tip = texture._levels[0]
        long_side = max(tip.width(), tip.height())
        cell = size * max(1, round(long_side / 160)) / long_side
        place = QTransform()
        place.translate(x, y)
        place.rotateRadians(angle)
        place.scale(cell, cell)
        self._specks = (QBrush(_speck_tile(stamp[5])), place * view)

    @staticmethod
    def _turn(stamp, direction):
        """The tip's turn relative to the motion, in degrees."""
        return math.degrees(stamp[3] - math.atan2(direction[1], direction[0]))

    @staticmethod
    def _frame_matrix(stamp, frame, direction, tip_long, view):
        """Motion frame (frame image pixels) -> device, centred on *stamp*."""
        x, y, size = stamp[:3]
        k = size / tip_long
        m = QTransform()
        m.translate(x, y)
        m.rotateRadians(math.atan2(direction[1], direction[0]))
        m.scale(k, k)
        m.translate(-frame['w'] / 2.0, -frame['h'] / 2.0)
        return m * view

    def _body(self, texture, level, stamp, tip_long, view, scale):
        """The body's next pieces for *stamp*: (device transform, turn) for
        the profile (maybe none). Sets self.head the first time the drag's
        direction is known (the first stamp's front is laid then).

        A long piece is cut in short ones whose width eases from the last
        piece's to this stamp's (FLARE), and each reaches back over the last
        by a device pixel plus what the turn since opens at its ends, so no
        seam or wedge shows between them, straight or round a curve."""
        self.head = None
        if self._first is None:
            self._first = self._prev = stamp
            return []
        prev = self._prev
        dx, dy = stamp[0] - prev[0], stamp[1] - prev[1]
        moved = math.hypot(dx, dy)
        if moved < max(0.25, (0.04 * stamp[2]) if self._dir is None else 0.25):
            return []                       # hardly moved: wait (the tail shows it)
        self._prev = stamp
        step = (dx / moved, dy / moved)
        if self._dir is None:
            self._dir = step
        else:
            e = self.TURN_EASE
            nx, ny = self._dir[0] + (step[0] - self._dir[0]) * e, self._dir[1] + (step[1] - self._dir[1]) * e
            n = math.hypot(nx, ny)
            old_dir = self._dir
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
            old_dir = d
        tx, ty = stamp[0], stamp[1]
        rx, ry = self._reach
        vx, vy = tx - rx, ty - ry
        far = math.hypot(vx, vy)
        if (not getattr(self, '_in_head', False) and far >= 0.25
                and vx * d[0] + vy * d[1] < self.REVERSE_COS * far):
            # Turned back (or very sharply): the eased direction still points
            # the old way, and a piece from where the body got to would be
            # sheared across it into a long thin spike. Start afresh here
            # instead, as a brush pulled back does: the tip as it is (its
            # front filled out to a square edge stood out as a hard slab).
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
        out = []
        for i in range(pieces):
            a, b = i / pieces, (i + 1) / pieces
            out.append({'s': (rx + (tx - rx) * a, ry + (ty - ry) * a),
                        'e': (rx + (tx - rx) * b, ry + (ty - ry) * b),
                        'w0': was + (width - was) * a, 'w1': was + (width - was) * b,
                        'd': d, 'turn': turn})
        return out

    # The body is a ribbon: its pieces meet along the line halfway between
    # their directions (a mitre), so round a curve no corner of one sticks
    # out past the next - square-ended pieces left a row of small ticks
    # along both edges of every curve (their ends cut through the soft rim,
    # and the stroke's max blend keeps every hard cut). Each piece is a
    # plain strip, cut to its two joints by a clip; it is drawn once the
    # next piece is known (its far joint); the last waits, and compose()
    # lays it square-ended meanwhile.

    @staticmethod
    def _bisector(d1, d2):
        bx, by = d1[0] + d2[0], d1[1] + d2[1]
        n = math.hypot(bx, by)
        return (bx / n, by / n) if n > 1e-6 else d2

    def _push_piece(self, piece, images, lay, view, scale):
        """Queue *piece*; the one before it is drawn now, its end cut to the
        joint between the two."""
        pend = self._pend
        piece['images'] = images
        if pend is not None:
            joint = self._bisector(pend['d'], piece['d'])
            pend['end_cut'] = joint
            piece['start_cut'] = joint
            self._draw_piece(pend, lay, view, scale)
        else:
            piece['start_cut'] = None           # the chain starts here: square
        self._pend = piece

    @staticmethod
    def piece_widths(piece, back, fwd):
        """The body piece's width where its strip begins (*back* before its
        start) and ends (*fwd* past its end): w0 -> w1 carried on in a
        straight line, so the pieces' edges meet as one - kept within w0..w1
        (round a sharp turn the strip reaches far past its joints, which
        the clip cuts away)."""
        (sx, sy), (ex, ey) = piece['s'], piece['e']
        w0, w1 = piece['w0'], piece['w1']
        span = math.hypot(ex - sx, ey - sy)
        if span < 1e-9:
            return w1, w1
        slope = (w1 - w0) / span
        lo, hi = min(w0, w1), max(w0, w1)
        return (min(hi, max(lo, w0 - slope * back)), min(hi, max(lo, w1 + slope * fwd)))

    def _piece_strip(self, piece, view, scale):
        """(device transform of the 1-px profile over the piece, made long
        enough to reach both cuts; clip path in picture coords or None)."""
        from PySide6.QtCore import QPointF
        from PySide6.QtGui import QPainterPath, QPolygonF
        d = piece['d']
        ax, ay = -d[1], d[0]                    # across the motion
        (sx, sy), (ex, ey) = piece['s'], piece['e']
        half = max(piece['w0'], piece['w1']) / 2
        px = 1.0 / max(scale, 1e-6)             # a device pixel, in picture units

        def reach(cut):
            # how far a cut along *cut* (a direction) leans past a square end
            if cut is None:
                return 0.0
            c = abs(cut[0] * d[0] + cut[1] * d[1])
            s = math.sqrt(max(0.0, 1.0 - c * c))
            return half * s / max(c, 0.3)

        back = reach(piece.get('start_cut')) + px
        fwd = reach(piece.get('end_cut')) + px
        bx, by = sx - d[0] * back, sy - d[1] * back
        fx, fy = ex + d[0] * fwd, ey + d[1] * fwd
        h = piece['images'][0].height()
        wb, wf = self.piece_widths(piece, back, fwd)
        if abs(wb - wf) <= 1e-3 * max(wb, wf, 1e-6):
            wd = wf
            m = QTransform(fx - bx, fy - by, ax * wd / h, ay * wd / h, bx - ax * wd / 2, by - ay * wd / 2)
        else:
            # A trapezoid, the width going from the piece's start to its end:
            # drawn at one width (its end's), every change of pressure left a
            # step in the stroke's edge - a staircase where it narrowed.
            m = QTransform()
            ok = QTransform.quadToQuad(
                QPolygonF([QPointF(0, 0), QPointF(1, 0), QPointF(1, h), QPointF(0, h)]),
                QPolygonF([QPointF(bx - ax * wb / 2, by - ay * wb / 2),
                           QPointF(fx - ax * wf / 2, fy - ay * wf / 2),
                           QPointF(fx + ax * wf / 2, fy + ay * wf / 2),
                           QPointF(bx + ax * wb / 2, by + ay * wb / 2)]), m)
            if not ok:
                wd = wf
                m = QTransform(fx - bx, fy - by, ax * wd / h, ay * wd / h, bx - ax * wd / 2, by - ay * wd / 2)
        # the clip: between the two joint lines, wide enough for the strip
        side = half * 1.6 + px

        def line(centre, cut, shift):
            if cut is None:
                cx, cy = -d[1], d[0]            # square: straight across
            else:
                cx, cy = -cut[1], cut[0]
            k = side / max(abs(cx * ax + cy * ay), 0.3)
            ox, oy = centre[0] + d[0] * shift, centre[1] + d[1] * shift
            return (ox - cx * k, oy - cy * k), (ox + cx * k, oy + cy * k)

        s0, s1 = line(piece['s'], piece.get('start_cut'), -px)
        e0, e1 = line(piece['e'], piece.get('end_cut'), px)
        # Joint lines that cross inside the stroke (a curve tighter than the
        # brush is wide) would cut the piece away: leave the start uncut then,
        # overlapping the last piece as it used to.
        if (e0[0] - s0[0]) * d[0] + (e0[1] - s0[1]) * d[1] <= 0 \
                or (e1[0] - s1[0]) * d[0] + (e1[1] - s1[1]) * d[1] <= 0:
            s0 = (bx - ax * side * 2 - d[0] * half, by - ay * side * 2 - d[1] * half)
            s1 = (bx + ax * side * 2 - d[0] * half, by + ay * side * 2 - d[1] * half)
        path = QPainterPath()
        path.addPolygon(QPolygonF([QPointF(*s0), QPointF(*e0), QPointF(*e1), QPointF(*s1)]))
        path.closeSubpath()
        return m * view, path

    def _draw_piece(self, piece, lay, view, scale):
        m, clip = self._piece_strip(piece, view, scale)
        grey, colour = piece['images']
        lay(m, grey, colour, clip=(clip, view))

    def _flush_piece(self, lay, view, scale):
        """Draw the waiting piece square-ended (the chain ends here)."""
        pend, self._pend = self._pend, None
        if pend is not None:
            pend['end_cut'] = None
            self._draw_piece(pend, lay, view, scale)

    def add(self, texture, stamps, view):
        """Lay *stamps* through *view* (picture -> device). Returns the device
        QRect that changed (the tail's old and new place included), or None."""
        offset = QTransform.fromTranslate(-self.rect.x(), -self.rect.y())
        mp, cp = QPainter(self.mask), QPainter(self.colour)
        mp.setCompositionMode(QPainter.CompositionMode_Lighten)
        for p in (mp, cp):
            p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        scale = math.sqrt(abs(view.determinant()))
        dirty = self._tail_box

        def grow(box):
            nonlocal dirty
            dirty = box if dirty is None else dirty.united(box)

        def lay(m, grey, colour, clip=None):
            for p, image in ((mp, grey), (cp, colour)):
                if clip is not None:
                    # a body piece: cut to its joints (clip in picture coords)
                    p.setTransform(clip[1] * offset)
                    p.setClipPath(clip[0])
                    if p is cp:
                        # Its colour goes in part of the way, like wet paint:
                        # a jittered colour laid at full strength started as a
                        # hard band across the stroke. (One colour: no change -
                        # compose() takes the colour at full strength.)
                        p.setOpacity(self.BODY_COLOUR_MIX)
                p.setTransform(m * offset)
                p.drawImage(0, 0, image)
                if clip is not None:
                    p.setClipping(False)
                    p.setOpacity(1.0)
            grow(m.mapRect(QRectF(0, 0, grey.width(), grey.height())).toAlignedRect())

        for stamp in stamps:
            on_screen = stamp[2] * scale
            step = round(stamp[4] * OPACITY_STEPS)
            if on_screen < 0.4 or step <= 0:
                continue
            level = texture.level(on_screen)
            step = min(OPACITY_STEPS, step)
            plain = texture.grey(level, step)
            tip_long = max(plain.width(), plain.height())
            if getattr(texture, 'stamp_max', False):
                # A soft round: every stamp as it is, the most any gave a
                # pixel kept (the mask's Lighten) - a smooth capsule. The swept
                # body and its front piece left seams across a feathered disc.
                lay(_stamp_matrix(stamp, plain, view), plain, texture.coloured(level, stamp[5]))
                continue
            is_first = self._first is None
            if is_first and is_speckled(stamp[5]):
                self._speck_field(texture, stamp, view)
            self._restart = False
            pieces = self._body(texture, level, stamp, tip_long, view, scale)
            if is_first:
                lay(_stamp_matrix(stamp, plain, view), plain, texture.coloured(level, stamp[5]))
                continue
            if self.head is not None:
                first, turn, d = self.head
                if self._restart:
                    # turned back: the ribbon so far ends here, a new one
                    # starts from the tip as it is
                    self._flush_piece(lay, view, scale)
                    # (the tip with its holes closed, streaks along the stroke - see motion_frame)
                    frame = texture.motion_frame(level, turn)
                    lay(self._frame_matrix(first, frame, d, tip_long, view),
                        texture.frame_variant(level, turn, 'whole', step=step),
                        texture.frame_variant(level, turn, 'whole', rgb=first[5]))
                else:
                    frame = texture.motion_frame(level, turn)
                    lay(self._frame_matrix(first, frame, d, tip_long, view),
                        texture.frame_variant(level, turn, 'whole', step=step),
                        texture.frame_variant(level, turn, 'whole', rgb=first[5]))
            fine = max(1, min(BODY_STEPS, round(stamp[4] * BODY_STEPS)))
            for piece in pieces:
                turn = piece['turn']
                images = (texture.frame_variant(level, turn, 'profile', step=fine, steps=BODY_STEPS),
                          texture.frame_variant(level, turn, 'profile', rgb=stamp[5]))
                self._push_piece(piece, images, lay, view, scale)
            if self._pend is not None:
                self._pend_view = (view, scale)
                # where the waiting piece is shown (square-ended, by compose)
                m, _clip = self._piece_strip(self._pend, view, scale)
                grow(m.mapRect(QRectF(0, 0, 1, self._pend['images'][0].height())).toAlignedRect()
                     .adjusted(-2, -2, 2, 2))
            if self._reach is not None:
                self._tail = (texture, level, step, stamp, view, self._dir)
                frame = texture.motion_frame(level, self._turn(stamp, self._dir))
                box = self._frame_matrix(stamp, frame, self._dir, tip_long, view).mapRect(
                    QRectF(0, 0, frame['w'], frame['h'])).toAlignedRect()
                self._tail_box = box
                grow(box)
        mp.end()
        cp.end()
        if dirty is None:
            return None
        return dirty.adjusted(-1, -1, 1, 1).intersected(self.rect)

    def _draw_tail(self, mask, colour, local):
        """Add the tail (see the class) to *mask* / *colour*, copies of the
        buffers' *local* rect."""
        from PySide6.QtCore import QPointF
        from PySide6.QtGui import QPainterPath, QPolygonF
        texture, level, step, stamp, view, d = self._tail
        plain = texture.grey(level, step)
        tip_long = max(plain.width(), plain.height())
        turn = self._turn(stamp, d)
        frame = texture.motion_frame(level, turn)
        to_local = view * QTransform.fromTranslate(-self.rect.x() - local.x(),
                                                   -self.rect.y() - local.y())
        frame_m = self._frame_matrix(stamp, frame, d, tip_long, QTransform()) * to_local
        # The fill only ahead of what the body has laid: from there on to the
        # tip's leading ink, so a short stroke does not paint over its head.
        rx, ry = self._reach
        big = 4 * (frame['w'] + frame['h']) * stamp[2] / tip_long + 4
        ax, ay = d                                             # forward
        lx, ly = -ay, ax                                       # sideways
        corners = [(rx + lx * big, ry + ly * big), (rx - lx * big, ry - ly * big),
                   (rx - lx * big + ax * big, ry - ly * big + ay * big),
                   (rx + lx * big + ax * big, ry + ly * big + ay * big)]
        path = QPainterPath()
        path.addPolygon(QPolygonF([to_local.map(QPointF(px, py)) for px, py in corners]))
        path.closeSubpath()
        mp, cp = QPainter(mask), QPainter(colour)
        mp.setCompositionMode(QPainter.CompositionMode_Lighten)
        for p in (mp, cp):
            p.setRenderHint(QPainter.SmoothPixmapTransform, True)
            p.setTransform(frame_m)
        # the tip itself, its holes closed and its streaks along the stroke ...
        mp.drawImage(0, 0, texture.frame_variant(level, turn, 'whole', step=step))
        cp.drawImage(0, 0, texture.frame_variant(level, turn, 'whole', rgb=stamp[5]))
        for p in (mp, cp):
            p.resetTransform()
            p.setClipPath(path)                      # ... and its fill, ahead only
            p.setTransform(frame_m)
        mp.drawImage(0, 0, texture.frame_variant(level, turn, 'back', step=step))
        cp.drawImage(0, 0, texture.frame_variant(level, turn, 'back', rgb=stamp[5]))
        mp.end()
        cp.end()

    def compose(self, rect):
        """The stroke inside device *rect*, premultiplied ARGB, its tail
        included."""
        local = rect.translated(-self.rect.x(), -self.rect.y())
        colour = self.colour.copy(local)
        mask = self.mask.copy(local)
        if self._pend is not None and self._pend_view is not None:
            # the body's last piece, still waiting for its joint: square-ended
            view, scale = self._pend_view
            pend = self._pend
            saved_cut = pend.get('end_cut')
            pend['end_cut'] = None                       # square-ended for now
            m, clip = self._piece_strip(pend, view, scale)
            pend['end_cut'] = saved_cut
            to_local = QTransform.fromTranslate(-self.rect.x() - local.x(), -self.rect.y() - local.y())
            mp, cp = QPainter(mask), QPainter(colour)
            mp.setCompositionMode(QPainter.CompositionMode_Lighten)
            for p, image in ((mp, pend['images'][0]), (cp, pend['images'][1])):
                p.setRenderHint(QPainter.SmoothPixmapTransform, True)
                p.setTransform(view * to_local)
                p.setClipPath(clip)
                p.setTransform(m * to_local)
                p.drawImage(0, 0, image)
            mp.end()
            cp.end()
        if self._tail is not None:
            self._draw_tail(mask, colour, local)
        # The colour at full strength (un-premultiplied, then made opaque), so
        # a soft edge keeps its colour instead of darkening; the coverage
        # alone decides how strong it is.
        # (Read the straight colour as opaque - converting ARGB32 to RGB32
        # would lay it over black first, darkening where it is part-laid.)
        if self._specks is None:
            straight = colour.convertToFormat(QImage.Format_ARGB32)
            straight.reinterpretAsFormat(QImage.Format_RGB32)
            out = straight.convertToFormat(QImage.Format_ARGB32_Premultiplied)
        else:
            brush, to_device = self._specks
            out = QImage(local.size(), QImage.Format_ARGB32_Premultiplied)
            p = QPainter(out)
            p.setCompositionMode(QPainter.CompositionMode_Source)
            p.setBrushOrigin(0, 0)
            # (the brush carries the mapping, not the painter: a rect dirty by
            # the frame gets the very pixels the whole stroke would)
            brush.setTransform(to_device * QTransform.fromTranslate(-self.rect.x() - local.x(),
                                                                   -self.rect.y() - local.y()))
            p.fillRect(out.rect(), brush)       # (unsmoothed: crisp specks)
            p.end()
        if self.source is not None:
            # History: the picture's own pixels under the stroke, pixel for
            # pixel, with the brush colour mixed in by the tint.
            picture, view, tint = self.source
            p = QPainter(out)
            p.setRenderHint(QPainter.SmoothPixmapTransform, True)
            p.setTransform(view * QTransform.fromTranslate(-self.rect.x() - local.x(),
                                                           -self.rect.y() - local.y()))
            p.setOpacity(max(0.0, min(1.0, 1.0 - tint)))
            p.drawImage(0, 0, picture)
            p.end()
        grey = mask.convertToFormat(QImage.Format_Grayscale8)
        alpha = QImage(grey.constBits(), grey.width(), grey.height(), grey.bytesPerLine(),
                       QImage.Format_Alpha8)
        p = QPainter(out)
        p.setCompositionMode(QPainter.CompositionMode_DestinationIn)
        p.drawImage(0, 0, alpha)
        p.end()
        return out


class SmearStroke:
    """💧 Blend: a blender brush. It carries a patch of the actual pixels -
    the tip's size and shape - lays it down at each stamp, so what is under
    the brush (its colours *and* texture) is dragged along, and then takes up
    some of what is there into the patch. It works on a live copy of the
    view, so it smears what it just smeared too. The brush colour goes in
    as well, the more the lower Mix is; at 100 % it only moves what is there.

    Same face as FlatStroke (add / compose), so the live view and the stroke
    kept after it are the very same pixels. *work*: the view under the stroke
    (as Sample reads it), device-sized; *rect*: the device area it covers.
    """

    REPLACES = False    # the carried patch laid over what is there (Smudge: in place of it)

    def __init__(self, rect, work, mix, spacing, paint_like=False):
        self.rect = rect
        # Pigment: what is taken up and the brush colour mix into the carried
        # patch like paint (see pigment.py), not like light.
        self.paint_like = paint_like
        self.work = work.convertToFormat(QImage.Format_ARGB32_Premultiplied).copy()
        # What the brush takes up comes from the view as it was: taken from
        # the live copy, each stamp - a few % of the tip past the last - took
        # up mostly what the last one had just laid, and the first colour
        # was dragged the whole way, whatever it passed.
        self.before = self.work.copy()
        self.mask = QImage(rect.size(), QImage.Format_RGB32)
        self.mask.fill(Qt.black)
        self.carried = None
        mix = max(0.0, min(1.0, mix))
        frac = max(0.005, spacing / 100.0)
        # Per tip length travelled: how much of what is under it the brush
        # takes up (less at Mix 100 %: it drags about two tip lengths), and
        # how much of its own colour it lays (none at Mix 100 %).
        self.pickup = 1.0 - (1.0 - (0.9 - 0.55 * mix)) ** frac
        self.tint = 1.0 - (1.0 - 0.6 * (1.0 - mix)) ** frac
        self.head = None
        # Mixing like paint costs more: the patch takes up what is under it
        # every few stamps (about every tenth of the tip), as much as those
        # stamps would have together. It is still laid at every stamp.
        self.every = max(1, round(0.10 / frac)) if paint_like else 1
        self.waiting = 0

    def add(self, texture, stamps, view):
        offset = QTransform.fromTranslate(-self.rect.x(), -self.rect.y())
        scale = math.sqrt(abs(view.determinant()))
        dirty = None
        for stamp in stamps:
            on_screen = stamp[2] * scale
            step = min(OPACITY_STEPS, round(stamp[4] * OPACITY_STEPS))
            if on_screen < 0.4 or step <= 0:
                continue
            level = texture.level(on_screen)
            tip = texture._levels[level]
            w, h = tip.width(), tip.height()
            m = _stamp_matrix(stamp, tip, view) * offset
            back, ok = m.inverted()
            if not ok:
                continue
            # Take up this stamp (see self.every)? Then what is under the tip
            # now, in the tip's own frame.
            self.waiting += 1
            under = None
            if self.carried is None or self.waiting >= self.every:
                under = QImage(w, h, QImage.Format_ARGB32_Premultiplied)
                under.fill(Qt.transparent)
                p = QPainter(under)
                p.setRenderHint(QPainter.SmoothPixmapTransform, True)
                p.setTransform(back)
                p.drawImage(0, 0, self.before)
                p.end()
            if self.carried is None:
                self.carried = under.copy()
            elif self.carried.width() != w or self.carried.height() != h:
                self.carried = self.carried.scaled(w, h, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
            # Lay what the brush carries, in the tip's shape ...
            lay = self.carried.copy()
            p = QPainter(lay)
            p.setCompositionMode(QPainter.CompositionMode_DestinationIn)
            p.drawImage(0, 0, tip)
            p.end()
            p = QPainter(self.work)
            p.setRenderHint(QPainter.SmoothPixmapTransform, True)
            p.setTransform(m)
            p.setOpacity(stamp[4])
            if self.REPLACES:
                # 👆 in place of what is there, not over it: the patch's
                # empty (see-through) parts take paint away too
                p.setCompositionMode(QPainter.CompositionMode_DestinationOut)
                p.drawImage(0, 0, tip)
                p.setCompositionMode(QPainter.CompositionMode_Plus)
            p.drawImage(0, 0, lay)
            p.end()
            mp = QPainter(self.mask)
            mp.setCompositionMode(QPainter.CompositionMode_Lighten)
            mp.setRenderHint(QPainter.SmoothPixmapTransform, True)
            mp.setTransform(m)
            mp.drawImage(0, 0, texture.grey(level, step))
            mp.end()
            # ... then take up some of what was here, and some of the brush's
            # own colour: carried, it is laid and dragged like the rest (laid
            # on the view instead, the next stamp covered it straight away).
            rgb = stamp[5] if isinstance(stamp[5], int) else stamp[5][1]
            if under is None:
                pass                    # not this stamp: the next takes up for both
            elif self.paint_like:
                n, self.waiting = self.waiting, 0
                carried = pigment.over(_image_array(self.carried), _image_array(under),
                                       1.0 - (1.0 - self.pickup) ** n,
                                       rgb, 1.0 - (1.0 - self.tint) ** n if self.tint > 0 else 0.0)
                self.carried = _array_image(carried)
            else:
                self.waiting = 0
                p = QPainter(self.carried)
                p.setOpacity(self.pickup)
                p.drawImage(0, 0, under)
                if self.tint > 0:
                    p.setOpacity(self.tint)
                    p.setCompositionMode(QPainter.CompositionMode_SourceAtop)
                    p.fillRect(0, 0, w, h, QColor((rgb >> 16) & 255, (rgb >> 8) & 255, rgb & 255))
                p.end()
            box = m.mapRect(QRectF(0, 0, w, h)).toAlignedRect().translated(self.rect.x(), self.rect.y())
            dirty = box if dirty is None else dirty.united(box)
        if dirty is None:
            return None
        return dirty.adjusted(-1, -1, 1, 1).intersected(self.rect)

    def compose(self, rect):
        """The smeared pixels inside device *rect*, where the brush went."""
        local = rect.translated(-self.rect.x(), -self.rect.y())
        out = self.work.copy(local)
        grey = self.mask.copy(local).convertToFormat(QImage.Format_Grayscale8)
        alpha = QImage(grey.constBits(), grey.width(), grey.height(), grey.bytesPerLine(),
                       QImage.Format_Alpha8)
        p = QPainter(out)
        p.setCompositionMode(QPainter.CompositionMode_DestinationIn)
        p.drawImage(0, 0, alpha)
        p.end()
        return out


def smudge_spacing(settings):
    """👆 Smudge's real stamp distance, % of the tip: Spacing, kept within
    3-10 % - a carried patch laid a fifth of the tip apart showed each
    lay as a step."""
    return max(3.0, min(10.0, float(settings.get('spacing', 20))))


class SmudgeStroke(SmearStroke):
    """👆 Smudge: a pixel smudge, as Photoshop's / Krita's. The brush carries
    a patch of the pixels under it and lays it at each step *in place of*
    what is there, then takes up some of what was there - so the paint
    under the tip is pulled along with the brush: the shape's own pixels
    are dragged out of it, and whatever the brush brought in (the empty
    canvas, another colour) takes their place. No colour of its own.

    It was one colour per stamp, picked up at the tip's middle and laid
    over the canvas: the shape it smudged stayed whole underneath, and the
    stamp's back half laid that colour before the shape too.

    *carry* (0..1, Carry): how far the paint is dragged - at 100 % it
    takes up a fifth of what is under it per tip length, at 0 nearly all
    of it (hardly drags). *backdrop*: what is under this layer (device
    px), when Sample is "this layer only": then the smudge takes paint
    away from the layer (see cut) and shows what that uncovers."""

    REPLACES = True

    def __init__(self, rect, work, carry, spacing, backdrop=None):
        super().__init__(rect, work, 1.0, spacing)
        carry = max(0.0, min(1.0, carry))
        frac = max(0.005, spacing / 100.0)
        self.pickup = 1.0 - (1.0 - (0.95 - 0.75 * carry)) ** frac
        self.tint = 0.0
        self.backdrop = None
        if backdrop is not None and backdrop.size() == self.work.size():
            self.backdrop = _image_array(backdrop.convertToFormat(QImage.Format_ARGB32_Premultiplied))

    def _reached(self, local):
        grey = self.mask.copy(local).convertToFormat(QImage.Format_Grayscale8)
        a = np.frombuffer(grey.constBits(), np.uint8, count=grey.bytesPerLine() * grey.height())
        return a.reshape(grey.height(), grey.bytesPerLine())[:, :grey.width()]

    def compose(self, rect):
        """As seen while smudging: with a backdrop, the smudged layer over
        it where the brush went (opaque there: what the smudge took away
        shows what is under the layer); without, the smudged pixels."""
        if self.backdrop is None:
            return super().compose(rect)
        local = rect.translated(-self.rect.x(), -self.rect.y()).intersected(self.work.rect())
        part = (slice(local.y(), local.y() + local.height()), slice(local.x(), local.x() + local.width()))
        top = _image_array(self.work.copy(local)).astype(np.float32)
        under = self.backdrop[part].astype(np.float32)
        over = top + under * (1.0 - top[..., 3:4] / 255.0)
        over *= self._reached(local).astype(np.float32)[..., None] / 255.0
        return _array_image(np.clip(over + 0.5, 0, 255).astype(np.uint8))

    def compose_layer(self, rect):
        """The layer's own smudged paint in device *rect*, where the brush went."""
        return SmearStroke.compose(self, rect)

    def cut(self, rect):
        """Where the smudge replaces the layer's paint, in device *rect*: an
        alpha mask (premultiplied ARGB, black)."""
        local = rect.translated(-self.rect.x(), -self.rect.y()).intersected(self.work.rect())
        a = self._reached(local)
        out = np.zeros(a.shape + (4,), np.uint8)
        out[..., 3] = a
        return _array_image(out)


class DragStroke:
    """🌊 Smear: a wet-paint drag, as in HeavyPaint. The tip is a row of
    hairs across the stroke, half a pixel apart, touching the canvas along a
    thin line. Each hair grabs paint that stands out from what it has been
    passing - a line, a speck, an edge - and carries it on whole, laying it
    as it goes until the load runs out: so lines and specks are transported
    in thin, sharp streaks instead of being averaged away. (Dragging a patch
    of pixels spread each speck into a soft, blurred dash; plain averaging
    let a thin line fade out within a few pixels.)

    *drag*: how far a load lasts (4 .. ~400 screen px). *grab*: how firmly
    a hair takes up what stands out. *edge*: how much it must stand out.
    *blur*: how far the plain blend of what a hair passes runs (0: none,
    only grabbed paint moves). The tip's density along each hair sets its
    strength, so a textured tip gives strong and weak hairs; Opacity how
    much of the smear shows.

    Same face as SmearStroke (add / compose). *work*: the view under the
    stroke (as Sample reads it), device-sized; *rect*: the device area."""

    def __init__(self, rect, work, drag, grab=0.7, edge=0.25, blur=0.2):
        self.rect = rect
        self.work = work.convertToFormat(QImage.Format_ARGB32_Premultiplied).copy()
        w, h, line = self.work.width(), self.work.height(), self.work.bytesPerLine()
        self.pixels = np.frombuffer(self.work.bits(), np.uint8, count=line * h).reshape(
            h, line)[:, :w * 4].reshape(h, w, 4)
        self.reached = np.zeros((h, w), np.uint8)     # where the tip went (compose's alpha)
        self._cl = None         # the arrays on the graphics card (drag_gpu.DragGpu), or None
        self._cl_tried = False
        # The stroke's ends feathered: how far along the stroke (device px)
        # each pixel was laid, so compose() fades the smear in over the first
        # stretch and out over the last - it began and ended in a straight cut.
        self.laid_at = np.zeros((h, w), np.float32)
        self.travel = 0.0       # device px the hairs have moved so far
        self.fade = 0.0         # how long each end's feather is (half the tip, the widest yet)
        self._recent = []       # (travel, device box) of the last stretch: its fade changes
        clamp = lambda v: max(0.0, min(1.0, v))
        # All in screen px moved (whatever the tip's size):
        # how far a grabbed load lasts (Drag) ...
        self.carry = 4.0 + 400.0 * clamp(drag) ** 2
        # ... how firmly a hair grabs what stands out, per px (Grab) ...
        self.grab = clamp(grab)
        # ... how much it must stand out: a channel apart by this share of
        # full scale from what the hair has been passing (Edge) ...
        self.edge = 0.02 + 0.45 * clamp(edge)
        # ... and how far the plain blend runs (Blur): what a hair passes,
        # averaged. Long, it spread a thin line's colour too thin to see -
        # the load is what carries it whole.
        self.blend = 0.5 + 60.0 * clamp(blur) ** 2
        # ... and the wet drag itself (Mix): how far a hair carries the
        # colour it passed, the paint pulled along as a mass. Only
        # what stands out was carried before - a flat colour, all of it
        # alike, hardly moved even with everything at full.
        self.pull = 120.0 * clamp(drag) ** 1.5
        self.hairs = None       # (S, 4) float32: each hair's blend of what it passed
        self.load = None        # (S, 4) float32: the paint each hair grabbed and carries ...
        self.amount = None      # (S,) float32: ... and how much of it is left, 0..1
        self.around = None      # (S, 4) float32: what each hair has been passing, lately
        self.last = None        # the last stamp's centre (local device px)
        self.way = None         # the stroke's way, smoothed
        self._masks = {}
        self._grids = {}

    # The graphics card pays from about this tip size (screen px): each step
    # costs ~0.5 ms there whatever its size, so smaller tips are quicker in
    # numpy (measured: Smear wins from ~500 px, Push from ~250 px).
    CL_MIN_TIP = 450

    def _cl_on(self, tip=0.0):
        """The stroke's arrays on the graphics card (made at the first
        step, when the tip is big enough to gain), or None - numpy then
        (see drag_gpu)."""
        if not self._cl_tried:
            self._cl_tried = True
            if tip < self.CL_MIN_TIP:
                return None
            from . import drag_gpu
            if drag_gpu.available():
                try:
                    self._cl = drag_gpu.DragGpu(self.pixels, self.reached, self.laid_at)
                except Exception as e:
                    drag_gpu.failed(e)
                    self._cl = None
        return self._cl

    def _cl_off(self, error):
        """The graphics card failed mid-stroke: its arrays back, numpy on."""
        from . import drag_gpu
        drag_gpu.failed(error)
        gpu, self._cl = self._cl, None
        if gpu is not None:
            try:
                h, w = self.reached.shape
                gpu.fetch(0, 0, w, h, self.pixels, self.reached, self.laid_at)
            except Exception:
                pass

    def _mask(self, texture, level, tip, m):
        """The tip's grey as the stamp lays it (turned and sized by *m*'s
        linear part), centred on an odd square, as a uint8 array. Kept per
        2° of turn and 1/32 of size: drawing it for every stamp was half the
        time, and the stamps of a stroke turn and grow only a little."""
        scale = math.hypot(m.m11(), m.m12())
        turn = math.atan2(m.m12(), m.m11())
        flip = m.determinant() < 0
        key = (id(texture), level, round(turn / 0.035), round(scale * 32), flip)
        mask = self._masks.get(key)
        if mask is not None:
            return mask
        turn, scale = round(turn / 0.035) * 0.035, max(1, round(scale * 32)) / 32.0
        w, h = tip.width(), tip.height()
        half = int(math.hypot(w, h) * scale / 2) + 2
        size = 2 * half + 1
        t = QTransform()
        t.translate(half + 0.5, half + 0.5)
        t.rotateRadians(turn)
        t.scale(scale, -scale if flip else scale)
        t.translate(-w / 2.0, -h / 2.0)
        grey = QImage(size, size, QImage.Format_Alpha8)
        grey.fill(0)
        p = QPainter(grey)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        p.setTransform(t)
        p.drawImage(0, 0, tip)
        p.end()
        line = grey.bytesPerLine()
        mask = np.frombuffer(grey.constBits(), np.uint8, count=line * size).reshape(
            size, line)[:, :size]
        # Cut to what the tip covers, plus a rim it reads ahead from: the
        # square round a turned tip's diagonal was half empty.
        ys, xs = np.nonzero(mask)
        if len(xs):
            reach = int(max(abs(xs - half).max(), abs(ys - half).max())) + 3
            if reach < half:
                mask = mask[half - reach:half + reach + 1, half - reach:half + reach + 1]
        mask = np.ascontiguousarray(mask)
        found = (mask, mask.astype(np.float32) / 255.0)
        if len(self._masks) > 400:
            self._masks.clear()
        self._masks[key] = found
        return found

    def _grid(self, size):
        grid = self._grids.get(size)
        if grid is None:
            r = np.arange(size, dtype=np.float32) - size // 2
            grid = self._grids[size] = (r[None, :].repeat(size, 0), r[:, None].repeat(size, 1))
        return grid

    def add(self, texture, stamps, view):
        scale = math.sqrt(abs(view.determinant()))
        h, w = self.reached.shape
        ox, oy = self.rect.x(), self.rect.y()
        dirty = None
        for stamp in stamps:
            on_screen = stamp[2] * scale
            if on_screen < 0.4 or stamp[4] <= 0:
                continue
            level = texture.level(on_screen)
            tip = texture._levels[level]
            m = _stamp_matrix(stamp, tip, view)
            centre = m.map(QPointF(tip.width() / 2.0, tip.height() / 2.0))
            fx_c, fy_c = centre.x() - ox, centre.y() - oy
            if self.last is None:
                self.last = (fx_c, fy_c)
                continue
            dx, dy = fx_c - self.last[0], fy_c - self.last[1]
            travelled = math.hypot(dx, dy)
            if travelled < 1.0:
                continue            # under a pixel: the next stamp moves it
            self.last = (fx_c, fy_c)
            self.travel += travelled
            self.fade = max(self.fade, 0.5 * on_screen)
            # Which way the hairs run: the stroke's way, smoothed a little.
            ux, uy = dx / travelled, dy / travelled
            if self.way is not None:
                ux, uy = self.way[0] * 0.85 + ux * 0.15, self.way[1] * 0.85 + uy * 0.15
                n = math.hypot(ux, uy) or 1.0
                ux, uy = ux / n, uy / n
            prev_way = self.way
            self.way = (ux, uy)
            vx, vy = -uy, ux
            _tip_a, grey = self._mask(texture, level, tip, m)
            half = grey.shape[0] // 2
            # The hairs, half a pixel apart across the stroke (whole pixels
            # left holes on a slant), each as strong as the tip is dense on
            # its line - a textured tip gives strong and weak hairs.
            js = (np.arange(4 * half + 1, dtype=np.float32) - 2 * half) * 0.5
            strength = self._profile(grey, half, ux, uy, vx, vy, js)
            # Where they touch: the line swept from the last stamp to this,
            # half a pixel at a time.
            ss = np.arange(0.0, travelled, 0.5, dtype=np.float32)
            ix = np.rint(fx_c - ux * ss[:, None] + vx * js[None, :]).astype(np.intp)
            iy = np.rint(fy_c - uy * ss[:, None] + vy * js[None, :]).astype(np.intp)
            inside = (ix >= 0) & (ix < w) & (iy >= 0) & (iy < h)
            if not inside.any():
                continue
            gpu = self._cl_on(on_screen)
            met = None
            if gpu is not None:
                try:
                    met = gpu.met(fx_c, fy_c, ux, uy, vx, vy, len(ss), half, len(js))
                except Exception as e:
                    self._cl_off(e)
                    gpu = None
            if met is None:
                np.clip(ix, 0, w - 1, out=ix)
                np.clip(iy, 0, h - 1, out=iy)
                old = self.pixels[iy, ix].astype(np.float32)      # (steps, hairs, 4)
                met = old.mean(0)
            self._fit_hairs(met)
            f32 = np.float32
            # The load runs out as it is laid along ...
            self.amount *= f32(math.exp(-travelled / self.carry))
            carried, carrying = self.load.copy(), self.amount.copy()    # laid here
            # ... and each hair grabs what stands out from what it has been
            # passing: a line or speck it crosses is taken up whole and
            # carried on, not averaged into the rest and lost.
            apart = np.abs(met - self.around).max(1) / 255.0
            stands = np.clip((apart - self.edge) / 0.12, 0.0, 1.0)
            # A weak hair (a sparse part of the tip) moves less paint.
            grabbed = 1.0 - (1.0 - self.grab * stands * strength) ** f32(travelled)
            # What it takes leaves the canvas: the ground it lay on (what the
            # hair has been passing) shows there instead - so a speck is
            # moved, its old place left empty, not copied on from where it
            # stays (as HeavyPaint's smudge moves its particles).
            ground = met + (self.around - met) * grabbed[:, None]
            room = (1.0 - self.amount) * grabbed
            total = self.amount + room
            safe = np.maximum(total, 1e-6)[:, None]
            self.load = (self.load * self.amount[:, None] + met * room[:, None]) / safe
            self.amount = total.astype(f32)
            # The plain blend (Blur) and the wet drag (Mix, longer the harder
            # the pen presses) - of what is left once the hair took its share -
            # and what the hair has been passing.
            press = max(0.0, min(1.0, stamp[4]))
            reach = self.blend + self.pull * (0.25 + 0.75 * press)
            self.hairs += (ground - self.hairs) * f32(1.0 - math.exp(-travelled / reach))
            if len(self.hairs) > 2:
                # Neighbouring hairs share a little of their colour: each
                # carried its own, and rounded to whole pixels on a slant
                # they laid it in alternating rows - thin stripes that
                # turned with the stroke. (The tip's own streaks stay: they
                # come from how much each hair grabs.)
                for name, rate in (('hairs', 0.5), ('load', 0.5)):
                    a = getattr(self, name)
                    soft = a.copy()
                    soft[1:-1] = (a[:-2] + 2.0 * a[1:-1] + a[2:]) * f32(0.25)
                    a += (soft - a) * f32(rate)
                amt = self.amount
                soft = amt.copy()
                soft[1:-1] = (amt[:-2] + 2.0 * amt[1:-1] + amt[2:]) * f32(0.25)
                self.amount = (amt + (soft - amt) * f32(0.5)).astype(f32)
            self.around += (met - self.around) * f32(1.0 - math.exp(-travelled / 12.0))
            # Each hair lays the load it brought (what it grabs here goes
            # down from the next step on, moved), its blend where it is spent.
            # Laid whole; how strongly (the hair's strength, Opacity) is kept
            # as the cover, the most any pass gave - mixed in on every pass,
            # pixels the half-pixel rows hit twice came out stronger than
            # their neighbours, a fine grid over the whole stroke.
            lay = self.hairs + (carried - self.hairs) * (carrying * strength)[:, None]
            # Shown at full strength (times Opacity) wherever the tip is: a
            # weak hair moves less instead (above). Shown only as strong as
            # the hair, the picture as it was showed through - a particle
            # moved on still showed at its old place.
            cover = np.where(strength > 0.02, 1.0, 0.0).astype(f32) * min(1.0, stamp[4])
            # Laid pixel by pixel over the area the tip swept since the last
            # step: each pixel takes the hair it lies under. (Laid as one
            # line per hair, the lines fanned apart round a bend and the
            # paint between them showed through - fans of fine rays.) Round
            # a bend the area reaches back over the wedge the last step's
            # left open on the outside.
            reach_back = 1.0
            if prev_way is not None:
                turn = abs(prev_way[0] * vx + prev_way[1] * vy)      # sin of the turn
                reach_back += half * min(1.0, turn) * 1.2
            corners_x = [fx_c + vx * half, fx_c - vx * half,
                         fx_c - ux * (travelled + reach_back) + vx * half,
                         fx_c - ux * (travelled + reach_back) - vx * half]
            corners_y = [fy_c + vy * half, fy_c - vy * half,
                         fy_c - uy * (travelled + reach_back) + vy * half,
                         fy_c - uy * (travelled + reach_back) - vy * half]
            bx0, bx1 = max(0, int(math.floor(min(corners_x)))), min(w, int(math.ceil(max(corners_x))) + 1)
            by0, by1 = max(0, int(math.floor(min(corners_y)))), min(h, int(math.ceil(max(corners_y))) + 1)
            if bx0 >= bx1 or by0 >= by1:
                continue
            mixing = (strength * f32(0.45 + 0.5 * press)).astype(f32)
            if gpu is not None:
                try:
                    gpu.lay((bx0, by0, bx1, by1), fx_c, fy_c, ux, uy, vx, vy, travelled, reach_back,
                            half, lay, mixing, cover, self.travel)
                    box = QRect(bx0 + ox, by0 + oy, bx1 - bx0, by1 - by0)
                    self._recent.append((self.travel, box))
                    dirty = box if dirty is None else dirty.united(box)
                    continue
                except Exception as e:
                    self._cl_off(e)
            gx = np.arange(bx0, bx1, dtype=f32)[None, :] - f32(fx_c)
            gy = np.arange(by0, by1, dtype=f32)[:, None] - f32(fy_c)
            back = -(gx * f32(ux) + gy * f32(uy))           # how far behind the tip (along)
            across = gx * f32(vx) + gy * f32(vy)
            hair = np.rint(across * 2.0).astype(np.intp) + 2 * half
            inside_px = ((back >= -0.5) & (back < travelled + reach_back)
                         & (hair >= 0) & (hair < len(strength)))
            hair_c = np.clip(hair, 0, len(strength) - 1)
            touched = inside_px & (cover[hair_c] > 0.02)
            if not touched.any():
                continue
            ys_t, xs_t = np.nonzero(touched)
            hs = hair_c[ys_t, xs_t]
            ys_t = ys_t + by0
            xs_t = xs_t + bx0
            # Mixed into what is there, not laid over it: each pixel goes
            # part of the way to its hair's colour (mixing, above) - as far
            # as the tip is dense there (soft at its rim) and the pen
            # presses. Laid whole, a smear was a solid blob of the colour it
            # started on.
            here = self.pixels[ys_t, xs_t].astype(f32)
            colour = here + (lay[hs] - here) * mixing[hs][:, None]
            self.pixels[ys_t, xs_t] = np.clip(colour + 0.5, 0, 255).astype(np.uint8)
            shown = (cover[hs] * 255.0).astype(np.uint8)
            self.reached[ys_t, xs_t] = np.maximum(self.reached[ys_t, xs_t], shown)
            behind = back[touched]
            self.laid_at[ys_t, xs_t] = (self.travel - np.clip(behind, 0.0, travelled)).astype(f32)
            box = QRect(int(xs_t.min()) + ox, int(ys_t.min()) + oy,
                        int(xs_t.max() - xs_t.min()) + 1, int(ys_t.max() - ys_t.min()) + 1)
            self._recent.append((self.travel, box))
            dirty = box if dirty is None else dirty.united(box)
        if dirty is not None:
            # The stretch behind the tip fades out (its end feather) - as the
            # tip moves on, its fade goes, so it is composed again too: left
            # as it was, each redraw's end stayed faded in mid-stroke.
            since = self.travel - self.fade - 2.0
            self._recent = [(t, b) for t, b in self._recent if t >= since]
            for _t, b in self._recent:
                dirty = dirty.united(b)
        return dirty

    def _fit_hairs(self, met):
        """Hairs for this many lines across: the first, or after the tip
        changed size (pressure) - the middle ones kept, new ones at the rim."""
        n_new = len(met)
        if self.hairs is None:
            self.hairs, self.around, self.load = met.copy(), met.copy(), met.copy()
            self.amount = np.zeros(n_new, np.float32)
            return
        if len(self.hairs) == n_new:
            return

        def fit(prev, fresh):
            n = min(len(prev), n_new)
            a, b = (len(prev) - n) // 2, (n_new - n) // 2
            fresh[b:b + n] = prev[a:a + n]
            return fresh
        self.hairs = fit(self.hairs, met.copy())
        self.around = fit(self.around, met.copy())
        self.load = fit(self.load, met.copy())
        self.amount = fit(self.amount, np.zeros(n_new, np.float32))

    @staticmethod
    def _profile(grey, half, ux, uy, vx, vy, js):
        """How dense the tip is along each hair's line (0..1, the densest 1)."""
        ts = np.linspace(-half, half, 25, dtype=np.float32)
        mx = np.clip(np.rint(half + vx * js[:, None] + ux * ts[None, :]), 0, 2 * half).astype(np.intp)
        my = np.clip(np.rint(half + vy * js[:, None] + uy * ts[None, :]), 0, 2 * half).astype(np.intp)
        dense = grey[my, mx].mean(1)
        # Neighbouring half-pixel hairs share pixels: smoothed over a pixel
        # or so, two of very different strength no longer take turns on the
        # same pixel (a fine cross-hatch over the carried paint).
        if len(dense) > 4:
            dense = np.convolve(dense, np.array([0.25, 0.5, 0.25], np.float32), mode='same')
        top = float(dense.max())
        # Lifted (square root): a sparse tip's hairs were so weak that what
        # they dragged away still showed through where it had been.
        return np.sqrt(dense / top) if top > 0 else dense

    def compose(self, rect):
        """The dragged pixels inside device *rect*, where the brush went."""
        local = rect.translated(-self.rect.x(), -self.rect.y()).intersected(self.work.rect())
        if self._cl is not None:
            # The pixels live on the graphics card: this part of them back.
            try:
                self._cl.fetch(local.x(), local.y(), local.x() + local.width(),
                               local.y() + local.height(), self.pixels, self.reached, self.laid_at)
            except Exception as e:
                self._cl_off(e)
        out = self.work.copy(local)
        part = (slice(local.y(), local.y() + local.height()), slice(local.x(), local.x() + local.width()))
        reached = self.reached[part].astype(np.float32)
        if self.fade > 0.0:
            # Feathered ends: faded in over the first self.fade px of the
            # stroke and out over the last (smoothstep), so it neither starts
            # nor stops in a straight cut.
            laid = self.laid_at[part]
            start = np.clip(laid / self.fade, 0.0, 1.0)
            end = np.clip((self.travel - laid) / self.fade, 0.0, 1.0)
            reached *= start * start * (3.0 - 2.0 * start) * end * end * (3.0 - 2.0 * end)
        reached = np.ascontiguousarray((reached + 0.5).astype(np.uint8))
        alpha = QImage(reached.data, local.width(), local.height(), local.width(),
                       QImage.Format_Alpha8)
        p = QPainter(out)
        p.setCompositionMode(QPainter.CompositionMode_DestinationIn)
        p.drawImage(0, 0, alpha)
        p.end()
        return out


class PushStroke(DragStroke):
    """🧊 Push: liquify, as a forward-warp tool does it. The paint under the
    tip goes along with it - the tip's middle carries it as far as the
    brush moves (times Strength), its rim less - so it bends, shears and is
    shoved along as a mass. Whatever is behind the tip comes along too:
    pushed from the grey (or the empty canvas) into a colour, the grey
    drives the colour away - the push goes both ways, not only outwards.

    How: a displacement field - for each pixel, how far the paint now there
    has come in all. Each step bends the field under the tip (the field is
    itself carried along, then the step's move added), and the pixels are
    taken once from the paint as it was before the stroke, at the place
    the field says (bilinear, cv2.remap in C++). Nothing is resampled step
    after step, so a long push never blurs or breaks into steps, and moving
    back over the same place undoes it as paint would.

    *ice*: neighbouring pixels move unevenly - each lags by its own (fixed,
    slightly clumped) share, so the paint cracks into icy fragments as it is
    pushed (HeavyPaint's Blur Ice); 0 is a clean liquify push.

    Same face as DragStroke (add / compose, feathered ends)."""

    CL_MIN_TIP = 10 ** 9        # cv2.remap does it - the old OpenCL kernel moved whole pixels

    def __init__(self, rect, work, strength, ice=0.4, backdrop=None, paint=None):
        super().__init__(rect, work, 0.5)
        clamp = lambda v: max(0.0, min(1.0, v))
        self.strength = clamp(strength)
        self.ice = clamp(ice)
        h, w = self.reached.shape
        self.source = self.pixels.copy()                # the paint as it was: sampled, never changed
        # *backdrop* (the view without this layer, device-sized): *work* is
        # this layer alone, and the push REPLACES its paint where it went -
        # the empty canvas pushed into a shape cuts it away. Shown live as
        # the layer over the backdrop (see compose); kept as the layer's
        # pushed paint plus where it replaces (compose_layer, cut).
        self.backdrop = None
        if backdrop is not None:
            b = backdrop.convertToFormat(QImage.Format_ARGB32_Premultiplied)
            if (b.width(), b.height()) == (w, h):
                self.backdrop = _image_array(b)
        # *paint* (the layer's own, where *work* has it as seen - faded by
        # its opacity, lit): what is kept is this, moved by the same field.
        self.paint = None
        if paint is not None:
            own = paint.convertToFormat(QImage.Format_ARGB32_Premultiplied)
            if (own.width(), own.height()) == (w, h):
                self.paint = _image_array(own)
        self.disp = np.zeros((h, w, 2), np.float32)     # how far the paint at each pixel has come
        rng = np.random.default_rng(20261112)
        lag = rng.random((h, w), dtype=np.float32)
        if _cv2 is not None:
            lag = _cv2.GaussianBlur(lag, (0, 0), 1.2)       # small clumps, not single pixels
            lo, hi = float(lag.min()), float(lag.max())
            lag = (lag - lo) / max(1e-6, hi - lo)
        self._lag = lag

    def add(self, texture, stamps, view):
        scale = math.sqrt(abs(view.determinant()))
        h, w = self.reached.shape
        ox, oy = self.rect.x(), self.rect.y()
        dirty = None
        for stamp in stamps:
            on_screen = stamp[2] * scale
            if on_screen < 0.4 or stamp[4] <= 0:
                continue
            level = texture.level(on_screen)
            tip = texture._levels[level]
            m = _stamp_matrix(stamp, tip, view)
            centre = m.map(QPointF(tip.width() / 2.0, tip.height() / 2.0))
            fx_c, fy_c = centre.x() - ox, centre.y() - oy
            if self.last is None:
                self.last = (fx_c, fy_c)
                continue
            dx, dy = fx_c - self.last[0], fy_c - self.last[1]
            travelled = math.hypot(dx, dy)
            # Steps of a pixel or two (more for a big tip): each moves the
            # whole tip's square, and the field carries sub-pixel moves on.
            if travelled < max(1.0, 0.01 * on_screen):
                continue
            self.last = (fx_c, fy_c)
            self.travel += travelled
            # (no feathered ends, unlike Smear: the field already falls off
            # with the tip, and a fade mixed in the paint from before the
            # push - a ghost ring of the old edge where each push ended)
            tip_a, grey = self._mask(texture, level, tip, m)
            size = grey.shape[0]
            half = size // 2
            cx, cy = round(fx_c), round(fy_c)
            x0, y0 = cx - half, cy - half
            ax0, ay0 = max(0, x0), max(0, y0)
            ax1, ay1 = min(w, x0 + size), min(h, y0 + size)
            if ax0 >= ax1 or ay0 >= ay1:
                continue
            sx0, sy0, sx1, sy1 = ax0 - x0, ay0 - y0, ax1 - x0, ay1 - y0
            # How much of the move each pixel takes: the tip's grey there,
            # Strength and the stamp's opacity, less its own lag (Ice).
            # A full body that falls off only near the rim (1 - (1 - grey)^3):
            # the paint is carried as a mass with a rounded front. The tip's
            # plain grey moved its middle alone - pushed out, the paint
            # ended in a bite.
            g = grey[sy0:sy1, sx0:sx1]
            g = (1.0 - (1.0 - g) ** 3) * np.float32(self.strength * min(1.0, stamp[4]))
            if self.ice:
                g = g * (1.0 - np.float32(self.ice) * self._lag[ay0:ay1, ax0:ax1])
            g = g.astype(np.float32)
            xs = np.arange(ax0, ax1, dtype=np.float32)[None, :]
            ys = np.arange(ay0, ay1, dtype=np.float32)[:, None]
            gx, gy = g * np.float32(dx), g * np.float32(dy)
            if _cv2 is not None:
                # the field carried along with the push, then the move added
                bx = np.ascontiguousarray(np.broadcast_to(xs, g.shape) - gx)
                by = np.ascontiguousarray(np.broadcast_to(ys, g.shape) - gy)
                d = _cv2.remap(self.disp, bx, by, _cv2.INTER_LINEAR, borderMode=_cv2.BORDER_REPLICATE)
                d[..., 0] += gx
                d[..., 1] += gy
                self.disp[ay0:ay1, ax0:ax1] = d
                # every pixel there: the paint from where the field says it came
                mx = np.ascontiguousarray(np.broadcast_to(xs, g.shape) - d[..., 0])
                my = np.ascontiguousarray(np.broadcast_to(ys, g.shape) - d[..., 1])
                self.pixels[ay0:ay1, ax0:ax1] = _cv2.remap(
                    self.source, mx, my, _cv2.INTER_LINEAR, borderMode=_cv2.BORDER_REPLICATE)
            else:
                # (no OpenCV: the same, to the nearest pixel)
                bxi = np.clip(np.rint(xs - gx), 0, w - 1).astype(np.intp)
                byi = np.clip(np.rint(ys - gy), 0, h - 1).astype(np.intp)
                d = self.disp[byi, bxi].copy()
                d[..., 0] += gx
                d[..., 1] += gy
                self.disp[ay0:ay1, ax0:ax1] = d
                mxi = np.clip(np.rint(xs - d[..., 0]), 0, w - 1).astype(np.intp)
                myi = np.clip(np.rint(ys - d[..., 1]), 0, h - 1).astype(np.intp)
                self.pixels[ay0:ay1, ax0:ax1] = self.source[myi, mxi]
            touched = tip_a[sy0:sy1, sx0:sx1] > 5
            if not touched.any():
                continue
            cover = np.uint8(round(255 * min(1.0, stamp[4])))
            region = self.reached[ay0:ay1, ax0:ax1]
            region[touched] = np.maximum(region[touched], cover)
            self.laid_at[ay0:ay1, ax0:ax1][touched] = self.travel
            box = QRect(ax0 + ox, ay0 + oy, ax1 - ax0, ay1 - ay0)
            self._recent.append((self.travel, box))
            dirty = box if dirty is None else dirty.united(box)
        if dirty is not None:
            since = self.travel - self.fade - 2.0
            self._recent = [(t, b) for t, b in self._recent if t >= since]
            for _t, b in self._recent:
                dirty = dirty.united(b)
        return dirty

    def compose(self, rect):
        """As seen while pushing: with a backdrop, the pushed layer over it
        where the push went (opaque there: it shows what the layer's pushed
        paint left uncovered); without, the pushed pixels (DragStroke)."""
        if self.backdrop is None:
            return super().compose(rect)
        local = rect.translated(-self.rect.x(), -self.rect.y()).intersected(self.work.rect())
        if self._cl is not None:
            try:
                self._cl.fetch(local.x(), local.y(), local.x() + local.width(),
                               local.y() + local.height(), self.pixels, self.reached, self.laid_at)
            except Exception as e:
                self._cl_off(e)
        part = (slice(local.y(), local.y() + local.height()), slice(local.x(), local.x() + local.width()))
        top = self.pixels[part].astype(np.float32)
        under = self.backdrop[part].astype(np.float32)
        over = top + under * (1.0 - top[..., 3:4] / 255.0)
        over *= self.reached[part].astype(np.float32)[..., None] / 255.0
        return _array_image(np.clip(over + 0.5, 0, 255).astype(np.uint8))

    def compose_layer(self, rect):
        """The layer's own pushed paint in device *rect*, where the push went."""
        if self.paint is None:
            return super().compose(rect)
        local = rect.translated(-self.rect.x(), -self.rect.y()).intersected(self.work.rect())
        x0, y0, w, h = local.x(), local.y(), local.width(), local.height()
        d = self.disp[y0:y0 + h, x0:x0 + w]
        mx = np.arange(x0, x0 + w, dtype=np.float32)[None, :] - d[..., 0]
        my = np.arange(y0, y0 + h, dtype=np.float32)[:, None] - d[..., 1]
        if _cv2 is not None:
            moved = _cv2.remap(self.paint, mx, my, _cv2.INTER_LINEAR, borderMode=_cv2.BORDER_REPLICATE)
        else:
            full_h, full_w = self.paint.shape[:2]
            moved = self.paint[np.clip(np.rint(my), 0, full_h - 1).astype(np.intp),
                               np.clip(np.rint(mx), 0, full_w - 1).astype(np.intp)]
        a = self.reached[y0:y0 + h, x0:x0 + w].astype(np.uint16)[..., None]
        return _array_image(((moved.astype(np.uint16) * a + 127) // 255).astype(np.uint8))

    def cut(self, rect):
        """Where the push replaces the layer's paint, in device *rect*: an
        alpha mask (premultiplied ARGB, black)."""
        local = rect.translated(-self.rect.x(), -self.rect.y()).intersected(self.work.rect())
        part = (slice(local.y(), local.y() + local.height()), slice(local.x(), local.x() + local.width()))
        a = self.reached[part]
        out = np.zeros(a.shape + (4,), np.uint8)
        out[..., 3] = a
        return _array_image(out)


def _image_array(image):
    """A premultiplied ARGB32 QImage as its own (h, w, 4) BGRA uint8 array."""
    w, h, line = image.width(), image.height(), image.bytesPerLine()
    data = np.frombuffer(image.constBits(), np.uint8, count=line * h).reshape(h, line)
    return data[:, :w * 4].reshape(h, w, 4).copy()


def _array_image(array):
    """(h, w, 4) BGRA uint8 back to a premultiplied ARGB32 QImage (a copy)."""
    array = np.ascontiguousarray(array)
    h, w = array.shape[:2]
    return QImage(array.data, w, h, w * 4, QImage.Format_ARGB32_Premultiplied).copy()


def _stamps_box(stamps, view, scale):
    xs, ys = [], []
    for x, y, size, *_ in stamps:
        cx = view.m11() * x + view.m21() * y + view.dx()
        cy = view.m12() * x + view.m22() * y + view.dy()
        r = size * scale * 0.75     # half the diagonal of the longest side, about
        xs += (cx - r, cx + r)
        ys += (cy - r, cy + r)
    return QRect(math.floor(min(xs)), math.floor(min(ys)),
                 math.ceil(max(xs) - min(xs)) + 2, math.ceil(max(ys) - min(ys)) + 2)


class BristleStroke(FlatStroke):
    """🖌 The Hair brush as bristles: each hair its own thin line, not a
    picture of hairs dragged (FlatStroke) - that made hairs as thick as the
    tip's pixels (2-3 px), and turning the pen switched between cached turns
    of the picture in blocks along the stroke.

    As a real brush (HeavyPaint's / Rebelle's bristles, Krita's bristle
    engine):
    * pressure splays it - a light touch is narrow, and only the hairs in
      the middle reach the paper; pressing brings the outer ones down too,
      each fading in, not switched on;
    * every hair follows the brush with its own lag, so a turn sweeps the
      hairs round in curves instead of the whole row jumping;
    * Vary: hairs of uneven thickness and strength; Ragged: frayed starts
      and hairs that run dry at different lengths; Hair edge: soft hairs.

    The same buffers and compose() as FlatStroke (coverage the max within a
    stroke, the colour cut by it). Worked out from the stamps alone (their
    7th value the pen's pressure): laid live, again from a project, or for
    an export, the same hairs."""

    LEVELS = 48         # coverage steps (lines drawn in batches of one strength)
    PER_HAIR = 2        # a stamp's colour (2, c1 .. cn): each hair's own (picked up, see sample_from)
    # Hairs carrying a colour that changes as they go (Wet): each segment's
    # colour laid this much over what the hair laid just before, like wet
    # paint - laid whole, every stamp's step showed as a block along the hair.
    CARRY_MIX = 0.45
    carries = False

    def __init__(self, rect, source=None, buffers=True):
        if buffers:
            super().__init__(rect, source)
        else:
            self.rect, self.source = rect, source       # (the model alone: gpu_flat.GpuBristleStroke draws)
        self._hairs = None
        self._pos = None            # (n, 2) where each hair is
        self._travel = 0.0
        self._at = None             # the last stamp's place
        self._under = None          # Wet: what is under the stroke (device px, BGRA), see sample_from
        self._carry = None          # Wet: (n, 3) the colour each hair carries now
        self.recorded = []          # Wet: per stamp, each hair's colour - kept in the stroke

    def sample_from(self, image):
        """Wet: *image* - what is under the stroke, in this stroke's device
        pixels - is what the hairs pick colour up from."""
        img = image.convertToFormat(QImage.Format_ARGB32)
        h, w = img.height(), img.width()
        a = np.frombuffer(img.constBits(), np.uint8, count=img.sizeInBytes())
        self._under = a.reshape(h, img.bytesPerLine())[:, :w * 4].reshape(h, w, 4).copy()

    def stamped(self, stamps):
        """*stamps* with each hair's colour as it was laid (Wet): laid again
        - undo, a project, an export - the stroke keeps the colours it
        picked up, without the canvas under it."""
        if not self.recorded or len(self.recorded) != len(stamps):
            return stamps
        return [s[:5] + ((self.PER_HAIR,) + c,) + tuple(s[6:]) for s, c in zip(stamps, self.recorded)]

    @staticmethod
    def _make(params, n_hint=None):
        n = max(2, min(400, int(params.get('n', 48))))
        vary = max(0, min(100, int(params.get('v', 50)))) / 100.0
        ragged = max(0, min(100, int(params.get('r', 40)))) / 100.0
        soft = max(0, min(100, int(params.get('s', 30)))) / 100.0
        width = max(1, min(200, int(params.get('w', 8)))) / 1000.0     # ‰ of the brush size
        flat = max(0, min(100, int(params.get('f', 35)))) / 100.0
        shape = max(0, min(100, int(params.get('h', 0)))) / 100.0
        wet = max(0, min(100, int(params.get('x', 0)))) / 100.0
        rng = np.random.default_rng(7919 + int(params.get('k', 17)))
        # A bundle, not a row: the hairs spread evenly over an ellipse
        # (Vogel's sunflower spiral), Flatness squashing it into a row. A
        # row alone, turned along the stroke by the pen's lean, closed up
        # into a single line.
        i = np.arange(n) + 0.5
        rad = np.sqrt(i / n) * (1.0 + vary * 0.25 * (rng.random(n) - 0.5))
        turn = i * math.pi * (3.0 - math.sqrt(5.0)) + vary * 0.6 * (rng.random(n) - 0.5)
        u = 0.5 * rad * np.cos(turn)                                    # across the brush
        v = 0.5 * rad * np.sin(turn) * (1.0 - flat)                     # along it
        if flat >= 0.999:
            u = (np.arange(n) + 0.5) / n - 0.5 + (rng.random(n) - 0.5) * vary / n
        reach = np.clip(np.hypot(u, v / max(1e-3, 1.0 - flat)) * 2.0 if flat < 0.999
                        else np.abs(u) * 2.0, 0.0, 1.0)                 # 0 middle .. 1 edge
        return {
            'u': u,
            'v': v,
            'thick': width * (1.0 + vary * 0.9 * (rng.random(n) - 0.5)),
            'strength': 1.0 - vary * 0.5 * rng.random(n),
            # the pressure a hair needs to touch: the middle ones first
            'contact': np.clip(0.03 + 0.72 * reach ** 1.4 + ragged * 0.25 * (rng.random(n) - 0.4),
                               0.0, 0.95),
            'start': ragged * 0.6 * rng.random(n),                      # x size: frayed start
            'dry': (10.0 + 45.0 * rng.random(n)) * (1.7 - ragged),      # x size: the ink it holds
            'dry_floor': 1.0 - 0.85 * ragged,
            'lag': 0.35 + 0.45 * rng.random(n),                         # share of the way per stamp
            'soft': soft,
            # Bristle shape: each hair's tip a flat dab, turned at random -
            # a ribbon where it is dragged broadside, a thin line edgeways
            'long': 1.0 + 5.0 * shape * (0.4 + 0.6 * rng.random(n)),
            'axis': rng.random(n) * math.pi,
            # Wet: how readily each hair takes up the paint it passes
            'wet': wet * (0.45 + 0.55 * rng.random(n)),
        }

    @staticmethod
    def _smooth(edge0, edge1, x):
        t = np.clip((x - edge0) / (edge1 - edge0), 0.0, 1.0)
        return t * t * (3.0 - 2.0 * t)

    @staticmethod
    def _colours(rgb, u):
        """Each hair's colour: one for all, or across the hairs (streaks)."""
        if isinstance(rgb, int):
            return [rgb] * len(u)
        bands = [c for c in rgb[1:] if isinstance(c, int)] if rgb and rgb[0] >= 0 else []
        if not bands:
            mean = particles_mean(rgb) if rgb and rgb[0] < 0 else 0xFFFFFF
            return [mean] * len(u)
        return [bands[min(len(bands) - 1, int((v + 0.5) * len(bands)))] for v in u]

    def _hair_colours(self, rgb, u, pos, step, size, view):
        """Each hair's colour for this stamp. Laid again: as the stamp keeps
        it. Wet, live: the colour each hair carries - it takes up the paint
        under it as it moves (by its wetness and how far it went), and is
        topped up with the brush's own colour as far as it is not wet."""
        n = len(u)
        if isinstance(rgb, tuple) and rgb and rgb[0] == self.PER_HAIR and len(rgb) == n + 1:
            return list(rgb[1:])
        if isinstance(rgb, tuple) and rgb and rgb[0] < 0:
            # Colour jitter 'every speck (particles)': every hair its own
            # colour from them (HeavyPaint's per-point colour), the same hair
            # the same colour all along - dealt once, from the first stamp
            # (the stamps' sets change at random: each stamp anew flickered).
            # Their average, as before, gave the bristles one colour.
            dealt = self.__dict__.get('_dealt')
            if dealt is None:
                # Dealt across the brush, not at random: hairs side by side
                # (by their place across it) in runs of 1-3 of one colour. The
                # bundle's hairs trail one another on the same track - given
                # different colours they took turns on top, step by step: a
                # dashed ladder along the strands.
                colours = [c for c in rgb[1:] if isinstance(c, int)] or [0xFFFFFF]
                rng = np.random.default_rng(104729 + n)
                dealt = [0] * n
                order = np.argsort(np.round(np.asarray(u) * 4 * n))       # (across, a hair's breadth apart)
                j = 0
                while j < n:
                    colour = colours[int(rng.integers(0, len(colours)))]
                    run = int(rng.integers(1, 4))
                    for idx in order[j:j + run]:
                        dealt[int(idx)] = colour
                    j += run
                dealt = self._dealt = dealt
            base = list(dealt)
        else:
            base = self._colours(rgb, u)
        if self._under is None:
            return base
        hairs = self._hairs
        fresh = np.array([((c >> 16) & 255, (c >> 8) & 255, c & 255) for c in base], np.float64)
        if self._carry is None:
            self._carry = fresh.copy()
        if pos is not None and step > 0:
            h, w = self._under.shape[:2]
            dx = view.m11() * pos[:, 0] + view.m21() * pos[:, 1] + view.dx() - self.rect.x()
            dy = view.m12() * pos[:, 0] + view.m22() * pos[:, 1] + view.dy() - self.rect.y()
            xi = np.clip(np.rint(dx).astype(np.int64), 0, w - 1)
            yi = np.clip(np.rint(dy).astype(np.int64), 0, h - 1)
            px = self._under[yi, xi].astype(np.float64)              # B G R A
            seen = px[:, 3] / 255.0
            under = px[:, [2, 1, 0]]
            k = (1.0 - np.exp(-hairs['wet'] * step / (0.5 * size))) * seen
            self._carry += (under - self._carry) * k[:, None]
            reload = (1.0 - hairs['wet']) * (1.0 - math.exp(-step / (0.8 * size)))
            self._carry += (fresh - self._carry) * reload[:, None]
        c = np.clip(np.rint(self._carry), 0, 255).astype(np.int64)
        cols = [int((r << 16) | (g << 8) | b) for r, g, b in c]
        self.recorded.append(tuple(cols))
        return cols

    def segments(self, texture, stamps, view):
        """The bristles moved on over *stamps* (picture coords, *view* to
        device px): per stamp laid, (p0, p1, width, alpha, colours) - where
        each hair's line runs (n, 2 device px of this stroke's rect), its
        width (device px), how strongly it lays (0..1) and its colour
        (0xRRGGBB ints, n). All in arrays: no work hair by hair, so a brush
        of a couple of hundred hairs costs about what one of a dozen does.
        The CPU (add) and the graphics card (gpu_flat.GpuBristleStroke) draw
        the same segments."""
        params = getattr(texture, 'hair_params', None) or {}
        if self._hairs is None:
            self._hairs = self._make(params)
        hairs = self._hairs
        u, v, lag = hairs['u'], hairs['v'], hairs['lag']
        scale = math.sqrt(abs(view.determinant())) or 1.0
        m11, m12, m21, m22 = view.m11(), view.m12(), view.m21(), view.m22()
        ox, oy = view.dx() - self.rect.x(), view.dy() - self.rect.y()
        out = []
        self.seg_stamps = index = []        # (which stamp each of *out* is)
        for i_stamp, stamp in enumerate(stamps):
            x, y, size, angle, opacity, rgb = stamp[:6]
            pressure = stamp[6] if len(stamp) > 6 else 1.0
            if size * scale < 0.4:
                self._hair_colours(rgb, u, None, 0.0, size, view)      # (kept in step with the stamps)
                continue
            ax, ay = -math.sin(angle), math.cos(angle)                  # across the brush
            fx, fy = ay, -ax                                            # along it
            spread = size * (0.42 + 0.58 * max(0.0, min(1.0, pressure)))
            tx = x + (ax * u + fx * v) * spread
            ty = y + (ay * u + fy * v) * spread
            if self._pos is None:
                self._pos = np.stack([tx, ty], 1)
                self._at = (x, y)
                self._hair_colours(rgb, u, None, 0.0, size, view)
                continue
            step = math.hypot(x - self._at[0], y - self._at[1])
            self._travel += step
            self._at = (x, y)
            prev = self._pos
            new = np.stack([prev[:, 0] + (tx - prev[:, 0]) * lag, prev[:, 1] + (ty - prev[:, 1]) * lag], 1)
            self._pos = new
            colours = np.asarray(self._hair_colours(rgb, u, new, step, size, view), np.int64)
            if self._under is not None or (isinstance(rgb, tuple) and rgb and rgb[0] == self.PER_HAIR):
                self.carries = True         # each hair's own colour, changing as it goes
            touch = self._smooth(hairs['contact'] - 0.12, hairs['contact'] + 0.05, pressure)
            started = self._smooth(hairs['start'] * size, hairs['start'] * size + 0.15 * size, self._travel)
            ink = np.maximum(hairs['dry_floor'],
                             1.0 - np.clip((self._travel - hairs['dry'] * size) / (3.0 * size), 0.0, 1.0))
            alpha = np.clip(opacity * hairs['strength'] * touch * started * ink, 0.0, 1.0)
            # each hair's trace: its flat tip's width across the way it moves
            mx, my = new[:, 0] - prev[:, 0], new[:, 1] - prev[:, 1]
            ml = np.hypot(mx, my)
            ml[ml < 1e-9] = 1.0
            nx, ny = -my / ml, mx / ml
            axis = hairs['axis'] + angle
            along = np.abs(np.cos(axis) * nx + np.sin(axis) * ny)
            across = np.sqrt(np.clip(1.0 - along * along, 0.0, 1.0))
            width = hairs['thick'] * size * scale * np.hypot(hairs['long'] * along, across)
            thin = width < 0.8                  # thinner than a pixel: fainter instead
            alpha = np.where(thin, alpha * width / 0.8, alpha)
            width = np.where(thin, 0.8, width)
            p0 = np.stack([m11 * prev[:, 0] + m21 * prev[:, 1] + ox, m12 * prev[:, 0] + m22 * prev[:, 1] + oy], 1)
            p1 = np.stack([m11 * new[:, 0] + m21 * new[:, 1] + ox, m12 * new[:, 0] + m22 * new[:, 1] + oy], 1)
            out.append((p0, p1, width, alpha, colours))
            index.append(i_stamp)
        return out

    def add(self, texture, stamps, view):
        """Lay *stamps* (see segments) into the buffers with QPainter: lines
        batched by strength, width and colour. While the stroke has one
        colour every line goes in one batch (their order does not matter);
        once colours differ - Wet, streaks, jitter - each stamp is a batch of
        its own, so crossing hairs lie in the order they were laid however
        the stamps come in (live a pen event at a time, again all at once)."""
        segs = self.segments(texture, stamps, view)
        if not segs:
            return None
        hairs = self._hairs
        batches = []
        buckets = {}
        x0 = y0 = math.inf
        x1 = y1 = -math.inf
        for p0, p1, width, alpha, colours in segs:
            if self.carries:
                # (in 32 steps a channel: the hairs share batches - on the CPU
                # one line a draw is what costs)
                colours = (colours & 0xF8F8F8) | 0x040404
            if not self.__dict__.get('_many'):
                one = self.__dict__.setdefault('_one', int(colours[0]))
                if (colours != one).any():
                    self._many = True
            if self.__dict__.get('_many') and buckets:
                batches.append(buckets)
                buckets = {}
            live = np.flatnonzero(alpha > 0.01)
            if not len(live):
                continue
            levels = np.maximum(1, np.rint(alpha[live] * self.LEVELS)).astype(np.int64)
            steps = np.rint(width[live] * 4) / 4
            for j, i in enumerate(live):
                key = (int(levels[j]), float(steps[j]), int(colours[i]))
                buckets.setdefault(key, []).append(
                    QLineF(float(p0[i, 0]), float(p0[i, 1]), float(p1[i, 0]), float(p1[i, 1])))
            x0 = min(x0, float(p0[live, 0].min()), float(p1[live, 0].min()))
            x1 = max(x1, float(p0[live, 0].max()), float(p1[live, 0].max()))
            y0 = min(y0, float(p0[live, 1].min()), float(p1[live, 1].min()))
            y1 = max(y1, float(p0[live, 1].max()), float(p1[live, 1].max()))
        if buckets:
            batches.append(buckets)
        if not batches:
            return None
        soft = hairs['soft']
        mp, cp = QPainter(self.mask), QPainter(self.colour)
        mp.setCompositionMode(QPainter.CompositionMode_Lighten)
        for p in (mp, cp):
            p.setRenderHint(QPainter.Antialiasing, True)
        widest = 0.0
        for (level, w, rgb), lines in (item for batch in batches for item in batch.items()):
            grey = int(round(255 * level / self.LEVELS))
            colour = QColor((rgb >> 16) & 255, (rgb >> 8) & 255, rgb & 255)
            if self.carries:
                colour.setAlphaF(self.CARRY_MIX)
            if soft > 0:
                # a soft hair: a faint wider halo under the core
                halo = w * (1.0 + 2.0 * soft)
                widest = max(widest, halo)
                g = int(grey * 0.35)
                mp.setPen(QPen(QColor(g, g, g), halo, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
                mp.drawLines(lines)
                cp.setPen(QPen(colour, halo, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
                cp.drawLines(lines)
            widest = max(widest, w)
            mp.setPen(QPen(QColor(grey, grey, grey), w, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            mp.drawLines(lines)
            cp.setPen(QPen(colour, w, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            cp.drawLines(lines)
        mp.end()
        cp.end()
        pad = int(math.ceil(widest)) + 2
        box = QRect(int(math.floor(x0)) - pad, int(math.floor(y0)) - pad,
                    int(math.ceil(x1 - x0)) + 2 * pad, int(math.ceil(y1 - y0)) + 2 * pad)
        return box.translated(self.rect.x(), self.rect.y()).intersected(self.rect)


_GPU_BRISTLES_OFF = False


def _bristles_on_gpu(texture, stamps, view, box):
    """🖌 A bristle stroke laid again (undo, a zoom, a project, an export)
    on the graphics card - the same segments as the CPU's, a couple of
    hundred hairs in ms, not seconds. The image of *box* (device px), or
    None to lay it on the CPU: no card, too big, or a live stroke using its
    buffers right now."""
    global _GPU_BRISTLES_OFF
    if _GPU_BRISTLES_OFF or box.width() > 8192 or box.height() > 8192:
        return None
    try:
        from random_image_viewer.drawing import gpu_paint
        from random_image_viewer.drawing.gpu_flat import GpuBristleStroke, flat_kit
        gpu = gpu_paint.shared()
        if getattr(flat_kit(gpu), 'busy', None) is not None:
            return None
        layer = gpu_paint.GpuLayer(box.width(), box.height(), precision='byte')
    except Exception as e:
        print(f"🖌 Bristles laid on the CPU: {e}")
        _GPU_BRISTLES_OFF = True
        return None
    try:
        model = BristleStroke(QRect(box.x(), box.y(), box.width(), box.height()), buffers=False)
        g = GpuBristleStroke(layer, model)
        try:
            segs = _bristle_segments_again(model, texture, stamps, view)
            if segs is None:
                g.add(texture, stamps, view)
                _bristle_segments_keep(model, texture, stamps, view)
            else:
                g.add_segments(segs)
            g.preview()
            return g.read_overlay(QRect(0, 0, box.width(), box.height()))
        finally:
            g.cancel()
    except Exception as e:
        print(f"🖌 Bristles laid on the CPU: {e}")
        return None
    finally:
        layer.release()


# ⛰ The last bristle stroke's segments laid again, kept: the thickness map
# lays each stroke twice (the relief under it levelled, then its own) - the
# same hairs going the same way, only stronger or weaker stamp by stamp.
# Working out where the hairs go was most of each lay (~40 ms of ~45); the
# second lay takes the first's, each hair's strength scaled by its stamp's
# (linear: a hair's strength is its stamp's opacity times things of 1 or
# less - never clipped).
_BRISTLE_KEPT = None


def _bristle_key(model, texture, stamps, view):
    return (id(texture), getattr(texture, 'path', None),
            (view.m11(), view.m12(), view.m21(), view.m22(), view.dx(), view.dy()),
            (model.rect.x(), model.rect.y(), model.rect.width(), model.rect.height()),
            tuple(tuple(st[:4]) + tuple(st[5:]) for st in stamps))


def _bristle_segments_keep(model, texture, stamps, view):
    global _BRISTLE_KEPT
    segs = getattr(model, 'last_segments', None)
    if not segs or model.carries:
        _BRISTLE_KEPT = None
        return
    _BRISTLE_KEPT = (_bristle_key(model, texture, stamps, view), segs, list(model.seg_stamps),
                     [float(st[4]) for st in stamps], model._hairs)


def _bristle_segments_again(model, texture, stamps, view):
    """The kept segments for *stamps* (those, at other opacities) scaled
    to theirs - or None: not the same stroke."""
    kept = _BRISTLE_KEPT
    if kept is None:
        return None
    key, segs, index, was, hairs = kept
    if len(stamps) != len(was) or key != _bristle_key(model, texture, stamps, view):
        return None
    now = [float(st[4]) for st in stamps]
    out = []
    for (p0, p1, width, alpha, colours), i in zip(segs, index):
        if was[i] <= 0.0:
            if now[i] > 0.0:
                return None
            out.append((p0, p1, width, alpha, colours))
        else:
            out.append((p0, p1, width, alpha * (now[i] / was[i]), colours))
    model._hairs = hairs
    return out


def is_bristles(texture):
    """The Hair brush laid as bristles (BristleStroke)."""
    return bool(getattr(texture, 'bristle_engine', False))


def flat_stroke(texture, rect, source=None):
    """The flat stroke for *texture*: bristles for the Hair brush, else
    the tip dragged (FlatStroke)."""
    return BristleStroke(rect, source) if is_bristles(texture) else FlatStroke(rect, source)


def paint_stamps(painter, texture, stamps, to_display, clip=None, flat=False, history=None):
    """Draw *stamps* (picture coords) through *to_display*, a QTransform from
    picture to the painter's device. *clip*: (w, h) of the device, to skip
    stamps that land off it. *flat*: as a FlatStroke, without build-up.
    *history*: (picture, tint) - paint the picture back instead (flat).

    A flat stroke returns (image, top-left, whole): what it laid, and whether
    that is the whole stroke (none of it cut off at the device's edge) - so
    a caller can keep it and reuse it while only the view's offset changes."""
    if texture is None or not stamps:
        return None
    if getattr(texture, 'procedural', False) and not flat and history is None:
        _paint_spray(painter, texture, stamps, to_display, clip)
        return None
    base = painter.worldTransform()
    if flat or history is not None:
        view = to_display * base
        device = painter.device()
        full = _stamps_box(stamps, view, math.sqrt(abs(view.determinant())))
        box = full.intersected(QRect(0, 0, device.width(), device.height()))
        if box.isEmpty():
            return None
        source = (history[0], view, history[1]) if history is not None else None
        image = (_bristles_on_gpu(texture, stamps, view, box)
                 if source is None and is_bristles(texture) else None)
        if image is None:
            stroke = flat_stroke(texture, box, source)
            stroke.add(texture, stamps, view)
            image = stroke.compose(box)
        painter.save()
        painter.resetTransform()
        painter.drawImage(box.topLeft(), image)
        painter.restore()
        return image, box.topLeft(), box == full
    scale = math.sqrt(abs(to_display.determinant()))
    view = to_display * base
    # The stamps build up in a buffer of their own at 16 bits a channel, and
    # it goes on the device once: laid one by one on 8 bits, every faint
    # stamp was rounded and a soft or see-through build-up came out in
    # bands. (The same blend: stamps over stamps, then over the device.)
    device = painter.device()
    target = painter
    buffer = None
    box = None
    try:
        dw, dh = device.width(), device.height()
    except Exception:
        dw = dh = 0
    if dw > 0 and dh > 0:
        box = _stamps_box(stamps, view, math.sqrt(abs(view.determinant())) or 1.0).intersected(
            QRect(0, 0, dw, dh))
        if box.isEmpty():
            return None
        if box.width() * box.height() <= 40_000_000:
            buffer = QImage(box.size(), QImage.Format_RGBA64_Premultiplied)
            buffer.fill(Qt.transparent)
            painter = QPainter(buffer)
            view = view * QTransform.fromTranslate(-box.x(), -box.y())
    painter.save()
    painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
    for x, y, size, angle, opacity, rgb, *_pressure in stamps:
        on_screen = size * scale
        if on_screen < 0.4 or opacity <= 0:
            continue
        if clip is not None:
            cx = to_display.m11() * x + to_display.m21() * y + to_display.dx()
            cy = to_display.m12() * x + to_display.m22() * y + to_display.dy()
            if cx < -on_screen or cy < -on_screen or cx > clip[0] + on_screen or cy > clip[1] + on_screen:
                continue
        level = texture.level(on_screen)
        image = texture.coloured(level, rgb)
        k = size / max(image.width(), image.height())
        m = QTransform()
        m.translate(x, y)
        m.rotateRadians(angle)
        m.scale(k, k)
        m.translate(-image.width() / 2.0, -image.height() / 2.0)
        painter.setTransform(m * view)
        painter.setOpacity(opacity)
        painter.drawImage(0, 0, image)
    painter.restore()
    if buffer is not None:
        painter.end()
        target.save()
        target.resetTransform()
        target.drawImage(box.topLeft(), buffer)
        target.restore()


def _paint_spray(painter, texture, stamps, to_display, clip=None):
    """Spray stamps, procedurally: each stamp a cloud of round specks (thick
    in the middle, thinning out), drawn as dots on the device - sharp at any
    size or zoom, no texture scaled up. Every stamp seeds its own random
    numbers from its position and size, so it lays the same specks each
    time. Specks smaller than a pixel go down as a pixel with their share of
    ink, so a zoomed-out spray keeps its weight. Colour tuples (particles,
    streaks jitter) give every speck one of their colours."""
    import struct
    import zlib
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QPen, QPolygonF
    view = to_display * painter.worldTransform()
    device = painter.device()
    if clip is not None:
        w, h = clip
    else:
        dpr = device.devicePixelRatioF() if hasattr(device, 'devicePixelRatioF') else 1.0
        w, h = device.width() / max(1.0, dpr), device.height() / max(1.0, dpr)
    scale = math.sqrt(abs(view.determinant())) or 1.0
    n = int(getattr(texture, 'specks', 150))
    rel = float(getattr(texture, 'speck', 0.02))
    palette = {}
    parts = []
    for x, y, size, angle, opacity, rgb, *_pressure in stamps:
        if opacity <= 0:
            continue
        radius = size * 0.5 * scale
        cx = view.m11() * x + view.m21() * y + view.dx()
        cy = view.m12() * x + view.m22() * y + view.dy()
        if cx + radius < -4 or cy + radius < -4 or cx - radius > w + 4 or cy - radius > h + 4:
            continue
        rng = np.random.default_rng(zlib.crc32(struct.pack('<4d', x, y, size, angle)))
        r = np.minimum(0.98, np.abs(rng.normal(0.0, 0.45, n)))
        turn = rng.uniform(0.0, 2.0 * math.pi, n)
        d = rel * size * scale * rng.uniform(0.45, 1.0, n) * (1.0 - 0.35 * r)
        alpha = rng.uniform(0.55, 1.0, n) * opacity
        colours = list(rgb[1:]) if isinstance(rgb, tuple) else [rgb]
        pick = rng.integers(0, len(colours), n)
        index = np.array([palette.setdefault(c, len(palette)) for c in colours])[pick]
        parts.append((cx + np.cos(turn) * r * radius, cy + np.sin(turn) * r * radius, d, alpha, index))
    if not parts:
        return
    px, py, d, alpha, index = (np.concatenate(k) for k in zip(*parts))
    keep = (px > -d) & (py > -d) & (px < w + d) & (py < h + d)
    px, py, d, alpha, index = px[keep], py[keep], d[keep], alpha[keep], index[keep]
    if not len(px):
        return
    tiny = d < 1.0
    alpha[tiny] *= d[tiny] * d[tiny]        # a pixel with the speck's share of ink
    d = np.maximum(d, 1.0)
    size_bin = np.round(np.log2(d) * 4).astype(np.int64)            # ~19 % steps
    alpha_bin = np.clip(np.round(alpha * 8), 0, 8).astype(np.int64)
    drawn = alpha_bin > 0
    px, py, size_bin, alpha_bin, index = (a[drawn] for a in (px, py, size_bin, alpha_bin, index))
    if not len(px):
        return
    key = (index * 4096 + (size_bin + 64)) * 16 + alpha_bin
    order = np.argsort(key, kind='stable')
    key, px, py = key[order], px[order], py[order]
    starts = np.flatnonzero(np.r_[True, key[1:] != key[:-1]])
    ends = np.r_[starts[1:], len(key)]
    colours = {i: c for c, i in palette.items()}
    painter.save()
    painter.resetTransform()
    painter.setRenderHint(QPainter.Antialiasing, True)
    xs, ys = px.tolist(), py.tolist()
    for a, b in zip(starts.tolist(), ends.tolist()):
        k = int(key[a])
        c = colours[k // (4096 * 16)]
        pen = QPen(QColor((c >> 16) & 255, (c >> 8) & 255, c & 255),
                   2.0 ** ((k // 16 % 4096 - 64) / 4.0), Qt.SolidLine, Qt.RoundCap)
        painter.setPen(pen)
        painter.setOpacity((k % 16) / 8.0)
        painter.drawPoints(QPolygonF([QPointF(xs[i], ys[i]) for i in range(a, b)]))
    painter.restore()
