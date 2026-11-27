"""Two-finger pan on a Wacom tablet's touch surface (Intuos Pro and alike).

The tablet's touch is a touchpad, not a touchscreen: Windows hands apps no
raw fingers from it. The Wacom driver turns gestures into mouse and wheel
events itself - pinch into Ctrl+wheel, a two-finger drag into vertical
scrolling only (or nothing, with its Scroll gesture off). So panning sideways
was impossible however the app read its input.

Painting apps read the fingers through Wacom's own multi-touch library
(WacomMT.dll, "Wacom Feel Multi-Touch API", installed with the driver). This
does the same, in observer mode - the driver's own gestures (pinch zoom) keep
working - and reports how far the point between two fingers moved, in
millimetres on the tablet. No library, no touch tablet: it stays quiet.
"""

import ctypes
import math
import sys
import time
from ctypes import POINTER, Structure, c_bool, c_float, c_int, c_size_t, c_ushort, c_void_p

from PySide6.QtCore import QObject, Signal

API_VERSION = 4
OBSERVER = 1                    # WMTProcessingModeObserver: the driver keeps its gestures
FINGER_DOWN, FINGER_HOLD = 1, 2


class _Capability(Structure):
    _fields_ = [("Version", c_int), ("DeviceID", c_int), ("Type", c_int),
                ("LogicalOriginX", c_float), ("LogicalOriginY", c_float),
                ("LogicalWidth", c_float), ("LogicalHeight", c_float),
                ("PhysicalSizeX", c_float), ("PhysicalSizeY", c_float),
                ("ReportedSizeX", c_int), ("ReportedSizeY", c_int),
                ("ScanSizeX", c_int), ("ScanSizeY", c_int),
                ("FingerMax", c_int), ("BlobMax", c_int), ("BlobPointsMax", c_int),
                ("CapabilityFlags", c_int)]


class _Finger(Structure):
    _fields_ = [("FingerID", c_int), ("X", c_float), ("Y", c_float),
                ("Width", c_float), ("Height", c_float), ("Sensitivity", c_ushort),
                ("Orientation", c_float), ("Confidence", c_bool), ("TouchState", c_int)]


class _FingerCollection(Structure):
    _fields_ = [("Version", c_int), ("DeviceID", c_int), ("FrameNumber", c_int),
                ("FingerCount", c_int), ("Fingers", POINTER(_Finger))]


if sys.platform == "win32":
    _CALLBACK = ctypes.WINFUNCTYPE(c_int, POINTER(_FingerCollection), c_void_p)
else:                                   # pragma: no cover - Windows only
    _CALLBACK = None


class WacomTwoFingerPan(QObject):
    """Emits panned(dx_mm, dy_mm) while two fingers move on the tablet."""

    panned = Signal(float, float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.active = False
        self._lib = None
        self._callbacks = []            # keep the ctypes callbacks alive
        self._last = {}                 # device -> (id pair, midpoint mm)
        self._sizes = {}                # device -> (mm per logical x, y, origin x, y)
        self._gesture = {}              # device -> [id pair, start middle, start spread, 'pan' | 'pinch' | None]
        self.pinch_t = 0.0              # time.monotonic() of the last pinch movement

    DECIDE_MM = 3.0                     # fingers moved / spread this far: pan or pinch decided

    def pinching(self, within=0.6):
        """Two fingers pinching on the tablet now (or a moment ago): the
        driver's Ctrl+wheel then is that pinch, not a mouse."""
        if any(g[3] != 'pan' for g in list(self._gesture.values())):
            return True             # (two fingers down, not panning: the pinch before it is decided)
        return time.monotonic() - self.pinch_t < within

    def start(self):
        """Hook up to every touch tablet attached. Returns why not, or ''."""
        if _CALLBACK is None:
            return "not Windows"
        try:
            lib = ctypes.WinDLL("WacomMT.dll")
        except OSError:
            return "no WacomMT.dll (Wacom driver)"
        try:
            lib.WacomMTInitialize.argtypes = [c_int]
            lib.WacomMTInitialize.restype = c_int
            if lib.WacomMTInitialize(API_VERSION) != 0:
                return "WacomMTInitialize failed"
            lib.WacomMTGetAttachedDeviceIDs.argtypes = [POINTER(c_int), c_size_t]
            lib.WacomMTGetAttachedDeviceIDs.restype = c_int
            lib.WacomMTGetDeviceCapabilities.argtypes = [c_int, POINTER(_Capability)]
            lib.WacomMTGetDeviceCapabilities.restype = c_int
            lib.WacomMTRegisterFingerReadCallback.argtypes = [c_int, c_void_p, c_int, _CALLBACK, c_void_p]
            lib.WacomMTRegisterFingerReadCallback.restype = c_int
            ids = (c_int * 16)()
            count = lib.WacomMTGetAttachedDeviceIDs(ids, ctypes.sizeof(ids))
            hooked = 0
            for device in ids[:max(0, count)]:
                cap = _Capability()
                cap.Version = 1
                if lib.WacomMTGetDeviceCapabilities(device, ctypes.byref(cap)) != 0:
                    continue
                if cap.LogicalWidth <= 0 or cap.LogicalHeight <= 0:
                    continue
                self._sizes[device] = (cap.PhysicalSizeX / cap.LogicalWidth,
                                       cap.PhysicalSizeY / cap.LogicalHeight,
                                       cap.LogicalOriginX, cap.LogicalOriginY)
                callback = _CALLBACK(self._on_fingers)
                if lib.WacomMTRegisterFingerReadCallback(device, None, OBSERVER, callback, None) == 0:
                    self._callbacks.append(callback)
                    hooked += 1
            if not hooked:
                return "no Wacom touch device"
            self._lib = lib
            self.active = True
            return ""
        except Exception as e:          # an older / odd driver: no pan, no crash
            return f"WacomMT error: {e}"

    def stop(self):
        if self._lib is not None:
            try:
                self._lib.WacomMTQuit()
            except Exception:
                pass
        self._lib = None
        self.active = False

    def _on_fingers(self, packet, _user):
        """The driver's thread: two fingers down -> how far their middle moved."""
        try:
            data = packet.contents
            device = data.DeviceID
            size = self._sizes.get(device)
            if size is None:
                return 0
            # Confidence: the driver's own palm rejection - a wrist or palm
            # resting while painting is not confidently a finger.
            down = [data.Fingers[i] for i in range(data.FingerCount)
                    if data.Fingers[i].TouchState in (FINGER_DOWN, FINGER_HOLD)
                    and data.Fingers[i].Confidence]
            if len(down) != 2:
                self._last.pop(device, None)
                self._gesture.pop(device, None)
                return 0
            a, b = sorted(down, key=lambda f: f.FingerID)
            mx = ((a.X + b.X) / 2 - size[2]) * size[0]
            my = ((a.Y + b.Y) / 2 - size[3]) * size[1]
            spread = math.hypot((a.X - b.X) * size[0], (a.Y - b.Y) * size[1])
            pair = (a.FingerID, b.FingerID)
            last = self._last.get(device)
            self._last[device] = (pair, (mx, my))
            # 🤏 A pan or a pinch: the first few mm decide, held until the
            # fingers lift. The middle between two pinching fingers wanders
            # too - taken as a pan, the picture slid away while the driver
            # zoomed it, each pinch from somewhere else.
            g = self._gesture.get(device)
            if g is None or g[0] != pair:
                g = self._gesture[device] = [pair, (mx, my), spread, None]
            if g[3] is None:
                moved = math.hypot(mx - g[1][0], my - g[1][1])
                stretched = abs(spread - g[2])
                if max(moved, stretched) < self.DECIDE_MM:
                    return 0
                g[3] = 'pinch' if stretched > moved else 'pan'
                if g[3] == 'pan':
                    # the way already come, not lost to the deciding
                    self.panned.emit(mx - g[1][0], my - g[1][1])
                    return 0
            if g[3] == 'pinch':
                self.pinch_t = time.monotonic()
                return 0
            if last is not None and last[0] == pair:
                dx, dy = mx - last[1][0], my - last[1][1]
                if (dx or dy) and math.hypot(dx, dy) < 40:      # mm: no jumps
                    self.panned.emit(dx, dy)                    # queued to the GUI thread
        except Exception:
            pass
        return 0
