import os
import json
import time
import numpy as np
import psutil
from PyQt6.QtCore import QObject, QThread, pyqtSignal, QTimer

from utils.data_engine import TimeSeriesEngine
from utils.zmq_listener import ZMQLiveEngine
from network_config import (
    ZMQ_PORT_PLC_PUB, ZMQ_PORT_VACUUM_PUB, ZMQ_PORT_SRC_TURBO_PUB,
    TOPIC_PLC_DATA, TOPIC_VACUUM_DATA, TOPIC_SRC_TURBO_DATA
)
from utils.payload_mapper import DynamicPayloadMapper


class HistoryFetchWorker(QThread):
    """Background thread to prevent UI freezing during disk reads."""
    data_fetched = pyqtSignal(float, float, object, object, int)

    def __init__(self, engine):
        super().__init__()
        self.engine = engine
        self.req_start = 0.0
        self.req_end = 0.0
        self.stride = 1

    def fetch(self, start_ts: float, end_ts: float, stride: int = 1):
        self.req_start = start_ts
        self.req_end = end_ts
        self.stride = stride
        self.start()

    def run(self):
        try:
            ts, vals = self.engine.query(self.req_start, self.req_end, stride=self.stride)
            self.data_fetched.emit(self.req_start, self.req_end, ts, vals, self.stride)
        except Exception as e:
            print(f"[HistoryWorker] Error fetching data: {e}")
            self.data_fetched.emit(self.req_start, self.req_end, np.array([]), np.array([]), self.stride)


class InfiniteDataCache(QObject):
    """Unified RAM buffer managing both live ZMQ streams and historical disk data."""
    data_updated = pyqtSignal()
    markers_changed = pyqtSignal()

    def __init__(self, log_dir: str):
        super().__init__()
        self.engine = TimeSeriesEngine(log_dir)

        self.history_worker = HistoryFetchWorker(self.engine)
        self.history_worker.data_fetched.connect(self._on_history_fetched)

        self.zmq_listener = ZMQLiveEngine(ZMQ_PORT_PLC_PUB, ZMQ_PORT_VACUUM_PUB, ZMQ_PORT_SRC_TURBO_PUB)
        self.zmq_listener.data_ready.connect(self._on_live_data)

        # --- MEMORY MANAGEMENT PARAMETERS ---
        self.max_capacity = 200000  # Max points kept in RAM (~5.5 hours at 10Hz)
        self.chunk_size = 5000  # Block allocation size (~8.3 minutes of space)

        # 1. The Background Buffers (The true memory)
        self._x_buffer = np.zeros(self.chunk_size, dtype=np.float64)
        self._y_buffers = {key: np.zeros(self.chunk_size, dtype=np.float64) for key in self.engine.channel_keys}

        self.write_ptr = 0

        # Initialize the shared mapper
        self.payload_mapper = DynamicPayloadMapper()
        self.keys = sorted(list(self.payload_mapper.valid_keys))
        self.num_keys = len(self.keys)

        # 2. The UI Views (These act as windows so the UI never sees the blank trailing zeros)
        self.tags = []
        self.x_time = self._x_buffer[:0]
        self.y_data = {key: self._y_buffers[key][:0] for key in self.engine.channel_keys}

        self.oldest_loaded_ts = time.time()
        self.is_fetching = False
        self.current_stride = 1

        # --- HEALTH MONITORING ---
        self.process = psutil.Process(os.getpid())

        # Initialize CPU percent baseline (first call always returns 0.0)
        self.process.cpu_percent()

        self.health_timer = QTimer()
        self.health_timer.timeout.connect(self._print_health_stats)
        self.health_timer.start(60000)  # Print every 60,000 ms (1 minute)

        # --- THROTTLED RENDER LOOP ---
        self.render_timer = QTimer()
        self.render_timer.timeout.connect(self._emit_render_signal)
        self.render_timer.start(250)  # Emits update at 4Hz (every 250ms)

    def notify_markers_changed(self):
        """Tells all connected windows to reload markers from disk."""
        self.markers_changed.emit()

    def _emit_render_signal(self):
        """Safely triggers a UI redraw at a controlled frame rate."""
        if self.write_ptr > 0 and not self.is_fetching:
            self.data_updated.emit()

    def _print_health_stats(self):
        """Polls the OS for process-specific memory and CPU utilization."""
        # RSS (Resident Set Size) is the non-swapped physical memory the process has used
        mem_mb = self.process.memory_info().rss / (1024 * 1024)
        cpu_pct = self.process.cpu_percent()

        print(f"[Health] CPU: {cpu_pct:>4.1f}% | RAM: {mem_mb:>6.1f} MB | Buffer: {self.write_ptr}/{self.max_capacity}")

    def register_tags(self, tags: list):
        self.tags = [tag for tag in tags if tag in self.engine.channel_keys]

    def start(self):
        self.zmq_listener.start()

    def stop(self):
        self.zmq_listener.stop()
        self.history_worker.wait()

    def request_history(self, start_ts: float, end_ts: float, stride=1):
        """Triggers a disk read ONLY if requesting data we don't already have."""
        """Deduplicates requests from multiple windows to prevent disk spam."""
        if self.is_fetching:
            return

            # 1. If the requested data is ALREADY in our Omniscient RAM, abort.
            # We add a 1-second buffer to handle floating point jitter.
        if start_ts >= (self.oldest_loaded_ts - 1.0):
            return

            # 2. Only fetch the "gap" between what we have and what is requested.
            # This prevents loading the same data twice.
        fetch_end = min(end_ts, self.oldest_loaded_ts)

        # If the gap is too small (less than a few seconds), ignore it.
        if fetch_end - start_ts < 2.0:
            return

        self.is_fetching = True
        print(f"[Cache] Multi-window request gap: {fetch_end - start_ts:.1f}s. Fetching...")
        self.history_worker.fetch(start_ts, fetch_end, stride)

    def _on_history_fetched(self, req_start, req_end, ts_array, vals_array, stride):
        if len(ts_array) == 0:
            self.is_fetching = False
            self.oldest_loaded_ts = min(self.oldest_loaded_ts, req_start)
            return

        # Use the valid view (self.x_time), not the raw background buffer
        if len(self.x_time) > 0:
            cutoff_idx = np.searchsorted(ts_array, self.x_time[0], side='left')
            new_x = ts_array[:cutoff_idx]
        else:
            cutoff_idx = len(ts_array)
            new_x = ts_array

        if len(new_x) == 0:
            self.is_fetching = False
            return

        # Prepend to the raw background buffers
        self._x_buffer = np.concatenate((new_x, self._x_buffer))
        for idx, tag in enumerate(self.engine.channel_keys):
            self._y_buffers[tag] = np.concatenate((vals_array[:cutoff_idx, idx], self._y_buffers[tag]))

        # Shift the pointer to account for the new historical data
        self.write_ptr += len(new_x)

        # Update the UI views
        self.x_time = self._x_buffer[:self.write_ptr]
        for tag in self.engine.channel_keys:
            self.y_data[tag] = self._y_buffers[tag][:self.write_ptr]

        self.oldest_loaded_ts = min(self.oldest_loaded_ts, req_start)
        self.current_stride = min(self.current_stride, stride)
        self.is_fetching = False
        self.data_updated.emit()

    def _on_live_data(self, raw_data_dict: dict):
        if not self.engine.channel_keys:
            return

        flat_data = self.payload_mapper.parse(raw_data_dict)

        # 1. Expand buffers if we hit the end
        if self.write_ptr >= len(self._x_buffer):
            self._x_buffer = np.concatenate((self._x_buffer, np.zeros(self.chunk_size)))
            for tag in self.engine.channel_keys:
                self._y_buffers[tag] = np.concatenate((self._y_buffers[tag], np.zeros(self.chunk_size)))

        # 2. Insert the live data using the pointer
        self._x_buffer[self.write_ptr] = time.time()

        for tag in self.engine.channel_keys:
            val = flat_data.get(tag)
            if val is None:
                # Hold previous value if network drops, or NaN if it's the very first tick
                val = self._y_buffers[tag][self.write_ptr - 1] if self.write_ptr > 0 else np.nan
            self._y_buffers[tag][self.write_ptr] = val

        self.write_ptr += 1

        # 3. Update the windows for the UI to read
        self.x_time = self._x_buffer[:self.write_ptr]
        for tag in self.engine.channel_keys:
            self.y_data[tag] = self._y_buffers[tag][:self.write_ptr]

        # 4. Prune Old Memory (Indefinite Runtime Protection)
        if self.write_ptr > self.max_capacity:
            trim_amount = 50000  # Drop the oldest ~1.3 hours of data from RAM

            self._x_buffer = self._x_buffer[trim_amount:]
            for tag in self.engine.channel_keys:
                self._y_buffers[tag] = self._y_buffers[tag][trim_amount:]

            self.write_ptr -= trim_amount

            self.x_time = self._x_buffer[:self.write_ptr]
            for tag in self.engine.channel_keys:
                self.y_data[tag] = self._y_buffers[tag][:self.write_ptr]