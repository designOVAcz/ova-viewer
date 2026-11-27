"""🎭 Colour mask: a selection made from colours, for taking a background away.

A click picks the colour there (a few pixels round it, averaged, so noise does
not decide). Every pixel within *threshold* of it (CIELAB distance, so a
threshold means about the same in dark and light colours) is in the mask, with
a soft edge as wide as a third of the threshold - hair and blurred edges come
out half in, not as stairs. Contiguous: only what touches the click.

Several colours make one mask: each adds to it (+) or takes from it (-), in the
order picked. Then for the whole mask: invert, grow / shrink, feather.

All of it is numpy / OpenCV on an image's pixels; the viewer decides which
image (the picture, a layer) and at what size.
"""

import cv2
import numpy as np
from PySide6.QtGui import QImage

SOFT_SHARE = 0.35       # the soft edge: this share of the threshold ...
SOFT_MIN = 1.5          # ... and at least this (Lab units)
EMPTY_ALPHA = 8         # under this alpha a pixel counts as empty (a layer's)


def to_arrays(image):
    """(lab float32 HxWx3, alpha uint8 HxW) of a QImage. Lab in its own
    units (L 0..100, a / b about -128..127)."""
    image = image.convertToFormat(QImage.Format_ARGB32)       # straight alpha
    w, h = image.width(), image.height()
    buf = np.frombuffer(image.constBits(), np.uint8, count=image.sizeInBytes())
    bgra = buf.reshape(h, image.bytesPerLine() // 4, 4)[:, :w]
    lab = cv2.cvtColor(np.ascontiguousarray(bgra[..., :3]), cv2.COLOR_BGR2Lab).astype(np.float32)
    lab[..., 0] *= 100.0 / 255.0
    lab[..., 1:] -= 128.0
    return lab, np.ascontiguousarray(bgra[..., 3])


def seed_colour(lab, alpha, x, y, radius=2):
    """The colour picked at pixel (x, y): Lab averaged over a small patch,
    and whether it is an empty (see-through) spot."""
    h, w = alpha.shape
    x, y = int(min(max(x, 0), w - 1)), int(min(max(y, 0), h - 1))
    y0, y1, x0, x1 = max(0, y - radius), min(h, y + radius + 1), max(0, x - radius), min(w, x + radius + 1)
    empty = alpha[y, x] < EMPTY_ALPHA
    patch = lab[y0:y1, x0:x1].reshape(-1, 3)
    solid = alpha[y0:y1, x0:x1].reshape(-1) >= EMPTY_ALPHA
    if not empty and solid.any():
        patch = patch[solid]
    return patch.mean(axis=0), bool(empty)


def lab_to_rgb(lab):
    """A Lab colour (as seed_colour gives) as an 8-bit (r, g, b): for its chip."""
    px = np.array([[[lab[0] * 255.0 / 100.0, lab[1] + 128.0, lab[2] + 128.0]]], np.float32)
    px = np.clip(px, 0, 255).astype(np.uint8)
    b, g, r = cv2.cvtColor(px, cv2.COLOR_Lab2BGR)[0, 0]
    return int(r), int(g), int(b)


def similarity(lab, alpha, x, y, threshold, contiguous, radius=2):
    """How much each pixel is the colour at (x, y): float32 0..1."""
    ref, empty = seed_colour(lab, alpha, x, y, radius)
    if empty:
        # an empty spot on a layer: the emptiness round it, not a colour
        m = (alpha < EMPTY_ALPHA).astype(np.float32)
    else:
        t = float(threshold)
        soft = max(SOFT_MIN, t * SOFT_SHARE)
        d = lab - ref
        d = np.sqrt(np.einsum('ijk,ijk->ij', d, d))
        m = np.clip((t + soft * 0.5 - d) / soft, 0.0, 1.0)
        m[alpha < EMPTY_ALPHA] = 0.0
    if contiguous:
        h, w = alpha.shape
        cx, cy = int(min(max(x, 0), w - 1)), int(min(max(y, 0), h - 1))
        n, labels = cv2.connectedComponents((m > 0.02).astype(np.uint8), connectivity=4)
        own = labels[cy, cx]
        if own == 0:
            m = np.zeros_like(m)
            m[cy, cx] = 1.0     # (a tiny threshold on a noisy spot: the spot itself)
        else:
            m = np.where(labels == own, m, 0.0).astype(np.float32)
    return m


def combine(shape, parts):
    """One mask from [(op, similarity)] in order: '+' adds, '-' takes away."""
    mask = np.zeros(shape, np.float32)
    for op, m in parts:
        if op == '-':
            mask *= 1.0 - m
        else:
            np.maximum(mask, m, out=mask)
    return mask


def finish(mask, invert=False, grow=0.0, feather=0.0):
    """The whole mask: inverted, grown (or shrunk, grow < 0) and feathered
    by so many pixels (of this mask)."""
    if invert:
        mask = 1.0 - mask
    r = int(round(abs(grow)))
    if r:
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1))
        mask = (cv2.dilate if grow > 0 else cv2.erode)(mask, kernel, borderType=cv2.BORDER_REPLICATE)
    if feather > 0.05:
        mask = cv2.GaussianBlur(mask, (0, 0), sigmaX=feather / 2.0, borderType=cv2.BORDER_REPLICATE)
    return np.clip(mask, 0.0, 1.0).astype(np.float32, copy=False)


def _premultiplied(bgra):
    h, w = bgra.shape[:2]
    data = np.ascontiguousarray(bgra)
    return QImage(data.data, w, h, w * 4, QImage.Format_ARGB32_Premultiplied).copy()


def tint_image(mask, rgb=(255, 0, 0), strength=0.55):
    """The mask as a see-through tint (premultiplied QImage): what is in it, coloured."""
    a = mask * (255.0 * strength)
    out = np.empty(mask.shape + (4,), np.uint8)
    k = a / 255.0
    out[..., 0] = rgb[2] * k
    out[..., 1] = rgb[1] * k
    out[..., 2] = rgb[0] * k
    out[..., 3] = a
    return _premultiplied(out)


def checker_image(mask, cell=8):
    """What a removal would leave: a grey checkerboard laid where the mask is."""
    h, w = mask.shape
    yy, xx = np.indices((h, w))
    grey = np.where(((xx // cell) + (yy // cell)) % 2 == 0, 204.0, 153.0)
    out = np.empty((h, w, 4), np.uint8)
    out[..., 0] = out[..., 1] = out[..., 2] = grey * mask
    out[..., 3] = mask * 255.0
    return _premultiplied(out)


def alpha_image(mask):
    """The mask as black with it for alpha: a 🧊 cut (see _paint_snapshot)."""
    out = np.zeros(mask.shape + (4,), np.uint8)
    out[..., 3] = np.round(mask * 255.0)
    return _premultiplied(out)


def masked(image, mask):
    """*image* with *mask* for (extra) alpha: what is in the mask, kept."""
    image = image.convertToFormat(QImage.Format_ARGB32_Premultiplied)
    w, h = image.width(), image.height()
    buf = np.frombuffer(image.constBits(), np.uint8, count=image.sizeInBytes())
    px = buf.reshape(h, image.bytesPerLine() // 4, 4)[:, :w].astype(np.float32)
    px *= mask[..., None]
    return _premultiplied(np.round(px).astype(np.uint8))
