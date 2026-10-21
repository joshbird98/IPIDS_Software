"""SINDIE launcher: start the operator HMI and the data viewer without PyCharm, and keep the
services running in the background, with a tray icon showing their state.

    pythonw main_launcher.py                    the launcher's window
    pythonw main_launcher.py --tray --keep-running   in the tray, starting the services if they
                                                aren't running (what Start with Windows runs)
    pythonw main_launcher.py --sim              the same for the simulator's system

Use the Python of PyCharm's project interpreter (its virtual environment): it has every library
SINDIE needs. The launcher's Windows shortcuts point at it. See docs/getting-started/
starting-the-software.md.
"""
import argparse
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from src.core.env import flag, use_simulator     # (reads nothing at import)
if "--sim" in sys.argv[1:] or flag("SIMULATE"):
    # The simulator's ports and data: before anything else from src is imported (they read them at
    # import). SINDIE_SIMULATE=1 alone would give the simulator's data with the real system's ports.
    use_simulator()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="SINDIE launcher")
    ap.add_argument("--sim", action="store_true", help="the simulator's system (its ports and data_sim/)")
    ap.add_argument("--tray", action="store_true", help="start in the tray, without showing the window")
    ap.add_argument("--keep-running", action="store_true",
                    help="start the service manager if it isn't running, and again if it stops")
    args = ap.parse_args(argv)

    os.environ.setdefault("QT_API", "PyQt6")
    from PyQt6.QtWidgets import QApplication
    from src.core.log_setup import setup_logging
    from src.gui import theme
    from src.launcher.instance import SingleInstance
    from src.launcher.window import LauncherWindow

    setup_logging("launcher")
    app = QApplication(sys.argv[:1])
    app.setApplicationName("SINDIE")
    guard = SingleInstance("launcher")
    if not guard.claim("activate" if not args.tray else "tray"):
        return 0                    # (the running launcher was asked to come forward)
    theme.install(app)
    window = LauncherWindow(keep_running=True if args.keep_running else None)
    guard.activated.connect(lambda message: window.show_window() if message == "activate" else None)
    app.setQuitOnLastWindowClosed(window.tray is None)
    if not (args.tray and window.tray is not None):
        window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
