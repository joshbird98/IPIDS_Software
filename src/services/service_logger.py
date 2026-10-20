import gc
import json
import logging
import os
import queue
import shutil
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
UNRECOVERED = JOURNAL_NAME + ".unrecovered_"     # a journal kept because it couldn't be recovered at start-up
PLAUSIBLE_SINCE = 946684800.0                     # 2000-01-01: journal rows stamped earlier are damage, not data

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
        self._f = open(self.path, "ab", buffering=0)

    def append(self, ts: np.ndarray, vals: np.ndarray):
        """Append records; on a failed or partial write (disk full) the file is cut back to its last
        whole record and OSError raised - a part-written record would misalign every later one."""
        if self._f is None or len(ts) == 0:
            return
        rec = np.empty(len(ts), dtype=self.dtype)
        rec["ts"] = ts
        rec["v"] = vals
        data = rec.tobytes()
        size = os.fstat(self._f.fileno()).st_size
        try:
            written = self._f.write(data)
            if written != len(data):
                raise OSError(f"journal write cut short ({written} of {len(data)} bytes) - disk full?")
            os.fsync(self._f.fileno())
        except OSError:
            try:
                self._f.truncate(size)
            except OSError as e:          # can't repair it: stop journaling rather than write misaligned
                log.error("Crash journal could not be cut back after a failed write (%s) - journaling stopped", e)
                self.close()
            raise

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


def latest_chunk_end(directory: str, before: float = None) -> float:
    """Largest timestamp in the most recent chunk file (0 if none) - used to drop journal
    rows that already made it into a chunk before a crash. before: only chunks written up to
    then (by the time in their name), for a journal kept from an earlier run."""
    try:
        chunks = sorted(f for f in os.listdir(directory)
                        if f.startswith("chunk_") and f.endswith(".parquet") and "_temp" not in f)
    except OSError:
        return 0.0
    if before is not None:
        newest = datetime.fromtimestamp(before).strftime("chunk_%Y%m%d_%H%M%S")
        chunks = [c for c in chunks if c[:len(newest)] <= newest]
    best = 0.0
    for name in chunks[-3:]:
        try:
            v = pl.scan_parquet(os.path.join(directory, name)).select(pl.col("timestamp").max()).collect().item()
            if v is not None:
                best = max(best, float(v))
        except (OSError, pl.exceptions.PolarsError) as e:  # unreadable chunk: the compactor will report it
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


def recover_journals(directory: str) -> tuple:
    """Turn rows left in the journal by a crash - and in journals kept from earlier runs whose
    recovery failed - into chunk files, each on its own: one that fails (its chunk can't be
    written) stays for the next start - the current one moved aside, or a fresh journal would
    overwrite it - and the others are still recovered. Returns (rows recovered, [(file, error)])."""
    try:
        kept = sorted(f for f in os.listdir(directory) if f.startswith(UNRECOVERED) and ".bad_" not in f)
    except OSError:
        kept = []
    rows, failed = 0, []
    for name in kept + [JOURNAL_NAME]:
        try:
            rows += _recover_journal_file(directory, os.path.join(directory, name), name != JOURNAL_NAME)
        except Exception as e:   # disk full, polars errors...: this one waits; the rest go ahead
            log.warning("Crash journal %s not recovered: %s", name, e, exc_info=True)
            if name == JOURNAL_NAME:
                try:
                    name = keep_unrecovered_journal(directory)
                except OSError as e2:
                    e = f"{e} - and it couldn't be kept ({e2}), so its rows are lost"
            failed.append((name, str(e)))
    return rows, failed


def recover_journal(directory: str) -> int:
    """recover_journals, raising if any journal couldn't be recovered (tools and tests)."""
    rows, failed = recover_journals(directory)
    if failed:
        raise OSError("; ".join(f"{name}: {error}" for name, error in failed))
    return rows


def keep_unrecovered_journal(directory: str) -> str:
    """Move the journal aside (a fresh one would overwrite it) for recover_journal to try again
    at the next start. Returns the new name ('' if there was none)."""
    path = os.path.join(directory, JOURNAL_NAME)
    if not os.path.exists(path):
        return ""
    name = f"{UNRECOVERED}{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    os.replace(path, os.path.join(directory, name))
    return name


def _recover_journal_file(directory: str, path: str, kept: bool) -> int:
    if not os.path.exists(path):
        return 0
    got = CrashJournal.read(path)
    if got is None:
        log.error("Crash journal %s is unreadable - moved aside", path)
        os.replace(path, path + f".bad_{int(time.time())}")
        return 0
    keys, ts, vals = got
    if len(ts):
        plausible = np.isfinite(ts) & (ts > PLAUSIBLE_SINCE) & (ts < time.time() + 86400.0)
        if not plausible.any():     # e.g. records misaligned by a write the disk cut short
            log.error("Crash journal %s holds no plausible timestamps - moved aside", path)
            os.replace(path, path + f".bad_{int(time.time())}")
            return 0
        ts, vals = ts[plausible], vals[plausible]
        already = latest_chunk_end(directory, before=float(ts[-1]) if kept else None)
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


PENDING_LIMIT_S = 2 * 3600.0       # unwritten data held in RAM while the disk won't take it, then the oldest goes
RETRY_S = 60.0                     # meanwhile, try again this often
LOW_DISK_BYTES = 2 * 1024 ** 3     # warn when the data drive has less free space than this
IO_REPORT_S = 900.0                # repeat a write-failure event at most this often


class ChunkWriter:
    """The logger's chunk writing, on its I/O thread.

    A chunk the disk won't take (full, permissions, ...) stays in RAM and is tried again every
    RETRY_S, oldest first, so it is saved within a minute of the disk taking data again. Beyond
    PENDING_LIMIT_S of waiting data (by its timestamps - a forced flush makes a short chunk) the
    oldest is dropped (CRITICAL event), so RAM can't grow without bound: 15 minutes of data is
    channels x 18000 rows x 4 bytes, ~25 MB at 340 channels.

    The crash journal starts afresh only after a new chunk - and every one still waiting - is
    written: only then is every row it holds in a chunk. (A retry between flushes must not: the
    journal then also holds rows logged since the last flush, which are in no chunk yet.) A data
    drive running low on space - the likely cause - is a WARNING before it fills."""

    def __init__(self, directory: str, flush_interval: float, events, journal=None, write=None,
                 clock=time.time, disk_free=None):
        self.directory = directory
        self.flush_interval = float(flush_interval)
        self.events = events
        self.journal = journal
        self.waiting = []           # (filepath, ts, data, keys), oldest first
        self._write = write or write_chunk
        self._clock = clock
        self._disk_free = disk_free or (lambda: shutil.disk_usage(self.directory).free)
        self._last_try = 0.0
        self._failing = False
        self._report = Throttle(IO_REPORT_S, clock=clock)
        self._space_report = Throttle(3600.0, clock=clock)

    def add(self, item):
        """A new chunk (just flushed): written now, after any still waiting."""
        self.waiting.append(item)
        self.write_waiting(restart_journal=True)

    def retry_due(self) -> bool:
        return bool(self.waiting) and self._clock() - self._last_try >= RETRY_S

    def waiting_s(self) -> float:
        """Seconds of data waiting: the oldest chunk's first row to the newest one's last."""
        if not self.waiting or not len(self.waiting[0][1]) or not len(self.waiting[-1][1]):
            return 0.0
        return float(self.waiting[-1][1][-1] - self.waiting[0][1][0])

    def write_waiting(self, restart_journal: bool = False) -> bool:
        """Write the waiting chunks, oldest first; True when none is left. restart_journal: only
        for a just-flushed chunk (see the class docstring)."""
        self._last_try = self._clock()
        written = 0
        while self.waiting:
            item = self.waiting[0]
            try:
                self._write(*item)
            except Exception as e:   # disk full, permissions, polars errors...: kept for the next try
                log.warning("Failed to write %s: %s", os.path.basename(item[0]), e, exc_info=True)
                self._failed(item, e)
                return False
            self.waiting.pop(0)
            written += 1
        if self._failing:
            self._failing = False
            self._report.reset("write")
            self.events.log_general(f"Logger is writing to disk again: {written} waiting chunk(s) saved")
        if restart_journal and self.journal is not None:
            try:
                self.journal.start()
            except OSError as e:
                self.events.log_general(f"Cannot restart crash journal: {e}", severity="ERROR")
        self._check_space()
        return True

    def _free_gb(self):
        try:
            return self._disk_free() / 1024 ** 3
        except OSError:
            return None

    def _failed(self, item, error):
        self._failing = True
        while len(self.waiting) > 1 and self.waiting_s() > PENDING_LIMIT_S:
            dropped = self.waiting.pop(0)
            self.events.log_general(f"Dropped unwritable chunk {os.path.basename(dropped[0])}: the disk hasn't taken "
                                    f"data for over {PENDING_LIMIT_S / 3600:g} h", severity="CRITICAL")
        if (n := self._report.ready("write")) is not None:
            free = self._free_gb()
            self.events.log_general(
                f"[Logger I/O Error] Cannot write {os.path.basename(item[0])}: {error}"
                + (f" ({free:.1f} GB free on the data drive)" if free is not None else "")
                + f" - {self.waiting_s() / 60:.0f} min of data waiting in RAM, tried again every {RETRY_S:g} s; "
                f"beyond {PENDING_LIMIT_S / 3600:g} h of it the oldest is dropped{Throttle.suffix(n)}", severity="ERROR")

    def _check_space(self):
        free = self._free_gb()
        if free is not None and free * 1024 ** 3 < LOW_DISK_BYTES \
                and (n := self._space_report.ready("space")) is not None:
            self.events.log_general(f"The data drive is low on space: {free:.1f} GB free for {self.directory} - "
                                    f"the logger can't save data once it is full{Throttle.suffix(n)}",
                                    severity="WARNING")


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
            recovered, failed = recover_journals(self.log_directory)
            if recovered:
                self.events.log_general(f"Recovered {recovered} logged rows from the crash journal",
                                        severity="WARNING")
            for name, error in failed:      # kept (moved aside, so the new journal doesn't overwrite it)
                self.events.log_general(f"Crash journal {name} could not be turned into a chunk ({error}) - "
                                        "tried again when the logger next starts", severity="ERROR")
        except Exception:   # recovering old rows must never stop new ones being logged
            log.exception("Crash-journal recovery failed")
        self.journal = CrashJournal(self.log_directory, self.keys)
        try:
            self.journal.start()
        except OSError as e:
            self.events.log_general(f"Cannot create crash journal ({e}) - a crash would lose unflushed data",
                                    severity="ERROR")
            self.journal = None

        # 7. Disk I/O Background Thread - with its own event socket (ZeroMQ sockets aren't thread-safe)
        self.io_events = EventHelper("service_logger")
        self.writer = ChunkWriter(self.log_directory, self.flush_interval, self.io_events, journal=self.journal)
        self._io_report = Throttle(IO_REPORT_S)
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

    def _heartbeat(self):
        self.hb_socket.send_json({"service": "service_logger", "ts": time.time()})
        self.last_hb_time = time.perf_counter()

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

            # Wait for the background thread (max 2 s), heartbeating meanwhile: the manager calls
            # the logger hung after 2 s without one, and a slow disk mustn't get it killed
            deadline = time.monotonic() + 2.0
            while not completion_event.wait(timeout=0.25):
                self._heartbeat()
                if time.monotonic() >= deadline:
                    return False
            return True
        else:
            # Standard fire-and-forget background flush
            self.write_queue.put(("chunk", filepath, ts_data, val_data, self.keys, None))
            return True

    def _disk_writer_loop(self):
        while self.running or not self.write_queue.empty():
            try:
                task = self.write_queue.get(timeout=1.0)
            except queue.Empty:
                if self.writer.retry_due():      # chunks the disk refused: tried again every minute
                    self.writer.write_waiting()
                continue
            try:
                if task[0] == "journal":
                    _, ts, vals = task
                    try:
                        self.journal.append(ts, vals)
                    except OSError as e:
                        if (n := self._io_report.ready("journal")) is not None:
                            self.io_events.log_general(f"Crash journal write failed: {e}{Throttle.suffix(n)}",
                                                       severity="ERROR")
                    continue

                _, filepath, ts, data, keys, completion_event = task
                try:
                    self.writer.add((filepath, ts, data, keys))
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
                    self._heartbeat()

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

                # 3. Sleep to the tick (no busy-wait for the last 2 ms any more: the ticks are fixed
                # times 50 ms apart, so a sleep ending ~1 ms late never adds up; the rows carry
                # their own timestamps)
                sleep_time = self.next_tick - time.perf_counter()
                if sleep_time > 0:
                    time.sleep(sleep_time)

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
            except Exception:   # the logger keeps logging; the cause goes to the log (rate-limited)
                if (n := self._throttle.ready("loop")) is not None:
                    log.exception("Main loop error%s", Throttle.suffix(n))

        self.running = False
        self.events.log_general("Shutdown signal received. Forcing final disk flush...")
        self._queue_flush()
        self.io_thread.join(timeout=10.0)
        if self.writer.waiting:
            self.events.log_general(f"Shutting down with {self.writer.waiting_s() / 60:.0f} min of data the disk "
                                    "wouldn't take: only what the crash journal holds is recovered at the next "
                                    "start", severity="CRITICAL")
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
