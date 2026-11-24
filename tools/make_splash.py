"""The launcher's splash image (shown by the exe itself the moment it starts,
before Python and Qt load - see the spec): one thin line in the middle of the
screen, nothing else. Just a sign that the app is starting.

The line is the globe's own equator: the same width as the globe in the Qt
splash and the grays along its seam, so when Qt takes over the line simply
grows the rest of the globe around it (see splash.py). This script measures the
globe in ova_viewer.png, writes the line image and prints the numbers
splash.py uses (EQUATOR_*), which must match:

    .venv\\Scripts\\python tools\\make_splash.py splash_boot.png
"""
import os
import sys

import numpy as np
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "splash_boot.png")

# The Qt splash fits the globe image into this box (splash.DynamicSplash).
BOX_W, BOX_H = 420, 300
STOPS = 16

src = Image.open(os.path.join(ROOT, "ova_viewer.png")).convert("RGBA")
im = np.asarray(src).astype(np.float32)
H, W = im.shape[:2]
lum = im[:, :, :3].mean(axis=2)
sphere = (im[:, :, 3] > 200) & (lum < 200)

# The seam: the biggest step down from the light upper half to the dark lower
# one, in the middle of the sphere. The line is its bright edge, the last
# light row.
xs = np.nonzero(sphere.any(axis=0))[0]
cx = (xs.min() + xs.max()) // 2
col = lum[:, cx - 40:cx + 40].mean(axis=1)
inside = np.nonzero(sphere[:, cx])[0]
steps = np.diff(col)[inside.min() + 5:inside.max() - 5]
seam = inside.min() + 5 + int(np.argmin(steps))           # last light row
row = np.nonzero(sphere[seam])[0]
x0, x1 = int(row.min()), int(row.max()) + 1

scale = min(BOX_W / W, BOX_H / H)
line_w = round((x1 - x0) * scale)

# Tones along that row, averaged over a few px so facet edges don't flicker.
rgb = im[seam - 1:seam + 1, x0:x1, :3].mean(axis=0)
k = np.ones(5) / 5
smooth = np.stack([np.convolve(rgb[:, c], k, mode="same") for c in range(3)], axis=1)
pos = np.linspace(0, len(smooth) - 1, line_w)
line = np.stack([np.interp(pos, np.arange(len(smooth)), smooth[:, c]) for c in range(3)], axis=1)
Image.fromarray(np.clip(line.round(), 0, 255).astype(np.uint8)[None, :, :], "RGB").save(out)

stops = []
for t in np.linspace(0, 1, STOPS):
    r, g, b = (int(round(v)) for v in smooth[int(round(t * (len(smooth) - 1)))])
    stops.append((round(float(t), 3), (r, g, b)))
print("saved", out, (line_w, 1))
print(f"EQUATOR_Y = {seam / H:.5f}        # row {seam} of {H}")
print(f"EQUATOR_X = ({x0 / W:.5f}, {x1 / W:.5f})   # x {x0}..{x1} of {W}")
print("EQUATOR_STOPS = (")
for t, c in stops:
    print(f"    ({t}, {c}),")
print(")")
