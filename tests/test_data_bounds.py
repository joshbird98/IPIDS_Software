"""The logged data's time span, from the logger's file names (src/core/data_engine.py)."""
from datetime import datetime


def test_bounds_come_from_day_and_chunk_file_names(tmp_path):
    from src.core.data_engine import TimeSeriesEngine
    (tmp_path / "daily_logs").mkdir()
    (tmp_path / "parquet_logs").mkdir()
    for name in ("day_20260519.parquet", "day_20260519_macro_1m.parquet", "day_20260601.parquet"):
        (tmp_path / "daily_logs" / name).write_bytes(b"")
    for name in ("chunk_20260724_001151.parquet", "chunk_20260724_002651.parquet", "chunk_20260724_004151_temp.parquet"):
        (tmp_path / "parquet_logs" / name).write_bytes(b"")
    engine = TimeSeriesEngine(str(tmp_path))
    lo, hi = engine.data_bounds()
    assert lo == datetime(2026, 5, 19).timestamp()
    assert hi == datetime(2026, 7, 24, 0, 26, 51).timestamp() + 15 * 60     # the last complete chunk
    assert TimeSeriesEngine(str(tmp_path / "nothing")).data_bounds() is None
