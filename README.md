# Ova Viewer

A fast, feature-rich media viewer for Windows built with PySide6. View images, PDFs, EPUBs, comic archives (CBR/CBZ), animated GIFs, and videos in a single window — with a full annotation layer and non-destructive image enhancement stack.

---

## Features

**Viewing**
- 📁 Open any folder (including subfolders) via button or drag & drop
- 🖼️ Images · 📄 PDFs (single / 2-page / 3-page book spread) · 📚 EPUBs · 🗜️ CBR/CBZ comics · 🎞️ Animated GIFs · 🎬 Videos
- 🔍 Zoom & pan (mouse wheel + right-click drag)
- 🔄 Rotate, flip horizontal/vertical
- 🖥️ Fullscreen (F11) and minimal/frameless mode
- 🕒 Auto-advance timer with circular countdown overlay
- ⏮️ History navigation (back/forward through viewed images)
- 🔀 Random or alphabetical sorting

**Annotation**
- 📏 Vertical, horizontal, and free-angle line tools
- ✏️ Freehand draw with pressure-sensitive thickness (pen/stylus/tablet)
- Undo, toggle visibility, clear all lines
- Save annotated view to file

**Color & Enhancement**
- 💉 **Color Snap eyedropper** — hover to preview, click to pick a color from the image
- 🪄 **Auto-extract palette** — extracts dominant colors into the floating palette panel
- 🎨 **Floating palette panel** — resizable, draggable, persists across tool switches; swatches organized by extraction session
- 🎞️ **CUBE LUT support** with GPU-accelerated (OpenCL) processing and adjustable strength
- Grayscale, contrast, and gamma sliders with per-effect toggles

**Other**
- 🎨 Adaptive dark/light theme (follows Windows system setting)
- ⌨️ Full keyboard shortcut set (see [SHORTCUTS.md](SHORTCUTS.md))
- Copy current image to clipboard (📋)

---

## Installation

```sh
git clone https://github.com/designOVAcz/ova-viewer.git
cd ova-viewer
pip install -r requirements.txt
# Optional GPU LUT acceleration:
# pip install pyopencl
python main.py
```

---

## Usage

1. Drag a folder into the app or click 📁 to open one
2. Navigate with **← →** or enable the auto-advance timer (**⚡**)
3. Annotate with the line/draw tools in the toolbar
4. Toggle **💉** to pick colors from the image; use **🪄** to auto-extract a palette
5. Load a `.cube` LUT file and adjust strength with the slider
6. Press **F11** for fullscreen, **Esc** to exit

---

## Build a Standalone Executable

```sh
pip install pyinstaller
pyinstaller "Ova Viewer.spec"
# Output: dist/Ova Viewer.exe
```

---

## Screenshot

![Main Window](screenshots/main_window.png)

---

## License

MIT License.

