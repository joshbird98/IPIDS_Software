import os
import time
import numpy as np
import psutil
from PyQt6.QtCore import QObject, QThread, pyqtSignal, QTimer

from src.core.data_engine import TimeSeriesEngine
from src.core.zmq_listener import ZMQLiveEngine
from src.core.network_map import (
    ZMQ_PORT_PLC_PUB, ZMQ_PORT_VACUUM_PUB, ZMQ_PORT_SRC_TURBO_PUB,
    ZMQ_PORT_SPELLMAN_PUB, ZMQ_PORT_MAGNET_PUB, ZMQ_PORT_SMU_PUB
)
from src.core.perf_utils import PerfTracker


class StatelessQueryWorker(QThread):
    data_fetched = pyqtSignal(float, float, object, object, int)

    def __init__(self, engine):
        super().__init__()
        self.engine = engine

    def fetch(self, start_ts: float, end_ts: float, request_id: int, selected_tags: list):
        self.req_start = start_ts
        self.req_end = end_ts
        self.req_id = request_id
        self.selected_tags = selected_tags
        self.start()

    def run(self):
        try:
            ts, vals_dict = self.engine.query_stateless(
                self.req_start, self.req_end, self.selected_tags
            )

            # Safety Valve Downsampling
            if len(ts) > 4000:
                # PREVENT MUTATION: Save the raw time array to align with all raw value arrays
                original_ts = ts

                first_key = list(vals_dict.keys())[0]
                ts, _ = self.engine.downsample_minmax(original_ts, vals_dict[first_key], target_points=4000)

                for k, v in vals_dict.items():
                    _, vals_dict[k] = self.engine.downsample_minmax(original_ts, v, target_points=4000)

            self.data_fetched.emit(self.req_start, self.req_end, ts, vals_dict, self.req_id)
        except Exception as e:
            import traceback
            print(f"[Worker] Error during fetch:\n{traceback.format_exc()}")
            self.data_fetched.emit(self.req_start, self.req_end, np.array([]), {}, self.req_id)

class DualPipelineCache(QObject):
    live_updated = pyqtSignal()
    historical_updated = pyqtSignal(object, object, int)
    markers_changed = pyqtSignal()

    def __init__(self, log_dir: str):
        super().__init__()
        self.engine = TimeSeriesEngine(log_dir)
        self.query_worker = StatelessQueryWorker(self.engine)

        self.query_worker.data_fetched.connect(self._on_historical_fetched)
        self.query_worker.finished.connect(self._on_worker_finished)

        self.zmq_listener = ZMQLiveEngine(
            ZMQ_PORT_PLC_PUB, ZMQ_PORT_VACUUM_PUB, ZMQ_PORT_SRC_TURBO_PUB,
            ZMQ_PORT_SPELLMAN_PUB, ZMQ_PORT_MAGNET_PUB, ZMQ_PORT_SMU_PUB
        )
        self.zmq_listener.data_ready.connect(self._on_live_data)

        self.live_capacity = 100000
        self._x_live = np.zeros(self.live_capacity, dtype=np.float64)
        self._y_live = {key: np.zeros(self.live_capacity, dtype=np.float64) for key in self.engine.channel_keys}

        # Ring buffer pointer state
        self.live_ptr = 0
        self.buffer_wrapped = False

        self.key_last_seen = {key: time.time() for key in self.engine.channel_keys}

        self.client_tags = {}
        self.tags = set()

        self.current_request_id = 0
        self.is_fetching = False

        self.process = psutil.Process(os.getpid())
        self.health_timer = QTimer()
        self.health_timer.timeout.connect(self._print_health_stats)
        self.health_timer.start(10000)

    def _print_health_stats(self):
        mem_mb = self.process.memory_info().rss / (1024 * 1024)
        perf = PerfTracker.get_stats()
        print(
            f"[UI] Time {time.time()} | CPU: {self.process.cpu_percent():>4.1f}% | RAM: {mem_mb:>6.1f} MB | {perf} | Buffer Pointer: {self.live_ptr}")
        self._debug_memory_footprint()

    def _debug_memory_footprint(self):
        """Calculates the strict physical byte size of loaded data structures."""
        # 1. Measure the Live Circular Buffers (NumPy)
        x_mb = self._x_live.nbytes / (1024 * 1024)
        y_mb = sum(arr.nbytes for arr in self._y_live.values()) / (1024 * 1024)
        live_total_mb = x_mb + y_mb

        # 2. Measure the Preloaded Historical Data (Polars)
        # Note: If self.engine.chunk_mart uses a different naming convention, adjust accordingly.
        try:
            chunk_mart_mb = sum(df.estimated_size("mb") for df in self.engine.chunk_mart.values())
        except AttributeError:
            chunk_mart_mb = 0.0

        try:
            mart_1m_mb = self.engine.mart_1m.estimated_size("mb") if self.engine.mart_1m is not None else 0.0
            mart_20m_mb = self.engine.mart_20m.estimated_size("mb") if self.engine.mart_20m is not None else 0.0
        except AttributeError:
            mart_1m_mb = 0.0
            mart_20m_mb = 0.0

        print(f"\n[MEM DEBUG] --- RAW DATA FOOTPRINT ---")
        print(f"[MEM DEBUG] Live Ring Buffer : {live_total_mb:.1f} MB")
        print(f"[MEM DEBUG] Active Chunks RAM: {chunk_mart_mb:.1f} MB")
        print(f"[MEM DEBUG] 1m Macro Mart RAM: {mart_1m_mb:.1f} MB")
        print(f"[MEM DEBUG] 20m Macro Mart RAM: {mart_20m_mb:.1f} MB")
        print(f"[MEM DEBUG] Total Raw Data   : {live_total_mb + chunk_mart_mb + mart_1m_mb + mart_20m_mb:.1f} MB")
        print(f"[MEM DEBUG] --------------------------\n")

    def register_client_tags(self, client_id: int, tags: list):
        self.client_tags[client_id] = set(tags)
        self.tags = set().union(*self.client_tags.values())

    def start(self):
        self.zmq_listener.start()

    def stop(self):
        self.zmq_listener.stop()
        self.query_worker.wait()

    def request_historical_window(self, start_ts: float, end_ts: float, selected_tags: list):
        self.current_request_id += 1
        self.pending_request = (start_ts, end_ts, self.current_request_id, selected_tags)

        if not self.is_fetching:
            self._dispatch_pending()
        return self.current_request_id

    def _dispatch_pending(self):
        if getattr(self, 'pending_request', None) is None:
            return

        start_ts, end_ts, req_id, selected_tags = self.pending_request
        self.pending_request = None

        self.is_fetching = True
        self.active_request_id = req_id
        self.query_worker.fetch(start_ts, end_ts, req_id, selected_tags)

    def _on_historical_fetched(self, req_start, req_end, ts_array, vals_dict, req_id):
        if req_id == getattr(self, 'active_request_id', -1):
            self.historical_updated.emit(ts_array, vals_dict, req_id)

    def _on_worker_finished(self):
        self.is_fetching = False
        if getattr(self, 'pending_request', None) is not None:
            # THIS DEFERRAL PERMANENTLY FIXES THE THREAD DEADLOCK
            QTimer.singleShot(0, self._dispatch_pending)

    def _on_live_data(self, flat_data: dict):
        t0 = time.perf_counter()
        if not self.engine.channel_keys: return

        current_time = time.time()

        # O(1) True Ring Buffer Assignment
        idx = self.live_ptr % self.live_capacity
        self._x_live[idx] = current_time

        for tag in self.engine.channel_keys:
            if tag in flat_data:
                val = flat_data[tag]
                self.key_last_seen[tag] = current_time
            else:
                prev_idx = (self.live_ptr - 1) % self.live_capacity if self.live_ptr > 0 else 0
                val = self._y_live[tag][prev_idx] if self.live_ptr > 0 else np.nan
                if current_time - self.key_last_seen[tag] > 1.0: val = np.nan

            self._y_live[tag][idx] = val

        self.live_ptr += 1
        if self.live_ptr >= self.live_capacity:
            self.buffer_wrapped = True
            self.live_ptr = self.live_ptr % self.live_capacity

        # REMOVED the expensive np.concatenate logic entirely!
        PerfTracker.log("ingest", t0)