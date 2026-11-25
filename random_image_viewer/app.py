import os
import sys
import time


def _boot_splash():
    """The exe launcher's own splash (see the spec), or None when running
    from source / built without it. Up from the first moment, before Qt."""
    try:
        import pyi_splash
        return pyi_splash
    except Exception:
        return None


def _boot_text(boot, text):
    if boot is not None:
        try:
            boot.update_text(text)
        except Exception:
            pass


def _boot_close(boot):
    try:
        boot.close()
    except Exception:
        pass


def _boot_line_hide():
    """The launcher's line window (Tk, this process), hidden: its handle, or
    None (not Windows, not found)."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        pid, found = os.getpid(), []

        def each(hwnd, _):
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value == pid and user32.IsWindowVisible(hwnd):
                name = ctypes.create_unicode_buffer(64)
                user32.GetClassNameW(hwnd, name, 64)
                if name.value.startswith("Tk"):
                    found.append(hwnd)
            return True

        user32.EnumWindows(ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(each), 0)
        if not found:
            return None
        # (all of them: with its transparent pixels the launcher's window may
        # come as more than one - one left up kept the short start of the
        # line on screen over the splash)
        for hwnd in found:
            user32.ShowWindow(hwnd, 0)                  # SW_HIDE
        return found[0]
    except Exception:
        return None


def main():
    launch_t0 = time.perf_counter()
    boot = _boot_splash()
    _boot_text(boot, "starting … the log")
    # Capture prints, tracebacks and Qt warnings before anything else runs:
    # the exe has no console, so this is the only place they end up.
    from random_image_viewer import log_capture
    log_capture.install()
    # ---- Stage 0: Minimal Qt to show splash ASAP ----
    _boot_text(boot, "starting … loading Qt (slow the first time after an update)")
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import Qt
    # MUST be set before the QApplication is fully initialised.
    # Without this Qt silently converts all touch input into synthesised mouse
    # events, so widgets never see TouchBegin/TouchUpdate/TouchEnd.
    QApplication.setAttribute(Qt.AA_SynthesizeMouseForUnhandledTouchEvents, False)
    # 🎮 The GPU canvas: OpenGL contexts share their textures (a layer
    # painted offscreen is drawn by the canvas window as it is), and frames
    # do not wait for the screen's refresh - waiting held the app ~16 ms a
    # frame, every pen move (Windows' compositor still shows them in step).
    try:
        from PySide6.QtGui import QSurfaceFormat
        QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
    except Exception:
        pass
    from random_image_viewer.splash import show_splash, close_splash
    # The launcher's line through the QApplication: Qt makes the process
    # DPI-aware per monitor, and from then on the line's window (laid out by
    # an unaware Tk, centred) is taken at its numbers in real pixels - it
    # jumps up and left and shrinks to 2/3 at 150 %. So it is hidden for the
    # QApplication (~0.1 s), and the splash takes over with the same line
    # drawn by Qt - same place, same size at any scale - before the launcher's
    # window goes. The globe then fades in around it (the line is its equator).
    line = _boot_line_hide() if boot is not None else None
    if boot is not None and line is None:
        _boot_close(boot)
        boot = None
    app = QApplication(sys.argv)
    splash = show_splash(app)
    if boot is not None:
        _boot_close(boot)
    splash.reveal()

    def status(msg):
        splash.set_status(msg)

    # ---- Stage 1: Core framework imports ----
    status("Loading Qt framework (PySide6 core modules)\u2026")
    from PySide6.QtCore import QTimer  # noqa: F811

    # ---- Stage 2: Platform utilities ----
    status("Detecting OS theme and configuring platform integration\u2026")
    from random_image_viewer.platform_utils import (
        setup_image_allocation_limit, is_windows_dark_mode, enable_windows_dark_title_bar,
        set_window_cloaked
    )

    # ---- Stage 3: Styles ----
    status("Preparing dark/light adaptive stylesheet\u2026")
    from random_image_viewer.styles import get_adaptive_stylesheet

    # ---- Stage 4: Image utilities ----
    status("Loading image decoders (JPEG, PNG, GIF, BMP)\u2026")
    from random_image_viewer import image_utils  # noqa: F401

    # ---- Stage 5: PDF utilities ----
    status("Loading PDF engine (PyMuPDF)\u2026")
    from random_image_viewer import pdf_utils  # noqa: F401
    # ---- Stage 5b: EPUB utilities ----
    status("Loading EPUB reader\u2026")
    from random_image_viewer import epub_utils  # noqa: F401
    # ---- Stage 6: Widgets ----
    status("Loading viewer widgets (image label, sliders, countdown)\u2026")
    from random_image_viewer.widgets import (  # noqa: F401
        image_label, enhancement_widget, circular_countdown, clickable_slider
    )

    # ---- Stage 7: Main window (heaviest single import) ----
    status("Loading main window module (this is the largest import)\u2026")
    from random_image_viewer.viewer.main_window import RandomImageViewer

    # (No GPU probe here: importing pyopencl costs ~0.3 s and the GPU code
    # checks availability lazily the first time an effect needs it.)

    # ---- Build the application ----
    status("Configuring image memory limits and applying stylesheet\u2026")
    setup_image_allocation_limit()
    app.setStyleSheet(get_adaptive_stylesheet())

    status("Building main window: toolbar, sliders, image canvas\u2026")
    # Emoji/symbol buttons would otherwise make Qt scan every installed font
    # (~1.5 s) the first time one is measured; see font_utils.
    from random_image_viewer.font_utils import SymbolFontPinner, warm_font_fallbacks
    pinner = SymbolFontPinner()
    app.installEventFilter(pinner)
    viewer = RandomImageViewer()

    status("Loading window\u2026")
    # No white flash: Windows shows a new window as a blank (white) rectangle
    # until the app paints it. So create the native window first, give it the
    # dark title bar, and show it cloaked; reveal it only once the first frame
    # is painted. The splash stays up until then, so there is never a gap.
    viewer.winId()
    if is_windows_dark_mode():
        enable_windows_dark_title_bar(viewer)
    cloaked = set_window_cloaked(viewer, True)
    viewer.show()
    app.processEvents()  # deliver show/resize/layout events
    viewer.repaint()     # paint the first frame now, while still hidden
    if cloaked:
        set_window_cloaked(viewer, False)
    close_splash(splash)
    # In front and active: it came up behind the window it was started from
    # (Windows gives the foreground to the launcher's splash, then nobody).
    # Again once the splash has faded and closed - it was on top meanwhile.
    from random_image_viewer.platform_utils import bring_to_front
    from random_image_viewer.splash import FADE_OUT_MS
    bring_to_front(viewer)
    QTimer.singleShot(FADE_OUT_MS + 120, lambda: bring_to_front(viewer))

    # Report once the event loop has drained the show/paint work, so the
    # number reflects a genuinely interactive window rather than just the
    # moment show() returned.
    QTimer.singleShot(0, lambda: viewer.report_startup_ready(launch_t0))

    # Files/folders passed on the command line - this is how Explorer's
    # "Open with" and default-app double-click hand over the file.
    # Read sys.argv, not app.arguments(): on Windows Qt round-trips the
    # arguments through the ANSI code page, turning e.g. Cyrillic names into
    # '?' so the path no longer exists. Python's argv is the real Unicode
    # command line.
    launch_args = sys.argv[1:]
    if launch_args:
        print(f"Launch arguments: {launch_args!r}")
    launch_paths = []
    for a in launch_args:
        if os.path.exists(a):
            launch_paths.append(os.path.abspath(a))
        else:
            print(f"Warning: launch argument is not an existing path: {a!r}")
    if launch_paths:
        QTimer.singleShot(0, lambda: viewer.open_paths(launch_paths))

    app.removeEventFilter(pinner)
    # The warm-up holds Qt's font lock while it runs, so start it only after
    # the first frame (and anything opened from the command line) is on
    # screen - starting it earlier stalled the first paint for ~1 s.
    QTimer.singleShot(500, lambda: warm_font_fallbacks(app))

    sys.exit(app.exec())
