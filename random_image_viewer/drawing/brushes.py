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

import numpy as np
from PySide6.QtCore import Qt, QRect, QRectF
from PySide6.QtGui import QColor, QImage, QPainter, QTransform

from . import pigment

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
    'jitter_per_stamp': False,  # True: every stamp its own colour; 'streaks':
                                #   every streak across the tip its own colour;
                                #   'particles': every speck its own colour
    'scatter': 0,               # %: stamps thrown off the stroke, up to this share of their size
    'size_jitter': 0,           # %: stamps up to this much smaller, at random
    'flat': True,               # stamps don't build up within a stroke (see FlatStroke)
    'mode': 'paint',            # one of MODES
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
}

MODES = ('paint', 'blend', 'smudge', 'history', 'pick', 'erase')
# 'erase': the stroke - tip, size, pressure, opacity - rubs paint out of the
# layer it is drawn on instead of laying any (see main_window: Stroke.erase)
# Modes that blend like Blend ('pick': Blend with the colour picked under the
# pen where the stroke starts, as the eyedropper would - see main_window)
BLENDING = ('blend', 'pick')
# Each mode keeps its own slider (Mix / Carry / Tint): what suits one makes
# no sense in another - Carry 100 % made Blend invisible.
MODE_MIX = {'paint': 50, 'blend': 50, 'smudge': 70, 'history': 0, 'pick': 50, 'erase': 0}
BLEND_SHOW_MAX = 0.65   # Blend at Mix 100 %: at most this much of what is under shows through
OPACITY_STEPS = 32      # flat strokes: stamp opacities are rounded to these
ROUND = ':round'        # the built-in soft round tip, for Round in Blend / Smudge
BRISTLES = ':bristles'  # the built-in bristle tip (a row of hairs)
DUST = ':dust'          # the built-in airbrush / particle tip (a cloud of specks)
SPRAY = ':spray'        # the procedural spray: specks drawn as dots, sharp at any size
RAKE = ':rake'          # a rake: tines in a row, dragged into parallel lines (see rake_path)
SPRAY_SPREAD_MAX = 20
PARTICLE_COLOURS = 16   # 'particles' jitter: colours a stamp's specks are spread over
PARTICLE_SETS = 16      # ... and the different colourings a stroke picks from
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


def _mix(a, b, t):
    """0xRRGGBB colours *a* and *b* mixed, *t* of the way to *b*."""
    out = 0
    for shift in (16, 8, 0):
        ca, cb = (a >> shift) & 255, (b >> shift) & 255
        out |= round(ca + (cb - ca) * t) << shift
    return out


def jitter_color(color, settings, rng=random):
    """*color* (a QColor) shifted at random by the Hue / Sat / Value ±
    settings, its alpha kept: one jittered colour, as a line or shape gets."""
    hj, sj, vj = (settings['hue_jitter'] / 100.0, settings['sat_jitter'] / 100.0,
                  settings['val_jitter'] / 100.0)
    out = QColor(color)
    if not (hj or sj or vj):
        return out
    h, s, v = colorsys.rgb_to_hsv(color.redF(), color.greenF(), color.blueF())
    h = (h + rng.uniform(-0.5, 0.5) * hj) % 1.0
    s = min(1.0, max(0.0, s + rng.uniform(-1, 1) * sj))
    v = min(1.0, max(0.0, v + rng.uniform(-1, 1) * vj))
    r, g, b = colorsys.hsv_to_rgb(h, s, v)
    out.setRgbF(r, g, b, color.alphaF())
    return out


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
        rgb = (keep,) + tuple(rgb[1:])      # banded along the dragged side, as the body
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
            ink - the stroke's end, the tip's own ragged front.
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

        def image(values2d):
            out = np.ascontiguousarray(values2d.clip(0, 255).astype(np.uint8))
            px = np.repeat(out[..., None], 4, axis=-1)
            hh, ww = out.shape
            return QImage(np.ascontiguousarray(px).tobytes(), ww, hh, ww * 4,
                          QImage.Format_ARGB32_Premultiplied).copy()
        rows = np.where(has, v, 0)[:, None]
        front = np.maximum(alpha, np.where((cols >= lead) & has[:, None], rows, 0))
        back = np.maximum(alpha, np.where((cols <= lead) & has[:, None], rows, 0))
        frame = {'w': fw, 'h': fh, 'profile': image(v[:, None]),
                 'front': image(front), 'back': image(back)}
        self._tints[key] = frame
        return frame

    def frame_variant(self, level, turn, part, step=None, rgb=None):
        """A motion_frame() mask as grey at opacity *step*, or coloured *rgb*
        (one colour, or streaks - banded across the motion)."""
        frame = self.motion_frame(level, turn)
        turn = int(round(turn / self.FRAME_STEP) * self.FRAME_STEP) % 360
        source = frame[part]
        if rgb is None:
            key = ('mf-grey', level, turn, part, step)
            image = self._tints.get(key)
            if image is None:
                image = self._variant('grey', source, opacity=step / OPACITY_STEPS)
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
        rgb = (1,) + tuple(rgb[1:])         # bands run along the motion
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
        if self.settings['jitter_per_stamp'] == 'particles' and len(set(self._palette)) > 1:
            self._particles = [(-1 - i,) + tuple(self._rng.choice(self._palette)
                                                 for _ in range(PARTICLE_COLOURS))
                               for i in range(PARTICLE_SETS)]
        self._last = None       # (x, y, pressure) of the previous point
        self._pending = None    # first point, waiting for a real pressure
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
        h, sat, val = colorsys.rgb_to_hsv(color.redF(), color.greenF(), color.blueF())
        rng = self._rng
        palette = []
        for _ in range(PALETTE_SIZE):
            nh = (h + rng.uniform(-0.5, 0.5) * hj) % 1.0
            ns = min(1.0, max(0.0, sat + rng.uniform(-1, 1) * sj))
            nv = min(1.0, max(0.0, val + rng.uniform(-1, 1) * vj))
            r, g, b = colorsys.hsv_to_rgb(nh, ns, nv)
            palette.append((round(r * 255) << 16) | (round(g * 255) << 8) | round(b * 255))
        return palette

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
        degrees, or None. Returns the stamps it made."""
        if not self._pressure_on:
            pressure = 1.0
        if tilt is not None and math.hypot(*tilt) >= TILT_MIN:
            lean = self._picture_direction(tilt[0], tilt[1])
            if lean is not None:
                self._tilt = self._blend(self._tilt, lean, 0.35)
        if pressure is None:
            self._pending = (x, y)
            return []
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
                if self._streaks is not None and self._sampler is not None:
                    # Streaks picking up colour change it stamp by stamp:
                    # close stamps, so it flows instead of stepping in bands.
                    step = min(step, self._stamp_size(p) * 0.06)
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
        """A tap that never moved still leaves one stamp, at *pressure*."""
        if self._pending is not None:
            self.add(*self._pending, pressure=pressure)
        return self.stamps

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
        self.head = None

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
            reach = f_frame['w'] * first[2] / tip_long / 2
            self._reach = (first[0] + d[0] * reach, first[1] + d[1] * reach)
            self._width = f_frame['h'] * first[2] / tip_long
            self.head = (first, self._turn(first, d), d)
            old_dir = d
        tx, ty = stamp[0], stamp[1]
        rx, ry = self._reach
        vx, vy = tx - rx, ty - ry
        far = math.hypot(vx, vy)
        if far >= 0.25 and vx * d[0] + vy * d[1] < self.REVERSE_COS * far:
            # Turned back (or very sharply): the eased direction still points
            # the old way, and a piece from where the body got to would be
            # sheared across it into a long thin spike. Start afresh here
            # instead, with the tip's front, as a brush pulled back does.
            self._dir = step
            frame = texture.motion_frame(level, self._turn(stamp, step))
            self._reach = (tx, ty)
            self._width = frame['h'] * k
            self.head = (stamp, self._turn(stamp, step), step)
            return []
        if (tx - rx) * d[0] + (ty - ry) * d[1] < 0.25:
            return []
        self._reach = (tx, ty)
        span = math.hypot(tx - rx, ty - ry)
        was = self._width
        width = min(max(width, was - self.FLARE * span), was + self.FLARE * span)
        self._width = width
        turned = abs(math.atan2(old_dir[0] * d[1] - old_dir[1] * d[0],
                                old_dir[0] * d[0] + old_dir[1] * d[1]))
        back = 1.0 / max(scale, 1e-6) + width / 2 * math.sin(min(turned, math.pi / 4))
        fx, fy = (tx - rx) / span, (ty - ry) / span
        ax, ay = -d[1], d[0]                # across the motion
        pieces = max(1, min(32, math.ceil(span * scale / 6.0)))
        out = []
        for i in range(pieces):
            a, b = i / pieces, (i + 1) / pieces
            wd = was + (width - was) * b
            sx, sy = rx + (tx - rx) * a - fx * back, ry + (ty - ry) * a - fy * back
            ex_, ey_ = rx + (tx - rx) * b, ry + (ty - ry) * b
            m = QTransform(ex_ - sx, ey_ - sy, ax * wd / frame['h'], ay * wd / frame['h'],
                           sx - ax * wd / 2, sy - ay * wd / 2)
            out.append((m * view, turn))
        return out

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

        def lay(m, grey, colour):
            mp.setTransform(m * offset)
            mp.drawImage(0, 0, grey)
            cp.setTransform(m * offset)
            cp.drawImage(0, 0, colour)
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
            is_first = self._first is None
            pieces = self._body(texture, level, stamp, tip_long, view, scale)
            if is_first:
                lay(_stamp_matrix(stamp, plain, view), plain, texture.coloured(level, stamp[5]))
                continue
            if self.head is not None:
                first, turn, d = self.head
                frame = texture.motion_frame(level, turn)
                lay(self._frame_matrix(first, frame, d, tip_long, view),
                    texture.frame_variant(level, turn, 'front', step=step),
                    texture.frame_variant(level, turn, 'front', rgb=first[5]))
            for m, turn in pieces:
                lay(m, texture.frame_variant(level, turn, 'profile', step=step),
                    texture.frame_variant(level, turn, 'profile', rgb=stamp[5]))
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
        tip_m = _stamp_matrix(stamp, plain, QTransform()) * to_local
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
            p.setTransform(tip_m)
        mp.drawImage(0, 0, plain)                    # the tip itself ...
        cp.drawImage(0, 0, texture.coloured(level, stamp[5]))
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
        if self._tail is not None:
            self._draw_tail(mask, colour, local)
        # The colour at full strength (un-premultiplied, then made opaque), so
        # a soft edge keeps its colour instead of darkening; the coverage
        # alone decides how strong it is.
        out = (colour.convertToFormat(QImage.Format_ARGB32)
               .convertToFormat(QImage.Format_RGB32)
               .convertToFormat(QImage.Format_ARGB32_Premultiplied))
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
        stroke = FlatStroke(box, source)
        stroke.add(texture, stamps, view)
        image = stroke.compose(box)
        painter.save()
        painter.resetTransform()
        painter.drawImage(box.topLeft(), image)
        painter.restore()
        return image, box.topLeft(), box == full
    scale = math.sqrt(abs(to_display.determinant()))
    painter.save()
    painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
    view = to_display * base
    for x, y, size, angle, opacity, rgb in stamps:
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
    for x, y, size, angle, opacity, rgb in stamps:
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
