from random_image_viewer.platform_utils import is_windows_dark_mode

DARK_STYLESHEET = """
QWidget { background-color: #232629; color: #b7bcc1; font-size: 11px; }
QLabel, QCheckBox, QSpinBox, QListWidget, QToolButton { font-size: 11px; color: #b7bcc1; }
QStatusBar { font-size: 10px; color: #888; }
QSplitter::handle { background: #232629; border: none; height: 1px; }
QPushButton, QToolButton {
    background: transparent;
    color: #c7ccd1;
    border: none;
    border-radius: 4px;
    min-width: 24px;
    min-height: 24px;
    font-size: 13px;
    padding: 0 2px;
}
QPushButton:hover, QToolButton:hover { background: #2e3034; }
QPushButton:checked, QToolButton:checked { background: #424242; color: #fff; }
QListWidget::item:selected { background: #354e6e; color: #fff; }
QCheckBox:checked { color: #424242; }
QSlider::groove:horizontal {
    background: #35383b;
    height: 6px;
    border-radius: 3px;
}
QSlider::handle:horizontal {
    background: #b7bcc1;
    width: 14px;
    height: 14px;
    border-radius: 7px;
    margin: -4px 0;
}
QSlider::handle:horizontal:hover {
    background: #424242;
}
QSpinBox {
    background: #35383b;
    border: 1px solid #4a4d50;
    border-radius: 3px;
    padding: 2px;
    selection-background-color: #424242;
}
QToolBar::separator {
    background: #35383b;
    width: 1px;
    margin: 0 4px;
}
QToolBar {
    border: none;
    background: #232629;
}
QMenu {
    background: #232629;
    border: 1px solid #35383b;
    color: #b7bcc1;
}
QMenu::item {
    padding: 4px 20px;
    background: transparent;
}
QMenu::item:selected {
    background: #424242;
    color: #fff;
}
QMenu::separator {
    height: 1px;
    background: #35383b;
    margin: 2px 0;
}
"""

LIGHT_STYLESHEET = """
QWidget { background-color: #ffffff; color: #333333; font-size: 11px; }
QLabel, QCheckBox, QSpinBox, QListWidget, QToolButton { font-size: 11px; color: #333333; }
QStatusBar { font-size: 10px; color: #666; }
QSplitter::handle { background: #e0e0e0; border: none; height: 1px; }
QPushButton, QToolButton {
    background: transparent;
    color: #333333;
    border: none;
    border-radius: 4px;
    min-width: 24px;
    min-height: 24px;
    font-size: 13px;
    padding: 0 2px;
}
QPushButton:hover, QToolButton:hover { background: #e6e6e6; }
QPushButton:checked, QToolButton:checked { background: #0078d4; color: #fff; }
QListWidget::item:selected { background: #0078d4; color: #fff; }
QCheckBox:checked { color: #0078d4; }
QSlider::groove:horizontal {
    background: #e0e0e0;
    height: 6px;
    border-radius: 3px;
}
QSlider::handle:horizontal {
    background: #666666;
    width: 14px;
    height: 14px;
    border-radius: 7px;
    margin: -4px 0;
}
QSlider::handle:horizontal:hover {
    background: #0078d4;
}
QSpinBox {
    background: #ffffff;
    border: 1px solid #cccccc;
    border-radius: 3px;
    padding: 2px;
    selection-background-color: #0078d4;
}
QToolBar::separator {
    background: #cccccc;
    width: 1px;
    margin: 0 4px;
}
QToolBar {
    border: none;
    background: #f5f5f5;
}
QMenu {
    background: #ffffff;
    border: 1px solid #cccccc;
    color: #333333;
}
QMenu::item {
    padding: 4px 20px;
    background: transparent;
}
QMenu::item:selected {
    background: #0078d4;
    color: #fff;
}
QMenu::separator {
    height: 1px;
    background: #cccccc;
    margin: 2px 0;
}
"""


def get_adaptive_stylesheet():
    """Get stylesheet based on OS theme"""
    if is_windows_dark_mode():
        return DARK_STYLESHEET
    else:
        return LIGHT_STYLESHEET
