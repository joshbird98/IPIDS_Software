"""Zoomed in, the viewer's lines turned to invisible dots when a second file held rows for the same
minutes (src/core/data_engine.py). Rows from the other file put a missing value between every two
readings of a channel, and the plot breaks its line at each one. Zoomed out the gaps were hidden by
the min/max thinning above 8000 rows, so the lines vanished only past a certain zoom.

On 27 Oct the viewer drew 10:22-10:23 on 26 Oct as 2813 separate dots per channel, 40 rows a second
where the logger writes 20."""
import logging
from datetime import datetime

import numpy as np
import polars as pl
import pytest

from src.core.data_engine import TimeSeriesEngine, bridge_short_gaps

X, Y = "ion_beam.source.turbo_pump.rb_current", "ion_beam.source.vacuum_gauge_1.rb_pressure"
START = datetime(2026, 10, 26, 10, 15).timestamp()
END = START + 900.0


@pytest.fixture
def root(tmp_path):
    (tmp_path / "daily_logs").mkdir()
    (tmp_path / "parquet_logs").mkdir()
    return tmp_path


def day_file(root, x=None):
    """The logger's rows at 20 Hz: X steps 0.7 -> 1.2 A at 10:22:47, Y a pressure."""
    ts = np.arange(START, END, 0.05)
    if x is None:
        x = np.where(ts < datetime(2026, 10, 26, 10, 22, 47).timestamp(), 0.7, 1.2)
    pl.DataFrame({"timestamp": ts, X: x, Y: np.full(len(ts), 1e-6)}).write_parquet(
        root / "daily_logs" / "day_20261026.parquet")
    return ts, x


def query(root, start=datetime(2026, 10, 26, 10, 21, 50).timestamp(), span=169.0):
    eng = TimeSeriesEngine(str(root))
    return eng.query_stateless(start, start + span, [X, Y])


def dots(v):
    """How many separate pieces the plot draws (connect='finite')."""
    fin = np.isfinite(v)
    return int((np.diff(np.r_[0, fin.astype(int)]) == 1).sum())


def test_rows_of_a_file_without_the_channels_in_between(root):
    ts, x = day_file(root)
    other = np.arange(START, END, 0.05) + 0.025          # e.g. a chunk from another logger
    pl.DataFrame({"timestamp": other, "ion_beam.other.thing": np.ones(len(other))}).write_parquet(
        root / "parquet_logs" / "chunk_20261026_103000.parquet")
    t, vals = query(root)
    assert len(t) == pytest.approx(169 * 40, abs=2)       # both files' rows are there...
    assert dots(vals[X]) == 1 and dots(vals[Y]) == 1      # ...and each channel is one line
    assert np.isfinite(vals[X][1:]).all()                 # (the first row is the other file's)
    # each row shows the logger's reading at or before it
    held = x[np.searchsorted(ts, t, side="right") - 1]
    assert np.allclose(vals[X][1:], held[1:])


def test_rows_of_a_logger_that_had_no_readings(root):
    day_file(root)
    other = np.arange(START, END, 0.05) + 0.02
    pl.DataFrame({"timestamp": other, X: np.full(len(other), np.nan), Y: np.full(len(other), np.nan)}) \
        .write_parquet(root / "parquet_logs" / "chunk_20261026_103000.parquet")
    _, vals = query(root)
    assert dots(vals[X]) == 1 and dots(vals[Y]) == 1


def test_real_gaps_stay_gaps(root):
    ts = np.arange(START, END, 0.05)
    x = np.full(len(ts), 0.7)
    gap = (ts >= START + 420.0) & (ts < START + 423.0)     # the logger's NaN for a source silent > 1 s
    x[gap] = np.nan
    x[np.searchsorted(ts, START + 440.0)] = np.nan         # one bad reading on its own: no gap
    day_file(root, x)
    t, vals = query(root, START + 400.0, 60.0)
    assert dots(vals[X]) == 2
    assert np.isnan(vals[X][(t >= START + 420.0) & (t < START + 423.0)]).all()


def test_the_same_rows_in_two_files_are_merged(root):
    """e.g. chunks the compactor couldn't delete after making the day file (Windows file locks)"""
    ts, _ = day_file(root)
    keep = ts < START + 600.0
    pl.DataFrame({"timestamp": ts[keep], Y: np.full(keep.sum(), 1e-6)}).write_parquet(
        root / "parquet_logs" / "chunk_20261026_102500.parquet")       # without X
    t, vals = query(root)
    assert len(t) == pytest.approx(169 * 20, abs=2)       # one row per time
    assert np.all(np.diff(t) > 0)
    assert np.isfinite(vals[X]).all() and np.isfinite(vals[Y]).all()


def test_overlapping_files_are_named_in_the_log_once(root, caplog):
    day_file(root)
    other = np.arange(START, END, 0.05) + 0.025
    pl.DataFrame({"timestamp": other, X: np.ones(len(other))}).write_parquet(
        root / "parquet_logs" / "chunk_20261026_103000.parquet")
    eng = TimeSeriesEngine(str(root))
    with caplog.at_level(logging.WARNING, logger="data_engine"):
        for _ in range(3):
            eng.query_stateless(START + 100.0, START + 200.0, [X])
    warned = [r.getMessage() for r in caplog.records if "more than one file" in r.getMessage()]
    assert len(warned) == 1
    assert "chunk_20261026_103000.parquet" in warned[0] and "day_20261026.parquet" in warned[0]


def test_files_that_follow_on_are_not_reported(root, caplog):
    ts, x = day_file(root)
    later = np.arange(END, END + 900.0, 0.05)
    pl.DataFrame({"timestamp": later, X: np.ones(len(later))}).write_parquet(
        root / "parquet_logs" / "chunk_20261026_104500.parquet")
    with caplog.at_level(logging.WARNING, logger="data_engine"):
        t, vals = query(root, END - 60.0, 120.0)
    assert not [r for r in caplog.records if "more than one file" in r.getMessage()]
    assert dots(vals[X]) == 1


def test_bridge_short_gaps():
    t = np.arange(0, 2.0, 0.05)
    v = np.arange(len(t), dtype=float)
    v[1::2] = np.nan                                       # every other row missing
    out = bridge_short_gaps(t, v)
    assert np.isfinite(out[:-1]).all() and np.isnan(out[-1])   # (nothing after the last: left alone)
    assert np.array_equal(out[1:-1:2], v[0:-2:2])          # held, not interpolated
    v = np.ones(len(t))
    v[:3] = np.nan                                         # nothing before: left alone
    v[10:20] = np.nan                                      # 0.55 s with nothing: a gap
    out = bridge_short_gaps(t, v)
    assert np.isnan(out[:3]).all() and np.isnan(out[10:20]).all()
    # 2 Hz readings among 20 Hz rows: bridged by their own spacing, but never across more than 1 s
    t = np.arange(0, 10.0, 0.05)
    v = np.full(len(t), np.nan)
    v[::10] = 1.0
    assert np.isfinite(bridge_short_gaps(t, v)[:-9]).all()
    v = np.full(len(t), np.nan)
    v[::40] = 1.0                                          # 2 s apart: gaps
    assert np.isnan(bridge_short_gaps(t, v)[1:40]).all()


def test_no_reading_shows_dashes():
    from src.gui.data_viewer.viewer_lanes import LanesMixin
    fmt = LanesMixin._format_with_si_prefix
    assert fmt(None, float("nan"), "A") == "---"           # was 'nan pA'
    assert fmt(None, float("nan"), "mB") == "---"
    assert fmt(None, 1.2, "A") == "1.20 A"
