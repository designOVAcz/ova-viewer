"""🎨 A painter's colour model on OKLab: hue, saturation, lightness.

Lightness is OKLab's L (0 black .. 1 white), so equal steps look equal and
every hue at one lightness looks as light as the others (HSV's yellow and
blue at "100 %" are nothing alike). Saturation is relative: 1 is the most
intense colour the screen (sRGB) can show at that hue and lightness, 0 the
grey of that lightness - so the whole range is always usable, and no colour
falls outside what can be shown. Hue is OKLab's angle, 0..1 round the wheel.

Used by the colour picker (widgets/color_picker_popup.py): a field of hue
across, lightness down, at one saturation, and a saturation slider.
"""

import math

import numpy as np

from .brushes import _linear_to_srgb, _oklab_to_linear, _rgb_to_oklab

C_TOP = 0.37        # past the most chroma sRGB holds anywhere (~0.32)


def max_chroma(L, h):
    """The most OKLab chroma that fits sRGB at lightness *L* and hue angle
    *h* (radians) - arrays of the same shape, or numbers."""
    L = np.asarray(L, np.float64)
    h = np.asarray(h, np.float64)
    ca, sa = np.cos(h), np.sin(h)
    shape = np.broadcast(L, h).shape

    def fits(c):
        # (a hair's tolerance, clipped away when drawn: on the way out to pure
        # blue red dips to -0.0007 and comes back - strict, the bluest blues
        # were out of reach)
        lin = _oklab_to_linear(np.stack(np.broadcast_arrays(L, c * ca, c * sa), -1))
        return np.all((lin >= -1.5e-3) & (lin <= 1 + 1.5e-3), -1)

    # Not a plain bisection from grey: in OKLab the sRGB slice is not convex
    # round blue - going out from grey it leaves the gamut and comes back in
    # (pure blue lies past a dent), so the first edge is not the last. Steps
    # down from the top find the outermost colour that fits, then halvings
    # pin it down between that step and the one above.
    steps = 40
    lo = np.zeros(shape)
    found = np.zeros(shape, bool)
    for i in range(steps, 0, -1):
        c = C_TOP * i / steps
        ok = fits(np.full(shape, c)) & ~found
        lo = np.where(ok, c, lo)
        found |= ok
    hi = np.minimum(lo + C_TOP / steps, C_TOP)
    for _ in range(14):
        mid = (lo + hi) / 2
        ok = fits(mid)
        lo = np.where(ok, mid, lo)
        hi = np.where(ok, hi, mid)
    return lo


def to_rgb(h, s, l, cmax=None):
    """(..., 3) sRGB 0..1 from hue *h* (0..1), saturation *s*, lightness *l*
    (0..1). *cmax*: max_chroma for (l, h) when already known (the picker's
    field keeps it, as only the saturation changes while it is dragged)."""
    h = np.asarray(h, np.float64)
    l = np.clip(np.asarray(l, np.float64), 0.0, 1.0)
    angle = h * 2 * math.pi
    if cmax is None:
        cmax = max_chroma(l, angle)
    C = np.clip(np.asarray(s, np.float64), 0.0, 1.0) * cmax
    lab = np.stack(np.broadcast_arrays(l, C * np.cos(angle), C * np.sin(angle)), -1)
    return _linear_to_srgb(_oklab_to_linear(lab))


def from_rgb(r, g, b):
    """(h, s, l) of an sRGB colour (0..1 each). A grey's hue means nothing:
    it comes back as None (keep the one you had)."""
    L, a, bb = (float(v) for v in _rgb_to_oklab(np.array([r, g, b], np.float64)))
    C = math.hypot(a, bb)
    if C < 1e-4:
        return None, 0.0, min(1.0, max(0.0, L))
    angle = math.atan2(bb, a)
    cmax = float(max_chroma(L, angle))
    s = 1.0 if cmax <= 1e-6 else min(1.0, C / cmax)
    return (angle / (2 * math.pi)) % 1.0, s, min(1.0, max(0.0, L))
