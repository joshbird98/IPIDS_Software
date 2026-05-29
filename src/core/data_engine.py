import os
import glob
import numpy as np
import polars as pl
import json
import warnings


class TimeSeriesEngine:
    def __init__(self, log_dir):
        self.log_dir = log_dir
        self.channel_keys = self._load_master_schema()
        self._schema_cache = {}

    def _load_master_schema(self) -> list:
        registry_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../config/system_tags.json'))
        try:
            with open(registry_path, "r") as f:
                return sorted(list(json.load(f).keys()))
        except FileNotFoundError:
            return []

    def _inject_nan_gaps(self, ts_array, vals_dict, gap_threshold):
        """Injects NaN rows where time jumps, forcing PyQtGraph to break the line visually."""
        if len(ts_array) < 2: return ts_array, vals_dict

        diffs = np.diff(ts_array)
        gap_indices = np.where(diffs > gap_threshold)[0]

        if len(gap_indices) > 0:
            insert_idx = gap_indices + 1
            ts_array = np.insert(ts_array, insert_idx, ts_array[gap_indices] + 0.001)
            for col in vals_dict:
                vals_dict[col] = np.insert(vals_dict[col], insert_idx, np.nan)
        return ts_array, vals_dict

    # 4. Helper: Efficient Schema-Aware Scanning
    def get_safe_scan(self, filepath, cols):
        if filepath not in self._schema_cache:
            # Safely get schema
            try:
                self._schema_cache[filepath] = pl.scan_parquet(filepath).collect_schema().names()
            except AttributeError:
                # Fallback for older Polars versions
                self._schema_cache[filepath] = pl.scan_parquet(filepath).schema.names()

        # Determine which requested columns actually exist in this file
        schema = self._schema_cache[filepath]
        valid_cols = [c for c in cols if c in schema]

        # Use 'columns=' keyword argument instead of 'include_columns'
        return pl.scan_parquet(filepath).select(valid_cols)

    def query_stateless(self, start_ts: float, end_ts: float, selected_tags: list):
        """Pure stateless router with schema caching and projection pushdown."""
        import time
        #t_start = time.perf_counter()
        span = end_ts - start_ts
        print(f"\n[Engine] Query Started! Request Span: {start_ts:.1f} to {end_ts:.1f}")

        # 1. Determine configuration based on zoom span
        import datetime
        today_midnight = datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

        # Determine if we need Macro files or Raw files
        # We only use macros if the ENTIRE window is in the past (before today)
        is_macro = (span > 14400) and (end_ts < today_midnight)

        if is_macro:
            # Use 20m for very long spans, 1m for shorter historical spans
            file_suffixes = ["*_macro_20m.parquet"] if span > 86400 * 10 else ["*_macro_1m.parquet"]
            gap_threshold = 1200.0 * 2.0 if span > 86400 * 10 else 60.0 * 2.0
        else:
            # If we are touching "Today", we MUST use raw data
            file_suffixes = ["*_raw.parquet", "day_*.parquet", "chunk_*.parquet"]
            gap_threshold = 15.0

        # 2. Determine Required Columns (Projection Pushdown)
        if is_macro:
            required_cols = ["timestamp_min", "timestamp_max"]
            for t in selected_tags:
                required_cols.extend([f"{t}_min", f"{t}_max"])
        else:
            required_cols = ["timestamp"] + selected_tags

        # 3. File Discovery
        available_files = []
        for suf in file_suffixes:
            available_files.extend(glob.glob(os.path.join(self.log_dir, "parquet_logs", suf)))
            available_files.extend(glob.glob(os.path.join(self.log_dir, "daily_logs", suf)))
        available_files = list(set(available_files))
        if not is_macro:
            available_files = [f for f in available_files if "_macro" not in os.path.basename(f)]

        print(f"[Engine] Found {len(available_files)} files using suffixes {file_suffixes}")
        if available_files:
            print(f"[Engine] Sample file path: {available_files[0]}")

        if not available_files:
            print("[Engine] EXIT: No files found! Check log_dir and suffixes.")
            return np.array([]), {}

        # 5. Execute Query
        try:
            if is_macro:
                lfs = []
                for f in available_files:
                    # Add a safe scan for each file
                    lf = self.get_safe_scan(f, required_cols).filter(
                        (pl.col("timestamp_max") >= start_ts) & (pl.col("timestamp_min") <= end_ts)
                    )
                    lfs.append(lf)

                # Concat all found macro files
                df = pl.concat(lfs, how="diagonal").collect().sort("timestamp_min")

                if df.is_empty(): return np.array([]), {}

                n_bins = len(df)
                ts_array = np.empty(n_bins * 2, dtype=np.float64)
                ts_array[0::2] = df["timestamp_min"].to_numpy()
                ts_array[1::2] = df["timestamp_max"].to_numpy()

                vals_dict = {}
                for col in selected_tags:
                    min_c, max_c = f"{col}_min", f"{col}_max"
                    if min_c in df.columns and max_c in df.columns:
                        arr = np.empty(n_bins * 2, dtype=np.float64)
                        arr[0::2] = df[min_c].cast(pl.Float64, strict=False).to_numpy()
                        arr[1::2] = df[max_c].cast(pl.Float64, strict=False).to_numpy()
                        vals_dict[col] = arr

                return self._inject_nan_gaps(ts_array, vals_dict, gap_threshold)

            else:
                lfs = [
                    self.get_safe_scan(f, required_cols).filter(
                        (pl.col("timestamp") >= start_ts) & (pl.col("timestamp") <= end_ts)
                    ) for f in available_files
                ]
                print(f"[Engine] Executing Polars concat & scan on {len(lfs)} files...")
                df = pl.concat(lfs, how="diagonal").collect().sort("timestamp")
                print(f"[Engine] Scan Complete. Rows returned: {len(df)}")

                if df.is_empty():# --- CRITICAL DEBUG INJECT ---
                    sample_df = pl.read_parquet(available_files[0]).head(1)
                    if "timestamp" in sample_df.columns:
                        disk_val = sample_df["timestamp"][0]
                        print(f"[Engine] ERROR: df is empty after filter! ")
                        print(f"[Engine] -> Disk 'timestamp' sample: {disk_val} (Type: {type(disk_val)})")
                        print(f"[Engine] -> UI request bounds: {start_ts} to {end_ts} (Type: {type(start_ts)})")
                    else:
                        print(f"[Engine] ERROR: 'timestamp' column not found in Parquet! Columns are: {sample_df.columns}")
                    return np.array([]), {}

                ts_array = df["timestamp"].cast(pl.Float64, strict=False).to_numpy()
                vals_dict = {col: df[col].cast(pl.Float64, strict=False).to_numpy()
                             for col in selected_tags if col in df.columns}

                return self._inject_nan_gaps(ts_array, vals_dict, gap_threshold)

        except Exception as e:
            print(f"[Engine] Stateless Query Exception: {e}")
            return np.array([]), {}

    @staticmethod
    def downsample_minmax(times, values, target_points=2000):
        n_points = len(values)
        if n_points <= target_points:
            return times, values

        # 1. Place the global override at the ABSOLUTE top of the execution block
        warnings.filterwarnings("ignore", category=RuntimeWarning)

        try:
            chunk_size = max(1, n_points // (target_points // 2))
            n_chunks = n_points // chunk_size
            n_usable = n_chunks * chunk_size

            data_view = values[:n_usable].reshape(n_chunks, chunk_size)
            time_view = times[:n_usable].reshape(n_chunks, chunk_size)

            all_nan = np.isnan(data_view).all(axis=1)

            with np.errstate(invalid='ignore'):
                mins = np.nanmin(data_view, axis=1)
                maxs = np.nanmax(data_view, axis=1)

            mins[all_nan] = np.nan
            maxs[all_nan] = np.nan

            final_values = np.empty(n_chunks * 2, dtype=values.dtype)
            final_values[0::2] = mins
            final_values[1::2] = maxs

            final_times = np.empty(n_chunks * 2, dtype=times.dtype)
            final_times[0::2] = time_view[:, 0]
            final_times[1::2] = time_view[:, -1]

            return final_times, final_values

        finally:
            # 2. Guarantee reset at the absolute exit point of the function
            warnings.filterwarnings("default", category=RuntimeWarning)