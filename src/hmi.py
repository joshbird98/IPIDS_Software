import sys
import os

os.environ["QT_API"] = "PyQt6"

from PyQt6.QtWidgets import QApplication
from qfluentwidgets import setTheme, Theme

from src.core.os_helper import harden_windows_process
from src.gui.control.control_main import ControlMainWindow
from src.core.theme import get_global_stylesheet, is_dark_mode

if __name__ == "__main__":
    harden_windows_process()
    app = QApplication(sys.argv)

    # Sync Fluent components to the JSON config
    setTheme(Theme.DARK if is_dark_mode else Theme.LIGHT)

    # Apply the custom stylesheet globally
    app.setStyleSheet(get_global_stylesheet())

    window = ControlMainWindow()
    window.show()
    sys.exit(app.exec())