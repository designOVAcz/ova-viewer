# Keyboard Shortcuts & Controls

## General

| Shortcut | Action |
|---|---|
| `Esc` | Exit fullscreen / bring the hidden UI back |
| `F11` | Toggle fullscreen |
| `Ctrl+Esc` | Force exit fullscreen |
| `Alt+F4` | Close application |
| `Ctrl+O` | Open folder |
| `Ctrl+V` | Paste an image or a file path from the clipboard |
| `Ctrl+G` | Go to image number / PDF page |
| `Ctrl+U` | Hide / show all panels (minimal mode) |
| ▣ in the status bar | Hide / show the tool panels only (frame and status bar stay) |
| Right-click → Show Log | Live log (marked "new errors" / "new warnings" when something needs a look) |
| `Ctrl+Shift+R` | Reset tool panels to the default stack (⟲ bottom-left does the same); 🎨 Color, 🖌 Brush and 📚 Layers close, and open docked down the left edge again |
| `Delete` | Move the current file to the Recycle Bin |
| `Tab` | Start / stop the slideshow (⚡ auto-advance in the viewer, ▶ in the grid) — a running slideshow carries on when you switch between viewer and grid |

## Navigation

| Input | Action |
|---|---|
| `Left` / `Right` | Previous / next image (seek ±5 s while a video plays) |
| Mouse wheel | Previous / next image (when not zoomed in) |
| Middle-click | Next image |

While a drawing tool is on and something you drew is still unsaved, the mouse
wheel zooms instead of changing the image, so a stray scroll cannot throw the
drawing away. Turn the tool off and the wheel changes image again (which clears
the drawing).

## Thumbnail Grid

| Input | Action |
|---|---|
| `G` / ▦ button / double-click the picture | Open / close the thumbnail grid — with a PDF, EPUB or CBR/CBZ open it shows that document's pages (double-click a page to go to it) |
| 📄 in the GRID panel | Switch between the open document's pages and the whole folder |
| Click | Frame it: zoom so it fills the window and centre it; a GIF, animated WebP or video starts playing in place (muted) |
| Click the framed picture again | Fly back to the view you came from (a playing GIF / video pauses / resumes instead) |
| Click the empty background | Stop playback and fly back to the view you came from |
| Double-click / `Enter` / `Space` | Open that item in the viewer |
| Arrow keys, `PgUp` / `PgDn`, `Home` / `End` | Move the selection; a GIF / video plays when the selection stops on it. While a picture is framed, the frame moves along with it |
| `Delete` | Move the selected file to the Recycle Bin; the next one takes its place (not for a document's pages) |
| Mouse wheel | Smooth scroll (`Shift` + wheel: sideways) |
| Right button held + mouse wheel, `Ctrl` + wheel, `Ctrl++` / `Ctrl+-` | Zoom in / out around the pointer. Out it stops once at the fit, then goes smaller - down to 10 %, to see the picture as a thumbnail (you can still paint on it) |
| Right-drag (or middle-drag) | Pan in any direction; flick and let go to glide |
| Middle-click | Next picture (framed; random with 🔀); during a slideshow it skips ahead and the show carries on |
| `Ctrl+0` | Zoom back to fit the width |
| ▶ in the GRID panel | Slideshow: frame one picture after another (GIFs / videos play); seconds and order follow ⚡ and 🔀, and with ⏭ autoplay-next on a GIF / video plays to its end before the show moves on. Click another picture to jump the show there and carry on from it. ▶ again (or a drag, wheel, key or a click on the empty background) stops it on the last picture / clip — click that picture to fly back to the overview |
| `Esc` | Back to the viewer |

## Drawing

| Shortcut | Action |
|---|---|
| `I` | Colour snap (eyedropper) on / off. Picked up while a tool is on (brush, eraser, a fill …), one pick takes the colour and hands back to that tool; with no tool on it stays on for more picks |
| `Ctrl+Z` | Undo the last mark (while placing a polygon: remove its last corner) |
| T slider (opacity, with %) | Opacity of the next marks: click anywhere on it to jump there, then drag; wheel / arrow keys step 5 %. Marks already drawn keep theirs |
| `Ctrl+Shift+A` | Toggle antialiasing |
| `Ctrl+Shift+P` | Test pen pressure |

Drawing also works on a paused video or GIF. Marks stay on screen through play and seek, and are cleared when you move to another file. Unticking 🖼 Picture in 📚 Layers hides the video or GIF too and leaves only your marks on the background; sound and subtitles carry on.

### Brush tip 🖌

| Input | Action |
|---|---|
| 🖌 next to the brush size | Brush panel: Round (the plain pen) or a PNG texture |
| Free draw picked up | Opens the colour box too (pinned) if it is not open. All panels hide when the window is minimised and come back on restore |
| `[` / `]` | Brush size smaller / bigger, by about 10 % a press (hold to keep going) |
| `V` / ⬚ (Draw panel) | Select + transform: draw a lasso round marks on the selected layer (or click one; `Ctrl+A` takes all of them). Drag inside the frame to move them, a corner to scale them around the opposite corner, just outside it to turn them (`Shift`: 15° steps). `Enter` or a double-click inside applies, `Esc` cancels, `Ctrl+Z` undoes. Brush strokes are painted again at the new size, so they stay sharp. Ruler lines across the whole picture are not taken |
| 💾 in ◧ Studio (right-click any time) | Save the painting only, the picture only, or both side by side, at 1× / 2× / 4×. Each is painted again at that size (strokes, spray and lines stay sharp, line widths grow with it), upright and without the view's effects. A plain click outside Studio still saves the view as shown |
| 👁 (Draw panel) | Show / hide the guides only (vertical and horizontal ruler lines, free lines, polylines); brush strokes, fills and snapshots stay. Picking a guide tool shows them again. Hide a whole layer with its tick in 📚 |
| 🪮 Rake (a tip in 🖌) | A row of tines the brush size wide, turned across the stroke: Paint lays parallel lines, Smudge combs each tine's colour along, Erase scratches parallel gaps. Tines / Tine / Wobble sliders in the Brush panel (they show with the Rake). Stamps are spaced closer automatically so each tine draws a line, not dots |
| `1` / `2` / `3` / `4` / `5` / `6` | Brush mode: Paint / Blend / Smudge / History / Pick blend / Erase (also picks up the brush) |
| 🧽 Erase (6th brush mode, ⌫ in the Draw panel) | The brush rubs out instead of painting: the same tip, size, pressure and opacity, so a soft or textured tip erases softly or with its grain, and a low opacity only thins the paint. It takes paint only off the selected layer, never the picture or other layers. `Ctrl+Z` brings back what it rubbed out. Clicking ⌫ again (or another mode) returns to the mode you had before |
| 🎯 Pick blend (5th brush mode) | Blend, but the brush colour is picked first: at the first touch the colour on screen under the pen becomes the brush colour, as the eyedropper would, then it blends with that - so a blend never brings in a colour that is not already there. The colour box shows the picked colour and keeps it after the stroke. Mix works as in Blend |
| 🖍 / 💧 / 👆 / 🕘 next to 🖌 | Brush mode, click to switch: Paint (its own colour) → Blend (a blender brush: it drags the pixels under it - colours and texture - along and mixes them, and mixes the brush colour in; Mix low: more brush colour, shorter drag; 100 %: only moves what is there, the furthest) → Smudge (no colour of its own: picks up what is under the pen and drags it along, Carry sets how far - the same whatever the Spacing, and the smear always thins out, also over empty parts of a layer) → History (paints the original picture back, pixel for pixel, through the tip - like Photoshop's history brush - over your paint; Tint mixes the brush colour in, 0 % is the picture itself). Also at the top of the Brush panel. Each mode keeps its own Mix / Carry / Tint |
| Sample (Blend / Smudge) | Where they pick their colour up: **this layer and below** (default - the picture with its effects and the layers up to this one, never those above), **this layer only** (just its own paint; on its empty parts a smudge lays nothing, as in Photoshop / Krita), or **everything on screen** (all layers) |
| Mix (Blend / Smudge) | **Like paint (pigment)** (default): colours mix as paint does - blue and yellow make green, red and blue a purple, white lightens without washing out (Kubelka-Munk, as in spectral.js / Rebelle). **Like light**: a plain average - blue and yellow make grey. Blend's paint mixing runs on the GPU (OpenCL) when there is one |
| Size (Brush panel, top) | Brush size, the toolbar slider's twin: the two move together |
| 📁 / `+` in the panel | Add every PNG in a folder / single PNGs (remembered); `−` takes one off the list |
| Rotate | Pen tilt turns each stamp; with no tilt (mouse, pen upright) it follows the stroke direction. The tip then keeps the pen's lean whichever way you draw, so a flat tip drawn along its long side leaves a thin line, like a real flat brush. Stroke direction (ignore tilt): the tip turns with the stroke and always lies across it, at full width in every direction (HeavyPaint-style). Or a fixed angle. Angle / Angle ± add a turn and a random turn |
| Paint | Flat strokes (default, HeavyPaint-style): the tip is dragged along like paint, so it doesn't build up on itself: it starts with the tip's own shape and ends with it (its ragged front, at the size the last pen pressure gave it), the body widening and narrowing smoothly with pressure in between, and its grain and gaps run on along the stroke as streaks (a dense part of the tip a strong streak, a sparse one a faint one), at any spacing. Build up: every stamp adds ink |
| Spacing | Gap between stamps as a share of their size |
| Hue ± / Sat ± / Value ± | Colour jitter: once per stroke, on every stamp, or across the tip (streaks): every bristle its own colour, running on along the stroke. With streaks, Blend and Smudge mix / drag the colour under each streak on its own, so a smudge pulls the colours under the whole width along side by side |
| Jitter lines & shapes too (Brush panel) | Ruler lines, straight lines, polylines and filled shapes (lasso, polygon, rectangle, ellipse, triangle) get a colour jittered by Hue / Sat / Value ± as well - a new one for each line or shape, shown in its preview already. Works with the round pen too |
| Bristles (first tip after Round) | Built-in bristle brush: a dense body of hairs with a ragged edge. Try it with streaks and some Hue / Value ± |
| Dust (airbrush) (next tip) | Built-in particle / airbrush tip: a cloud of specks, thick in the middle. Its stamps always build up (spray), never flat. Best big, with Angle ± 180°, some Scatter and Size ±; with **Colour jitter: every speck (particles)** and Hue / Sat / Value ± every speck gets its own colour |
| ↺ (Brush panel, top right) | Resets every brush setting (spacing, angle, jitter, scatter, mix …) to the defaults; the tip, mode, size and colour stay |
| Spray (procedural) (tip after Dust) | A spray drawn as real dots, not a texture: sharp at any size and zoom. **Spread** makes the cloud up to 20x the brush size (400 px -> 8000 px), **Specks** sets how many per stamp (a few for a sparse dusting), **Speck** their size. These three sliders show only with this tip. With **Colour jitter: every speck (particles)** every dot gets its own colour |
| Scatter / Size ± | Scatter throws each stamp off the stroke at random, up to that share of its size; Size ± makes each stamp up to that much smaller. Any tip |
| Pressure: size / opacity | What pen pressure changes (with 🎨 on) |

History paints the picture as it was loaded (before effects and marks); on a layer with ✨ effects on, the effects then apply to it like to the picture, so to bring the original colours back over an effect, untick ✨ for that layer. A Blend stroke is kept as the pixels it made (like a 📸 snapshot: it zooms, turns, erases and undoes like any mark). Blend and Smudge read the view when the stroke starts (picture, effects and earlier marks, as Sample says) and keep the colours in the stroke, so it stays the same on zoom, undo and save. Round uses a soft round stamp in those modes, with the stamp settings below. A texture is white (or bright) where it paints: transparent PNGs and grey-on-black ones both work. The cursor is the tip's outline at the brush size, turned the way the next stamp will be: with the pen's lean while hovering, with the stroke while painting. In Build up, the T (opacity) slider is the flow: lower it and the stamps build up, so the texture shows. A grainy or bristly tip with streaks colour jitter gives the HeavyPaint oil look. Textured strokes are part of the picture: they grow when you zoom in, unlike the round pen, whose width stays in screen pixels.

### Layers 📚

| Input | Action |
|---|---|
| `L` | Layers panel on / off |
| ✎ chip in the status bar | The layer you are drawing on (shown once there is more than one layer): 📄 a canvas layer, ✨ the effects reach it, struck through while it is hidden. Click it for 📚. Switching layers also shows the name beside the pointer for a moment |
| Click a layer | Draw on it; `Ctrl+Z` and the eraser work on this layer only |
| Tick box | Show / hide the layer (drawing on a hidden layer shows it again) |
| Double-click a layer | Rename it |
| Blend list / Opacity | How the selected layer mixes with what is under it, at any time after drawing |
| ✨ Effects apply to this layer | The view's effects (LUT, contrast, gamma, grayscale, curves, value / colour / object groups, form lines, edges) also run over this layer's paint - strokes, lines and fills - as if it were part of the picture. On by default; untick it to keep a layer's paint on top, untouched. The stroke you are drawing already shows with the effects, as it will look when done. Still pictures only (over a video / GIF the paint stays on top) |
| `+` / `−` / `▲` / `▼` | New layer / delete layer (no undo) / move up / move down |
| 🖼 Picture / 📄 Canvas (Layers panel, under the layers) | The picture is a layer: the stack, top to bottom, is your layers, 🖼 Picture, 📄 Canvas, Background. Untick 🖼 Picture to hide it (there is no separate toolbar button any more) - the canvas, or the background, shows in its place. Its own blend list and Opacity mix the picture into what is under it - the canvas, or the background - like a layer into the ones below (Opacity 50 % over the grey canvas: a faded picture to paint over; Multiply: the picture darkens the canvas). New Canvas with a picture open makes the 📄 Canvas row: a middle grey sheet the size of the picture, under it - the picture is unticked so you sketch on grey; tick 🖼 Picture to see the picture under your paint, untick 📄 Canvas to hide the sheet. The canvas goes with the drawing when you move to another file, and a picture the sketch hid comes back |
| 📄 in the Layers panel | Canvas layer: a sheet of mid-grey paper over the picture to sketch on, a layer like any other - untick it to see the picture again, Opacity makes it tracing paper, ✨ decides whether the effects reach it (untick: effects on the picture underneath, the sketch stays clean), and the eraser and 🕘 History cut through it to the picture. The paper goes with the drawing when you move to another file. New Canvas with nothing open (or on a sketch) makes a blank sketch picture |
| 📸 (next to 📚) | Snapshot: what is on screen now — picture with its effects, marks and background — goes onto a new layer above the selected one, like 💾 would save it. It zooms and turns with the picture, can be blended, erased or undone (`Ctrl+Z`), and goes when you move to another file |
| Background swatch | Colour of the canvas around the picture (default black, remembered); it also shows when the picture is hidden |

Every drawing tool also works on the background around the picture, so notes can go beside it. They land on the selected layer, stay in place relative to the picture when you zoom, pan or rotate, and are saved by 💾. Crop keeps only what is inside the picture.

### Crop ✂

| Input | Action |
|---|---|
| `C` | Crop tool on / off |
| Drag | Draw the frame |
| Drag an edge / corner, or inside the frame | Resize / move the frame |
| Double-click inside / `Enter` | Cut: saves a new picture to Downloads, the original stays |
| `Esc` | Cancel |

### Straight line

| Input | Action |
|---|---|
| Press, drag, lift the pen | Draw the line, from where you pressed to where you lift |
| Click, then click the end point | The same line, in two clicks |

### Polygon fill ⬡

| Input | Action |
|---|---|
| Click | Place a corner |
| Double-click / `Enter` / click the first corner | Fill the polygon |
| `Backspace` | Remove the last corner |
| `Esc` | Cancel the polygon |

### Polyline 〽

Polygon fill without the fill: straight lines in the current colour, transparency and brush size.

| Input | Action |
|---|---|
| Click | Place a corner |
| Double-click / `Enter` | Finish as an open line |
| Click the first corner | Close it into an outline |
| `Backspace` / `Ctrl+Z` | Remove the last corner |
| `Esc` | Cancel the polyline |

## Zoom & View

| Input | Action |
|---|---|
| `Ctrl++` (or `Ctrl+=`) | Zoom in |
| `Ctrl+-` | Zoom out |
| `Ctrl+0` | Reset zoom |
| ⟲ and the % at the left of the status bar | The zoom, in % of the picture's real size; ⟲ fits the picture to the window again (as `Ctrl+0`) |
| Thin white line along the bottom of the status bar | Drawn marks are being laid again for the new zoom (many textured strokes can take a few seconds); it fills as it goes and goes away when done. Until then the marks shown are a quick stretched copy, and parts that were off screen before may be missing for that moment |
| `Ctrl` + mouse wheel | Zoom at the pointer |
| Right button held + mouse wheel | Zoom at the pointer |
| Mouse wheel (zoomed in) | Pan up / down (`Shift` + wheel: left / right) |
| Right-drag (zoomed in) | Pan |
| Two fingers on a Wacom tablet's touch surface (zoomed in) | Pan in any direction - read straight from the tablet (Wacom's multi-touch library), since its driver passes on vertical scrolling only. Pinch still zooms through the driver's Zoom gesture. Not while you paint: with the pen near the tablet (or heard from in the last half second) touch does not pan, and a resting palm / wrist is not counted as a finger |
| Left-drag (zoomed in, no tool on) | Pan |
| Right-click (not zoomed in) | Context menu |
| Double-click | Open the thumbnail grid (no drawing tool on); with nothing open, exit fullscreen |
| `Ctrl+H` | Flip horizontal |

Flip vertical has no working shortcut: the context menu lists `Ctrl+V`, but
`Ctrl+V` pastes. Use the flip button or the context menu.

## Enhancements

| Shortcut | Action |
|---|---|
| `Ctrl+R` | Reset all enhancements |

## Video & GIF Playback

| Input | Action |
|---|---|
| Click the timeline | Jump to that moment |
| Drag the timeline | Scrub (a drag can start from the click) |
| Hover the timeline | Preview the frame at that moment |
| 🔁 button | Loop: play the clip again when it ends (turns off ⏭ autoplay-next) |
| ⏭ button | Autoplay next: move to the next item when the clip ends (turns off 🔁 loop) |

| Shortcut | Action |
|---|---|
| `Space` | Play / pause (video or animated GIF) |
| `M` | Toggle mute |
| `Left Arrow` | Seek back 5 seconds |
| `Right Arrow` | Seek forward 5 seconds |

## Drag & Drop

| Drop Target | Action |
|---|---|
| Folder | Open folder and load all media |
| PDF file | Open PDF viewer |
| EPUB file | Open EPUB viewer |
| Image / GIF / Video file | Open file and load parent folder for navigation |
| Anything, with ➕ add mode on (or `Ctrl` held) | Add it to the current list instead of replacing it — what is on screen stays, duplicates are skipped |

`Ctrl` + click on 📁 also adds the chosen folder just this once. In the 📁 dialog, Ctrl/Shift-click to pick several folders at once.

## Supported Formats

- **Images:** `.jpg`, `.jpeg`, `.png`, `.bmp`, `.gif`, `.webp`, `.jp2`, `.j2k`, `.jpf`, `.jpx`
  (animated `.gif` and `.webp` play like a video, with the same controls)
- **Videos:** `.mp4`, `.avi`, `.mkv`, `.mov`, `.webm`, `.flv`, `.wmv`
- **Documents:** `.pdf`, `.epub`
