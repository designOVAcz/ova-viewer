"""Frames-per-second box whose wheel and arrow keys step through film rates."""

from PySide6.QtWidgets import QSpinBox

FPS_PRESETS = (1, 2, 3, 4, 5, 6, 8, 10, 12, 15, 20, 24, 25, 30, 48, 50, 60)


class FpsSpinBox(QSpinBox):
    """Scrolling jumps 12 → 15 → 24 → 25 → 30 …; any value can still be typed."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setRange(FPS_PRESETS[0], FPS_PRESETS[-1])
        self.setSuffix(" fps")

    def stepBy(self, steps):
        value = self.value()
        for _ in range(abs(steps)):
            if steps > 0:
                value = next((p for p in FPS_PRESETS if p > value), value)
            else:
                value = next((p for p in reversed(FPS_PRESETS) if p < value), value)
        self.setValue(value)
