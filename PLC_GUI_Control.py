from PyQt5.QtGui import QImage, QPainter
from PyQt5 import QtWidgets, QtCore, QtGui

# Import configuration and settings

# Import the backend
from utils.plc_control import *
import requests
import psutil
import os

import ctypes
from PyQt5.QtGui import QIcon
from PyQt5.QtWidgets import QApplication, QSplashScreen
from PyQt5.QtGui import QPixmap, QColor
from PyQt5.QtCore import Qt

from PyQt5.QtGui import QPixmap, QIcon, QPainter, QColor, QBrush

import sys

# Import the controllers
from controllers.plot_controller import PlotController
from controllers.recipe_controller import RecipeController
from controllers.general_controller import GeneralController
from controllers.ion_source_controller import IonSourceController
from controllers.beamline_controller import BeamlineController
from controllers.mass_scan_controller import MassScanController

class MainGUI(QMainWindow):
    def __init__(self, splash=None):
        super().__init__()
        self.setWindowTitle("IPIDS")
        #self.showMaximized()
        uic.loadUi(UI_LAYOUT_FILE, self)  # Load your existing .ui file

        # --- SETUP THE BACKEND (PLC interface and threads) ---
        self.plc = PLC_Interface(splash=splash)
        self.thread = QThread()
        self.worker = PLCWorker(self.plc)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.data_updated.connect(self.update_gui_loop)
        #self.thread.start()

        # --- SETUP THE CONTROLLERS ---
        self.ctrl_plot = PlotController(self, self.plc, self.frame_plot_container)
        self.ctrl_general = GeneralController(self, self.plc)
        self.ctrl_ion_source = IonSourceController(self, self.plc)
        self.ctrl_beamline = BeamlineController(self, self.plc)
        self.ctrl_mass_scan = MassScanController(self.plc)

        # --- SETUP THE PLOTTER ---
        # Create a dedicated timer for PLOTTING (Drawing)
        self.plot_render_timer = QTimer()
        self.plot_render_timer.timeout.connect(self.ctrl_plot.refresh_display)
        # Set to 100ms (10 FPS) -> This makes the GUI feel smooth
        self.plot_render_timer.start(100)

        # --- SETUP THE GUI ---
        self.all_binders = []
        self.all_binders.extend(self.ctrl_general.get_binders())
        self.all_binders.extend(self.ctrl_ion_source.get_binders())
        self.all_binders.extend(self.ctrl_beamline.get_binders())

        # Init Recipe Controller with this list
        self.recipe_controller = RecipeController(self, self.plc, self.all_binders)

        self.process = psutil.Process(os.getpid())

    def update_gui_loop(self):
        self.ctrl_general.update_virtual_tags()
        self.ctrl_plot.refresh_display()
        self.ctrl_mass_scan.process_logic()

        for binder in self.all_binders:
            binder.update_from_plc()

    # Stop the worker thread, tell the PLC to clean up (loggers, dumps), and close child windows
    def closeEvent(self, event):
        self.hide()
        QApplication.processEvents()
        if hasattr(self, 'worker'):
            self.worker.running = False
        if hasattr(self, 'plc'):
            self.plc.shutdown()
        if hasattr(self, 'ctrl_mass_scan'):
             self.ctrl_mass_scan.window.close()

        event.accept()


# ========== Run the App ==========
if __name__ == "__main__":
    myappid = 'ipids.hmi.v1'
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)
    except AttributeError:
        pass

    app = QApplication(sys.argv)
    app.setApplicationName("IPIDS")

    base_dir = os.path.dirname(os.path.abspath(__file__))
    image_folder = os.path.join(base_dir, 'images')

    logo_path = os.path.join(image_folder, 'IPIDS_logo.png')
    icon_path = os.path.join(image_folder, 'IPIDS_logo_tight.png')

    icon = QIcon(icon_path)
    app.setWindowIcon(icon)

    # 1. PREPARE SPLASH IMAGE
    raw_pixmap = QPixmap(logo_path)
    if raw_pixmap.isNull():
        # Fallback if image missing
        pixmap = QPixmap(600, 400)
        pixmap.fill(Qt.black)
    else:
        pixmap = raw_pixmap.scaled(600, 400, 1, 1)

    # 2. CREATE SPLASH
    splash = QSplashScreen(pixmap, Qt.WindowStaysOnTopHint)
    splash.setAttribute(Qt.WA_TranslucentBackground)
    splash.setMask(pixmap.mask())
    splash.show()

    splash.showMessage("Initializing Core Systems...", Qt.AlignBottom | Qt.AlignCenter, Qt.white)
    app.processEvents()

    # 3. START MAIN APP
    # (This triggers PLC_Interface init -> which loads History automatically!)
    # The splash will update text because PLC_Interface uses the passed 'splash' object.
    win = MainGUI(splash=splash)

    win.setWindowTitle("IPIDS")
    if os.path.exists(icon_path):
        win.setWindowIcon(QIcon(icon_path))

    # 3. SETUP LOADING THREAD
    from utils.history_worker import HistoryLoaderWorker

    # Access the history manager instance directly
    hist_manager = win.plc.history

    loader_thread = HistoryLoaderWorker(hist_manager)


    def update_splash(msg):
        splash.showMessage(msg, Qt.AlignBottom | Qt.AlignCenter, Qt.white)


    def on_load_finished():
        # 4. PRE-RENDER (Now that data exists)
        splash.showMessage("Rendering Visualization...", Qt.AlignBottom | Qt.AlignCenter, Qt.white)
        if hasattr(win, 'ctrl_plot'):
            win.ctrl_plot.refresh_display()

        # 5. LAUNCH
        win.thread.start()
        win.showMaximized()
        splash.finish(win)


    loader_thread.progress_update.connect(update_splash)
    loader_thread.finished.connect(on_load_finished)

    # START LOADING
    loader_thread.start()

    sys.exit(app.exec_())