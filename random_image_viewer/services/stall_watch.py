"""⏳ Stall watch: any time the app is busy for over a second, a loader shows -
whatever the work is - and what kept it busy is written down.

The work runs on the app's main thread, so nothing Qt can paint while it
goes on: a card of its own (see _ProgressCard) works only where the work
itself repaints it step by step. This watches from a thread of its own:
the main thread's heartbeat (a 100 ms timer) stops coming, and after
AFTER_S this thread shows the app's thin amber busy line along the bottom of
the window (as _BusyLine), its amber piece running across: a layered Win32
window shown and animated by Windows from this thread, so it shows while
the main thread is stuck. Its pixels are made with numpy here - no Qt.

Meanwhile it samples where the main thread is (sys._current_frames) a few
times a second; when the stall ends, it is written to
%LOCALAPPDATA%/OvaViewer/stalls.log - the code that took the time, so a
hang seen once can be found and fixed.

Not shown while one of the app's own loaders is up (hold / release), nor
when another app is in front, nor minimised.
"""

import collections
import ctypes
import os
import sys
import threading
import time

AFTER_S = 1.0           # busy this long: the loader shows
BEAT_MS = 100           # the main thread's heartbeat
SAMPLE_S = 0.1          # where the main thread is, this often while stalled
LOG_MAX = 2_000_000     # bytes: then the log starts over (the old one kept as .old)
LINE_H = 3              # logical px: the busy line along the window's bottom (as _BusyLine)
SWEEPS_PER_S = 0.8      # its amber piece runs across this often
AMBER = (240, 160, 48)  # the app's amber
TRACK_ALPHA = 40        # the line's faint white track


def log_path():
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "OvaViewer", "stalls.log")


class StallWatch:
    def __init__(self, window):
        self._window = window
        self._last = time.monotonic()
        self._holds = 0
        self._main_id = threading.main_thread().ident
        self._scale = None              # the window's device pixel ratio (from the beat)
        self._hwnd = None               # the main window's (read by the thread)
        self._running = False
        self._thread = None
        self._timer = None
        self._ui = None                 # the loader window (the thread's; tests read .shown)

    # -- main thread -----------------------------------------------------------

    def start(self):
        if sys.platform != "win32" or self._running:
            return
        from PySide6.QtCore import QTimer
        try:
            self._hwnd = int(self._window.winId())
        except Exception:
            return
        self._beat()
        self._timer = QTimer(self._window)
        self._timer.setInterval(BEAT_MS)
        self._timer.timeout.connect(self._beat)
        self._timer.start()
        self._running = True
        self._thread = threading.Thread(target=self._run, name="stall-watch", daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._timer is not None:
            self._timer.stop()

    def hold(self):
        """One of the app's own loaders is up: this one stays away."""
        self._holds += 1

    def release(self):
        self._holds = max(0, self._holds - 1)

    def _beat(self):
        self._last = time.monotonic()
        try:
            self._scale = float(self._window.devicePixelRatioF())
        except Exception:
            pass

    # -- the watching thread -------------------------------------------------

    def _run(self):
        ui = None
        try:
            ui = _LayeredCard()
        except Exception as e:
            print(f"⏳ stall watch: no loader window ({e})")
        self._ui = ui
        stalled_at = None
        samples = []
        others = collections.Counter()
        next_sample = 0.0
        while self._running:
            if ui is not None:
                ui.pump()
            now = time.monotonic()
            last = self._last
            age = now - last
            if age > AFTER_S:
                if stalled_at is None:
                    stalled_at = last
                    samples = []
                    others = collections.Counter()
                    next_sample = 0.0
                if now >= next_sample:
                    next_sample = now + SAMPLE_S
                    stack = self._main_stack()
                    if stack:
                        samples.append(stack)
                    others.update(self._other_threads())
                if ui is not None:
                    if self._holds == 0 and self._ours_in_front():
                        try:
                            ui.show(self._hwnd, self._scale or 1.0, (age * SWEEPS_PER_S) % 1.0)
                        except Exception as e:
                            print(f"⏳ stall watch: {e}")
                            ui = None
                    else:
                        ui.hide()
            else:
                if ui is not None:
                    ui.hide()
                if stalled_at is not None:
                    self._write(last - stalled_at, samples, others)
                    stalled_at = None
            time.sleep(0.03)
        if ui is not None:
            ui.hide()

    def _ours_in_front(self):
        u = _user32()
        if self._hwnd and u.IsIconic(ctypes.c_void_p(self._hwnd)):
            return False
        fg = u.GetForegroundWindow()
        if not fg:
            return False
        pid = ctypes.c_ulong()
        u.GetWindowThreadProcessId(ctypes.c_void_p(fg), ctypes.byref(pid))
        return pid.value == os.getpid()

    def _main_stack(self):
        frame = sys._current_frames().get(self._main_id)
        if frame is None:
            return None
        # walked by hand: traceback.extract_stack reads the source lines
        # (linecache stats every file) - 13 ms a sample, taken from the
        # very thread being watched
        out = []
        while frame is not None:
            code = frame.f_code
            out.append((os.path.basename(code.co_filename), frame.f_lineno, code.co_name))
            frame = frame.f_back
        out.reverse()
        return out

    def _other_threads(self):
        """What the app's other threads (an autosave ...) are on: a thread
        holding Python's lock in one long call stalls the main one too."""
        names = {t.ident: t.name for t in threading.enumerate()}
        out = []
        for ident, frame in sys._current_frames().items():
            name = names.get(ident, "?")
            if ident in (self._main_id, threading.get_ident()) or name in ("OvaHangWatch",):
                continue
            code = frame.f_code
            out.append(f"{name}: {os.path.basename(code.co_filename)}:{frame.f_lineno} {code.co_name}")
        return out

    def _write(self, seconds, samples, others=None):
        """The stall into the log: how long, where the time went (the
        innermost lines, and the call chain most samples share)."""
        if seconds < AFTER_S:
            return
        try:
            lines = [f"{time.strftime('%Y-%m-%d %H:%M:%S')}  busy {seconds:.1f} s  "
                     f"({len(samples)} samples)"]
            if samples:
                leaves = collections.Counter(s[-1] for s in samples)
                lines.append("  where it was (innermost):")
                for (f, n, name), c in leaves.most_common(6):
                    lines.append(f"    {c * 100 // len(samples):3d}%  {f}:{n} {name}")
                # the frames (by function) most samples go through, outer -> inner
                calls = collections.Counter()
                for s in samples:
                    for key in {(f, name) for f, _n, name in s}:
                        calls[key] += 1
                common = [(f, n, name) for f, n, name in samples[0]
                          if calls[(f, name)] >= len(samples) / 2]
                lines.append("  call chain (in half the samples or more, outer -> inner):")
                lines += [f"    {f}:{n} {name}" for f, n, name in common[-18:]]
                if others:
                    lines.append("  other threads meanwhile:")
                    lines += [f"    {c * 100 // len(samples):3d}%  {what}" for what, c in others.most_common(5)]
            path = log_path()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            if os.path.isfile(path) and os.path.getsize(path) > LOG_MAX:
                os.replace(path, path[:-4] + ".old.log")
            with open(path, "a", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n\n")
            print(f"⏳ The app was busy {seconds:.1f} s - see {path}")
        except Exception as e:
            print(f"⏳ stall watch: could not write the log ({e})")


_USER32 = None


def _user32():
    """user32 of this module's own: argtypes set on ctypes.windll.user32
    would change them for the rest of the app."""
    global _USER32
    if _USER32 is None:
        _USER32 = ctypes.WinDLL("user32")
    return _USER32


class _LayeredCard:
    """A see-through, click-through, never-focused topmost popup owned by
    the watching thread: Windows shows it while the main thread is stuck."""

    def __init__(self):
        from ctypes import wintypes as w
        self.w = w
        u, g = _user32(), ctypes.WinDLL("gdi32")    # (own copies: the argtypes set here stay here)
        self.u, self.g = u, g
        u.CreateWindowExW.restype = w.HWND
        u.CreateWindowExW.argtypes = [w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD, ctypes.c_int, ctypes.c_int,
                                      ctypes.c_int, ctypes.c_int, w.HWND, w.HMENU, w.HINSTANCE, w.LPVOID]
        u.GetDC.restype = w.HDC
        u.GetDC.argtypes = [w.HWND]
        u.ReleaseDC.argtypes = [w.HWND, w.HDC]
        u.UpdateLayeredWindow.argtypes = [w.HWND, w.HDC, ctypes.c_void_p, ctypes.c_void_p, w.HDC,
                                          ctypes.c_void_p, w.COLORREF, ctypes.c_void_p, w.DWORD]
        u.UpdateLayeredWindow.restype = w.BOOL
        u.ShowWindow.argtypes = [w.HWND, ctypes.c_int]
        u.SetWindowPos.argtypes = [w.HWND, w.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.UINT]
        u.GetClientRect.argtypes = [w.HWND, ctypes.POINTER(w.RECT)]
        u.ClientToScreen.argtypes = [w.HWND, ctypes.POINTER(w.POINT)]
        u.IsIconic.argtypes = [w.HWND]
        u.GetForegroundWindow.restype = w.HWND
        u.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
        u.PeekMessageW.argtypes = [ctypes.POINTER(w.MSG), w.HWND, w.UINT, w.UINT, w.UINT]
        u.TranslateMessage.argtypes = [ctypes.POINTER(w.MSG)]
        u.DispatchMessageW.argtypes = [ctypes.POINTER(w.MSG)]
        g.CreateCompatibleDC.restype = w.HDC
        g.CreateCompatibleDC.argtypes = [w.HDC]
        g.CreateDIBSection.restype = w.HBITMAP
        g.CreateDIBSection.argtypes = [w.HDC, ctypes.c_void_p, w.UINT, ctypes.POINTER(ctypes.c_void_p),
                                       w.HANDLE, w.DWORD]
        g.SelectObject.restype = w.HGDIOBJ
        g.SelectObject.argtypes = [w.HDC, w.HGDIOBJ]
        g.DeleteObject.argtypes = [w.HGDIOBJ]
        ex = 0x00080000 | 0x00000008 | 0x00000080 | 0x08000000 | 0x00000020
        # (LAYERED | TOPMOST | TOOLWINDOW | NOACTIVATE | TRANSPARENT: click-through)
        self.hwnd = u.CreateWindowExW(ex, "STATIC", "", 0x80000000, 0, 0, 10, 10,   # WS_POPUP
                                      None, None, None, None)
        if not self.hwnd:
            raise OSError("CreateWindowEx failed")
        self.mem = g.CreateCompatibleDC(None)
        self.bmp = None
        self.bits = ctypes.c_void_p()
        self.size = None
        self.shown = False

    def pump(self):
        msg = self.w.MSG()
        while self.u.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):        # PM_REMOVE
            self.u.TranslateMessage(ctypes.byref(msg))
            self.u.DispatchMessageW(ctypes.byref(msg))

    def _bitmap(self, w, h):
        if self.size == (w, h):
            return
        if self.bmp:
            self.g.DeleteObject(self.bmp)

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32), ("biHeight", ctypes.c_int32),
                        ("biPlanes", ctypes.c_uint16), ("biBitCount", ctypes.c_uint16),
                        ("biCompression", ctypes.c_uint32), ("biSizeImage", ctypes.c_uint32),
                        ("biXPelsPerMeter", ctypes.c_int32), ("biYPelsPerMeter", ctypes.c_int32),
                        ("biClrUsed", ctypes.c_uint32), ("biClrImportant", ctypes.c_uint32)]
        info = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)  # top-down
        self.bmp = self.g.CreateDIBSection(self.mem, ctypes.byref(info), 0, ctypes.byref(self.bits), None, 0)
        if not self.bmp:
            raise OSError("CreateDIBSection failed")
        self.g.SelectObject(self.mem, self.bmp)
        self.size = (w, h)

    @staticmethod
    def line_pixels(w, h, phase):
        """The busy line as BGRA premultiplied bytes: a faint white track and
        an amber piece a quarter of it long, *phase* (0..1) of its way across."""
        import numpy as np
        row = np.empty((w, 4), np.uint8)
        row[:] = TRACK_ALPHA                            # white at that alpha, premultiplied
        seg = max(8, w // 4)
        x0 = int(-seg + (w + seg) * phase)
        a, b = max(0, x0), min(w, x0 + seg)
        if b > a:
            row[a:b] = (AMBER[2], AMBER[1], AMBER[0], 255)
        return np.ascontiguousarray(np.broadcast_to(row, (h, w, 4))).tobytes()

    def show(self, owner, scale, phase):
        """The line along the bottom edge of *owner*'s inside (where the
        status bar's busy line lies)."""
        if not owner:
            return
        w_ = self.w
        rect = w_.RECT()
        self.u.GetClientRect(owner, ctypes.byref(rect))
        corner = w_.POINT(0, rect.bottom)
        self.u.ClientToScreen(owner, ctypes.byref(corner))
        w, h = max(1, rect.right - rect.left), max(1, round(LINE_H * scale))
        x, y = corner.x, corner.y - h
        self._bitmap(w, h)
        data = self.line_pixels(w, h, phase)
        ctypes.memmove(self.bits, data, len(data))

        class BLEND(ctypes.Structure):
            _fields_ = [("op", ctypes.c_ubyte), ("flags", ctypes.c_ubyte), ("alpha", ctypes.c_ubyte),
                        ("format", ctypes.c_ubyte)]
        pt, size, src = w_.POINT(x, y), w_.SIZE(w, h), w_.POINT(0, 0)
        blend = BLEND(0, 0, 255, 1)                     # AC_SRC_OVER, AC_SRC_ALPHA
        screen = self.u.GetDC(None)
        try:
            self.u.UpdateLayeredWindow(self.hwnd, screen, ctypes.byref(pt), ctypes.byref(size), self.mem,
                                       ctypes.byref(src), 0, ctypes.byref(blend), 2)       # ULW_ALPHA
        finally:
            self.u.ReleaseDC(None, screen)
        if not self.shown:
            # HWND_TOPMOST, no activation, shown
            self.u.SetWindowPos(self.hwnd, w_.HWND(-1), 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010 | 0x0040)
            self.shown = True

    def hide(self):
        if self.shown:
            self.u.ShowWindow(self.hwnd, 0)
            self.shown = False
