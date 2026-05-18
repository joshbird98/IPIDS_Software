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
import os

from src.core.network_config import (
    ZMQ_PORT_PLC_PUB, ZMQ_PORT_VACUUM_PUB, ZMQ_PORT_SRC_TURBO_PUB,
    TOPIC_PLC_DATA, TOPIC_VACUUM_DATA, TOPIC_SRC_TURBO_DATA,
    ZMQ_PORT_HEARTBEAT
)
from src.core.payload_mapper import DynamicPayloadMapper

# --- Dynamic Path Resolution ---
# Locates IPIDS_Software/ based on the location of this script (src/services/service_logger.py)
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(CURRENT_DIR, "../../"))

DEFAULT_LOG_DIRECTORY = os.path.join(PROJECT_ROOT, "data", "logging")
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

        self.sub_socket.setsockopt(zmq.SUBSCRIBE, TOPIC_PLC_DATA)
        self.sub_socket.setsockopt(zmq.SUBSCRIBE, TOPIC_VACUUM_DATA)

        if isinstance(TOPIC_SRC_TURBO_DATA, str):
            self.sub_socket.setsockopt(zmq.SUBSCRIBE, TOPIC_SRC_TURBO_DATA.encode('utf-8'))
        else:
            self.sub_socket.setsockopt(zmq.SUBSCRIBE, TOPIC_SRC_TURBO_DATA)

        self.poller = zmq.Poller()
        self.poller.register(self.sub_socket, zmq.POLLIN)

        # 3. Schema & State
        self.payload_mapper = DynamicPayloadMapper()
        self.keys = sorted(list(self.payload_mapper.valid_keys))
        self.num_keys = len(self.keys)
        self.current_state = {k: np.nan for k in self.keys}

        # 4. Load Configuration
        try:
            with open(LOGGER_CONFIG_PATH, "r") as f:
                cfg = json.load(f)
                self.tick_rate = cfg.get("tick_rate_seconds", 0.050)
                self.flush_interval = cfg.get("flush_interval_seconds", 900.0)
                overhead = cfg.get("buffer_overhead_multiplier", 1.1)
        except FileNotFoundError:
            print(f"[Logger WARNING] Config not found at {LOGGER_CONFIG_PATH}. Using defaults.")
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

    def _update_state_cache(self, topic: bytes, payload_bytes: bytes):
        try:
            # Decode the topic for the warning printout
            topic_str = topic.decode('utf-8', errors='ignore')
            data = orjson.loads(payload_bytes)

            # Pass the topic to the mapper
            flat_dict = self.payload_mapper.parse(data, topic=topic_str)
            self.current_state.update(flat_dict)
        except orjson.JSONDecodeError:
            pass

    def _log_current_state(self):
        current_ts = time.time()

        row = np.array([float(self.current_state.get(k, np.nan)) for k in self.keys], dtype=np.float32)

        self.ts_buffer[self.ptr] = current_ts
        self.val_buffer[self.ptr, :] = row
        self.ptr += 1

        if self.ptr >= self.max_rows:
            print("[Logger WARNING] RAM Buffer exceeded max bounds. Forcing emergency flush.")
            self._queue_flush()

    def _queue_flush(self):
        if self.ptr == 0:
            return

        queue_start = time.perf_counter()

        ts_data = self.ts_buffer[:self.ptr].copy()
        val_data = self.val_buffer[:self.ptr, :].copy()

        self.ptr = 0
        self.last_flush = time.time()

        filename = f"chunk_{datetime.now().strftime('%Y%m%d_%H%M%S')}.parquet"
        filepath = os.path.join(DEFAULT_LOG_DIRECTORY, filename)

        self.write_queue.put((filepath, ts_data, val_data, self.keys))

        queue_time_ms = (time.perf_counter() - queue_start) * 1000
        print(f"[Logger I/O] Array copied to write queue in {queue_time_ms:.2f} ms")

    def _disk_writer_loop(self):
        while self.running:
            try:
                task = self.write_queue.get(timeout=1.0)
                filepath, ts, data, keys = task

                write_start = time.perf_counter()

                try:
                    df_dict = {"timestamp": ts}
                    for i, key in enumerate(keys):
                        df_dict[key] = data[:, i]

                    df = pl.DataFrame(df_dict)

                    temp_path = filepath.replace(".parquet", "_temp.parquet")
                    df.write_parquet(temp_path, compression="zstd")

                    if os.path.exists(temp_path):
                        if os.path.exists(filepath):
                            os.remove(filepath)
                        os.rename(temp_path, filepath)

                    write_time_ms = (time.perf_counter() - write_start) * 1000

                    ram_mb = df.estimated_size("mb")
                    file_kb = os.path.getsize(filepath) / 1024

                    print(f"[Logger I/O] Flushed {len(ts)} rows. "
                          f"RAM: {ram_mb:.1f}MB -> Disk: {file_kb:.1f}KB. "
                          f"Time: {write_time_ms:.1f} ms -> {os.path.basename(filepath)}")

                except Exception as io_err:
                    print(f"[Logger I/O Error] Failed to write {os.path.basename(filepath)}: {io_err}")
                finally:
                    self.write_queue.task_done()

            except queue.Empty:
                continue

    def run(self):
        import gc
        gc.disable()

        print(f"[Logger] {1.0 / self.tick_rate}Hz Parquet Logger Online. Tracking {self.num_keys} channels.")

        current_time = time.perf_counter()
        self.next_tick = current_time + self.tick_rate
        self.last_flush = current_time
        last_tick_time = current_time

        self.perf_max_tick_delta_ms = 0.0

        while self.running:
            try:
                current_time = time.perf_counter()

                # 1. 2Hz Heartbeat
                if current_time - self.last_hb_time >= 0.5:
                    self.hb_socket.send_json({"service": "service_logger", "ts": time.time()})
                    self.last_hb_time = current_time

                # 2. INSTANT ZMQ Poll (Do not let ZMQ handle sleeping)
                zmq_start_drain = time.perf_counter()
                socks = dict(self.poller.poll(timeout=0))  # <--- Changed to 0

                if self.sub_socket in socks and socks[self.sub_socket] == zmq.POLLIN:
                    while True:
                        try:
                            topic, payload = self.sub_socket.recv_multipart(flags=zmq.NOBLOCK)
                            self._update_state_cache(topic, payload)
                        except zmq.Again:
                            break

                zmq_duration_ms = (time.perf_counter() - zmq_start_drain) * 1000
                self.perf_max_zmq_drain_ms = max(self.perf_max_zmq_drain_ms, zmq_duration_ms)

                # 3. The Precision Sleep & Spin-Wait
                sleep_time = self.next_tick - time.perf_counter()
                if sleep_time > 0.002:
                    # Sleep until we are 2ms away from the deadline
                    time.sleep(sleep_time - 0.002)

                # Burn CPU cycles for the final <2ms to hit the deadline perfectly
                while time.perf_counter() < self.next_tick:
                    pass

                # 4. 20Hz Strict Logging Tick
                current_time = time.perf_counter()
                self._log_current_state()

                tick_delta_ms = (current_time - last_tick_time) * 1000
                self.perf_max_tick_delta_ms = max(self.perf_max_tick_delta_ms, tick_delta_ms)
                last_tick_time = current_time

                self.next_tick += self.tick_rate
                self.tick_count += 1

                # 5. 15-Minute Flush
                if current_time - self.last_flush >= self.flush_interval:
                    self._queue_flush()
                    self.last_flush = current_time
                    gc.collect()

                    # 6. Performance Monitoring
                if self.tick_count >= int(10.0 / self.tick_rate):
                    print(f"[Logger Health] Buffer: {self.ptr}/{self.max_rows} | "
                          f"Max Drain: {self.perf_max_zmq_drain_ms:.2f}ms | "
                          f"Max Tick Delta: {self.perf_max_tick_delta_ms:.2f}ms (Target: {self.tick_rate * 1000:.1f}ms)")

                    self.tick_count = 0
                    self.perf_max_tick_delta_ms = 0.0
                    self.perf_max_zmq_drain_ms = 0.0

            except KeyboardInterrupt:
                break
            except Exception as e:
                print(f"[Logger Main Loop Error] {e}")

        self.running = False
        print("[Logger] Shutdown signal received. Forcing final disk flush...")
        self._queue_flush()
        self.io_thread.join(timeout=5.0)
        print("[Logger] Shutdown complete.")

if __name__ == "__main__":
    # Force Windows high-resolution timers (1ms precision)
    if os.name == 'nt':
        import ctypes

        ctypes.windll.winmm.timeBeginPeriod(1)
    svc = TelemetryLoggerService()
    svc.run()