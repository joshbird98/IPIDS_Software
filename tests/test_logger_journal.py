import math
import os
import signal
import subprocess
import sys
import time

import numpy as np
import orjson
import polars as pl
import pytest
import zmq

from src.services import service_logger as sl
from src.services.service_logger import JOURNAL_NAME, ChunkWriter, CrashJournal, recover_journal, write_chunk
from src.core.payload_mapper import DynamicPayloadMapper

KEYS = ["a.rb_x", "b.rb_y"]
GB = 1024 ** 3


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class Disk:
    """write_chunk into a directory that can be made to refuse (as when the disk is full)."""

    def __init__(self):
        self.full = True
        self.written = []

    def write(self, path, ts, data, keys):
        if self.full:
            raise OSError(28, "No space left on device")
        write_chunk(path, ts, data, keys)
        self.written.append(os.path.basename(path))


def of(events, severity):
    return [e["message"] for e in events.events if e["severity"] == severity]


def chunk(t0, seconds, rows=10):
    """A chunk's (ts, vals) spanning `seconds` of data from t0."""
    ts = t0 + np.linspace(0.0, seconds, rows)
    return ts, np.zeros((rows, len(KEYS)), dtype=np.float32)


def test_unwritable_chunks_wait_in_ram_and_are_retried_every_minute(tmp_path, fake_events):
    """A chunk the disk refuses waits in RAM and is tried every minute; the crash journal keeps
    every row meanwhile - also after a retry succeeds, as it then holds rows logged since the flush
    that are in no chunk yet - and starts afresh only after the next flush is written."""
    clock, disk = Clock(), Disk()
    journal = CrashJournal(str(tmp_path), KEYS)
    journal.start()
    ts, vals = chunk(1_700_000_000.0, 900.0)
    journal.append(ts, vals)                                        # journaled before the flush
    w = ChunkWriter(str(tmp_path), 900.0, fake_events, journal=journal, write=disk.write, clock=clock,
                    disk_free=lambda: 50 * GB)
    w.add((str(tmp_path / "chunk_1.parquet"), ts, vals, KEYS))
    [error] = of(fake_events, "ERROR")
    assert "No space left on device" in error and "15 min of data waiting" in error and "every 60 s" in error
    later_ts, later_vals = chunk(ts[-1] + 0.05, 120.0, rows=20)
    journal.append(later_ts, later_vals)                            # logged since the flush: in no chunk
    clock.t += sl.RETRY_S - 1
    assert not w.retry_due()
    clock.t += 1
    assert w.retry_due() and not w.write_waiting()
    assert len(of(fake_events, "ERROR")) == 1                         # not repeated every minute
    disk.full = False
    clock.t += sl.RETRY_S
    assert w.retry_due() and w.write_waiting()
    assert disk.written == ["chunk_1.parquet"] and not w.waiting and not w.retry_due()
    assert "writing to disk again" in fake_events.events[-1]["message"]
    assert CrashJournal.read(str(tmp_path / JOURNAL_NAME))[1].size == 30   # the later rows are still there
    w.add((str(tmp_path / "chunk_2.parquet"), later_ts, later_vals, KEYS))   # the next flush
    assert CrashJournal.read(str(tmp_path / JOURNAL_NAME))[1].size == 0    # now every row is in a chunk


def test_beyond_two_hours_of_waiting_data_the_oldest_is_dropped(tmp_path, fake_events):
    """The limit is the data's time span, not a number of chunks: a forced flush (viewer start,
    export) makes a chunk of a few seconds."""
    disk, t0 = Disk(), 1_700_000_000.0
    w = ChunkWriter(str(tmp_path), 900.0, fake_events, write=disk.write, clock=Clock(), disk_free=lambda: 50 * GB)
    for i in range(20):                                              # 20 forced flushes, 5 s each
        w.add((f"short_{i}.parquet", *chunk(t0 + 6 * i, 5.0), KEYS))
    assert len(w.waiting) == 20 and of(fake_events, "CRITICAL") == []
    w.waiting.clear()
    for i in range(9):                                               # 9 x 15 min
        w.add((f"chunk_{i}.parquet", *chunk(t0 + 900 * i, 899.95), KEYS))
    assert [p for p, *_ in w.waiting][0] == "chunk_1.parquet" and w.waiting_s() <= sl.PENDING_LIMIT_S
    [dropped] = of(fake_events, "CRITICAL")
    assert "chunk_0.parquet" in dropped and "2 h" in dropped


def test_low_disk_space_is_a_warning_before_it_fills(tmp_path, fake_events):
    clock, disk = Clock(), Disk()
    disk.full = False
    ts, vals = _rows(1_700_000_000.0, 10)
    w = ChunkWriter(str(tmp_path), 900.0, fake_events, write=disk.write, clock=clock, disk_free=lambda: 1.5 * GB)
    w.add((str(tmp_path / "chunk_1.parquet"), ts, vals, KEYS))
    clock.t += 900
    w.add((str(tmp_path / "chunk_2.parquet"), ts, vals, KEYS))
    [warning] = of(fake_events, "WARNING")                           # once an hour, not every chunk
    assert "1.5 GB free" in warning and disk.written == ["chunk_1.parquet", "chunk_2.parquet"]


def test_the_io_thread_retries_without_restarting_the_journal(fake_events):
    import queue
    import threading
    import types
    from src.core.log_setup import Throttle
    calls = []

    class Writer:
        def retry_due(self):
            return True

        def write_waiting(self, restart_journal=False):
            calls.append(restart_journal)
            rig.running = False
            return True
    rig = types.SimpleNamespace(running=True, write_queue=queue.Queue(), writer=Writer(), journal=None,
                                io_events=fake_events, _io_report=Throttle(900))
    t = threading.Thread(target=sl.TelemetryLoggerService._disk_writer_loop, args=(rig,))
    t.start()
    t.join(5)
    assert calls == [False]


def journal_with(directory, ts, vals):
    j = CrashJournal(str(directory), KEYS)
    j.start()
    j.append(ts, vals)
    j.close()


def test_a_journal_that_cant_be_recovered_is_kept_for_the_next_start(tmp_path, monkeypatch):
    """Recovery at start-up can fail on the same full disk: the journal is moved aside (a fresh one
    would overwrite it) and recovered at a later start - without dropping its rows as 'already in
    a chunk' because of chunks written after it."""
    ts, vals = _rows(1_700_000_000.0, 100)
    journal_with(tmp_path, ts, vals)
    real_write = sl.write_chunk
    monkeypatch.setattr(sl, "write_chunk", lambda *a: (_ for _ in ()).throw(OSError(28, "No space left")))
    rows, failed = sl.recover_journals(str(tmp_path))
    [(kept, error)] = failed
    assert rows == 0 and "No space left" in error
    assert kept.startswith(JOURNAL_NAME + ".unrecovered_") and not (tmp_path / JOURNAL_NAME).exists()
    monkeypatch.setattr(sl, "write_chunk", real_write)
    later_ts, later_vals = _rows(1_700_003_600.0, 50)                # the next run logged on meanwhile
    from datetime import datetime
    name = datetime.fromtimestamp(later_ts[-1]).strftime("chunk_%Y%m%d_%H%M%S.parquet")   # as the logger names it
    write_chunk(str(tmp_path / name), later_ts, later_vals, KEYS)
    assert recover_journal(str(tmp_path)) == 100
    assert not (tmp_path / kept).exists()


def test_one_journal_that_fails_doesnt_stop_the_others(tmp_path, monkeypatch):
    """Each journal is recovered on its own: a kept one that still can't be written, or one damaged
    beyond use, mustn't keep the current journal from being recovered (it was then moved aside at
    every start, and nothing was recovered any more)."""
    journal_with(tmp_path, *_rows(1_700_000_000.0, 40))
    os.replace(tmp_path / JOURNAL_NAME, tmp_path / (JOURNAL_NAME + ".unrecovered_20231114_221400"))
    damaged = tmp_path / (JOURNAL_NAME + ".unrecovered_20231114_221500")      # records misaligned
    journal_with(tmp_path, *_rows(1_700_000_100.0, 40))
    raw = (tmp_path / JOURNAL_NAME).read_bytes()
    header_end = len(CrashJournal.MAGIC) + 4 + int.from_bytes(raw[len(CrashJournal.MAGIC):len(CrashJournal.MAGIC) + 4],
                                                               "little")
    damaged.write_bytes(raw[:header_end] + b"\x07\x00\x00" + raw[header_end:])
    journal_with(tmp_path, *_rows(1_700_000_200.0, 30))              # the current journal
    real_write, calls = sl.write_chunk, []

    def write(path, *a):                                              # the first one still can't be written
        calls.append(path)
        if len(calls) == 1:
            raise OSError(28, "No space left")
        real_write(path, *a)
    monkeypatch.setattr(sl, "write_chunk", write)
    rows, failed = sl.recover_journals(str(tmp_path))
    assert rows == 30 and [name for name, _ in failed] == [JOURNAL_NAME + ".unrecovered_20231114_221400"]
    names = sorted(os.listdir(tmp_path))
    assert any(".unrecovered_20231114_221500.bad_" in n for n in names)          # set aside, not retried
    rows, failed = sl.recover_journals(str(tmp_path))                 # the next start
    assert rows == 40 and failed == []


def test_a_write_the_disk_cuts_short_leaves_the_journal_aligned(tmp_path):
    j = CrashJournal(str(tmp_path), KEYS)
    j.start()
    ts, vals = _rows(1_700_000_000.0, 30)
    j.append(ts[:10], vals[:10])
    real = j._f

    class Short:                                   # the disk fills part-way through the next write
        def write(self, data):
            return real.write(data[:len(data) // 2 + 3])

        def __getattr__(self, name):
            return getattr(real, name)
    j._f = Short()
    with pytest.raises(OSError):
        j.append(ts[10:20], vals[10:20])
    j._f = real
    j.append(ts[20:], vals[20:])                   # space freed again
    j.close()
    keys, rts, rvals = CrashJournal.read(str(tmp_path / JOURNAL_NAME))
    np.testing.assert_array_equal(rts, np.concatenate([ts[:10], ts[20:]]))
    np.testing.assert_array_equal(rvals, np.concatenate([vals[:10], vals[20:]]))


def _rows(t0, n):
    ts = t0 + np.arange(n) * 0.05
    vals = np.column_stack([np.arange(n, dtype=np.float32), np.full(n, 7.0, dtype=np.float32)])
    return ts, vals


def test_journal_round_trip_and_torn_record(tmp_path):
    j = CrashJournal(str(tmp_path), KEYS)
    j.start()
    ts, vals = _rows(1_700_000_000.0, 100)
    j.append(ts[:60], vals[:60])
    j.append(ts[60:], vals[60:])
    j.close()
    with open(tmp_path / JOURNAL_NAME, "ab") as f:
        f.write(b"\x01\x02\x03")             # crash in the middle of a record
    keys, rts, rvals = CrashJournal.read(str(tmp_path / JOURNAL_NAME))
    assert keys == KEYS
    np.testing.assert_array_equal(rts, ts)
    np.testing.assert_array_equal(rvals, vals)


def test_recovery_writes_chunk_and_removes_journal(tmp_path):
    j = CrashJournal(str(tmp_path), KEYS)
    j.start()
    ts, vals = _rows(1_700_000_000.0, 200)
    j.append(ts, vals)
    j.close()
    assert recover_journal(str(tmp_path)) == 200
    assert not (tmp_path / JOURNAL_NAME).exists()
    files = [f for f in os.listdir(tmp_path) if f.endswith("_recovered.parquet")]
    assert len(files) == 1 and files[0].startswith("chunk_")
    df = pl.read_parquet(tmp_path / files[0])
    assert df.height == 200 and df.columns == ["timestamp"] + KEYS


def test_rows_already_in_a_chunk_are_not_duplicated(tmp_path):
    ts, vals = _rows(1_700_000_000.0, 300)
    # crash between "chunk written" and "journal restarted"
    write_chunk(str(tmp_path / "chunk_20231114_221500.parquet"), ts[:200], vals[:200], KEYS)
    j = CrashJournal(str(tmp_path), KEYS)
    j.start()
    j.append(ts, vals)
    j.close()
    assert recover_journal(str(tmp_path)) == 100


def test_empty_journal_is_just_removed(tmp_path):
    CrashJournal(str(tmp_path), KEYS).start()
    assert recover_journal(str(tmp_path)) == 0
    assert os.listdir(tmp_path) == []


def test_mapper_turns_null_into_nan():
    m = DynamicPayloadMapper()
    m.valid_keys = {"x.rb_v", "x.stat_en"}
    out = m.parse({"x.rb_v": None, "x.stat_en": True, "other": 1})
    assert math.isnan(out["x.rb_v"]) and out["x.stat_en"] == 1.0 and "other" not in out


@pytest.mark.slow
def test_logger_killed_mid_chunk_loses_at_most_seconds(tmp_path):
    """Run the real logger process, feed it telemetry, kill -9 it, restart it: the rows
    since the last chunk come back from the journal (previously up to 15 min were lost)."""
    from src.core.network_map import ZMQ_PORT_PLC_PUB, TOPIC_PLC_DATA
    env = dict(os.environ, SINDIE_DATA_DIR=str(tmp_path), PYTHONPATH=os.getcwd())
    script = os.path.join("src", "services", "service_logger.py")
    tag = "ion_beam.source.extraction.rb_voltage"

    pub = zmq.Context.instance().socket(zmq.PUB)
    pub.bind(ZMQ_PORT_PLC_PUB)
    proc = subprocess.Popen([sys.executable, script], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        t_start = time.time()
        while time.time() - t_start < 8.5:          # > one 5 s journal interval
            pub.send_multipart([TOPIC_PLC_DATA, orjson.dumps({tag: 12.0, "timestamp": time.time()})])
            time.sleep(0.05)
        t_kill = time.time()
        if os.name == "nt":
            proc.kill()
        else:
            proc.send_signal(signal.SIGKILL)
        proc.wait(10)
    finally:
        if proc.poll() is None:
            proc.kill()
        pub.close(0)

    parquet_dir = tmp_path / "parquet_logs"
    assert (parquet_dir / JOURNAL_NAME).exists()
    assert recover_journal(str(parquet_dir)) > 0
    chunks = sorted(parquet_dir.glob("chunk_*_recovered.parquet"))
    df = pl.read_parquet(chunks[0])
    seen = df.filter(pl.col(tag) == 12.0)
    assert seen.height > 50
    lost = t_kill - df["timestamp"].max()
    assert lost < 6.0, f"lost {lost:.1f} s of data"
