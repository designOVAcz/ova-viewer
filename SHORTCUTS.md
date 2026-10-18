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
| `Ctrl+Shift+R` | Reset tool panels to the default stack |
| `Delete` | Move the current file to the Recycle Bin |

## Navigation

| Input | Action |
|---|---|
| `Left` / `Right` | Previous / next image (seek ±5 s while a video plays) |
| Mouse wheel | Previous / next image (when not zoomed in) |
| Middle-click | Next image |

While something you drew is still unsaved, the mouse wheel zooms instead of
changing the image, so a stray scroll cannot throw the drawing away.

## Thumbnail Grid

| Input | Action |
|---|---|
| `G` / ▦ button | Open / close the thumbnail grid |
| Click | Frame it: zoom so it fills the window and centre it; a GIF, animated WebP or video starts playing in place (muted) |
| Click the framed picture again | Fly back to the view you came from (a playing GIF / video pauses / resumes instead) |
| Click the empty background | Stop playback and fly back to the view you came from |
| Double-click / `Enter` / `Space` | Open that item in the viewer |
| Arrow keys, `PgUp` / `PgDn`, `Home` / `End` | Move the selection |
| Mouse wheel | Smooth scroll (`Shift` + wheel: sideways) |
| Right button held + mouse wheel, `Ctrl` + wheel, `Ctrl++` / `Ctrl+-` | Zoom in / out around the pointer |
| Right-drag (or middle-drag) | Pan in any direction; flick and let go to glide |
| `Ctrl+0` | Zoom back to fit the width |
| `Esc` | Back to the viewer |

## Drawing

| Shortcut | Action |
|---|---|
| `I` | Colour snap (eyedropper) on / off |
| `Ctrl+Z` | Undo the last mark (while placing a polygon: remove its last corner) |
| `Ctrl+Shift+A` | Toggle antialiasing |
| `Ctrl+Shift+P` | Test pen pressure |

### Polygon fill ⬡

| Input | Action |
|---|---|
| Click | Place a corner |
| Double-click / `Enter` / click the first corner | Fill the polygon |
| `Backspace` | Remove the last corner |
| `Esc` | Cancel the polygon |

## Zoom & View

| Input | Action |
|---|---|
| `Ctrl++` (or `Ctrl+=`) | Zoom in |
| `Ctrl+-` | Zoom out |
| `Ctrl+0` | Reset zoom |
| `Ctrl` + mouse wheel | Zoom at the pointer |
| Right button held + mouse wheel | Zoom at the pointer |
| Mouse wheel (zoomed in) | Pan up / down (`Shift` + wheel: left / right) |
| Right-drag (zoomed in) | Pan |
| Left-drag (zoomed in, no tool on) | Pan |
| Right-click (not zoomed in) | Context menu |
| Double-click | Exit fullscreen |
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

## Supported Formats

- **Images:** `.jpg`, `.jpeg`, `.png`, `.bmp`, `.gif`, `.webp`, `.jp2`, `.j2k`, `.jpf`, `.jpx`
  (animated `.gif` and `.webp` play like a video, with the same controls)
- **Videos:** `.mp4`, `.avi`, `.mkv`, `.mov`, `.webm`, `.flv`, `.wmv`
- **Documents:** `.pdf`, `.epub`
