"""Flat strokes: the GPU (drawing/gpu_flat.GpuFlatStroke) against the CPU
(brushes.paint_stamps(..., flat=True), i.e. FlatStroke), pixel for pixel.

    .venv\\Scripts\\python.exe tools\\compare_gpu_flat.py [--out DIR] [--quick]
                                                         [--time] [--worst N]

Strokes are made by StampTrail from pen points (straight, zigzag, a curve
that turns back; pressure 0.2..1), for the tips Soft, Round, hp_7 and hp_1,
sizes 20 / 101 / 300 px, spacing 1 % and 10 %, plain colour and 'particles'
colour jitter. Each is laid on a transparent ARGB32 premultiplied image on
the CPU, and on a GpuLayer fed a pen event at a time (with a preview every
few events, then commit) and read back. Printed per case: mean / max
difference per channel (B G R A, of 255, over the pixels either side
painted) and the share of those pixels off by more than 8. Side-by-side PNGs
(CPU | GPU | difference x8) of the worst cases go to --out.

--time: per-add() Python time and GPU time for the user's brush (hp_7,
101 px, spacing 1 %, particles), 300 pen events.
"""

import argparse
import math
import os
import random
import statistics
import sys
import time

import numpy as np
from PySide6.QtCore import QCoreApplication, QRect, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QTransform
from PySide6.QtWidgets import QApplication

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # the repo root

SCRATCH = (r"C:\Users\ova\AppData\Local\Temp\claude\c0645e46f79ecbeb"
           r"\4721d74b-4a87-4cf0-a07b-a38008a0f73b\scratchpad\gpu_flat")
TIPS = {'soft': None, 'round': None, 'hp_7': 'Y:/tmp/hp/blender/hp_7.png',
        'hp_1': 'Y:/tmp/hp/blender/hp_1.png'}
W, H = 1700, 1100


def tip(name):
    from random_image_viewer.drawing import brushes
    if name == 'soft':
        return brushes.load_texture(brushes.SOFT)
    if name == 'round':
        return brushes.load_texture(brushes.ROUND)
    return brushes.load_texture(TIPS[name])


def pen_points(path, size, n=None):
    """Pen points (x, y, pressure) for a path kind, picture px."""
    m = size * 0.8 + 40
    pts = []
    if path == 'straight':
        n = n or 120
        for i in range(n + 1):
            t = i / n
            pts.append((m + t * (W - 2 * m), H * 0.5 + t * 60, 1.0))
    elif path == 'ramp':           # diagonal, pressure 0.2 -> 1 -> 0.4
        n = n or 120
        for i in range(n + 1):
            t = i / n
            p = 0.2 + 0.8 * min(1.0, t * 1.6) if t < 0.7 else 1.0 - (t - 0.7) * 2
            pts.append((m + t * (W - 2 * m), m + t * (H - 2 * m), max(0.2, p)))
    elif path == 'zigzag':         # a wave with pressure following it
        n = n or 160
        for i in range(n + 1):
            t = i / n
            y = H * 0.5 + math.sin(t * 9.0) * (H * 0.5 - m) * 0.85
            pts.append((m + t * (W - 2 * m), y, 0.2 + 0.8 * (0.5 + 0.5 * math.sin(t * 13.0))))
    elif path == 'curl':           # a loop and a turn back (FlatStroke restarts)
        n = n or 160
        cx, cy, r = W * 0.45, H * 0.5, min(W, H) * 0.5 - m
        for i in range(n + 1):
            t = i / n
            if t < 0.6:
                a = t / 0.6 * 1.6 * math.pi
                pts.append((cx + r * math.cos(a), cy + r * math.sin(a), 0.5 + 0.5 * math.sin(t * 7)))
            else:
                k = (t - 0.6) / 0.4
                a = 1.6 * math.pi
                x0, y0 = cx + r * math.cos(a), cy + r * math.sin(a)
                pts.append((x0 + (0.5 - abs(k - 0.5)) * r * 1.2, y0 - k * 0.4 * r, 0.8))
    return pts


def make_stamps(texture, size, spacing, path, particles, angle=0, seed=1, n=None):
    """StampTrail's stamps, as a list per pen event."""
    from random_image_viewer.drawing import brushes
    s = dict(brushes.DEFAULT_SETTINGS)
    s.update(spacing=spacing, smoothing=0, angle=angle, flat=True)
    if particles:
        s.update(jitter_per_stamp='particles', hue_jitter=30, sat_jitter=30, val_jitter=30)
    trail = brushes.StampTrail(s, texture, QColor(200, 90, 40), size, QTransform(), True, seed=seed)
    events = []
    pts = pen_points(path, size, n)
    trail.add(pts[0][0], pts[0][1], None)
    for x, y, p in pts[1:]:
        events.append(list(trail.add(x, y, p)))
    before = len(trail.stamps)
    trail.finish(pts[-1][2])
    if len(trail.stamps) > before:
        events.append(trail.stamps[before:])
    return events


def cpu_flat(texture, stamps, view):
    from random_image_viewer.drawing import brushes
    img = QImage(W, H, QImage.Format_ARGB32_Premultiplied)
    img.fill(0)
    p = QPainter(img)
    brushes.paint_stamps(p, texture, stamps, view, flat=True)
    p.end()
    return img


def cpu_partial(texture, stamps, view):
    """FlatStroke with these stamps, composed as the live preview shows it."""
    from random_image_viewer.drawing import brushes
    stroke = brushes.FlatStroke(QRect(0, 0, W, H))
    stroke.add(texture, stamps, view)
    return stroke.compose(QRect(0, 0, W, H))


def arr(img):
    img = img.convertToFormat(QImage.Format_ARGB32_Premultiplied)
    a = np.frombuffer(img.constBits(), np.uint8).reshape(img.height(), img.bytesPerLine())
    return a[:, :img.width() * 4].reshape(img.height(), img.width(), 4).astype(np.int16)


def compare(cpu, gpu):
    a, b = arr(cpu), arr(gpu)
    d = np.abs(a - b)
    painted = (a[..., 3] > 0) | (b[..., 3] > 0)
    if not painted.any():
        return None
    dp = d[painted]
    return {'mean': dp.mean(axis=0), 'max': dp.max(axis=0),
            'over8': (dp.max(axis=1) > 8).mean() * 100, 'pixels': int(painted.sum()),
            'cov_cpu': a[..., 3].sum() / 255.0, 'cov_gpu': b[..., 3].sum() / 255.0}


def side_by_side(cpu, gpu, path):
    a, b = arr(cpu), arr(gpu)
    painted = (a[..., 3] > 0) | (b[..., 3] > 0)
    ys, xs = np.nonzero(painted)
    y0, y1 = max(0, ys.min() - 4), min(H, ys.max() + 5)
    x0, x1 = max(0, xs.min() - 4), min(W, xs.max() + 5)
    crop = QRect(x0, y0, x1 - x0, y1 - y0)

    def on_white(img):
        out = QImage(crop.size(), QImage.Format_RGB32)
        out.fill(Qt.white)
        p = QPainter(out)
        p.drawImage(0, 0, img.copy(crop))
        p.end()
        return out
    d = np.abs(a - b)[y0:y1, x0:x1].max(axis=2)
    dv = np.clip(d * 8, 0, 255).astype(np.uint8)
    px = np.empty(dv.shape + (4,), np.uint8)
    px[..., 0] = px[..., 1] = px[..., 2] = 255 - dv
    px[..., 2] = 255                        # off pixels red-ish
    px[..., 3] = 255
    diff = QImage(px.tobytes(), dv.shape[1], dv.shape[0], dv.shape[1] * 4, QImage.Format_RGB32).copy()
    w, h = crop.width(), crop.height()
    out = QImage(w * 3 + 8, h, QImage.Format_RGB32)
    out.fill(QColor(128, 128, 128))
    p = QPainter(out)
    p.drawImage(0, 0, on_white(cpu))
    p.drawImage(w + 4, 0, on_white(gpu))
    p.drawImage(2 * w + 8, 0, diff)
    p.end()
    out.save(path)


def gpu_flat(layer, texture, events, view, preview_every=3, mid=None):
    from random_image_viewer.drawing.gpu_flat import GpuFlatStroke
    layer.clear()
    stroke = GpuFlatStroke(layer)
    mid_img = None
    for i, ev in enumerate(events):
        stroke.add(texture, ev, view)
        if i % preview_every == 0:
            stroke.preview()
        if mid is not None and i == mid:
            stroke.preview()
            mid_img = stroke.read_overlay(QRect(0, 0, W, H))
    stroke.commit()
    return layer.read(QRect(0, 0, W, H)), mid_img


def cases(quick):
    out = []
    paths = ['straight', 'ramp', 'zigzag', 'curl']
    i = 0
    for name in ('soft', 'round', 'hp_7', 'hp_1'):
        for size in (20, 101, 300):
            for spacing in (1, 10):
                for particles in (False, True):
                    path = paths[i % len(paths)]
                    angle = 0 if (i // 4) % 3 else 30
                    i += 1
                    if quick and (size == 300 and spacing == 1):
                        continue
                    out.append((name, size, spacing, particles, path, angle))
    return out


def timing(layer, out_dir):
    """The user's brush: hp_7 101 px, spacing 1 %, particles - 300 events."""
    from random_image_viewer.drawing.gpu_flat import GpuFlatStroke
    gpu = layer.gpu
    texture = tip('hp_7')
    events = make_stamps(texture, 101, 1, 'zigzag', True, n=300)
    events = events[:300]
    counts = [len(e) for e in events]
    # warm: the motion frames / atlas entries the stroke needs, as the app's
    # earlier strokes would have made them (a cold run is printed too)
    for label in ('cold', 'warm', 'warm'):
        layer.clear()
        gpu.finish()
        stroke = GpuFlatStroke(layer)
        add_t, prev_t, gpu_t = [], [], []
        for ev in events:
            t0 = time.perf_counter()
            stroke.add(texture, ev, None)
            t1 = time.perf_counter()
            stroke.preview()
            t2 = time.perf_counter()
            gpu.finish()
            t3 = time.perf_counter()
            add_t.append((t1 - t0) * 1000)
            prev_t.append((t2 - t1) * 1000)
            gpu_t.append((t3 - t2) * 1000)
        t0 = time.perf_counter()
        stroke.commit()
        gpu.finish()
        commit_t = (time.perf_counter() - t0) * 1000
        print(f"  [{label}] add(): median {statistics.median(add_t):.3f} ms, mean "
              f"{statistics.mean(add_t):.3f}, p95 {sorted(add_t)[int(len(add_t) * .95)]:.3f}, max "
              f"{max(add_t):.2f} ms | preview(): median {statistics.median(prev_t):.3f} ms | "
              f"GPU wait after both: median {statistics.median(gpu_t):.3f} ms | commit {commit_t:.2f} ms")
    # GPU time alone: add + glFinish, preview + glFinish
    layer.clear()
    stroke = GpuFlatStroke(layer)
    a_gpu, p_gpu = [], []
    for ev in events:
        gpu.finish()
        t0 = time.perf_counter()
        stroke.add(texture, ev, None)
        gpu.finish()
        t1 = time.perf_counter()
        stroke.preview()
        gpu.finish()
        t2 = time.perf_counter()
        a_gpu.append((t1 - t0) * 1000)
        p_gpu.append((t2 - t1) * 1000)
    stroke.commit()
    print(f"  add()+glFinish median {statistics.median(a_gpu):.3f} ms; preview()+glFinish median "
          f"{statistics.median(p_gpu):.3f} ms; stamps per event: median {statistics.median(counts)}, "
          f"max {max(counts)}, total {sum(counts)}")
    # the CPU's own FlatStroke for the same events, for scale
    from random_image_viewer.drawing import brushes
    cpu = brushes.FlatStroke(QRect(0, 0, W, H))
    c_add, c_comp = [], []
    for ev in events:
        t0 = time.perf_counter()
        r = cpu.add(texture, ev, QTransform())
        t1 = time.perf_counter()
        if r is not None:
            cpu.compose(r)
        c_add.append((t1 - t0) * 1000)
        c_comp.append((time.perf_counter() - t1) * 1000)
    print(f"  (CPU FlatStroke for the same: add median {statistics.median(c_add):.3f} ms, "
          f"compose of the changed rect median {statistics.median(c_comp):.3f} ms)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default=SCRATCH)
    ap.add_argument('--quick', action='store_true')
    ap.add_argument('--time', action='store_true')
    ap.add_argument('--worst', type=int, default=6)
    ap.add_argument('--only', default='')
    ap.add_argument('--precision', default='byte', choices=('byte', 'half'))
    args = ap.parse_args()
    QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
    app = QApplication(sys.argv)    # noqa: F841
    from random_image_viewer.drawing import gpu_paint
    os.makedirs(args.out, exist_ok=True)
    gpu = gpu_paint.shared()
    print(f"GPU: {gpu.renderer}")
    layer = gpu_paint.GpuLayer(W, H, args.precision)
    if args.time:
        timing(layer, args.out)
        return
    results = []
    print(f"{'tip':5} {'size':>4} {'sp':>3} {'part':>4} {'path':8} {'ang':>3} {'stamps':>6} | "
          f"{'mean B,G,R,A':>23} | {'max B,G,R,A':>17} | {'>8':>6} | cov cpu/gpu | preview mean,>8")
    for name, size, spacing, particles, path, angle in cases(args.quick):
        tag = f"{name}_{size}_sp{spacing}_{'part' if particles else 'flat'}_{path}_a{angle}"
        if args.only and args.only not in tag:
            continue
        texture = tip(name)
        view = QTransform()
        events = make_stamps(texture, size, spacing, path, particles, angle,
                             seed=size * 13 + spacing)
        stamps = [s for e in events for s in e]
        mid = len(events) // 2
        cpu = cpu_flat(texture, stamps, view)
        gpu_img, mid_img = gpu_flat(layer, texture, events, view, mid=mid)
        r = compare(cpu, gpu_img)
        mid_stamps = [s for e in events[:mid + 1] for s in e]
        rp = compare(cpu_partial(texture, mid_stamps, view), mid_img)
        results.append((r, tag, cpu, gpu_img))
        print(f"{name:5} {size:4} {spacing:3} {'yes' if particles else 'no':>4} {path:8} {angle:3} "
              f"{len(stamps):6} | {', '.join(f'{v:.2f}' for v in r['mean']):>23} | "
              f"{', '.join(str(int(v)) for v in r['max']):>17} | {r['over8']:5.2f}% | "
              f"{r['cov_gpu'] / max(1e-9, r['cov_cpu']):.4f} | "
              f"{rp['mean'].mean():.2f}, {rp['over8']:.2f}%", flush=True)
    # a view that scales and turns (picture -> layer px), as the app's zoom
    for name in ('hp_7', 'hp_1'):
        texture = tip(name)
        view = QTransform().translate(W * 0.1, H * 0.05).rotate(17).scale(0.8, 0.8)
        events = make_stamps(texture, 101, 1, 'zigzag', True, seed=5)
        stamps = [s for e in events for s in e]
        cpu = cpu_flat(texture, stamps, view)
        gpu_img, _ = gpu_flat(layer, texture, events, view)
        r = compare(cpu, gpu_img)
        tag = f"{name}_101_sp1_part_zigzag_view-rot17-x0.8"
        results.append((r, tag, cpu, gpu_img))
        print(f"{tag:46} | {', '.join(f'{v:.2f}' for v in r['mean']):>23} | "
              f"{', '.join(str(int(v)) for v in r['max']):>17} | {r['over8']:5.2f}% | "
              f"{r['cov_gpu'] / max(1e-9, r['cov_cpu']):.4f}")
    means = [r[0]['mean'].mean() for r in results]
    overs = [r[0]['over8'] for r in results]
    print(f"\nall {len(results)} cases: mean diff avg {np.mean(means):.3f}/255, worst "
          f"{max(means):.3f}/255; >8: avg {np.mean(overs):.3f}%, worst {max(overs):.3f}%")
    worst = sorted(results, key=lambda r: -(r[0]['over8'] + r[0]['mean'].mean()))[:args.worst]
    for r, tag, cpu, gpu_img in worst:
        path = os.path.join(args.out, f"worst_{tag}.png")
        side_by_side(cpu, gpu_img, path)
        print(f"saved {path}")


if __name__ == '__main__':
    main()
