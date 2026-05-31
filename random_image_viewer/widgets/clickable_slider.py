from PySide6.QtWidgets import QSlider, QStyle, QStyleOptionSlider
from PySide6.QtCore import Qt


class ClickableSlider(QSlider):
    """A slider that allows clicking anywhere on the track to jump to that position"""

    def __init__(self, orientation=Qt.Horizontal, parent=None):
        super().__init__(orientation, parent)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            # Get the slider's style options
            opt = QStyleOptionSlider()
            self.initStyleOption(opt)

            # Get the groove rect (the track area)
            groove_rect = self.style().subControlRect(
                QStyle.CC_Slider, opt, QStyle.SC_SliderGroove, self
            )

            if self.orientation() == Qt.Horizontal:
                # Calculate the position as a percentage of the groove width
                click_pos = event.position().x() - groove_rect.x()
                groove_width = groove_rect.width()
                if groove_width > 0:
                    percentage = max(0.0, min(1.0, click_pos / groove_width))
                    # Calculate the new value based on the slider's range
                    value_range = self.maximum() - self.minimum()
                    new_value = self.minimum() + int(percentage * value_range)
                    self.setValue(new_value)
                    return
            else:  # Vertical orientation
                click_pos = event.position().y() - groove_rect.y()
                groove_height = groove_rect.height()
                if groove_height > 0:
                    # For vertical sliders, top = maximum, bottom = minimum
                    percentage = max(0.0, min(1.0, 1.0 - (click_pos / groove_height)))
                    value_range = self.maximum() - self.minimum()
                    new_value = self.minimum() + int(percentage * value_range)
                    self.setValue(new_value)
                    return

        # Fall back to default behavior for dragging
        super().mousePressEvent(event)
