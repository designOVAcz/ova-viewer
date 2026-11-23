"""⬚ Select + transform: which marks a lasso takes, and moving / scaling /
turning them.

Marks are data in picture coordinates (stroke points and stamps, fill
outlines, line ends, a snapshot's corners), so a transform is applied to
that data and the marks are laid again - textured strokes stay sharp at any
size. Scaling is uniform: a stamp is round-ish and has one size, so a
stretched stroke could not be laid again faithfully. Ruler lines (the ones
spanning the whole picture) are not taken: they have no place to move.
"""

import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QPolygonF, QTransform

# (kind) -> selectable at all
SELECTABLE = ('free_stroke', 'free_line', 'fill', 'image', 'erase')


def _star_band(mark):
    """How far to each side a line's ✨ stars reach (0: it has none)."""
    stars = mark.get('stars') if isinstance(mark, dict) else None
    if not stars or not stars.get('band'):
        return 0.0
    return stars['band'] / 2.0 + 6.0 * stars.get('speck', 1.0)


def _xy(p):
    if isinstance(p, QPointF):
        return p.x(), p.y()
    return p[0], p[1]


def mark_points(kind, mark):
    """The points that place *mark* (picture coords), with how far its paint
    reaches past them (for the frame): [(x, y, reach)]."""
    if kind == 'free_stroke':
        stamps = getattr(mark, 'stamps', None)
        if stamps:
            # A turned tip reaches its diagonal: ~0.71 of its size (as
            # brushes._stamps_box) - undo's patches must hold all of it.
            return [(s[0], s[1], s[2] * 0.75) for s in stamps]
        reach = (getattr(mark, 'thickness', None) or 1) / 2.0
        return [(*_xy(p), reach) for p in mark]
    band = _star_band(mark) if kind in ('free_line', 'fill') else 0.0
    if band:                        # ✨ stars along a line: as wide as they reach
        ends = mark['points'] if 'points' in mark else (mark['start'], mark['end'])
        return [(*_xy(p), band) for p in ends]
    if kind == 'free_line':
        reach = (mark.get('thickness') or 1) / 2.0
        return [(*_xy(mark['start']), reach), (*_xy(mark['end']), reach)]
    if kind == 'fill':
        reach = (mark.get('thickness') or 0) / 2.0 if mark.get('shape') == 'polyline' else 0.0
        return [(*_xy(p), reach) for p in mark.get('points') or []]
    if kind == 'image':
        (x0, y0), (x1, y1), (x2, y2) = mark['corners'][:3]
        x3, y3 = x1 + x2 - x0, y1 + y2 - y0
        return [(x0, y0, 0.0), (x1, y1, 0.0), (x2, y2, 0.0), (x3, y3, 0.0)]
    if kind == 'erase':
        return [(p[0], p[1], p[2]) for p in mark]
    return []


def lasso_takes(kind, mark, lasso):
    """True when the lasso (QPolygonF, picture coords) takes *mark*: any of
    its points inside, or - a fill or snapshot - the lasso inside it."""
    if kind not in SELECTABLE:
        return False
    points = mark_points(kind, mark)
    if not points:
        return False
    for x, y, _r in points:
        if lasso.containsPoint(QPointF(x, y), Qt.OddEvenFill):
            return True
    if kind in ('fill', 'image') and len(points) >= 3 and lasso.size():
        outline = QPolygonF([QPointF(x, y) for x, y, _r in
                             (points if kind == 'fill' else [points[0], points[1], points[3], points[2]])])
        return outline.containsPoint(lasso[0], Qt.OddEvenFill)
    return False


def bounds(marks):
    """QRectF around *marks* ((kind, mark) pairs), their paint included."""
    xs0, ys0, xs1, ys1 = [], [], [], []
    for kind, mark in marks:
        for x, y, r in mark_points(kind, mark):
            xs0.append(x - r); ys0.append(y - r); xs1.append(x + r); ys1.append(y + r)
    if not xs0:
        return QRectF()
    return QRectF(QPointF(min(xs0), min(ys0)), QPointF(max(xs1), max(ys1)))


def snapshot(marks):
    """What a transform changes in *marks*, to put back (undo / cancel)."""
    kept = []
    for kind, mark in marks:
        if kind == 'free_stroke':
            kept.append((kind, mark, (list(mark), list(mark.stamps) if mark.stamps else mark.stamps,
                                      mark.thickness, getattr(mark, 'clip', None),
                                      getattr(mark, 'pic_width', None))))
        elif kind in ('free_line', 'fill'):
            # all of it (points, widths, jitter ...): a transform puts in new values
            kept.append((kind, mark, dict(mark, points=list(mark.get('points') or []))
                         if kind == 'fill' else dict(mark)))
        elif kind == 'image':
            kept.append((kind, mark, list(mark['corners'])))
        elif kind == 'erase':
            kept.append((kind, mark, list(mark)))
    return kept


def restore(kept):
    """Put *kept* (from snapshot) back."""
    for kind, mark, state in kept:
        if kind == 'free_stroke':
            mark[:] = state[0]
            mark.stamps = state[1]
            mark.thickness = state[2]
            if len(state) > 3:
                mark.clip, mark.pic_width = state[3], state[4]
            mark.render = None
        elif kind in ('free_line', 'fill'):
            mark.clear()
            mark.update(state)
        elif kind == 'image':
            mark['corners'] = state
        elif kind == 'erase':
            mark[:] = state


def scale_and_turn(t):
    """(uniform scale, turn in radians) of an affine QTransform."""
    scale = math.sqrt(abs(t.determinant())) or 1.0
    return scale, math.atan2(t.m12(), t.m11())


def apply(marks, t):
    """Move / scale / turn *marks* by QTransform *t* (picture coords), in
    place. Sizes (stamps, pen widths, eraser radii) scale with it, stamps
    turn with it."""
    scale, turn = scale_and_turn(t)

    def moved(p):
        x, y = _xy(p)
        q = t.map(QPointF(x, y))
        if isinstance(p, QPointF):
            return q
        return (q.x(), q.y(), *p[2:])

    for kind, mark in marks:
        if kind == 'free_stroke':
            mark[:] = [moved(p) for p in mark]
            if mark.stamps:
                mark.stamps = [(*moved(s[:2]), s[2] * scale, s[3] + turn, *s[4:])
                               for s in mark.stamps]
            if mark.thickness:
                mark.thickness = max(1, mark.thickness * scale)
            if getattr(mark, 'clip', None):
                mark.clip = [moved(p) for p in mark.clip]
            if getattr(mark, 'pic_width', None):
                mark.pic_width = mark.pic_width * scale
            mark.render = None
        elif kind == 'free_line':
            mark['start'], mark['end'] = moved(mark['start']), moved(mark['end'])
            if mark.get('thickness'):
                mark['thickness'] = max(1, mark['thickness'] * scale)
        elif kind == 'fill':
            mark['points'] = [moved(p) for p in mark.get('points') or []]
            if mark.get('thickness'):
                mark['thickness'] = max(1, mark['thickness'] * scale)
        if kind in ('free_line', 'fill') and mark.get('gradient'):
            grad = dict(mark['gradient'])       # (a new one: the undo snapshot keeps the old)
            grad['start'], grad['end'] = moved(grad['start'])[:2], moved(grad['end'])[:2]
            mark['gradient'] = grad
        if kind in ('free_line', 'fill') and mark.get('stars'):
            stars = dict(mark['stars'])         # ✨ (a new one: the undo snapshot keeps the old)
            stars['speck'] = stars.get('speck', 1.0) * scale
            if stars.get('band'):
                stars['band'] = stars['band'] * scale
            mark['stars'] = stars
        if kind in ('free_line', 'fill') and mark.get('pic_width'):
            mark['pic_width'] = mark['pic_width'] * scale     # a painted line's width
        if kind in ('free_line', 'fill') and mark.get('jitter'):
            # 🎲 its blocks / streaks scale and turn with it (a new dict:
            # the undo snapshot keeps the old one)
            jit = dict(mark['jitter'])
            jit['cell'] = jit.get('cell', 1.0) * (scale if jit.get('mode') != 'particles' else 1.0)
            jit['angle'] = jit.get('angle', 0.0) + math.degrees(turn)
            mark['jitter'] = jit
        elif kind == 'image':
            mark['corners'] = [moved(p)[:2] if not isinstance(p, QPointF) else moved(p)
                               for p in mark['corners']]
        elif kind == 'erase':
            mark[:] = [(*moved(p[:2]), p[2] * scale) for p in mark]


def about(point, scale=1.0, degrees=0.0):
    """QTransform scaling by *scale* and turning by *degrees* around *point*."""
    return (QTransform().translate(point.x(), point.y())
            .rotate(degrees).scale(scale, scale).translate(-point.x(), -point.y()))
