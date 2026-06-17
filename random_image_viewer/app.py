import sys


def main():
    # ---- Stage 0: Minimal Qt to show splash ASAP ----
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import Qt
    # MUST be set before the QApplication is fully initialised.
    # Without this Qt silently converts all touch input into synthesised mouse
    # events, so widgets never see TouchBegin/TouchUpdate/TouchEnd.
    QApplication.setAttribute(Qt.AA_SynthesizeMouseForUnhandledTouchEvents, False)
    app = QApplication(sys.argv)

    from random_image_viewer.splash import show_splash, close_splash
    splash = show_splash(app)

    def status(msg):
        splash.set_status(msg)

    # ---- Stage 1: Core framework imports ----
    status("Loading Qt framework (PySide6 core modules)\u2026")
    from PySide6.QtCore import QTimer  # noqa: F811

    # ---- Stage 2: Platform utilities ----
    status("Detecting OS theme and configuring platform integration\u2026")
    from random_image_viewer.platform_utils import (
        setup_image_allocation_limit, is_windows_dark_mode, enable_windows_dark_title_bar
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

    # ---- Stage 8: GPU check ----
    status("Probing GPU for OpenCL acceleration (LUT / drawing)\u2026")
    from random_image_viewer.constants import _check_gpu_available
    _check_gpu_available()

    # ---- Build the application ----
    status("Configuring image memory limits and applying stylesheet\u2026")
    setup_image_allocation_limit()
    app.setStyleSheet(get_adaptive_stylesheet())

    status("Building main window: toolbar, sliders, image canvas\u2026")
    viewer = RandomImageViewer()

    if is_windows_dark_mode():
        QTimer.singleShot(0, lambda: enable_windows_dark_title_bar(viewer))

    status("Loading window\u2026")
    viewer.show()

    # Close both splashes
    close_splash(splash)

    sys.exit(app.exec())
