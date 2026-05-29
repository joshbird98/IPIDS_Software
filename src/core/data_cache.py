import os
import time
import numpy as np
import psutil
from PyQt6.QtCore import QObject, QThread, pyqtSignal, QTimer

from src.core.data_engine import TimeSeriesEngine
from src.core.zmq_listener import ZMQLiveEngine
from src.core.network_config import (
    ZMQ_PORT_PLC_PUB, ZMQ_PORT_VACUUM_PUB, ZMQ_PORT_SRC_TURBO_PUB,
    ZMQ_PORT_SPELLMAN_PUB, ZMQ_PORT_MAGNET_PUB
)

from src.core.perf_utils import PerfTracker


class StatelessQueryWorker(QThread):
    data_fetched = pyqtSignal(float, float, object, object, int)

    def __init__(self, engine):
        super().__init__()
        self.engine = engine
        self.req_start = 0.0
        self.req_end = 0.0
        self.req_id = 0
        self.selected_tags = []

    def fetch(self, start_ts: float, end_ts: float, request_id: int, selected_tags: list):
        self.req_start = start_ts
        self.req_end = end_ts
        self.req_id = request_id
        self.selected_tags = selected_tags  # Save the tags to the instance
        self.start()  # This triggers the run() method below

    def apply_safety_downsample(self, ts, vals_dict, target=4000):
        """Processes the dict through downsample_minmax in the thread."""
        if len(ts) <= target:
            return ts, vals_dict

        new_vals = {}
        # Decimate the time array using the first key (all channels share the time axis)
        first_key = list(vals_dict.keys())[0]
        final_times, _ = self.engine.downsample_minmax(ts, vals_dict[first_key], target_points=target)

        # Apply to all
        for k, v in vals_dict.items():
            _, final_vals = self.engine.downsample_minmax(ts, v, target_points=target)
            new_vals[k] = final_vals

        return final_times, new_vals

    def run(self):
        try:
            # Pass the tags through to the engine here
            ts, vals_dict = self.engine.query_stateless(
                self.req_start,
                self.req_end,
                self.selected_tags
            )
            # 2. Safety Valve Downsampling
            decimated_ts, decimated_vals = self.apply_safety_downsample(ts, vals_dict)

            # 3. Emit the decimated data
            self.data_fetched.emit(self.req_start, self.req_end, decimated_ts, decimated_vals, self.req_id)
        except Exception as e:
            print(f"[Worker] Error during fetch: {e}")
            self.data_fetched.emit(self.req_start, self.req_end, np.array([]), {}, self.req_id)


class DualPipelineCache(QObject):
    live_updated = pyqtSignal()
    historical_updated = pyqtSignal(object, object, int)
    markers_changed = pyqtSignal()

    def __init__(self, log_dir: str):
        super().__init__()
        self.engine = TimeSeriesEngine(log_dir)
        self.query_worker = StatelessQueryWorker(self.engine)

        # Connect BOTH the data signal and the thread lifecycle signal
        self.query_worker.data_fetched.connect(self._on_historical_fetched)
        self.query_worker.finished.connect(self._on_worker_finished)

        self.zmq_listener = ZMQLiveEngine(
            ZMQ_PORT_PLC_PUB, ZMQ_PORT_VACUUM_PUB, ZMQ_PORT_SRC_TURBO_PUB,
            ZMQ_PORT_SPELLMAN_PUB, ZMQ_PORT_MAGNET_PUB
        )
        self.zmq_listener.data_ready.connect(self._on_live_data)

        # LIVE DOMAIN: Fixed 1-Hour Rolling Buffer (10Hz = 36,000 points)
        self.live_capacity = 5000000 # was 432000
        self._x_live = np.zeros(self.live_capacity, dtype=np.float64)
        self._y_live = {key: np.zeros(self.live_capacity, dtype=np.float64) for key in self.engine.channel_keys}
        self.live_ptr = 0
        self.key_last_seen = {key: time.time() for key in self.engine.channel_keys}

        # VIEWER WINDOWS
        self.client_tags = {}
        self.tags = set()
        self.x_live_view = self._x_live[:0]
        self.y_live_view = {key: self._y_live[key][:0] for key in self.engine.channel_keys}

        self.current_request_id = 0
        self.is_fetching = False

        self.process = psutil.Process(os.getpid())
        self.health_timer = QTimer()
        self.health_timer.timeout.connect(self._print_health_stats)
        self.health_timer.start(60000)

    def _print_health_stats(self):
        mem_mb = self.process.memory_info().rss / (1024 * 1024)
        perf = PerfTracker.get_stats()
        print(f"[UI] Time {time.time()} | CPU: {self.process.cpu_percent():>4.1f}% | RAM: {mem_mb:>6.1f} MB | {perf} | Buffer: {self.live_ptr}")

    def register_client_tags(self, client_id: int, tags: list):
        ## self.tags = [tag for tag in tags if tag in self.engine.channel_keys] ## OLD LINE
        """Allows multiple windows to request different live streams safely."""
        self.client_tags[client_id] = set(tags)
        # The cache will subscribe to the UNION of all requested tags
        self.tags = set().union(*self.client_tags.values())

    def start(self):
        self.zmq_listener.start()

    def stop(self):
        self.zmq_listener.stop(); self.query_worker.wait()

    def request_historical_window(self, start_ts: float, end_ts: float, selected_tags: list):
        """Queues the newest time window requested by the UI."""
        self.current_request_id += 1
        # Store the selected_tags in the request tuple so _dispatch_pending can access it
        self.pending_request = (start_ts, end_ts, self.current_request_id, selected_tags)

        # If the worker is free, dispatch it immediately
        if not self.is_fetching:
            self._dispatch_pending()

        return self.current_request_id

    def _dispatch_pending(self):
        if not hasattr(self, 'pending_request') or self.pending_request is None:
            return

        start_ts, end_ts, req_id, selected_tags = self.pending_request
        self.pending_request = None  # Clear queue

        self.is_fetching = True
        self.active_request_id = req_id
        #print(f"[Cache] Dispatching Stateless Request ID: {req_id}...")
        self.query_worker.fetch(start_ts, end_ts, req_id, selected_tags)

    def _on_historical_fetched(self, req_start, req_end, ts_array, vals_dict, req_id):
        # This slot ONLY handles data delivery
        if req_id == getattr(self, 'active_request_id', -1):
            self.historical_updated.emit(ts_array, vals_dict, req_id)

    def _on_worker_finished(self):
        # This slot guarantees the thread is fully dead and safe to restart
        self.is_fetching = False

        # If the user scrolled while we were busy, immediately fetch the new bounds
        if getattr(self, 'pending_request', None) is not None:
            self._dispatch_pending()

    def _on_live_data(self, flat_data: dict):
        t0 = time.perf_counter()
        if not self.engine.channel_keys: return

        # 1. Roll the buffer if full (Ring Buffer style)
        if self.live_ptr >= self.live_capacity:
            shift = int(self.live_capacity * 0.1)  # Shift left by 10%
            self._x_live = np.roll(self._x_live, -shift)
            for tag in self.engine.channel_keys:
                self._y_live[tag] = np.roll(self._y_live[tag], -shift)
            self.live_ptr -= shift

        # 2. Insert Live Data
        current_time = time.time()
        self._x_live[self.live_ptr] = current_time

        for tag in self.engine.channel_keys:
            if tag in flat_data:
                val = flat_data[tag]
                self.key_last_seen[tag] = current_time
            else:
                val = self._y_live[tag][self.live_ptr - 1] if self.live_ptr > 0 else np.nan
                if current_time - self.key_last_seen[tag] > 1.0: val = np.nan
            self._y_live[tag][self.live_ptr] = val

        self.live_ptr += 1

        # 3. Update Views
        self.x_live_view = self._x_live[:self.live_ptr]
        for tag in self.engine.channel_keys:
            self.y_live_view[tag] = self._y_live[tag][:self.live_ptr]

        if not self.is_fetching:
            self.live_updated.emit()

        PerfTracker.log("ingest", t0)