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


# -- Barnstone's gamut (GRIDS_v3.pdf): St. Andrew's cross, the root
# rectangles' harmonic divisions, the theme of two, the rebated square.
# Worked out with the long side across, then turned for a standing picture.
# A one-point "line" is an eye: drawn as a dot (see paint_grids).

def _landscape(make):
    """*make*(w, h) for w >= h, used for a standing box too (turned)."""
    def wrapped(w, h):
        if w >= h:
            return make(w, h)
        return [[(y, x) for x, y in line] for line in make(h, w)]
    return wrapped


def _crossing(a, b, c, d):
    """Where the lines a-b and c-d cross, or None (parallel)."""
    x1, y1 = a
    x2, y2 = b
    x3, y3 = c
    x4, y4 = d
    den = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
    if abs(den) < 1e-12:
        return None
    t = ((x1 - x3) * (y3 - y4) - (y1 - y3) * (x3 - x4)) / den
    return (x1 + t * (x2 - x1), y1 + t * (y2 - y1))


def _st_andrew(w, h):
    """St. Andrew's cross: the square on the short side laid in from either
    end, with its diagonals (the X's), the rectangle's diagonals, the middle
    cross and the verticals where the squares' diagonals cross the
    rectangle's."""
    s = min(h, w)
    d1, d2 = ((0, 0), (w, h)), ((w, 0), (0, h))
    out = [list(d1), list(d2), [(w / 2, 0), (w / 2, h)], [(0, h / 2), (w, h / 2)],
           [(s, 0), (s, h)], [(w - s, 0), (w - s, h)]]
    squares = [((0, 0), (s, h)), ((0, h), (s, 0)), ((w - s, 0), (w, h)), ((w - s, h), (w, 0))]
    out += [list(q) for q in squares]
    xs = set()
    for q in squares:
        for d in (d1, d2):
            p = _crossing(q[0], q[1], d[0], d[1])
            if p is not None and 1e-6 < p[0] < w - 1e-6 and abs(p[1] - h / 2) > 1e-6:
                xs.add(round(p[0], 6))
    out += [[(x, 0), (x, h)] for x in sorted(xs) if abs(x - w / 2) > 1e-6]
    return out


def _root_n(n):
    """A root-n rectangle's harmonic division: n reciprocals side by side
    along the long side, each with its diagonals; the whole one's diagonals
    and the line across the middle."""
    def make(w, h):
        out = [[(0, 0), (w, h)], [(w, 0), (0, h)], [(0, h / 2), (w, h / 2)]]
        step = w / n
        for k in range(n):
            x0, x1 = k * step, (k + 1) * step
            if k:
                out.append([(x0, 0), (x0, h)])
            out += [[(x0, 0), (x1, h)], [(x0, h), (x1, 0)]]
        return out
    return _landscape(make)


def _theme_two(w, h):
    """The theme of two: halved along the long side; the diagonals and from
    each corner to the middle of the far long side - they cross on four
    eyes (the thirds), and the middle."""
    top, bottom = (w / 2, 0), (w / 2, h)
    out = [[(0, 0), (w, h)], [(w, 0), (0, h)], [top, bottom],
           [(0, 0), bottom], [(w, 0), bottom], [(0, h), top], [(w, h), top]]
    eyes = [(w / 3, h / 3), (2 * w / 3, h / 3), (w / 3, 2 * h / 3), (2 * w / 3, 2 * h / 3), (w / 2, h / 2)]
    return out + [[e] for e in eyes]


def _rebated(w, h):
    """The rebated square (3-4-5): the square on the short side laid in from
    the right, its diagonals, the rectangle's diagonals, and the two eyes
    where those cross the square's edge - with the lines across through
    them."""
    s = h
    x = w - s
    out = [[(0, 0), (w, h)], [(w, 0), (0, h)], [(x, 0), (x, h)],
           [(x, 0), (w, h)], [(x, h), (w, 0)]]
    y1, y2 = h * x / w, h - h * x / w
    for y in sorted({round(y1, 6), round(y2, 6)}):
        out.append([(0, y), (w, y)])
        out.append([(x, y)])
    return out


# -- perspective: 1, 2, 3 vanishing points and a fisheye (curvilinear) ----
# Their points (vanishing points; the fisheye's centre) are movable: kept as
# fractions of the picture (outside 0..1 beyond it), in the grid's own
# unmirrored frame - see POINTS and grid_lines(params=). Lines reaching past
# the picture are clipped to it (see paint_grids). The two vanishing points
# of 2 and 3 points share the horizon: moving one up moves both.

POINTS = {
    'persp1': [(0.5, 0.5)],
    'persp2': [(-0.6, 0.5), (1.6, 0.5)],
    'persp3': [(-0.6, 0.3), (1.6, 0.3), (0.5, 2.6)],
    'fisheye': [(0.5, 0.5)],
}
HORIZON_PAIR = ('persp2', 'persp3')     # points 0 and 1 on one horizon


def points(key, params=None):
    """*key*'s movable points (fractions), *params* (a list of them) or the
    defaults; None for a grid without any."""
    base = POINTS.get(key)
    if base is None:
        return None
    if params and len(params) == len(base):
        return [(float(x), float(y)) for x, y in params]
    return list(base)


def _towards(vp, targets):
    return [[vp, t] for t in targets]


def _one_point(w, h, pts=None, rings=7, rays=9):
    """1-point: rays from the vanishing point to the edges, the horizon
    through it, and the receding frames of a tunnel - rooms, roads, halls."""
    fx, fy = (pts or POINTS['persp1'])[0]
    vp = (fx * w, fy * h)
    edge = ([(w * i / (rays - 1), 0) for i in range(rays)] + [(w * i / (rays - 1), h) for i in range(rays)]
            + [(0, h * i / (rays - 1)) for i in range(1, rays - 1)]
            + [(w, h * i / (rays - 1)) for i in range(1, rays - 1)])
    out = _towards(vp, edge) + [[(0, vp[1]), (w, vp[1])]]
    s = 1.0
    for _ in range(rings):
        s *= 0.66
        # the picture's frame drawn towards the vanishing point
        c = [(x + (vp[0] - x) * (1 - s), y + (vp[1] - y) * (1 - s))
             for x, y in ((0, 0), (w, 0), (w, h), (0, h))]
        out.append(c + [c[0]])
    return out


def _fan(vp, w, h, rays, other_side):
    """Rays from *vp* across the picture, to points spread along the side
    away from it (x = 0 or w)."""
    x = 0.0 if other_side else w
    lo, hi = min(vp[1], 0.0) - 0.6 * h, max(vp[1], h) + 0.6 * h
    return _towards(vp, [(x, lo + (hi - lo) * i / (rays - 1)) for i in range(rays)])


def _two_point(w, h, pts=None, rays=13, verticals=8):
    """2-point: two vanishing points on the horizon, a fan from each, and
    the verticals that stay upright - a corner of a building, a box seen
    edge on."""
    (lx, ly), (rx, ry) = (pts or POINTS['persp2'])[:2]
    left, right = (lx * w, ly * h), (rx * w, ry * h)
    out = (_fan(left, w, h, rays, left[0] > w / 2) + _fan(right, w, h, rays, right[0] > w / 2))
    out += [[(w * k / verticals, 0), (w * k / verticals, h)] for k in range(1, verticals)]
    y = (left[1] + right[1]) / 2
    out.append([(0, y), (w, y)])
    return out


def _three_point(w, h, pts=None, rays=13):
    """3-point: two vanishing points on the horizon and a third far off it -
    below: looking down on tall things (right-click: from below, the third
    above)."""
    (lx, ly), (rx, ry), (dx, dy) = (pts or POINTS['persp3'])[:3]
    left, right, down = (lx * w, ly * h), (rx * w, ry * h), (dx * w, dy * h)
    out = (_fan(left, w, h, rays, left[0] > w / 2) + _fan(right, w, h, rays, right[0] > w / 2))
    # the third: rays to points spread along the edge away from it
    ty = -0.2 * h if down[1] > h / 2 else 1.2 * h
    lo, hi = min(down[0], 0.0) - 0.6 * w, max(down[0], w) + 0.6 * w
    out += _towards(down, [(lo + (hi - lo) * i / (rays - 1), ty) for i in range(rays)])
    y = (left[1] + right[1]) / 2
    out.append([(0, y), (w, y)])
    return out


def _fisheye(w, h, pts=None, per_side=5, corner=4.0, steps=72):
    """Fisheye (curvilinear): a square grid seen through an equidistant
    fisheye - straight lines bow out from its centre (movable), the horizon
    and the centre line through it stay straight. Fitted by the diagonal:
    the picture's corners see *corner* grid units out (~76 degrees), so the
    grid reaches them - fitted by the sides, the corners lay past the lens'
    90 degrees and stayed bare, and the lines crowded into a solid band."""
    fx, fy = (pts or POINTS['fisheye'])[0]
    cx, cy = fx * w, fy * h
    k = math.atan(corner) / math.sqrt(2.0)       # the edge middles' angle
    sx, sy = (w / 2) / k, (h / 2) / k

    def lens(u, v):
        r = math.hypot(u, v)
        f = math.atan(r) / r if r > 1e-9 else 1.0
        return (cx + u * f * sx, cy + v * f * sy)
    reach = math.tan(k)             # grid units to the edge middles
    step = reach / per_side
    far = corner * 1.6              # (on past the picture: a centre moved off it)
    count = int(far / step) + 1
    vals = [step * i for i in range(-count, count + 1)]
    out = []
    for c in vals:
        out.append([lens(c, -far + 2 * far * j / steps) for j in range(steps + 1)])
        out.append([lens(-far + 2 * far * j / steps, c) for j in range(steps + 1)])
    return out


_WITH_POINTS = {'persp1': _one_point, 'persp2': _two_point, 'persp3': _three_point,
                'fisheye': _fisheye}


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
    # -- the gamut (Barnstone; GRIDS_v3.pdf)
    'st_andrew': ("St. Andrew's cross", _landscape(_st_andrew), 1, (255, 140, 150),
                  "The square on the short side laid in from both ends, with its X's, the "
                  "diagonals and the middle cross"),
    'theme2': ("Theme of two · eyes", _landscape(_theme_two), 1, (180, 225, 120),
               "Halved along the long side: diagonals and corners to the far middles - the "
               "eyes (dots) where they cross"),
    'rebated': ("Rebated square · eyes", _landscape(_rebated), 2, (150, 215, 140),
                "The square laid in from one end (3-4-5): its diagonals, the rectangle's, and "
                "the two eyes on its edge - right-click mirrors it"),
    'root2': ("Root 2", _root_n(2), 1, (255, 170, 120),
              "A root-2 rectangle's division: 2 reciprocals, their diagonals, the whole one's"),
    'root3': ("Root 3", _root_n(3), 1, (240, 200, 110),
              "A root-3 rectangle's division: 3 reciprocals, their diagonals, the whole one's"),
    'root4': ("Root 4", _root_n(4), 1, (200, 210, 110),
              "A root-4 rectangle's division: 4 reciprocals (squares in a 2:1), their "
              "diagonals - the St. Andrew's crosses - and the whole one's"),
    'root5': ("Root 5", _root_n(5), 1, (150, 200, 230),
              "A root-5 rectangle's division: 5 reciprocals, their diagonals, the whole one's"),
    # -- perspective
    'persp1': ("1-point perspective", _one_point, 1, (120, 210, 255),
               "One vanishing point in the middle: rays to the edges, the horizon, receding "
               "frames - rooms, roads, corridors"),
    'persp2': ("2-point perspective", _two_point, 1, (255, 170, 90),
               "The horizon at mid-height, a vanishing point beyond each side, upright verticals "
               "- a building's corner, a box edge on"),
    'persp3': ("3-point perspective", _three_point, 2, (230, 120, 255),
               "Two vanishing points on a high horizon and a third far below - looking down on tall "
               "things; right-click: from below"),
    'fisheye': ("Fisheye (curvilinear)", _fisheye, 1, (120, 240, 170),
                "A grid through a fisheye lens: lines bow out from the middle, the horizon and "
                "centre line stay straight"),
}


def orientations(key):
    return GRIDS[key][2]


def grid_lines(key, rect, orient=0, params=None):
    """The grid *key* laid on *rect* (QRectF): a list of polylines (lists of
    (x, y)). *orient*: bit 1 mirrors left-right, bit 2 top-bottom.
    *params*: a perspective grid's points (see POINTS; None: the defaults)."""
    w, h = rect.width(), rect.height()
    if w <= 0 or h <= 0:
        return []
    if key in _WITH_POINTS:
        lines = _WITH_POINTS[key](w, h, points(key, params))
    else:
        lines = GRIDS[key][1](w, h)
    x0, y0 = rect.x(), rect.y()
    fx, fy = bool(orient & 1), bool(orient & 2)
    return [[(x0 + (w - px if fx else px), y0 + (h - py if fy else py)) for px, py in line]
            for line in lines]


def point_on(rect, orient, fraction):
    """A grid point (*fraction* of the picture, the grid's own frame) on
    *rect*, mirrored as *orient* says: (x, y)."""
    fx, fy = fraction
    if orient & 1:
        fx = 1.0 - fx
    if orient & 2:
        fy = 1.0 - fy
    return (rect.x() + fx * rect.width(), rect.y() + fy * rect.height())


def fraction_of(rect, orient, x, y):
    """The other way: (x, y) on *rect* as a grid point's fraction."""
    fx = (x - rect.x()) / max(1e-9, rect.width())
    fy = (y - rect.y()) / max(1e-9, rect.height())
    if orient & 1:
        fx = 1.0 - fx
    if orient & 2:
        fy = 1.0 - fy
    return (fx, fy)


def colour(key, opacity=1.0):
    c = QColor(*GRIDS[key][3])
    c.setAlphaF(max(0.0, min(1.0, opacity)))
    return c


def paint_grids(painter, rect, grids, opacity=0.8, width=1.0, params=None):
    """Lay *grids* ([(key, orient)]) over *rect*, each in its own colour,
    lines *width* device px (cosmetic: the same at any zoom). *params*:
    {key: points} for the perspective grids moved from their defaults."""
    params = params or {}
    painter.save()
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setBrush(Qt.NoBrush)
    # (lines towards vanishing points outside the picture stop at its edge)
    painter.setClipRect(QRectF(rect), Qt.IntersectClip)
    for key, orient in grids:
        if key not in GRIDS:
            continue
        pen = QPen(colour(key, opacity), width)
        pen.setCosmetic(True)
        painter.setPen(pen)
        path = QPainterPath()
        eyes = []
        for line in grid_lines(key, QRectF(rect), orient, params.get(key)):
            if len(line) == 1:
                eyes.append(line[0])        # an eye: a dot, below
                continue
            path.moveTo(QPointF(*line[0]))
            for p in line[1:]:
                path.lineTo(QPointF(*p))
        painter.drawPath(path)
        if eyes:
            # 👁 the eyes as dots of a fixed size on screen (as the lines'
            # width is), in the PDF's red, at the grid's opacity
            t = painter.transform()
            r = max(2.0, 2.6 * width)
            painter.save()
            painter.resetTransform()
            painter.setPen(Qt.NoPen)
            dot = QColor(235, 70, 50)
            dot.setAlphaF(max(0.0, min(1.0, opacity)))
            painter.setBrush(dot)
            for e in eyes:
                painter.drawEllipse(t.map(QPointF(*e)), r, r)
            painter.restore()
    painter.restore()
