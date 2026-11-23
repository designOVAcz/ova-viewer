"""The floating tool panels move by their title row only."""

from PySide6.QtCore import QPoint


def on_title_row(window, title, event):
    """Is the press on *window*'s title row: from its top edge down to just
    under *title*, the whole width - not between the controls below, where a
    slider missed by a hair took the panel along."""
    bottom = title.mapTo(window, QPoint(0, 0)).y() + title.height()
    y = window.mapFromGlobal(event.globalPosition().toPoint()).y()
    return 0 <= y <= bottom + 2
