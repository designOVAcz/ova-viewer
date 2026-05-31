# Ova Viewer

A simple, modern, cross-platform desktop app to view random images from any folder (including subfolders). Great for artists, inspiration, or just browsing your photo collection!

---

## Features

- 📁 **Open Any Folder:** Load images from a folder and its subfolders
- 🖱️ **Drag & Drop Support:** Simply drag folders from Explorer/Finder directly into the app
- 🎲 **Smart Navigation:** Random image viewing with alphabetical sorting toggle  
- 🔀 **Sorting Toggle:** Switch between random and alphabetical order with one click
- ⏮️ ⏭️ **History Navigation:** Go back and forward through viewed images
- 🕒 **Auto-Advance Timer:** Automatically switch to new images at set intervals with countdown overlay
- 🔍 **Zoom & Pan:** Mouse wheel zoom, right-click drag to pan when zoomed
- 🖥️ **Fullscreen Mode:** Toggle fullscreen with F11 key or toolbar button for immersive viewing
- 👁️ **Minimal Mode:** Hide all UI elements for distraction-free viewing, drag window in frameless mode
- 🖱️ **Smart Context Menu:** Right-click for actions (auto-hides when zoomed to avoid pan interference)
- 🎨 **OS-Adaptive Theme:** Automatically detects Windows dark/light mode preferences
- ⌨️ **Full Keyboard Support:** Complete shortcut system for all operations (arrows, F11, Esc, Ctrl+H/V, etc.)
- 🔄 **Image Transformations:** Flip horizontal/vertical with visual state indicators
- 📐 **Professional Line Drawing:** Three-mode annotation system (vertical, horizontal, free lines)
- 🖊️ **Pen Pressure Support:** Full pressure-sensitive drawing with tablets/styluses (variable line thickness)
- 🎨 **CUBE LUT Support:** Professional color grading with .cube LUT files and GPU acceleration
- 🖥️ **Cross-platform:** Works on Windows, macOS, and Linux

---

## Installation

1. **Clone this repo:**
   ```sh
   git clone https://github.com/YOUR_USERNAME/random-image-viewer.git
   cd random-image-viewer
   ```

2. **Install dependencies:**
   ```sh
   pip install pyside6
   # Optional: pip install pyopencl (for GPU-accelerated LUT processing)
   ```

---

## Usage

1. **Open Images:** Drag & drop any folder into the app or use the 📁 button
2. **Navigate:** Use arrow keys, toolbar buttons, or enable auto-advance timer
3. **Modes:** Toggle between Random (🎲) and Alphabetical (🔀) sorting
4. **Viewing:** Press F11 for fullscreen, right-click "Show/Hide UI" for minimal mode
5. **Enhance:** Use sliders for adjustments, apply LUTs for color grading
6. **Annotate:** Use line drawing tools (📏 ━ ╱) for image analysis
7. **Free Draw:** Use 🖊️ tool for pressure-sensitive drawing with pen/stylus (variable line thickness)

## Keyboard Shortcuts

- **←/→:** Navigate images  
- **F11:** Toggle fullscreen
- **Esc:** Exit fullscreen or minimal mode
- **Ctrl + +/-/0:** Zoom in/out/reset
- **Ctrl + H/V:** Flip horizontal/vertical

---

## Screenshot

![Main Window](screenshots/main_window.png)

---

## Compiling to a Standalone Binary

You can compile this app to a standalone executable for Windows, Linux, or Mac using [PyInstaller](https://pyinstaller.org/).

1. **Install PyInstaller:**
   ```sh
   pip install pyinstaller
   ```
2. **Build the Executable:**
   ```sh
   pyinstaller --noconfirm --onefile --windowed --icon=icon.ico --add-data "icon.svg;." --add-data "icon.png;." --add-data "icon.ico;." main.py
   ```
   - The binary will be in the `dist/` folder.
   - For cross-compiling, build on the target OS or use a cross-compilation toolchain.

---

## License

MIT License. **Enjoy browsing your images!**




