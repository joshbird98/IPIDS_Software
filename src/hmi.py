import sys
import os

if __package__ in (None, ""):
    # Started as a script (python src/hmi.py): make 'src' importable from any working directory
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

if "--sim" in sys.argv[1:]:
    # Simulation mode (fake devices, data in data_sim/): must be set before src.* is imported
    os.environ["IPIDS_SIMULATE"] = "1"

os.environ["QT_API"] = "PyQt6"

from PyQt6.QtWidgets import QApplication
from qfluentwidgets import setTheme, Theme

from src.core.log_setup import setup_logging
from src.core.os_helper import harden_windows_process
from src.gui.control.control_main import ControlMainWindow
from src.core.theme import get_global_stylesheet, is_dark_mode

if __name__ == "__main__":
    # SystemLogs/hmi.log: rejected/failed commands, widget errors and anything unhandled
    # (with a custom hook installed, PyQt logs slot exceptions instead of aborting the HMI)
    setup_logging("hmi")
    harden_windows_process()
    app = QApplication(sys.argv)

    # Sync Fluent components to the JSON config
    setTheme(Theme.DARK if is_dark_mode else Theme.LIGHT)

    # Apply the custom stylesheet globally
    app.setStyleSheet(get_global_stylesheet())

    window = ControlMainWindow()
    window.show()
    sys.exit(app.exec())
