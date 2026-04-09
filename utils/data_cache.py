import os
import json
import time
import numpy as np
from PyQt6.QtCore import QObject, QThread, pyqtSignal

from utils.data_engine import TimeSeriesEngine
from utils.zmq_listener import ZMQLiveEngine
from network_config import (
    ZMQ_PORT_PLC_PUB, ZMQ_PORT_VACUUM_PUB, ZMQ_PORT_SRC_TURBO_PUB,
    TOPIC_PLC_DATA, TOPIC_VACUUM_DATA, TOPIC_SRC_TURBO_DATA
)


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

    def __init__(self, log_dir: str):
        super().__init__()
        self.engine = TimeSeriesEngine(log_dir)
        self.vacuum_hw_map = self._build_vacuum_hw_map()

        self.history_worker = HistoryFetchWorker(self.engine)
        self.history_worker.data_fetched.connect(self._on_history_fetched)

        self.zmq_listener = ZMQLiveEngine(ZMQ_PORT_PLC_PUB, ZMQ_PORT_VACUUM_PUB, ZMQ_PORT_SRC_TURBO_PUB)
        self.zmq_listener.data_ready.connect(self._on_live_data)

        # Master Data Storage
        self.tags = []
        self.x_time = np.array([], dtype=np.float64)

        # THE OMNISCIENT CACHE: Pre-allocate arrays for ALL keys instantly upon boot
        self.y_data = {key: np.array([], dtype=np.float64) for key in self.engine.channel_keys}

        self.oldest_loaded_ts = time.time()
        self.is_fetching = False
        self.current_stride = 1

    def _build_vacuum_hw_map(self) -> dict:
        current_dir = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.dirname(current_dir)
        settings_path = os.path.join(project_root, "config", "vacuum_settings.json")

        hw_map = {}
        try:
            with open(settings_path, "r") as f:
                vacuum_settings = json.load(f)

            for node in ["10", "20"]:
                if node in vacuum_settings and "channels" in vacuum_settings[node]:
                    for ch, data in vacuum_settings[node]["channels"].items():
                        subsystem = data.get("subsystem", f"controller_{node}")
                        device = data.get("device", f"ch_{ch}")
                        hw_map[(node, ch)] = f"vacuum.{subsystem}.{device}"
        except Exception as e:
            print(f"[Cache] Failed to build HW map: {e}")

        return hw_map

    def _flatten_vacuum_data(self, data: dict) -> dict:
        flat_data = {}
        STATUS_MAP = {"OK": 0.0, "NO-SEN": 1.0, "RANGE?": 2.0, "S-OFF": 3.0, "ERROR-H": 4.0, "ERROR-L": 5.0,
                      "ERROR-S": 6.0}

        for node_id_str, node_data in data.items():
            if not isinstance(node_data, dict): continue

            for ch_id_str, ch_data in node_data.get("channels", {}).items():
                base_path = self.vacuum_hw_map.get((node_id_str, ch_id_str))
                if not base_path: continue

                p = ch_data.get("pressure")
                if p is not None: flat_data[f"{base_path}.pressure"] = float(p)

                s = ch_data.get("status")
                if s is not None: flat_data[f"{base_path}.status"] = STATUS_MAP.get(str(s).strip().upper(), -1.0)

        return flat_data

    def _flatten_turbo_data(self, data: dict) -> dict:
        if not isinstance(data.get("temps"), dict) or not isinstance(data.get("electrical"), dict):
            return {}

        flat_data = {}
        if "hz" in data: flat_data["vacuum.source_chamber.turbo_1.speed_hz"] = float(data["hz"])
        if "pct" in data: flat_data["vacuum.source_chamber.turbo_1.speed_pct"] = float(data["pct"])

        temps = data.get("temps", {})
        if "bearing" in temps: flat_data["vacuum.source_chamber.turbo_1.temp_bearing"] = float(temps["bearing"])
        if "converter" in temps: flat_data["vacuum.source_chamber.turbo_1.temp_converter"] = float(temps["converter"])

        elec = data.get("electrical", {})
        if "volts" in elec: flat_data["vacuum.source_chamber.turbo_1.voltage"] = float(elec["volts"])
        if "amps" in elec: flat_data["vacuum.source_chamber.turbo_1.current"] = float(elec["amps"])

        status = data.get("status", {})
        if "turning" in status: flat_data["vacuum.source_chamber.turbo_1.status_turning"] = 1.0 if status[
            "turning"] else 0.0
        if "ready" in status: flat_data["vacuum.source_chamber.turbo_1.status_ready"] = 1.0 if status["ready"] else 0.0
        if "error_active" in status: flat_data["vacuum.source_chamber.turbo_1.status_error"] = 1.0 if status[
            "error_active"] else 0.0

        return flat_data

    def register_tags(self, tags: list):
        """Updates which tags the UI is currently plotting."""
        self.tags = [tag for tag in tags if tag in self.engine.channel_keys]

    def start(self):
        self.zmq_listener.start()

    def stop(self):
        self.zmq_listener.stop()
        self.history_worker.wait()

    def request_history(self, start_ts: float, end_ts: float, stride=1):
        if self.is_fetching: return
        self.is_fetching = True
        self.history_worker.fetch(start_ts, end_ts, stride)

    def _on_history_fetched(self, req_start, req_end, ts_array, vals_array, stride):
        if len(ts_array) == 0:
            self.is_fetching = False
            self.oldest_loaded_ts = min(self.oldest_loaded_ts, req_start)
            return

        if len(self.x_time) > 0:
            cutoff_idx = np.searchsorted(ts_array, self.x_time[0], side='left')
            new_x = ts_array[:cutoff_idx]
        else:
            cutoff_idx = len(ts_array)
            new_x = ts_array

        if len(new_x) == 0:
            self.is_fetching = False
            return

        self.x_time = np.concatenate((new_x, self.x_time))

        # OMNISCIENT UPDATE: Save history for EVERY known key, even if the UI isn't looking at it yet
        for idx, tag in enumerate(self.engine.channel_keys):
            hist_y = vals_array[:cutoff_idx, idx]
            current_y = self.y_data[tag]
            self.y_data[tag] = np.concatenate((hist_y, current_y))

        self.oldest_loaded_ts = min(self.oldest_loaded_ts, req_start)
        self.current_stride = min(self.current_stride, stride)
        self.is_fetching = False
        self.data_updated.emit()

    def _on_live_data(self, raw_data_dict: dict):
        if not self.engine.channel_keys:
            return

        flat_data = {}
        for k, v in raw_data_dict.items():
            if not isinstance(v, dict):
                flat_data[k] = v
        flat_data.update(self._flatten_vacuum_data(raw_data_dict))
        flat_data.update(self._flatten_turbo_data(raw_data_dict))

        current_time = time.time()
        self.x_time = np.append(self.x_time, current_time)

        # OMNISCIENT UPDATE: Append live data for EVERY known key
        for tag in self.engine.channel_keys:
            val = flat_data.get(tag)
            if val is None:
                # If network drops, hold previous value, or use NaN if first tick (prevents plotting 0.0 log errors)
                val = self.y_data[tag][-1] if len(self.y_data[tag]) > 0 else np.nan

            self.y_data[tag] = np.append(self.y_data[tag], val)

        self.data_updated.emit()