"""Generate a splash screen PNG for PyInstaller's --splash flag."""

from PIL import Image, ImageDraw, ImageFont
import os

WIDTH, HEIGHT = 420, 160
BG = (30, 30, 30)
FG = (255, 255, 255)
SUB = (136, 136, 136)

img = Image.new("RGB", (WIDTH, HEIGHT), BG)
draw = ImageDraw.Draw(img)

# Try to use Segoe UI (Windows), fall back to default
def _font(size, bold=False):
    names = ["segoeuib.ttf", "segoeui.ttf", "arial.ttf", "arialbd.ttf"] if bold else ["segoeui.ttf", "arial.ttf"]
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()

title_font = _font(28, bold=True)
sub_font = _font(14)
emoji_font = _font(48)

# Dice emoji as text (fallback: just a square symbol)
try:
    draw.text((WIDTH // 2, 24), "\U0001f3b2", fill=FG, font=emoji_font, anchor="mt")
except Exception:
    draw.text((WIDTH // 2, 24), "[*]", fill=FG, font=title_font, anchor="mt")

draw.text((WIDTH // 2, 88), "Ova Viewer", fill=FG, font=title_font, anchor="mt")
draw.text((WIDTH // 2, 128), "Loading\u2026", fill=SUB, font=sub_font, anchor="mt")

out_path = os.path.join(os.path.dirname(__file__), "splash.png")
img.save(out_path)
print(f"Splash image saved to {out_path}")
