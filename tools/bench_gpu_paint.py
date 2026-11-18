"""Benchmark: GPU stamps (drawing/gpu_paint.py) against the CPU paint_stamps.

    .venv\\Scripts\\python.exe bench_gpu_paint.py [--precision half|byte] [--save DIR]

On a 5800x3000 layer: 400 stamps of 140 px, 400 of 400 px, 2000 of 20 px,
for each tip (the Soft built-in and, if there, Y:/tmp/hp/blender/hp_1.png);
build-up and flat. Prints time per batch, readback / upload of 600x600, and
the pixel difference to the CPU (mean / max per channel, B G R A).
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
from PySide6.QtGui import QColor, QImage, QMatrix4x4, QOpenGLContext, QPainter, QTransform
from PySide6.QtWidgets import QApplication

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # the repo root

W, H = 5800, 3000
EXTRA_TIP = 'Y:/tmp/hp/blender/hp_1.png'


def make_layer():
    """Half opaque paint, half clear - SourceOver on both."""
    a = np.zeros((H, W, 4), np.uint8)
    yy = (np.arange(H) * 200 // H).astype(np.uint8)[:, None]
    a[:, : W // 2, 0] = 90          # B
    a[:, : W // 2, 1] = yy          # G
    a[:, : W // 2, 2] = 40          # R
    a[:, : W // 2, 3] = 255
    img = QImage(a.tobytes(), W, H, W * 4, QImage.Format_ARGB32_Premultiplied).copy()
    return img


def make_stamps(n, size, seed):
    rng = random.Random(seed)
    colours = [0xD04020, 0x2050E0, 0x20A040, 0xF0E0A0, 0x101010]
    out = []
    for i in range(n):
        t = i / max(1, n - 1)
        x = 300 + t * (W - 600)
        y = H / 2 + math.sin(t * 9.0) * (H / 2 - size - 50) * 0.9
        out.append((x + rng.uniform(-5, 5), y + rng.uniform(-5, 5), size * rng.uniform(0.85, 1.0),
                    rng.uniform(-math.pi, math.pi), rng.uniform(0.15, 1.0),
                    colours[(i // 37) % len(colours)]))
    return out


def arr(img):
    img = img.convertToFormat(QImage.Format_ARGB32_Premultiplied)
    a = np.frombuffer(img.constBits(), np.uint8).reshape(img.height(), img.bytesPerLine())
    return a[:, : img.width() * 4].reshape(img.height(), img.width(), 4).astype(np.int16)


def diff(cpu, gpu):
    d = np.abs(arr(cpu) - arr(gpu))
    mean = d.reshape(-1, 4).mean(axis=0)
    mx = d.reshape(-1, 4).max(axis=0)
    over2 = (d.max(axis=2) > 2).mean() * 100
    return mean, mx, over2


def cpu_flat_reference(layer, texture, stamps, steps):
    """The FlatStroke core on the CPU, stamp for stamp: grey tips under
    Lighten (coverage = max), coloured tips over each other, composed as
    FlatStroke.compose does. (FlatStroke itself also sweeps a body between
    stamps; this is what the GPU's paint_flat reproduces.)"""
    from random_image_viewer.drawing import brushes
    box = brushes._stamps_box(stamps, QTransform(), 1.0).intersected(QRect(0, 0, W, H))
    mask = QImage(box.size(), QImage.Format_RGB32)
    mask.fill(Qt.black)
    colour = QImage(box.size(), QImage.Format_ARGB32_Premultiplied)
    colour.fill(Qt.transparent)
    mp, cp = QPainter(mask), QPainter(colour)
    mp.setCompositionMode(QPainter.CompositionMode_Lighten)
    view = QTransform.fromTranslate(-box.x(), -box.y())
    for p in (mp, cp):
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
    for st in stamps:
        level = texture.level(st[2])
        step = max(1, round(st[4] * steps))
        grey = texture.grey(level, step)
        col = texture.coloured(level, st[5])
        m = brushes._stamp_matrix(st, col, view)
        mp.setTransform(m)
        mp.drawImage(0, 0, grey)
        cp.setTransform(m)
        cp.drawImage(0, 0, col)
    mp.end()
    cp.end()
    straight = colour.convertToFormat(QImage.Format_ARGB32)
    straight.reinterpretAsFormat(QImage.Format_RGB32)
    out = straight.convertToFormat(QImage.Format_ARGB32_Premultiplied)
    g = mask.convertToFormat(QImage.Format_Grayscale8)
    alpha = QImage(g.constBits(), g.width(), g.height(), g.bytesPerLine(), QImage.Format_Alpha8)
    p = QPainter(out)
    p.setCompositionMode(QPainter.CompositionMode_DestinationIn)
    p.drawImage(0, 0, alpha)
    p.end()
    p = QPainter(layer)
    p.drawImage(box.topLeft(), out)
    p.end()
    return box


def timed(fn, repeat):
    times = []
    result = None
    for _ in range(repeat):
        t = time.perf_counter()
        result = fn()
        times.append((time.perf_counter() - t) * 1000)
    return result, times


def ms(times):
    return f"{statistics.median(times):8.1f} ms (first {times[0]:.1f})"


def share_test(gpu, layer):
    """A second context sharing with the global one (what every
    QOpenGLWidget gets under AA_ShareOpenGLContexts) draws the layer's
    texture with QOpenGLTextureBlitter: proof the screen view can use it."""
    from PySide6.QtGui import QOffscreenSurface
    from PySide6.QtOpenGL import QOpenGLFramebufferObject, QOpenGLTextureBlitter
    gpu.finish()
    ctx = QOpenGLContext()
    ctx.setShareContext(QOpenGLContext.globalShareContext())
    ctx.setFormat(gpu.context.format())
    ctx.create()
    surf = QOffscreenSurface()
    surf.setFormat(ctx.format())
    surf.create()
    ctx.makeCurrent(surf)
    sharing = QOpenGLContext.areSharing(ctx, gpu.context)
    fbo = QOpenGLFramebufferObject(256, 256)
    fbo.bind()
    f = ctx.functions()
    f.glViewport(0, 0, 256, 256)
    f.glClearColor(0, 0, 0, 0)
    f.glClear(0x4000)
    blit = QOpenGLTextureBlitter()
    blit.create()
    blit.bind()
    # the layer's top-left 256x256 of its 5800x3000, as is (row 0 at v = 0)
    from PySide6.QtCore import QRectF
    src = QOpenGLTextureBlitter.sourceTransform(QRectF(0, 0, 256, 256), layer_size := layer_dims(layer),
                                                QOpenGLTextureBlitter.OriginBottomLeft)
    blit.blit(layer.texture_id, QMatrix4x4(), src)
    blit.release()
    got = fbo.toImage(False)    # rows as GL has them: row 0 = y 0 = the layer's row 0
    ctx.doneCurrent()
    gpu.current()
    want = layer.read(QRect(0, 0, 256, 256))
    m, mx, _ = diff(want, got.convertToFormat(QImage.Format_ARGB32_Premultiplied))
    return sharing, mx.max()


def layer_dims(layer):
    from PySide6.QtCore import QSize
    return QSize(layer.width, layer.height)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--precision', default='half', choices=('half', 'byte'))
    ap.add_argument('--repeat', type=int, default=3)
    ap.add_argument('--save', default='')
    args = ap.parse_args()

    QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
    app = QApplication(sys.argv)
    from random_image_viewer.drawing import brushes, gpu_paint

    t = time.perf_counter()
    gpu = gpu_paint.shared()
    print(f"GPU: {gpu.renderer}  context {gpu.context.format().majorVersion()}."
          f"{gpu.context.format().minorVersion()}  made in {(time.perf_counter() - t) * 1000:.0f} ms;"
          f" layer {W}x{H} {args.precision}")

    base = make_layer()
    t = time.perf_counter()
    layer = gpu_paint.GpuLayer(W, H, args.precision)
    gpu.finish()
    t_alloc = (time.perf_counter() - t) * 1000
    _, up_full = timed(lambda: (layer.write(base), gpu.finish()), 3)
    print(f"alloc {t_alloc:.0f} ms; upload whole layer {ms(up_full)}")

    # readback / upload of 600x600
    r600 = QRect(2000, 1200, 600, 600)
    _, rb = timed(lambda: layer.read(r600), 20)
    patch = base.copy(r600)
    _, up = timed(lambda: (layer.write(patch, (2000, 1200)), gpu.finish()), 20)
    _, rb_full = timed(lambda: layer.read(), 2)
    print(f"read 600x600 {ms(rb)};  write 600x600 {ms(up)};  read whole layer {ms(rb_full)}")
    # round trip exact?
    back = layer.read(r600)
    m, mx, _ = diff(patch, back)
    print(f"round trip 600x600 max diff {mx.tolist()}")

    tips = [('soft', brushes.load_texture(brushes.SOFT))]
    if os.path.exists(EXTRA_TIP):
        tips.append(('hp_1', brushes.load_texture(EXTRA_TIP)))
    cases = [(400, 140), (400, 400), (2000, 20)]

    print()
    print(f"{'tip':6} {'case':>10} {'mode':6} {'CPU':>28} {'GPU (finished)':>28} {'GPU submit':>10}"
          f"   diff mean B,G,R,A / max B,G,R,A / %px>2")
    for name, tex in tips:
        for n, size in cases:
            stamps = make_stamps(n, size, seed=n * 7 + size)
            for mode in ('build', 'flat'):
                cpu_img = None

                def cpu_run():
                    nonlocal cpu_img
                    cpu_img = base.copy()
                    t0 = time.perf_counter()
                    if mode == 'build':
                        p = QPainter(cpu_img)
                        brushes.paint_stamps(p, tex, stamps, QTransform(), clip=(W, H))
                        p.end()
                    else:
                        cpu_flat_reference(cpu_img, tex, stamps, brushes.OPACITY_STEPS)
                    return (time.perf_counter() - t0) * 1000

                cpu_times = [cpu_run() for _ in range(args.repeat)]
                gpu_times, submit = [], []
                box = None
                for _ in range(args.repeat):
                    layer.write(base)
                    gpu.finish()
                    t0 = time.perf_counter()
                    if mode == 'build':
                        box = layer.paint_stamps(tex, stamps)
                    else:
                        box = layer.paint_flat(tex, stamps)
                    submit.append((time.perf_counter() - t0) * 1000)
                    gpu.finish()
                    gpu_times.append((time.perf_counter() - t0) * 1000)
                gpu_img = layer.read(box)
                cpu_crop = cpu_img.copy(box)
                mean, mx, over = diff(cpu_crop, gpu_img)
                print(f"{name:6} {n:5}x{size:<4} {mode:6} {ms(cpu_times):>28} {ms(gpu_times):>28}"
                      f" {statistics.median(submit):7.1f} ms"
                      f"   {', '.join(f'{v:.2f}' for v in mean)} / {mx.tolist()} / {over:.2f}%")
                if mode == 'flat':
                    # the app's own flat path (FlatStroke: swept body), for time only
                    def real_flat():
                        img = base.copy()
                        p = QPainter(img)
                        t0 = time.perf_counter()
                        brushes.paint_stamps(p, tex, stamps, QTransform(), clip=(W, H), flat=True)
                        p.end()
                        return (time.perf_counter() - t0) * 1000
                    print(f"{'':6} {'':10} {'(app FlatStroke CPU)':>35} {ms([real_flat() for _ in range(args.repeat)]):>20}")
                if args.save:
                    os.makedirs(args.save, exist_ok=True)
                    tag = f"{name}_{n}x{size}_{mode}"
                    cpu_crop.save(os.path.join(args.save, tag + '_cpu.png'))
                    gpu_img.save(os.path.join(args.save, tag + '_gpu.png'))

    sharing, mx = share_test(gpu, layer)
    print(f"\nshared context draws the layer texture: sharing={sharing}, max diff {mx}")


if __name__ == '__main__':
    main()
