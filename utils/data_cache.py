import time
import numpy as np
from PyQt6.QtCore import QObject, QThread, pyqtSignal

from utils.data_engine import TimeSeriesEngine
from utils.zmq_listener import ZMQLiveEngine
from network_config import ZMQ_PORT_PLC_PUB, ZMQ_PORT_VACUUM_PUB


class HistoryFetchWorker(QThread):
    """Background thread to prevent UI freezing during disk reads."""
    data_fetched = pyqtSignal(float, float, object, object)

    def __init__(self, engine: TimeSeriesEngine):
        super().__init__()
        self.engine = engine
        self.req_start = 0.0
        self.req_end = 0.0

    def fetch(self, start_ts: float, end_ts: float):
        self.req_start = start_ts
        self.req_end = end_ts
        self.start()

    def run(self):
        ts, vals = self.engine.query(self.req_start, self.req_end)
        self.data_fetched.emit(self.req_start, self.req_end, ts, vals)


class InfiniteDataCache(QObject):
    """Unified RAM buffer managing both live ZMQ streams and historical disk data."""
    data_updated = pyqtSignal()

    def __init__(self, log_dir: str):
        super().__init__()
        self.engine = TimeSeriesEngine(log_dir)

        # Threads
        self.history_worker = HistoryFetchWorker(self.engine)
        self.history_worker.data_fetched.connect(self._on_history_fetched)

        self.zmq_listener = ZMQLiveEngine(ZMQ_PORT_PLC_PUB, ZMQ_PORT_VACUUM_PUB)
        self.zmq_listener.data_ready.connect(self._on_live_data)

        # Master Data Storage
        self.tags = []
        self.x_time = np.array([], dtype=np.float64)
        self.y_data = {}  # { tag: np.array }

        self.oldest_loaded_ts = time.time()
        self.is_fetching = False

    def set_active_tags(self, tags: list):
        """Initializes tracking arrays when the UI config changes."""
        self.tags = [tag for tag in tags if tag in self.engine.channel_keys]
        for tag in self.tags:
            if tag not in self.y_data:
                self.y_data[tag] = np.array([], dtype=np.float64)

    def start(self):
        self.zmq_listener.start()

    def stop(self):
        self.zmq_listener.stop()
        self.history_worker.wait()

    def request_history(self, start_ts: float, end_ts: float):
        """Triggers an asynchronous disk read if panning into unloaded territory."""
        if self.is_fetching or start_ts >= self.oldest_loaded_ts:
            return

        self.is_fetching = True
        self.history_worker.fetch(start_ts, min(end_ts, self.oldest_loaded_ts))

    def _on_history_fetched(self, req_start, req_end, ts_array, vals_array):
        """Prepends historical data to the master RAM arrays."""
        if len(ts_array) == 0:
            self.oldest_loaded_ts = req_start
            self.is_fetching = False
            return

        # Combine arrays: History + Current Cache
        self.x_time = np.concatenate((ts_array, self.x_time))

        for tag in self.tags:
            try:
                idx = self.engine.channel_keys.index(tag)
                hist_y = vals_array[:, idx]
                if tag in self.y_data:
                    self.y_data[tag] = np.concatenate((hist_y, self.y_data[tag]))
                else:
                    self.y_data[tag] = hist_y
            except ValueError:
                pass

        self.oldest_loaded_ts = ts_array[0]
        self.is_fetching = False
        self.data_updated.emit()

    def _on_live_data(self, data_dict: dict):
        """Appends incoming ZMQ data to the master RAM arrays."""
        if not self.tags:
            return

        current_time = time.time()
        self.x_time = np.append(self.x_time, current_time)

        for tag in self.tags:
            val = data_dict.get(tag)
            if val is None:
                # Zero-order hold
                val = self.y_data[tag][-1] if len(self.y_data[tag]) > 0 else 0.0

            self.y_data[tag] = np.append(self.y_data[tag], val)

        self.data_updated.emit()