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


def setup_image_allocation_limit():
    """Increase Qt's image allocation limit to handle large images"""
    # Set allocation limit to 1GB (1024 MB) instead of default 256 MB
    QImageReader.setAllocationLimit(1024)
