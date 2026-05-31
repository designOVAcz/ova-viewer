from PySide6.QtWidgets import QWidget, QLabel, QToolButton, QHBoxLayout, QGridLayout
from PySide6.QtCore import Qt, QSize

from random_image_viewer.widgets.clickable_slider import ClickableSlider


class ResponsiveEnhancementWidget(QWidget):
    """A responsive widget that adapts layout based on available width"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.parent_viewer = None
        self.min_width_threshold = 280  # Reduced threshold for better detection
        self.current_layout_mode = "horizontal"  # Track current layout

        # Set minimum size to ensure visibility
        self.setMinimumSize(270, 24)
        self.setMaximumHeight(48)  # Allow for vertical layout

        # Create all controls
        self.create_controls()
        self.setup_horizontal_layout()  # Start with horizontal layout

    def create_controls(self):
        """Create all the enhancement controls"""
        # Grayscale controls
        self.gray_label = QLabel("Gray:")
        self.gray_label.setFixedWidth(30)
        self.gray_label.setStyleSheet("font-size: 9px;")

        self.grayscale_slider = ClickableSlider(Qt.Horizontal)
        self.grayscale_slider.setRange(0, 100)
        self.grayscale_slider.setValue(0)
        self.grayscale_slider.setFixedWidth(60)
        self.grayscale_slider.setFixedHeight(20)
        self.grayscale_slider.setToolTip("Grayscale: 0=Color, 100=B&W")

        # Contrast controls
        self.contrast_label = QLabel("Con:")
        self.contrast_label.setFixedWidth(25)
        self.contrast_label.setStyleSheet("font-size: 9px;")

        self.contrast_slider = ClickableSlider(Qt.Horizontal)
        self.contrast_slider.setRange(0, 200)
        self.contrast_slider.setValue(50)
        self.contrast_slider.setFixedWidth(60)
        self.contrast_slider.setFixedHeight(20)
        self.contrast_slider.setToolTip("Contrast: 50=Normal, 0=Flat, 200=Extreme")

        # Gamma controls
        self.gamma_label = QLabel("Gam:")
        self.gamma_label.setFixedWidth(25)
        self.gamma_label.setStyleSheet("font-size: 9px;")

        self.gamma_slider = ClickableSlider(Qt.Horizontal)
        self.gamma_slider.setRange(-200, 500)  # New range: -200=very dark, 0=normal, 500=very bright
        self.gamma_slider.setValue(0)
        self.gamma_slider.setFixedWidth(60)
        self.gamma_slider.setFixedHeight(20)
        self.gamma_slider.setToolTip("Gamma: -200=Very Dark, 0=Normal, 500=Very Bright")

        # Reset button
        self.reset_btn = QToolButton()
        self.reset_btn.setText("↺")
        self.reset_btn.setToolTip("Reset Enhancements")
        self.reset_btn.setFixedSize(16, 20)

    def setup_horizontal_layout(self):
        """Setup horizontal layout for wide windows"""
        self.clear_layout()

        layout = QHBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)  # Small margins
        layout.setSpacing(2)

        layout.addWidget(self.gray_label)
        layout.addWidget(self.grayscale_slider)
        layout.addWidget(self.contrast_label)
        layout.addWidget(self.contrast_slider)
        layout.addWidget(self.gamma_label)
        layout.addWidget(self.gamma_slider)
        layout.addWidget(self.reset_btn)

        self.current_layout_mode = "horizontal"
        self.setMaximumHeight(24)  # Single row height

    def setup_vertical_layout(self):
        """Setup vertical/grid layout for narrow windows"""
        self.clear_layout()

        layout = QGridLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)  # Small margins
        layout.setSpacing(1)

        # Row 1: Gray and Contrast
        layout.addWidget(self.gray_label, 0, 0)
        layout.addWidget(self.grayscale_slider, 0, 1)
        layout.addWidget(self.contrast_label, 0, 2)
        layout.addWidget(self.contrast_slider, 0, 3)

        # Row 2: Gamma and Reset
        layout.addWidget(self.gamma_label, 1, 0)
        layout.addWidget(self.gamma_slider, 1, 1)
        layout.addWidget(self.reset_btn, 1, 2, 1, 2)  # Span 2 columns

        self.current_layout_mode = "vertical"
        self.setMaximumHeight(48)  # Two row height

    def clear_layout(self):
        """Remove all widgets from current layout"""
        if self.layout():
            while self.layout().count():
                child = self.layout().takeAt(0)
                if child.widget():
                    child.widget().setParent(None)
            # Schedule the layout for deletion
            self.layout().deleteLater()

    def sizeHint(self):
        """Provide a size hint for the layout system"""
        if self.current_layout_mode == "horizontal":
            return QSize(270, 24)
        else:
            return QSize(200, 48)

    def set_parent_viewer(self, viewer):
        """Connect to parent viewer and setup signal connections"""
        self.parent_viewer = viewer
        self.grayscale_slider.valueChanged.connect(viewer.update_grayscale)
        self.contrast_slider.valueChanged.connect(viewer.update_contrast)
        self.gamma_slider.valueChanged.connect(viewer.update_gamma)
        self.reset_btn.clicked.connect(viewer.reset_enhancements)
