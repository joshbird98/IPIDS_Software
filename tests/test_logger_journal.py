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

from src.services.service_logger import JOURNAL_NAME, CrashJournal, recover_journal, write_chunk
from src.core.payload_mapper import DynamicPayloadMapper

KEYS = ["a.rb_x", "b.rb_y"]


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
    env = dict(os.environ, IPIDS_DATA_DIR=str(tmp_path), PYTHONPATH=os.getcwd())
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
