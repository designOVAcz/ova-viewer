from PySide6.QtWidgets import QSlider, QStyle, QStyleOptionSlider
from PySide6.QtCore import Qt


class ClickableSlider(QSlider):
    """A slider that jumps to wherever you click, and keeps dragging from there.

    Qt's own behaviour is to page towards the click, which is no use for a
    timeline or a size control. The press also starts a drag, so one gesture
    can land roughly and then adjust: the earlier version swallowed the press
    without starting a drag, which left the handle stuck until the next click.
    """

    def value_at(self, pos):
        """The value the given position over this widget points at."""
        opt = QStyleOptionSlider()
        self.initStyleOption(opt)
        style = self.style()
        groove = style.subControlRect(QStyle.CC_Slider, opt, QStyle.SC_SliderGroove, self)
        handle = style.subControlRect(QStyle.CC_Slider, opt, QStyle.SC_SliderHandle, self)
        # The handle's centre is what lands on the click, so the usable span is
        # the groove minus one handle, measured from half a handle in.
        if self.orientation() == Qt.Horizontal:
            span = groove.width() - handle.width()
            offset = pos.x() - groove.x() - handle.width() / 2.0
            upside_down = self.invertedAppearance()
        else:
            span = groove.height() - handle.height()
            offset = pos.y() - groove.y() - handle.height() / 2.0
            upside_down = not self.invertedAppearance()   # vertical: top is the maximum
        if span <= 0:
            return self.value()
        return QStyle.sliderValueFromPosition(
            self.minimum(), self.maximum(), int(round(offset)), int(span), upside_down)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.setSliderDown(True)       # so a drag can continue from this click
            self.setSliderPosition(self.value_at(event.position()))
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.isSliderDown() and (event.buttons() & Qt.LeftButton):
            self.setSliderPosition(self.value_at(event.position()))
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self.isSliderDown():
            self.setSliderPosition(self.value_at(event.position()))
            self.setSliderDown(False)      # emits sliderReleased
            event.accept()
            return
        super().mouseReleaseEvent(event)
