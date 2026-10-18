import gc
import json
import logging
import os
import queue
import struct
import threading
import time
from datetime import datetime

import numpy as np
import orjson
import polars as pl
import zmq

from src.core import paths
from src.core.event_helper import EventHelper
from src.core.log_setup import Throttle, setup_logging
from src.core.network_map import (
    ZMQ_PORT_PLC_PUB, ZMQ_PORT_VACUUM_PUB, ZMQ_PORT_SRC_TURBO_PUB, ZMQ_PORT_SPELLMAN_PUB, ZMQ_PORT_MAGNET_PUB, ZMQ_PORT_STEERER_MAGNET_PUB,
    ZMQ_PORT_SMU_PUB, ZMQ_PORT_ADAM_PUB,
    TOPIC_PLC_DATA, TOPIC_VACUUM_DATA, TOPIC_SRC_TURBO_DATA, TOPIC_SPELLMAN_DATA, TOPIC_MAGNET_DATA, TOPIC_STEERER_MAGNET_DATA, TOPIC_SMU_DATA,
    TOPIC_ADAM_DATA,
    ZMQ_PORT_HEARTBEAT, ZMQ_PORT_LOGGER_CMD,
)
from src.core.payload_mapper import DynamicPayloadMapper
from src.core.os_helper import harden_windows_process

# --- Dynamic Path Resolution ---
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(CURRENT_DIR, "../../"))

DEFAULT_LOG_DIRECTORY = paths.PARQUET_DIR
LOGGER_CONFIG_PATH = os.path.join(PROJECT_ROOT, "config", "logger_config.json")
JOURNAL_NAME = "logger_journal.bin"

log = logging.getLogger("service_logger")


class CrashJournal:
    """Append-only file holding the rows that are not yet in a parquet chunk.

    The logger keeps up to flush_interval_seconds (15 min) of rows in RAM and writes
    them as one parquet chunk. Without this journal a crash or power cut lost all of
    them. Every journal_interval_seconds the new rows are appended here and fsync'd;
    when the chunk is safely written the journal is started afresh. On start-up any
    rows left in the journal are turned into a '..._recovered' chunk, so at most a
    few seconds of data can be lost.

    Format: magic, uint32 header length, JSON header {"keys": [...]}, then fixed-size
    records (float64 timestamp + float32 per key). A record torn by a crash is ignored.
    """
    MAGIC = b"IPIDSJ1\n"   # file-format id from when the machine was called IPIDS; kept so older journals load

    def __init__(self, directory: str, keys: list):
        self.path = os.path.join(directory, JOURNAL_NAME)
        self.keys = list(keys)
        self.dtype = np.dtype([("ts", "<f8"), ("v", "<f4", (len(self.keys),))])
        self._f = None

    def start(self):
        """(Re)create an empty journal for the current key set."""
        self.close()
        header = orjson.dumps({"keys": self.keys, "created": time.time(), "version": 1})
        tmp = self.path + ".new"
        with open(tmp, "wb") as f:
            f.write(self.MAGIC + struct.pack("<I", len(header)) + header)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)
        self._f = open(self.path, "ab")

    def append(self, ts: np.ndarray, vals: np.ndarray):
        if self._f is None or len(ts) == 0:
            return
        rec = np.empty(len(ts), dtype=self.dtype)
        rec["ts"] = ts
        rec["v"] = vals
        self._f.write(rec.tobytes())
        self._f.flush()
        os.fsync(self._f.fileno())

    def close(self):
        if self._f is not None:
            try:
                self._f.close()
            except OSError:
                pass
            self._f = None

    @classmethod
    def read(cls, path: str):
        """Returns (keys, timestamps, values) from a journal file, or None if empty/unreadable."""
        try:
            with open(path, "rb") as f:
                raw = f.read()
        except OSError:
            return None
        if not raw.startswith(cls.MAGIC) or len(raw) < len(cls.MAGIC) + 4:
            return None
        pos = len(cls.MAGIC)
        (hlen,) = struct.unpack_from("<I", raw, pos)
        pos += 4
        try:
            header = orjson.loads(raw[pos:pos + hlen])
        except ValueError:
            return None
        pos += hlen
        keys = header.get("keys", [])
        dtype = np.dtype([("ts", "<f8"), ("v", "<f4", (len(keys),))])
        n = (len(raw) - pos) // dtype.itemsize          # drop a torn trailing record
        if n <= 0:
            return keys, np.empty(0), np.empty((0, len(keys)), dtype=np.float32)
        rec = np.frombuffer(raw, dtype=dtype, count=n, offset=pos)
        return keys, rec["ts"].copy(), rec["v"].copy()


def latest_chunk_end(directory: str) -> float:
    """Largest timestamp in the most recent chunk file (0 if none) - used to drop journal
    rows that already made it into a chunk before a crash."""
    try:
        chunks = sorted(f for f in os.listdir(directory)
                        if f.startswith("chunk_") and f.endswith(".parquet") and "_temp" not in f)
    except OSError:
        return 0.0
    best = 0.0
    for name in chunks[-3:]:
        try:
            v = pl.scan_parquet(os.path.join(directory, name)).select(pl.col("timestamp").max()).collect().item()
            if v is not None:
                best = max(best, float(v))
        except Exception as e:  # unreadable chunk: the compactor will report it
            log.warning("Could not read %s while checking the journal: %s", name, e)
    return best


def write_chunk(filepath: str, ts, data, keys):
    df_dict = {"timestamp": ts}
    for i, key in enumerate(keys):
        df_dict[key] = data[:, i]
    df = pl.DataFrame(df_dict)
    temp_path = filepath.replace(".parquet", "_temp.parquet")
    # High compression to heavily shrink the repeated run-length arrays
    df.write_parquet(temp_path, compression="zstd", compression_level=3)
    os.replace(temp_path, filepath)


def recover_journal(directory: str):
    """Turn rows left in the journal by a crash into a chunk file. Returns rows recovered."""
    path = os.path.join(directory, JOURNAL_NAME)
    if not os.path.exists(path):
        return 0
    got = CrashJournal.read(path)
    if got is None:
        log.error("Crash journal %s is unreadable - moved aside", path)
        os.replace(path, path + f".bad_{int(time.time())}")
        return 0
    keys, ts, vals = got
    if len(ts):
        already = latest_chunk_end(directory)
        keep = ts > already
        ts, vals = ts[keep], vals[keep]
    if len(ts):
        stamp = datetime.fromtimestamp(float(ts[-1])).strftime('%Y%m%d_%H%M%S')
        out = os.path.join(directory, f"chunk_{stamp}_recovered.parquet")
        n = 1
        while os.path.exists(out):
            out = os.path.join(directory, f"chunk_{stamp}_recovered{n}.parquet")
            n += 1
        write_chunk(out, ts, vals, keys)
        log.warning("Recovered %d row(s) (%.1f s) from the crash journal into %s",
                    len(ts), float(ts[-1] - ts[0]), os.path.basename(out))
    os.remove(path)
    return int(len(ts))


class TelemetryLoggerService:
    def __init__(self, log_directory: str = None):
        self.log_directory = log_directory or DEFAULT_LOG_DIRECTORY
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
        self.sub_socket.connect(ZMQ_PORT_ADAM_PUB)
        self.sub_socket.connect(ZMQ_PORT_STEERER_MAGNET_PUB)

        self.last_cmd_time = 0.0
        self.cmd_debounce = 2.0

        for topic in (TOPIC_PLC_DATA, TOPIC_VACUUM_DATA, TOPIC_SRC_TURBO_DATA, TOPIC_SPELLMAN_DATA,
                      TOPIC_MAGNET_DATA, TOPIC_STEERER_MAGNET_DATA, TOPIC_SMU_DATA, TOPIC_ADAM_DATA):
            self.sub_socket.setsockopt(zmq.SUBSCRIBE, topic)

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
        self.tick_rate = 0.050
        self.flush_interval = 900.0
        self.journal_interval = 5.0
        overhead = 1.1
        try:
            with open(LOGGER_CONFIG_PATH, "r") as f:
                cfg = json.load(f)
            self.tick_rate = cfg.get("tick_rate_seconds", 0.050)
            self.flush_interval = cfg.get("flush_interval_seconds", 900.0)
            self.journal_interval = cfg.get("journal_interval_seconds", 5.0)
            overhead = cfg.get("buffer_overhead_multiplier", 1.1)
        except (OSError, ValueError) as e:
            self.events.log_general(f"Logger config {LOGGER_CONFIG_PATH} not loaded ({e}). Using defaults.",
                                    severity="WARNING")

        # 5. High-Speed RAM Buffer
        self.max_rows = int((self.flush_interval / self.tick_rate) * overhead)
        self.ts_buffer = np.zeros(self.max_rows, dtype=np.float64)
        self.val_buffer = np.zeros((self.max_rows, self.num_keys), dtype=np.float32)
        self.ptr = 0
        self.journal_ptr = 0
        self.last_journal = time.time()

        # 6. Crash journal: first recover anything a previous crash left behind
        os.makedirs(self.log_directory, exist_ok=True)
        try:
            recovered = recover_journal(self.log_directory)
            if recovered:
                self.events.log_general(f"Recovered {recovered} logged rows from the crash journal",
                                        severity="WARNING")
        except Exception:
            log.exception("Crash-journal recovery failed")
        self.journal = CrashJournal(self.log_directory, self.keys)
        try:
            self.journal.start()
        except OSError as e:
            self.events.log_general(f"Cannot create crash journal ({e}) - a crash would lose unflushed data",
                                    severity="ERROR")
            self.journal = None

        # 7. Disk I/O Background Thread
        self._pending_chunks = []   # chunks whose write failed, retried before the next one
        self.write_queue = queue.Queue()
        self.running = True
        self.io_thread = threading.Thread(target=self._disk_writer_loop, name="logger-io", daemon=True)
        self.io_thread.start()

        self.next_tick = time.time() + self.tick_rate
        self.last_flush = time.time()
        self._throttle = Throttle(60.0)

        # --- PERFORMANCE METRICS ---
        self.tick_count = 0
        self.perf_max_loop_ms = 0.0
        self.perf_max_zmq_drain_ms = 0.0
        self.perf_max_tick_delta_ms = 0.0

    def stop(self):
        self.running = False

    def _update_state_cache(self, topic: bytes, payload_bytes: bytes):
        topic_str = topic.decode('utf-8', errors='ignore')
        try:
            data = orjson.loads(payload_bytes)
        except orjson.JSONDecodeError:
            if (n := self._throttle.ready(f"decode-{topic_str}")) is not None:
                log.warning("Undecodable payload on %s dropped%s", topic_str, Throttle.suffix(n))
            return
        if not isinstance(data, dict):
            return

        flat_dict = self.payload_mapper.parse(data, topic=topic_str)

        # 1. Update the state (Zero-Order Hold)
        self.current_state.update(flat_dict)

        # 2. Update Watchdog timer
        self.topic_last_seen[topic_str] = time.time()

        # 3. Learn which keys belong to this topic dynamically
        if topic_str not in self.topic_keys:
            self.topic_keys[topic_str] = set()
        self.topic_keys[topic_str].update(flat_dict.keys())

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
        row = np.array([self.current_state.get(k, np.nan) for k in self.keys], dtype=np.float32)

        self.ts_buffer[self.ptr] = current_ts
        self.val_buffer[self.ptr, :] = row
        self.ptr += 1

        if self.ptr >= self.max_rows:
            self.events.log_general("RAM buffer exceeded max bounds. Forcing emergency flush.", severity="WARNING")
            self._queue_flush()

    def _queue_journal(self):
        """Hand the rows logged since the last journal write to the I/O thread."""
        if self.journal is None or self.ptr <= self.journal_ptr:
            self.last_journal = time.time()
            return
        ts = self.ts_buffer[self.journal_ptr:self.ptr].copy()
        vals = self.val_buffer[self.journal_ptr:self.ptr, :].copy()
        self.journal_ptr = self.ptr
        self.last_journal = time.time()
        self.write_queue.put(("journal", ts, vals))

    def _queue_flush(self, wait_for_completion=False):
        if self.ptr == 0:
            return True  # Nothing to flush

        ts_data = self.ts_buffer[:self.ptr].copy()
        val_data = self.val_buffer[:self.ptr, :].copy()

        self.ptr = 0
        self.journal_ptr = 0
        self.last_flush = time.time()

        filename = f"chunk_{datetime.now().strftime('%Y%m%d_%H%M%S')}.parquet"
        filepath = os.path.join(self.log_directory, filename)

        if wait_for_completion:
            completion_event = threading.Event()
            self.write_queue.put(("chunk", filepath, ts_data, val_data, self.keys, completion_event))

            # Pause here until the background thread sets the event (Max 2 seconds)
            return completion_event.wait(timeout=2.0)
        else:
            # Standard fire-and-forget background flush
            self.write_queue.put(("chunk", filepath, ts_data, val_data, self.keys, None))
            return True

    def _disk_writer_loop(self):
        while self.running or not self.write_queue.empty():
            try:
                task = self.write_queue.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                if task[0] == "journal":
                    _, ts, vals = task
                    try:
                        self.journal.append(ts, vals)
                    except OSError as e:
                        if (n := self._throttle.ready("journal")) is not None:
                            self.events.log_general(f"Crash journal write failed: {e}{Throttle.suffix(n)}",
                                                    severity="ERROR")
                    continue

                _, filepath, ts, data, keys, completion_event = task
                try:
                    self._pending_chunks.append((filepath, ts, data, keys))
                    while self._pending_chunks:
                        item = self._pending_chunks[0]
                        try:
                            write_chunk(*item)
                        except Exception as io_err:  # disk full, permissions, polars errors...
                            log.exception("Failed to write %s", os.path.basename(item[0]))
                            self.events.log_general(
                                f"[Logger I/O Error] Failed to write {os.path.basename(item[0])}: {io_err} "
                                f"(kept in RAM and in the crash journal; retried at the next flush)",
                                severity="ERROR")
                            if len(self._pending_chunks) > 8:   # ~2 h of data: don't grow RAM without bound
                                dropped = self._pending_chunks.pop(0)
                                self.events.log_general(f"Dropped unwritable chunk {os.path.basename(dropped[0])}",
                                                        severity="CRITICAL")
                            break
                        self._pending_chunks.pop(0)
                    # Only when every row is safely in a chunk may the journal start empty again
                    if not self._pending_chunks and self.journal is not None:
                        try:
                            self.journal.start()
                        except OSError as e:
                            self.events.log_general(f"Cannot restart crash journal: {e}", severity="ERROR")
                finally:
                    # Wake up the main ZMQ thread immediately after the file is safe on disk
                    if completion_event:
                        completion_event.set()
            finally:
                self.write_queue.task_done()

    def _handle_ui_command(self):
        try:
            msg = self.cmd_socket.recv_string(flags=zmq.NOBLOCK)
        except zmq.Again:
            return
        except UnicodeDecodeError:
            self.cmd_socket.send_string("UNKNOWN_COMMAND")
            return
        try:
            if msg == "FORCE_FLUSH":
                now = time.time()
                if now - self.last_cmd_time > self.cmd_debounce:
                    self.events.log_general("Remote UI requested emergency disk flush.")
                    # Ask for a flush and wait for the background thread to confirm it
                    success = self._queue_flush(wait_for_completion=True)
                    self.last_cmd_time = now
                    self.cmd_socket.send_string("FLUSH_COMPLETE" if success else "FLUSH_TIMEOUT")
                else:
                    self.cmd_socket.send_string("FLUSH_DEBOUNCED")
            else:
                # Always reply to unknown commands to prevent state machine lockups
                self.cmd_socket.send_string("UNKNOWN_COMMAND")
        except zmq.ZMQError as e:
            log.error("UI command socket error: %s", e)

    def run(self):
        gc.disable()

        self.events.log_general(
            f"{1.0 / self.tick_rate}Hz Parquet Logger Online. Tracking {self.num_keys} channels "
            f"(chunk every {self.flush_interval:g} s, crash journal every {self.journal_interval:g} s).")

        current_time = time.perf_counter()
        self.next_tick = current_time + self.tick_rate
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
                        except zmq.Again:
                            break
                        except ValueError:
                            continue  # unexpected frame count
                        self._update_state_cache(topic, payload)

                zmq_duration_ms = (time.perf_counter() - zmq_start_drain) * 1000
                self.perf_max_zmq_drain_ms = max(self.perf_max_zmq_drain_ms, zmq_duration_ms)

                # If requested by external program (like viewer) flush data
                if self.cmd_socket in socks and socks[self.cmd_socket] == zmq.POLLIN:
                    self._handle_ui_command()

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
                if self.next_tick < time.perf_counter() - 1.0:
                    self.next_tick = time.perf_counter() + self.tick_rate   # don't burst after a stall
                self.tick_count += 1

                # 5. Crash journal (seconds) and chunk flush (15 minutes)
                now = time.time()
                if now - self.last_flush >= self.flush_interval:
                    self._queue_flush()
                    gc.collect()
                elif now - self.last_journal >= self.journal_interval:
                    self._queue_journal()

            except KeyboardInterrupt:
                break
            except Exception:
                if (n := self._throttle.ready("loop")) is not None:
                    log.exception("Main loop error%s", Throttle.suffix(n))

        self.running = False
        self.events.log_general("Shutdown signal received. Forcing final disk flush...")
        self._queue_flush()
        self.io_thread.join(timeout=10.0)
        if self.journal is not None:
            self.journal.close()
        self.events.log_general("Shutdown complete.")


if __name__ == "__main__":
    setup_logging("service_logger")
    harden_windows_process()
    if os.name == 'nt':
        import ctypes

        ctypes.windll.winmm.timeBeginPeriod(1)
    svc = TelemetryLoggerService()
    svc.run()
