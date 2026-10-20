import sys
import os

if __package__ in (None, ""):
    # Started as a script (python src/hmi.py): make 'src' importable from any working directory
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

if "--sim" in sys.argv[1:]:
    # Simulation mode (fake devices, data in data_sim/): must be set before src.* is imported
    os.environ["SINDIE_SIMULATE"] = "1"

os.environ["QT_API"] = "PyQt6"

from PyQt6.QtWidgets import QApplication

from src.core.log_setup import setup_logging
from src.core.os_helper import harden_windows_process
from src.gui import theme
from src.gui.control.control_main import ControlMainWindow

if __name__ == "__main__":
    # SystemLogs/hmi.log: rejected/failed commands, widget errors and anything unhandled
    # (with a custom hook installed, PyQt logs slot exceptions instead of aborting the HMI)
    setup_logging("hmi")
    # (No working-set ceiling: the HMI runs at about 200 MB resident - Qt, pyqtgraph, numpy - so the
    # services' 200 MB bound would have Windows trim it, and page it back in, under memory pressure)
    harden_windows_process(limit_working_set=False)
    app = QApplication(sys.argv)

    # The theme saved last (dark unless chosen otherwise: View > Theme), shared with the viewer
    theme.install(app)

    window = ControlMainWindow()
    window.show()
    sys.exit(app.exec())
