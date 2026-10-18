"""An exact or averaged export with a log file that can't be read says so in its metadata
(it used to be left out with only a console line, so the export looked complete)."""
import time

import numpy as np
import polars as pl

from src.core import export_data as ex
from src.core.data_engine import TimeSeriesEngine


def test_unreadable_log_file_is_named_in_the_export(tmp_path):
    logs = tmp_path / "parquet_logs"
    logs.mkdir()
    t0 = time.mktime((2026, 7, 10, 12, 0, 0, 0, 0, -1))
    ts = t0 + np.arange(0, 600, 1.0)
    pl.DataFrame({"timestamp": ts, "ion_beam.x": np.ones_like(ts)}).write_parquet(
        logs / "chunk_20260710_120000.parquet")
    (logs / "chunk_20260710_121000.parquet").write_bytes(b"not a parquet file")
    engine = TimeSeriesEngine(str(tmp_path))
    df, info = ex.collect_exact(engine, None, t0, t0 + 1200, ["ion_beam.x"], flush=False)
    assert df.height == 600 and info["log_rows"] == 600
    assert [u.split(":")[0] for u in info["unreadable_files"]] == ["chunk_20260710_121000.parquet"]
    meta = ex.make_metadata(t0, t0 + 1200, "exact", [], info, [], str(tmp_path), tick_s=1.0)
    assert any(n.startswith("INCOMPLETE") and "chunk_20260710_121000" in n for n in meta["notes"])
    _, info = ex.collect_mean(engine, None, t0, t0 + 1200, ["ion_beam.x"], 60.0, flush=False)
    assert len(info["unreadable_files"]) == 1


def test_viewer_warns_about_an_incomplete_export(tmp_path):
    import json
    from src.gui.data_viewer.viewer_export import export_incomplete_note
    (tmp_path / "metadata.json").write_text(json.dumps({"notes": ["Exact mode", "INCOMPLETE: x.parquet: bad"]}))
    assert export_incomplete_note(str(tmp_path)) == "INCOMPLETE: x.parquet: bad"
    (tmp_path / "metadata.json").write_text(json.dumps({"notes": ["Exact mode"]}))
    assert export_incomplete_note(str(tmp_path)) == ""
    assert export_incomplete_note(str(tmp_path / "missing")) == ""
