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
        """Prepends historical data, merges overlaps, and ensures monotonic time."""
        if len(ts_array) == 0:
            self.is_fetching = False
            # Even if empty, update the boundary so we don't keep asking for the same empty gap
            self.oldest_loaded_ts = min(self.oldest_loaded_ts, req_start)
            return

        # 1. Resolution Management
        if stride < self.current_stride:
            # SHARPENING: We are zooming in. Discard low-res and take high-res.
            self.x_time = ts_array
            for tag in self.tags:
                try:
                    idx = self.engine.channel_keys.index(tag)
                    self.y_data[tag] = vals_array[:, idx]
                except ValueError:
                    self.y_data[tag] = np.array([])
        else:
            # STITCHING: Combine new history with existing cache
            combined_x = np.concatenate((ts_array, self.x_time))

            # Find the indices that would sort the combined array
            # This is the "Magic Fix" for the back-and-forth curves
            sort_idx = np.argsort(combined_x)

            # Remove duplicates (in case history and live data overlapped)
            # We use return_index to find unique timestamp positions
            unique_x, unique_idx = np.unique(combined_x[sort_idx], return_index=True)
            self.x_time = unique_x

            for tag in self.tags:
                try:
                    idx = self.engine.channel_keys.index(tag)
                    hist_y = vals_array[:, idx]

                    # Merge Y values and apply the same sorting/uniqueness as X
                    current_y = self.y_data.get(tag, np.array([]))
                    combined_y = np.concatenate((hist_y, current_y))

                    # Apply sorting and filter duplicates
                    self.y_data[tag] = combined_y[sort_idx][unique_idx]
                except (ValueError, IndexError):
                    pass

        # 2. Update Boundaries
        self.oldest_loaded_ts = self.x_time[0]
        self.current_stride = stride
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