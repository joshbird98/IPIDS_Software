import zmq
import orjson
import json
import time
import os
import queue
import threading
import numpy as np
import polars as pl
from datetime import datetime
import threading

from src.core.event_helper import EventHelper
from src.core.network_map import (
    ZMQ_PORT_PLC_PUB, ZMQ_PORT_VACUUM_PUB, ZMQ_PORT_SRC_TURBO_PUB, ZMQ_PORT_SPELLMAN_PUB, ZMQ_PORT_MAGNET_PUB, ZMQ_PORT_SMU_PUB,
    TOPIC_PLC_DATA, TOPIC_VACUUM_DATA, TOPIC_SRC_TURBO_DATA, TOPIC_SPELLMAN_DATA, TOPIC_MAGNET_DATA, TOPIC_SMU_DATA,
    ZMQ_PORT_HEARTBEAT, ZMQ_PORT_LOGGER_CMD
)
from src.core.payload_mapper import DynamicPayloadMapper
from src.core.os_helper import harden_windows_process

# --- Dynamic Path Resolution ---
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(CURRENT_DIR, "../../"))

DEFAULT_LOG_DIRECTORY = os.path.join(PROJECT_ROOT, "data", "parquet_logs")
LOGGER_CONFIG_PATH = os.path.join(PROJECT_ROOT, "config", "logger_config.json")


class TelemetryLoggerService:
    def __init__(self):
        self.context = zmq.Context()

        # 1. Heartbeat Socket (2Hz)
        self.hb_socket = self.context.socket(zmq.PUB)
        self.hb_socket.connect(ZMQ_PORT_HEARTBEAT)
        self.last_hb_time = 0.0


        # 2. Data Subscription Sockets
        self.sub_socket = self.context.socket(zmq.SUB)
        self.sub_socket.connect(ZMQ_PORT_PLC_PUB)
        self.sub_socket.connect(ZMQ_PORT_VACUUM_PUB)
        self.sub_socket.connect(ZMQ_PORT_SRC_TURBO_PUB)
        self.sub_socket.connect(ZMQ_PORT_SPELLMAN_PUB)
        self.sub_socket.connect(ZMQ_PORT_MAGNET_PUB)
        self.sub_socket.connect(ZMQ_PORT_SMU_PUB)

        self.last_cmd_time = 0.0
        self.cmd_debounce = 2.0

        # Ensure topics are bytes
        def _ensure_bytes(t):
            return t.encode('utf-8') if isinstance(t, str) else t

        self.sub_socket.setsockopt(zmq.SUBSCRIBE, _ensure_bytes(TOPIC_PLC_DATA))
        self.sub_socket.setsockopt(zmq.SUBSCRIBE, _ensure_bytes(TOPIC_VACUUM_DATA))
        self.sub_socket.setsockopt(zmq.SUBSCRIBE, _ensure_bytes(TOPIC_SRC_TURBO_DATA))
        self.sub_socket.setsockopt(zmq.SUBSCRIBE, _ensure_bytes(TOPIC_SPELLMAN_DATA))
        self.sub_socket.setsockopt(zmq.SUBSCRIBE, _ensure_bytes(TOPIC_MAGNET_DATA))
        self.sub_socket.setsockopt(zmq.SUBSCRIBE, _ensure_bytes(TOPIC_SMU_DATA))

        self.events = EventHelper("service_logger")
        self.poller = zmq.Poller()
        self.poller.register(self.sub_socket, zmq.POLLIN)

        self.cmd_socket = self.context.socket(zmq.REP)
        self.cmd_socket.bind(ZMQ_PORT_LOGGER_CMD)  # Dedicated UI command port
        self.poller.register(self.cmd_socket, zmq.POLLIN)

        # 3. Schema & Zero-Order Hold State
        self.payload_mapper = DynamicPayloadMapper()
        self.keys = sorted(list(self.payload_mapper.valid_keys))
        self.num_keys = len(self.keys)

        # Base state initialized to NaN (will remain NaN until first packet arrives)
        self.current_state = {k: np.nan for k in self.keys}

        # --- WATCHDOG TRACKING ---
        self.topic_last_seen = {}  # topic_str -> unix_timestamp
        self.topic_keys = {}  # topic_str -> set(keys_provided_by_this_topic)
        self.topic_watchdog_tripped = {}  # topic_str -> bool

        # 4. Load Configuration
        try:
            with open(LOGGER_CONFIG_PATH, "r") as f:
                cfg = json.load(f)
                self.tick_rate = cfg.get("tick_rate_seconds", 0.050)
                self.flush_interval = cfg.get("flush_interval_seconds", 900.0)
                overhead = cfg.get("buffer_overhead_multiplier", 1.1)
        except FileNotFoundError:
            self.events.log_general(f"[Logger WARNING] Config not found at {LOGGER_CONFIG_PATH}. Using defaults.")
            self.tick_rate = 0.050
            self.flush_interval = 900.0
            overhead = 1.1

        # 5. High-Speed RAM Buffer
        self.max_rows = int((self.flush_interval / self.tick_rate) * overhead)
        self.ts_buffer = np.zeros(self.max_rows, dtype=np.float64)
        self.val_buffer = np.zeros((self.max_rows, self.num_keys), dtype=np.float32)
        self.ptr = 0

        # 6. Disk I/O Background Thread
        os.makedirs(DEFAULT_LOG_DIRECTORY, exist_ok=True)
        self.write_queue = queue.Queue()
        self.running = True
        self.io_thread = threading.Thread(target=self._disk_writer_loop, daemon=True)
        self.io_thread.start()

        self.next_tick = time.time() + self.tick_rate
        self.last_flush = time.time()

        # --- PERFORMANCE METRICS ---
        self.tick_count = 0
        self.perf_max_loop_ms = 0.0
        self.perf_max_zmq_drain_ms = 0.0
        self.perf_max_tick_delta_ms = 0.0

    def _update_state_cache(self, topic: bytes, payload_bytes: bytes):
        try:
            topic_str = topic.decode('utf-8', errors='ignore')
            data = orjson.loads(payload_bytes)

            flat_dict = self.payload_mapper.parse(data, topic=topic_str)

            # 1. Update the state (Zero-Order Hold)
            self.current_state.update(flat_dict)

            # 2. Update Watchdog timer
            self.topic_last_seen[topic_str] = time.time()

            # 3. Learn which keys belong to this topic dynamically
            if topic_str not in self.topic_keys:
                self.topic_keys[topic_str] = set()
            self.topic_keys[topic_str].update(flat_dict.keys())

        except orjson.JSONDecodeError:
            pass

    def _evaluate_watchdogs(self):
        """Checks if any service has stopped broadcasting and injects NaNs if necessary."""
        current_ts = time.time()

        for topic_str, last_seen in self.topic_last_seen.items():
            if current_ts - last_seen > 1.0:
                # Trip condition: No data for 1.0 seconds
                if not self.topic_watchdog_tripped.get(topic_str, False):
                    self.events.log_general(
                        message=f"Network Timeout: {topic_str} silent for >1s. Forcing NaNs.",
                        severity="WARNING",
                        event_type="WATCHDOG_TRIP"
                    )
                    self.topic_watchdog_tripped[topic_str] = True

                # Actively overwrite the cached values with NaN to create visual gaps
                for k in self.topic_keys.get(topic_str, set()):
                    self.current_state[k] = np.nan
            else:
                # Recovery condition
                if self.topic_watchdog_tripped.get(topic_str, False):
                    self.events.log_general(
                        message=f"Network Recovery: {topic_str} data stream restored.",
                        severity="INFO",
                        event_type="WATCHDOG_CLEAR"
                    )
                    self.topic_watchdog_tripped[topic_str] = False

    def _log_current_state(self):
        current_ts = time.time()

        # Build the 1D array perfectly ordered to the schema
        row = np.array([float(self.current_state.get(k, np.nan)) for k in self.keys], dtype=np.float32)

        self.ts_buffer[self.ptr] = current_ts
        self.val_buffer[self.ptr, :] = row
        self.ptr += 1

        if self.ptr >= self.max_rows:
            self.events.log_general("[Logger WARNING] RAM Buffer exceeded max bounds. Forcing emergency flush.")
            self._queue_flush()

    def _queue_flush(self, wait_for_completion=False):
        if self.ptr == 0:
            return True  # Nothing to flush

        ts_data = self.ts_buffer[:self.ptr].copy()
        val_data = self.val_buffer[:self.ptr, :].copy()

        self.ptr = 0
        self.last_flush = time.time()

        filename = f"chunk_{datetime.now().strftime('%Y%m%d_%H%M%S')}.parquet"
        filepath = os.path.join(DEFAULT_LOG_DIRECTORY, filename)

        if wait_for_completion:
            completion_event = threading.Event()
            # Pass the event as the 5th item in the tuple
            self.write_queue.put((filepath, ts_data, val_data, self.keys, completion_event))

            # Pause here until the background thread sets the event (Max 2 seconds)
            success = completion_event.wait(timeout=2.0)
            return success
        else:
            # Standard fire-and-forget background flush
            self.write_queue.put((filepath, ts_data, val_data, self.keys, None))
            return True

    def _disk_writer_loop(self):
        while self.running:
            try:
                task = self.write_queue.get(timeout=1.0)

                # Safely unpack the tuple (handles both normal 4-item and forced 5-item flushes)
                filepath, ts, data, keys = task[:4]
                completion_event = task[4] if len(task) > 4 else None

                try:
                    df_dict = {"timestamp": ts}
                    for i, key in enumerate(keys):
                        df_dict[key] = data[:, i]

                    import polars as pl
                    df = pl.DataFrame(df_dict)
                    temp_path = filepath.replace(".parquet", "_temp.parquet")

                    # High compression to heavily shrink the repeated run-length arrays
                    df.write_parquet(temp_path, compression="zstd", compression_level=3)

                    if os.path.exists(temp_path):
                        if os.path.exists(filepath):
                            os.remove(filepath)
                        os.rename(temp_path, filepath)

                except Exception as io_err:
                    self.events.log_general(
                        f"[Logger I/O Error] Failed to write {os.path.basename(filepath)}: {io_err}")
                finally:
                    # Wake up the main ZMQ thread immediately after the file is safe on disk
                    if completion_event:
                        completion_event.set()

                    self.write_queue.task_done()

            except queue.Empty:
                continue

    def run(self):
        import gc
        gc.disable()

        self.events.log_general(
            f"{1.0 / self.tick_rate}Hz Parquet Logger Online. Tracking {self.num_keys} channels.")

        current_time = time.perf_counter()
        self.next_tick = current_time + self.tick_rate
        self.last_flush = current_time
        last_tick_time = current_time

        self.events.log_general("Injecting Boot-Up Null Marker to sever historical plot lines.")
        self._log_current_state()

        while self.running:
            try:
                current_time = time.perf_counter()

                # 1. 2Hz Heartbeat
                if current_time - self.last_hb_time >= 0.5:
                    self.hb_socket.send_json({"service": "service_logger", "ts": time.time()})
                    self.last_hb_time = current_time

                # 2. INSTANT ZMQ Poll
                zmq_start_drain = time.perf_counter()
                socks = dict(self.poller.poll(timeout=0))

                if self.sub_socket in socks and socks[self.sub_socket] == zmq.POLLIN:
                    while True:
                        try:
                            topic, payload = self.sub_socket.recv_multipart(flags=zmq.NOBLOCK)
                            self._update_state_cache(topic, payload)
                        except zmq.Again:
                            break

                zmq_duration_ms = (time.perf_counter() - zmq_start_drain) * 1000
                self.perf_max_zmq_drain_ms = max(self.perf_max_zmq_drain_ms, zmq_duration_ms)

                # If requested by external program (like viewer) flush data
                if self.cmd_socket in socks and socks[self.cmd_socket] == zmq.POLLIN:
                    try:
                        # 1. Read the simple string command (no longer multipart JSON)
                        msg = self.cmd_socket.recv_string(flags=zmq.NOBLOCK)

                        if msg == "FORCE_FLUSH":
                            now = time.time()

                            if now - self.last_cmd_time > self.cmd_debounce:
                                self.events.log_general("Remote UI requested emergency disk flush.")

                                # 1. Ask for a flush and wait for the background thread to confirm it
                                success = self._queue_flush(wait_for_completion=True)
                                self.last_cmd_time = now

                                # 2. Reply to the UI based on the exact outcome
                                if success:
                                    self.cmd_socket.send_string("FLUSH_COMPLETE")
                                else:
                                    self.cmd_socket.send_string("FLUSH_TIMEOUT")
                            else:
                                self.cmd_socket.send_string("FLUSH_DEBOUNCED")
                        else:
                            # Always reply to unknown commands to prevent state machine lockups
                            self.cmd_socket.send_string("UNKNOWN_COMMAND")

                    except Exception as e:
                        print(f"Cmd Socket Error: {e}")
                        # Attempt to release the lock if an error occurred after receiving the message
                        try:
                            self.cmd_socket.send_string("ERROR_DURING_FLUSH")
                        except:
                            pass

                # 3. Precision Sleep & Spin-Wait
                sleep_time = self.next_tick - time.perf_counter()
                if sleep_time > 0.002:
                    time.sleep(sleep_time - 0.002)

                while time.perf_counter() < self.next_tick:
                    pass

                current_time = time.perf_counter()

                # --- NEW: Evaluate Watchdogs before Logging ---
                self._evaluate_watchdogs()

                # 4. 20Hz Strict Logging Tick
                self._log_current_state()

                tick_delta_ms = (current_time - last_tick_time) * 1000
                self.perf_max_tick_delta_ms = max(self.perf_max_tick_delta_ms, tick_delta_ms)
                last_tick_time = current_time

                self.next_tick += self.tick_rate
                self.tick_count += 1

                # 5. 15-Minute Flush
                if time.time() - self.last_flush >= self.flush_interval:
                    self._queue_flush()
                    gc.collect()

            except KeyboardInterrupt:
                break
            except Exception as e:
                print(f"[Logger Main Loop Error] {e}")

        self.running = False
        self.events.log_general("Shutdown signal received. Forcing final disk flush...")
        self._queue_flush()
        self.io_thread.join(timeout=5.0)
        self.events.log_general("Shutdown complete.")


if __name__ == "__main__":
    harden_windows_process()
    if os.name == 'nt':
        import ctypes

        ctypes.windll.winmm.timeBeginPeriod(1)
    svc = TelemetryLoggerService()
    svc.run()