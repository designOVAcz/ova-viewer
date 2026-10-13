"""Keep emoji / symbol glyphs from stalling startup.

The first time Qt draws a character the widget's font lacks (the emoji and
arrows used as button icons: 📁 ⟲ ↻ …) it builds its font-fallback list by
enumerating every installed font family. With thousands of fonts installed
that blocks the GUI thread for ~1.5 s. Two remedies:

* :class:`SymbolFontPinner` gives every single-glyph button / label a font
  that contains its glyph, so nothing visible at startup needs fallback.
  The families (and their order) are the ones Qt's own fallback picks on
  Windows, so the icons look exactly as before.
* :func:`warm_font_fallbacks` builds the fallback list on a background
  thread once the window is up, so glyphs drawn later (menus, tooltips,
  dialogs) do not hitch either.
"""

import atexit

from PySide6.QtCore import QThread, QMetaObject, QObject, QEvent, Qt
from PySide6.QtGui import QRawFont, QTextDocument, QTextCursor
from PySide6.QtWidgets import QAbstractButton, QLabel

# Qt's Windows fallback order for symbols missing from Segoe UI (families
# that are not installed are skipped; Arial Unicode MS comes with Office).
SYMBOL_FONT_FAMILIES = ("Tahoma", "MS UI Gothic", "Arial Unicode MS",
                        "Segoe UI Emoji", "Segoe UI Symbol")

_raw_fonts = {}


def _raw_font(font):
    key = font.key()
    raw = _raw_fonts.get(key)
    if raw is None:
        raw = _raw_fonts[key] = QRawFont.fromFont(font)
    return raw


def symbol_font_family(font, text):
    """Family that has every glyph of ``text`` that ``font`` lacks.

    Returns None when ``font`` already covers ``text`` or nothing does.
    """
    # Variation selectors (U+FE0F) count too: Qt needs a glyph for them, and
    # requiring one steers emoji-style text to the emoji font, as Qt does.
    missing = {c for c in text if not c.isspace()
               and not _raw_font(font).supportsCharacter(ord(c))}
    if not missing:
        return None
    for family in SYMBOL_FONT_FAMILIES:
        probe = type(font)(font)
        probe.setFamily(family)
        raw = _raw_font(probe)
        if raw.familyName() == family and all(
                raw.supportsCharacter(ord(c)) for c in missing):
            return family
    return None


def pin_symbol_font(widget):
    """Give a symbol-only button / label a font holding its glyph (see
    module docstring). Text that mixes symbols with words is left alone -
    switching its font would restyle the words too."""
    if not isinstance(widget, (QAbstractButton, QLabel)):
        return
    text = widget.text()
    if not text or any(c.isalnum() and ord(c) < 0x2000 for c in text):
        return
    family = symbol_font_family(widget.font(), text)
    if family:
        f = widget.font()
        f.setFamily(family)
        widget.setFont(f)


class SymbolFontPinner(QObject):
    """App-wide event filter running :func:`pin_symbol_font` on each widget
    as it is polished - which Qt does before the first size hint, i.e.
    before any text is shaped. Install it while the main window is built and
    remove it afterwards: a Python filter on every event costs too much to
    keep for the app's lifetime."""

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Polish:
            pin_symbol_font(obj)
        return False


def warm_font_fallbacks(app):
    """Build Qt's font-fallback list on a background thread.

    The work must run as pure C++: any Python on that thread would hold the
    GIL for the whole scan and stall the GUI thread just the same. So the
    text is inserted and undone here (no layout exists yet, so that is
    cheap) and the worker gets a queued call to the C++ slot ``redo()``,
    which re-inserts and lays it out. While it runs it holds Qt's font
    database lock, so call this after the window is shown, not during
    startup.
    """
    thread = QThread()
    thread.setObjectName("font-fallback-warmup")
    thread.start(QThread.LowPriority)
    doc = QTextDocument()
    doc.setDefaultFont(app.font())
    QTextCursor(doc).insertText("⟲▶\U0001F4C2")
    doc.undo()
    doc.documentLayout()  # so redo() lays the text out (shaping -> fallback)
    doc.moveToThread(thread)
    QMetaObject.invokeMethod(doc, "redo", Qt.QueuedConnection)

    def _stop():
        thread.quit()
        thread.wait()
    atexit.register(_stop)
    # Keep the objects alive for the lifetime of the app.
    app._font_warmup = (thread, doc)
