import os
import time
import numpy as np
import psutil
from PyQt6.QtCore import QObject, QThread, pyqtSignal, QTimer

from src.core.data_engine import TimeSeriesEngine
from src.core.zmq_listener import ZMQLiveEngine
from src.core.live_buffer import LiveStore
from src.core.network_map import (
    ZMQ_PORT_PLC_PUB, ZMQ_PORT_VACUUM_PUB, ZMQ_PORT_SRC_TURBO_PUB,
    ZMQ_PORT_SPELLMAN_PUB, ZMQ_PORT_MAGNET_PUB, ZMQ_PORT_SMU_PUB, ZMQ_PORT_ADAM_PUB,
    ZMQ_PORT_STEERER_MAGNET_PUB
)
from src.core.perf_utils import PerfTracker


class StatelessQueryWorker(QThread):
    data_fetched = pyqtSignal(float, float, object, object, int)
    TARGET_POINTS = 8000

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
            # The viewer now fetches 2x the visible width (margin for panning), so
            # 8000 points (4000 min/max bins) keeps ~1 bin per pixel on screen.
            target = self.TARGET_POINTS
            if len(ts) > target:
                # PREVENT MUTATION: Save the raw time array to align with all raw value arrays
                original_ts = ts

                first_key = list(vals_dict.keys())[0]
                ts, _ = self.engine.downsample_minmax(original_ts, vals_dict[first_key], target_points=target)

                for k, v in vals_dict.items():
                    _, vals_dict[k] = self.engine.downsample_minmax(original_ts, v, target_points=target)

            self.data_fetched.emit(self.req_start, self.req_end, ts, vals_dict, self.req_id)
        except Exception as e:
            import traceback
            print(f"[Worker] Error during fetch:\n{traceback.format_exc()}")
            self.data_fetched.emit(self.req_start, self.req_end, np.array([]), {}, self.req_id)

class DualPipelineCache(QObject):
    LIVE_KEEP_SECONDS = 1800.0   # live history kept per source (was ~15 min total)
    live_updated = pyqtSignal()
    historical_updated = pyqtSignal(object, object, int)
    markers_changed = pyqtSignal()

    def __init__(self, log_dir: str):
        super().__init__()
        self.engine = TimeSeriesEngine(log_dir)
        self.query_worker = StatelessQueryWorker(self.engine)

        self.query_worker.data_fetched.connect(self._on_historical_fetched)
        self.query_worker.finished.connect(self._on_worker_finished)

        # Live data: one ring per publisher (see src/core/live_buffer.py). Replaces the
        # single 100k x 336-column ring (~270 MB, ~15 min) that wrote a full forward-
        # filled row for every message from any service.
        self.live = LiveStore(allowed_tags=self.engine.channel_keys, keep_seconds=self.LIVE_KEEP_SECONDS)

        self.zmq_listener = ZMQLiveEngine(
            ZMQ_PORT_PLC_PUB, ZMQ_PORT_VACUUM_PUB, ZMQ_PORT_SRC_TURBO_PUB,
            ZMQ_PORT_SPELLMAN_PUB, ZMQ_PORT_MAGNET_PUB, ZMQ_PORT_SMU_PUB, ZMQ_PORT_ADAM_PUB, ZMQ_PORT_STEERER_MAGNET_PUB,
            sink=self.live.ingest,
        )

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
            f"[UI] Time {time.time()} | CPU: {self.process.cpu_percent():>4.1f}% | RAM: {mem_mb:>6.1f} MB | {perf}")
        rows = self.live.stats()
        if rows:
            print("[LIVE] source             msg/s  tags  buffered   MB")
            for name, rate, ntags, span, mb in rows:
                print(f"[LIVE] {name:<18} {rate:6.1f} {ntags:5d} {span / 60:7.1f} min {mb:5.1f}")
        else:
            print("[LIVE] no live data received")
        self._debug_memory_footprint()

    def _debug_memory_footprint(self):
        """Calculates the strict physical byte size of loaded data structures."""
        # 1. Measure the Live Circular Buffers (NumPy)
        live_total_mb = self.live.nbytes() / (1024 * 1024)

        # 2. Measure the Preloaded Historical Data (Polars)
        # Note: If self.engine.chunk_mart uses a different naming convention, adjust accordingly.
        try:
            # list() snapshots the dict atomically: sync_active_chunks() mutates
            # chunk_mart from a background thread, and iterating it live can raise
            # "dictionary changed size during iteration" inside this Qt timer slot,
            # which aborts the app under PyQt6.
            chunk_mart_mb = sum(df.estimated_size("mb") for df in list(self.engine.chunk_mart.values()))
        except Exception:
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

    def _on_live_data(self, flat_data: dict, source: str = "default"):
        """Compatibility entry point (old signal-based path / tests): ingest one
        message stamped with the current time."""
        self.live.ingest(source, time.time(), flat_data)