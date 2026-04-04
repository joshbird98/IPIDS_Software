from PyQt5.QtCore import QThread, pyqtSignal
import time


class HistoryLoaderWorker(QThread):
    # Signals to update the Splash Screen
    progress_update = pyqtSignal(str)
    finished = pyqtSignal()

    def __init__(self, history_manager):
        super().__init__()
        self.history = history_manager

    def run(self):
        # We pass a lambda to the loader so it can emit signals back to us
        # This keeps the UI updated during the heavy loop
        def keep_alive():
            pass  # We could emit % here if we calculated file size

        self.progress_update.emit("Scanning large log files...")

        # Call the load function (which we will optimize next)
        success = self.history.load_log_file(keep_alive_func=keep_alive)

        if success:
            self.progress_update.emit("Optimizing storage...")
        else:
            self.progress_update.emit("No recent data found.")

        self.finished.emit()