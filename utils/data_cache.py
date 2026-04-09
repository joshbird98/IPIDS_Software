import time
import numpy as np
from PyQt6.QtCore import QObject, QThread, pyqtSignal

from utils.data_engine import TimeSeriesEngine
from utils.zmq_listener import ZMQLiveEngine
from network_config import ZMQ_PORT_PLC_PUB, ZMQ_PORT_VACUUM_PUB



class HistoryFetchWorker(QThread):
    """
    Background thread to prevent UI freezing during disk reads.
    Signal format: (start_ts, end_ts, ts_array, vals_array, stride)
    """
    data_fetched = pyqtSignal(float, float, object, object, int)

    def __init__(self, engine):
        super().__init__()
        self.engine = engine
        self.req_start = 0.0
        self.req_end = 0.0
        self.stride = 1

    def fetch(self, start_ts: float, end_ts: float, stride: int = 1):
        """Prepares the worker and starts the thread."""
        self.req_start = start_ts
        self.req_end = end_ts
        self.stride = stride
        # Start the QThread's run() method
        self.start()

    def run(self):
        """Execution logic in the background thread."""
        try:
            ts, vals = self.engine.query(self.req_start, self.req_end, stride=self.stride)
            self.data_fetched.emit(self.req_start, self.req_end, ts, vals, self.stride)
        except Exception as e:
            print(f"[HistoryWorker] Error fetching data: {e}")
            # Emit empty arrays on error to unblock the 'is_fetching' flag in cache
            self.data_fetched.emit(self.req_start, self.req_end, np.array([]), np.array([]), self.stride)


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

        self.current_stride = 1
        self.loaded_ranges = [] # List of (start, end, stride) to prevent redundant fetches

    def register_tags(self, tags: list):
        """Adds new tags to the master tracking list without overwriting existing ones."""
        valid_tags = [tag for tag in tags if tag in self.engine.channel_keys]

        for tag in valid_tags:
            if tag not in self.tags:
                self.tags.append(tag)
            if tag not in self.y_data:
                # Pre-pad with NaNs to match the global timeline length
                current_x_len = len(self.x_time)
                self.y_data[tag] = np.full(current_x_len, np.nan, dtype=np.float64)

    def start(self):
        self.zmq_listener.start()

    def stop(self):
        self.zmq_listener.stop()
        self.history_worker.wait()

    def request_history(self, start_ts: float, end_ts: float, stride=1):
        """Triggers an asynchronous disk read if panning into unloaded territory."""
        if self.is_fetching:
            return

        # Simple resolution logic:
        # If the window is huge, we don't want 10Hz data.
        self.is_fetching = True
        self.history_worker.fetch(start_ts, end_ts, stride)

    def _on_history_fetched(self, req_start, req_end, ts_array, vals_array, stride):
        """Lightning-fast merge: Trims overlap and prepends without sorting."""
        if len(ts_array) == 0:
            self.is_fetching = False
            self.oldest_loaded_ts = min(self.oldest_loaded_ts, req_start)
            return

        # 1. FAST TRIM: Find exactly where history overlaps with our live RAM
        if len(self.x_time) > 0:
            # np.searchsorted is O(log N) - almost instantly finds the cutoff index
            cutoff_idx = np.searchsorted(ts_array, self.x_time[0], side='left')
            new_x = ts_array[:cutoff_idx]
        else:
            cutoff_idx = len(ts_array)
            new_x = ts_array

        # If the requested history is entirely engulfed by our RAM already, abort
        if len(new_x) == 0:
            self.is_fetching = False
            return

        # 2. PREPEND X-AXIS
        self.x_time = np.concatenate((new_x, self.x_time))

        # 3. PREPEND Y-AXIS
        for tag in self.tags:
            current_y = self.y_data.get(tag, np.array([]))

            try:
                idx = self.engine.channel_keys.index(tag)
                # Slice the Y-data exactly where we sliced the X-data
                hist_y = vals_array[:cutoff_idx, idx]
            except ValueError:
                hist_y = np.full(len(new_x), np.nan)

            self.y_data[tag] = np.concatenate((hist_y, current_y))

        # 4. Update Boundaries
        self.oldest_loaded_ts = min(self.oldest_loaded_ts, req_start)
        self.current_stride = min(self.current_stride, stride)
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