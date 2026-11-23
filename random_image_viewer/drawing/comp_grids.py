"""Composition grids: the classic guides painters and photographers lay over
a picture - thirds, the golden section and spiral, the armature of the
rectangle, dynamic symmetry and more.

Every grid is worked out for the rectangle it is laid on (its own aspect:
the diagonals are that rectangle's, 45° lines are true 45°), as polylines.
The same drawing makes the palette's thumbnails and the guides on the canvas.

    lines = grid_lines('armature', QRectF(...), orient=0)
    paint_grids(painter, rect, [('thirds', 0), ('spiral', 2)], opacity=0.8)

*orient*: the grids that are not symmetric (the spiral, the golden
triangles) turn by mirroring - bit 1 left-right, bit 2 top-bottom.
"""

import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen

PHI = (1 + 5 ** 0.5) / 2
INV = 1 / PHI               # 0.618...


def _ray_to_edge(p, d, w, h):
    """From *p* (inside the w x h box at the origin) along *d*: where it
    leaves the box."""
    ts = []
    for coord, dc, lim in ((p[0], d[0], w), (p[1], d[1], h)):
        if dc > 1e-12:
            ts.append((lim - coord) / dc)
        elif dc < -1e-12:
            ts.append(-coord / dc)
    t = min(t for t in ts if t > 1e-9) if ts else 0.0
    return (p[0] + d[0] * t, p[1] + d[1] * t)


def _foot(p, a, b):
    """The foot of the perpendicular from *p* to the line a-b."""
    ax, ay = a
    dx, dy = b[0] - ax, b[1] - ay
    t = ((p[0] - ax) * dx + (p[1] - ay) * dy) / (dx * dx + dy * dy)
    return (ax + dx * t, ay + dy * t)


# -- the grids, in a w x h box at the origin --------------------------------

def _thirds(w, h):
    return ([[(w * k / 3, 0), (w * k / 3, h)] for k in (1, 2)]
            + [[(0, h * k / 3), (w, h * k / 3)] for k in (1, 2)])


def _quarters(w, h):
    return ([[(w * k / 4, 0), (w * k / 4, h)] for k in (1, 2, 3)]
            + [[(0, h * k / 4), (w, h * k / 4)] for k in (1, 2, 3)])


def _golden(w, h):
    xs, ys = (w * (1 - INV), w * INV), (h * (1 - INV), h * INV)
    return [[(x, 0), (x, h)] for x in xs] + [[(0, y), (w, y)] for y in ys]


def _centre(w, h):
    return [[(w / 2, 0), (w / 2, h)], [(0, h / 2), (w, h / 2)],
            [(0, 0), (w, h)], [(w, 0), (0, h)]]


def _diagonals45(w, h):
    """The diagonal method: 45° lines from the four corners, to where they
    meet the far long side, and the lines through those points."""
    out = []
    for c, d in (((0, 0), (1, 1)), ((0, h), (1, -1)), ((w, 0), (-1, 1)), ((w, h), (-1, -1))):
        out.append([c, _ray_to_edge(c, d, w, h)])
    if w >= h:
        out += [[(h, 0), (h, h)], [(w - h, 0), (w - h, h)]]
    else:
        out += [[(0, w), (w, w)], [(0, h - w), (w, h - w)]]
    return out


def _rabatment(w, h):
    """The two squares on the short side, from either end, with their
    diagonals - where the eye settles on a long rectangle."""
    if w >= h:
        s = h
        return [[(s, 0), (s, h)], [(w - s, 0), (w - s, h)],
                [(0, 0), (s, h)], [(0, h), (s, 0)],
                [(w - s, 0), (w, h)], [(w - s, h), (w, 0)]]
    s = w
    return [[(0, s), (w, s)], [(0, h - s), (w, h - s)],
            [(0, 0), (w, s)], [(w, 0), (0, s)],
            [(0, h - s), (w, h)], [(w, h - s), (0, h)]]


def _armature(w, h):
    """The armature of the rectangle (14 lines): both diagonals, each corner
    to the middles of the two far sides, and the rhombus through the
    middles."""
    top, right, bottom, left = (w / 2, 0), (w, h / 2), (w / 2, h), (0, h / 2)
    out = [[(0, 0), (w, h)], [(w, 0), (0, h)]]
    out += [[(0, 0), right], [(0, 0), bottom], [(w, 0), left], [(w, 0), bottom],
            [(0, h), right], [(0, h), top], [(w, h), left], [(w, h), top]]
    out.append([top, right, bottom, left, top])
    return out


def _dynamic(w, h):
    """Dynamic symmetry: both diagonals, the four reciprocals (from each
    corner square to the other diagonal, on to the far side) and the lines
    through the four 'eyes' where they cross."""
    d1, d2 = ((0, 0), (w, h)), ((w, 0), (0, h))
    out = [list(d1), list(d2)]
    xs, ys = set(), set()
    for corner, (a, b) in (((w, 0), d1), ((0, h), d1), ((0, 0), d2), ((w, h), d2)):
        eye = _foot(corner, a, b)
        d = (eye[0] - corner[0], eye[1] - corner[1])
        out.append([corner, _ray_to_edge(corner, d, w, h)])
        xs.add(round(eye[0], 6))
        ys.add(round(eye[1], 6))
    out += [[(x, 0), (x, h)] for x in sorted(xs)] + [[(0, y), (w, y)] for y in sorted(ys)]
    return out


def _triangles(w, h):
    """Golden triangles: a diagonal, and from the other two corners the
    perpendicular onto it."""
    a, b = (0, 0), (w, h)
    return [[a, b], [(w, 0), _foot((w, 0), a, b)], [(0, h), _foot((0, h), a, b)]]


def _arc(c, p0, p1, n=24):
    """A quarter circle round *c* from *p0* to *p1* (the short way)."""
    r = math.hypot(p0[0] - c[0], p0[1] - c[1])
    a0 = math.atan2(p0[1] - c[1], p0[0] - c[0])
    a1 = math.atan2(p1[1] - c[1], p1[0] - c[0])
    da = (a1 - a0 + math.pi) % (2 * math.pi) - math.pi
    return [(c[0] + r * math.cos(a0 + da * i / n), c[1] + r * math.sin(a0 + da * i / n))
            for i in range(n + 1)]


def _spiral(w, h, turns=10):
    """The golden spiral in its golden rectangles: squares cut off in turn
    (left, top, right, bottom ...) with a quarter circle in each. Worked
    out in a golden rectangle, then stretched to the w x h box."""
    portrait = h > w
    gw, gh = PHI, 1.0
    x, y, rw, rh = 0.0, 0.0, gw, gh
    lines, spiral = [], []
    for i in range(turns):
        side = ('left', 'top', 'right', 'bottom')[i % 4]
        if side == 'left':
            s = rh
            lines.append([(x + s, y), (x + s, y + rh)])
            spiral += _arc((x + s, y + s), (x, y + s), (x + s, y))
            x, rw = x + s, rw - s
        elif side == 'top':
            s = rw
            lines.append([(x, y + s), (x + rw, y + s)])
            spiral += _arc((x, y + s), (x, y), (x + s, y + s))
            y, rh = y + s, rh - s
        elif side == 'right':
            s = rh
            lines.append([(x + rw - s, y), (x + rw - s, y + rh)])
            spiral += _arc((x + rw - s, y), (x + rw, y), (x + rw - s, y + s))
            rw = rw - s
        else:
            s = rw
            lines.append([(x, y + rh - s), (x + rw, y + rh - s)])
            spiral += _arc((x + rw, y + rh - s), (x + rw, y + rh), (x, y + rh - s))
            rh = rh - s
    out = lines + [spiral]
    if portrait:            # the same, turned to stand up
        out = [[(py * w / gh, px * h / gw) for px, py in line] for line in out]
    else:
        out = [[(px * w / gw, py * h / gh) for px, py in line] for line in out]
    return out


# key: (name, maker, orientations, colour, what it is)
GRIDS = {
    'thirds': ("Rule of thirds", _thirds, 1, (120, 200, 255),
               "Thirds: lines and their crossings, where interest sits naturally"),
    'golden': ("Golden section", _golden, 1, (255, 196, 90),
               "Phi grid: lines at 0.382 / 0.618 - a tighter, calmer thirds"),
    'spiral': ("Golden spiral", _spiral, 4, (255, 150, 90),
               "Fibonacci spiral in its golden rectangles - right-click turns it"),
    'triangles': ("Golden triangles", _triangles, 2, (240, 120, 200),
                  "A diagonal and the perpendiculars onto it from the other corners - "
                  "right-click mirrors it"),
    'armature': ("Armature", _armature, 1, (140, 220, 120),
                 "Armature of the rectangle: 14 lines - diagonals, corners to the middles, "
                 "the rhombus"),
    'dynamic': ("Dynamic symmetry", _dynamic, 1, (110, 230, 210),
                "Diagonals, reciprocals and the lines through the four 'eyes'"),
    'diagonal': ("Diagonal method", _diagonals45, 1, (255, 110, 110),
                 "45° lines from the corners: key points lie on them"),
    'rabatment': ("Rabatment", _rabatment, 1, (200, 170, 255),
                  "The two squares on the short side, with their diagonals"),
    'centre': ("Centre & diagonals", _centre, 1, (230, 230, 230),
               "The middle cross and both diagonals - symmetry and balance"),
    'quarters': ("Quarters", _quarters, 1, (170, 190, 210),
                 "A 4 × 4 grid: halves and quarters, for placing and measuring"),
}


def orientations(key):
    return GRIDS[key][2]


def grid_lines(key, rect, orient=0):
    """The grid *key* laid on *rect* (QRectF): a list of polylines (lists of
    (x, y)). *orient*: bit 1 mirrors left-right, bit 2 top-bottom."""
    w, h = rect.width(), rect.height()
    if w <= 0 or h <= 0:
        return []
    lines = GRIDS[key][1](w, h)
    x0, y0 = rect.x(), rect.y()
    fx, fy = bool(orient & 1), bool(orient & 2)
    return [[(x0 + (w - px if fx else px), y0 + (h - py if fy else py)) for px, py in line]
            for line in lines]


def colour(key, opacity=1.0):
    c = QColor(*GRIDS[key][3])
    c.setAlphaF(max(0.0, min(1.0, opacity)))
    return c


def paint_grids(painter, rect, grids, opacity=0.8, width=1.0):
    """Lay *grids* ([(key, orient)]) over *rect*, each in its own colour,
    lines *width* device px (cosmetic: the same at any zoom)."""
    painter.save()
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setBrush(Qt.NoBrush)
    for key, orient in grids:
        if key not in GRIDS:
            continue
        pen = QPen(colour(key, opacity), width)
        pen.setCosmetic(True)
        painter.setPen(pen)
        path = QPainterPath()
        for line in grid_lines(key, QRectF(rect), orient):
            path.moveTo(QPointF(*line[0]))
            for p in line[1:]:
                path.lineTo(QPointF(*p))
        painter.drawPath(path)
    painter.restore()
