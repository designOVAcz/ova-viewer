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
