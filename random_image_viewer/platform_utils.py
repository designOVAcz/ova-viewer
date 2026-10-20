import os

try:
    import winreg  # For Windows registry access (dark mode detection)
    import ctypes
    import ctypes.wintypes  # For Windows API calls (dark mode title bar)
except ImportError:
    winreg = None  # Not on Windows
    ctypes = None

from PySide6.QtGui import QImageReader
from PySide6.QtCore import QTimer


def process_uptime_seconds():
    """Seconds since this process was created, or None if unavailable.

    Measured from the OS process-creation time rather than from a Python
    timestamp, so the figure includes interpreter start-up and — in a
    PyInstaller one-file build — the bootloader's unpacking, which is the part
    that actually dominates a cold launch.
    """
    if ctypes is None or os.name != 'nt':
        return None
    try:
        wintypes = ctypes.wintypes
        kernel32 = ctypes.windll.kernel32
        # Declare the signatures: GetCurrentProcess returns the (HANDLE)-1
        # pseudo-handle, and without an explicit HANDLE restype ctypes passes
        # it on as a 32-bit int, which GetProcessTimes rejects on 64-bit.
        lp_filetime = ctypes.POINTER(wintypes.FILETIME)
        kernel32.GetCurrentProcess.argtypes = []
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE, lp_filetime,
                                             lp_filetime, lp_filetime, lp_filetime]
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        kernel32.GetSystemTimeAsFileTime.argtypes = [lp_filetime]

        created, exited, kernel_t, user_t = (wintypes.FILETIME() for _ in range(4))
        if not kernel32.GetProcessTimes(
                kernel32.GetCurrentProcess(), ctypes.byref(created), ctypes.byref(exited),
                ctypes.byref(kernel_t), ctypes.byref(user_t)):
            return None
        now = wintypes.FILETIME()
        kernel32.GetSystemTimeAsFileTime(ctypes.byref(now))

        def _ticks(ft):
            return (ft.dwHighDateTime << 32) | ft.dwLowDateTime

        elapsed = (_ticks(now) - _ticks(created)) / 1e7  # FILETIME = 100 ns ticks
        return elapsed if 0 <= elapsed < 86400 else None
    except Exception:
        return None


def is_windows_dark_mode():
    """Detect if Windows is using dark mode"""
    if not winreg or os.name != 'nt':
        return True  # Default to dark mode on non-Windows or if winreg unavailable

    try:
        # Check Windows theme setting
        registry_key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                    r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
        value, _ = winreg.QueryValueEx(registry_key, "AppsUseLightTheme")
        winreg.CloseKey(registry_key)
        return value == 0  # 0 = dark mode, 1 = light mode
    except Exception:
        return True  # Default to dark mode if detection fails


def enable_windows_dark_title_bar(window):
    """Enable dark mode title bar on Windows 10/11"""
    if not ctypes or os.name != 'nt':
        return  # Not on Windows or ctypes unavailable

    try:
        # Get the window handle
        hwnd = int(window.winId())
        print(f"DEBUG: Window handle: {hwnd}")

        # Try the Windows 11 method first (DWMWA_USE_IMMERSIVE_DARK_MODE = 20)
        DWMWA_USE_IMMERSIVE_DARK_MODE = 20
        value = ctypes.c_int(1)  # Enable dark mode

        # Try to load dwmapi.dll and call DwmSetWindowAttribute
        dwmapi = ctypes.windll.dwmapi
        result = dwmapi.DwmSetWindowAttribute(
            hwnd,
            DWMWA_USE_IMMERSIVE_DARK_MODE,
            ctypes.byref(value),
            ctypes.sizeof(value)
        )
        print(f"DEBUG: Windows 11 dark mode result: {result}")

        # If that fails, try the Windows 10 method (DWMWA_USE_IMMERSIVE_DARK_MODE_BEFORE_20H1 = 19)
        if result != 0:
            DWMWA_USE_IMMERSIVE_DARK_MODE_BEFORE_20H1 = 19
            result2 = dwmapi.DwmSetWindowAttribute(
                hwnd,
                DWMWA_USE_IMMERSIVE_DARK_MODE_BEFORE_20H1,
                ctypes.byref(value),
                ctypes.sizeof(value)
            )
            print(f"DEBUG: Windows 10 dark mode result: {result2}")

        return result == 0  # Return success status

    except Exception as e:
        print(f"DEBUG: Exception in dark title bar: {e}")
        return False


def set_window_cloaked(window, cloaked):
    """Hide/reveal a window via DWM cloaking (Windows 8+).

    A cloaked window is fully alive - shown, laid out and painted - but the
    compositor does not put it on screen. Showing the main window cloaked and
    revealing it once its first frame is painted avoids the white rectangle
    Windows otherwise displays until Qt's first paint. Unlike window opacity
    it leaves no layered-window overhead behind. Returns True on success.
    """
    if not ctypes or os.name != 'nt':
        return False
    try:
        DWMWA_CLOAK = 13
        value = ctypes.c_int(1 if cloaked else 0)
        return ctypes.windll.dwmapi.DwmSetWindowAttribute(
            int(window.winId()), DWMWA_CLOAK,
            ctypes.byref(value), ctypes.sizeof(value)) == 0
    except Exception:
        return False


def pick_folders(window=None, title="", start_dir=""):
    """Windows' own folder picker, with several folders selectable at once.

    Qt's folder dialog only ever returns one folder, but the native Explorer
    dialog (IFileOpenDialog) can pick many: Ctrl/Shift-click folders, then
    "Select Folder". Driven through its COM vtable with ctypes, like the other
    Windows calls here, so no extra dependency is needed.

    Returns the chosen folder paths, ``[]`` if the user cancelled, or None if
    the native dialog is unavailable (the caller then falls back to Qt's).
    """
    if not ctypes or os.name != 'nt':
        return None
    wintypes = ctypes.wintypes
    ole32, shell32 = ctypes.windll.ole32, ctypes.windll.shell32
    HRESULT, PVOID = ctypes.c_long, ctypes.c_void_p

    class GUID(ctypes.Structure):
        _fields_ = [("d1", wintypes.DWORD), ("d2", wintypes.WORD),
                    ("d3", wintypes.WORD), ("d4", ctypes.c_ubyte * 8)]

    def guid(text):
        g = GUID()
        ole32.CLSIDFromString(ctypes.c_wchar_p(text), ctypes.byref(g))
        return g

    def method(obj, index, *argtypes):
        """Bound call of vtable slot *index* on COM object pointer *obj*."""
        vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(PVOID))).contents
        fn = ctypes.WINFUNCTYPE(HRESULT, PVOID, *argtypes)(vtable[index])
        return lambda *args: fn(obj, *args)

    def release(obj):
        if obj:
            ctypes.WINFUNCTYPE(wintypes.ULONG, PVOID)(
                ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(PVOID))).contents[2])(obj)

    CLSID_FILE_OPEN_DIALOG = "{DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7}"
    IID_IFILE_OPEN_DIALOG = "{D57C7288-D4AD-4768-BE02-9D969532D960}"
    IID_ISHELL_ITEM = "{43826D1E-E718-42EE-BC55-A1E261C37BFE}"
    FOS_PICKFOLDERS, FOS_FORCEFILESYSTEM = 0x20, 0x40
    FOS_ALLOWMULTISELECT, FOS_PATHMUSTEXIST = 0x200, 0x800
    SIGDN_FILESYSPATH = 0x80058000
    ERROR_CANCELLED = 0x800704C7
    # IFileOpenDialog vtable slots (IUnknown 0-2, IModalWindow 3, IFileDialog 4-26).
    SHOW, SET_OPTIONS, GET_OPTIONS, SET_FOLDER, SET_TITLE, GET_RESULTS = 3, 9, 10, 12, 17, 27
    ARRAY_GET_COUNT, ARRAY_GET_ITEM, ITEM_GET_NAME = 7, 8, 5

    dialog = PVOID()
    try:
        if ole32.CoCreateInstance(ctypes.byref(guid(CLSID_FILE_OPEN_DIALOG)), None, 1,
                                  ctypes.byref(guid(IID_IFILE_OPEN_DIALOG)),
                                  ctypes.byref(dialog)) != 0:
            return None
        options = wintypes.DWORD()
        method(dialog, GET_OPTIONS, ctypes.POINTER(wintypes.DWORD))(ctypes.byref(options))
        method(dialog, SET_OPTIONS, wintypes.DWORD)(
            options.value | FOS_PICKFOLDERS | FOS_FORCEFILESYSTEM
            | FOS_ALLOWMULTISELECT | FOS_PATHMUSTEXIST)
        if title:
            method(dialog, SET_TITLE, ctypes.c_wchar_p)(title)
        if start_dir and os.path.isdir(start_dir):
            folder = PVOID()
            if shell32.SHCreateItemFromParsingName(
                    ctypes.c_wchar_p(start_dir), None,
                    ctypes.byref(guid(IID_ISHELL_ITEM)), ctypes.byref(folder)) == 0:
                method(dialog, SET_FOLDER, PVOID)(folder)
                release(folder)
        hwnd = int(window.winId()) if window is not None else 0
        hr = method(dialog, SHOW, wintypes.HWND)(hwnd)
        if hr != 0:
            return [] if (hr & 0xFFFFFFFF) == ERROR_CANCELLED else None
        results = PVOID()
        if method(dialog, GET_RESULTS, ctypes.POINTER(PVOID))(ctypes.byref(results)) != 0:
            return None
        paths = []
        try:
            count = wintypes.DWORD()
            method(results, ARRAY_GET_COUNT, ctypes.POINTER(wintypes.DWORD))(ctypes.byref(count))
            for i in range(count.value):
                item, name = PVOID(), PVOID()
                if method(results, ARRAY_GET_ITEM, wintypes.DWORD,
                          ctypes.POINTER(PVOID))(i, ctypes.byref(item)) != 0:
                    continue
                if method(item, ITEM_GET_NAME, wintypes.DWORD,
                          ctypes.POINTER(PVOID))(SIGDN_FILESYSPATH, ctypes.byref(name)) == 0:
                    paths.append(ctypes.wstring_at(name.value))
                    ole32.CoTaskMemFree(name)
                release(item)
        finally:
            release(results)
        return paths
    except Exception as e:
        print(f"Native folder picker unavailable: {e}")
        return None
    finally:
        release(dialog)


def setup_image_allocation_limit():
    """Increase Qt's image allocation limit to handle large images"""
    # Set allocation limit to 1GB (1024 MB) instead of default 256 MB
    QImageReader.setAllocationLimit(1024)
