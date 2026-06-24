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
        self.mart_1m = None
        self.mart_20m = None

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

    def populate_macro_marts(self):
        """Silently loads macro rollups into RAM for instantaneous slicing."""
        print("[Engine] Hoisting In-Memory Data Marts...")
        files_1m = glob.glob(os.path.join(self.log_dir, "**/*macro_1m.parquet"), recursive=True)
        files_20m = glob.glob(os.path.join(self.log_dir, "**/*macro_20m.parquet"), recursive=True)

        try:
            # 1. Load entire history of 20m summaries
            if files_20m:
                self.mart_20m = pl.scan_parquet(files_20m).collect()
                print(f"[Engine] 20m Mart online. Rows: {self.mart_20m.height}")

            # 2. Load rolling 30-day window of 1m summaries
            if files_1m:
                cutoff_ts = time.time() - (30 * 86400)
                self.mart_1m = pl.scan_parquet(files_1m) \
                    .filter(pl.col("timestamp_min") >= cutoff_ts) \
                    .collect()
                print(f"[Engine] 1m Mart online (30-day window). Rows: {self.mart_1m.height}")
        except Exception as e:
            print(f"[Engine] Data Mart population failed: {e}")
            # Request for manual check: If this fails, double-check that the compactor
            # is correctly writing the files to the directories matched by the glob patterns.

    def query_stateless(self, start_ts: float, end_ts: float, selected_tags: list):
        if not selected_tags: return np.array([]), {}

        span = end_ts - start_ts
        target_df = None
        is_macro = False

        # --- 1. RESOLUTION ROUTING ---
        if span > (30 * 86400) and self.mart_20m is not None:
            # > 30 Days: Route to 20m RAM Mart
            is_macro = True
            target_df = self.mart_20m.filter(
                (pl.col("timestamp_min") >= start_ts) & (pl.col("timestamp_min") <= end_ts))

        elif span > 7200 and self.mart_1m is not None:
            # 2 Hours to 30 Days: Route to 1m RAM Mart
            is_macro = True
            target_df = self.mart_1m.filter((pl.col("timestamp_min") >= start_ts) & (pl.col("timestamp_min") <= end_ts))

        # --- 2. MACRO EXTRACTION (RAM) ---
        if is_macro and target_df is not None:
            if target_df.height == 0: return np.array([]), {}

            # Interleave Min/Max for the Y-Axis Envelope
            ts_min = target_df["timestamp_min"].to_numpy()
            ts_max = target_df["timestamp_max"].to_numpy()
            ts_arr = np.empty((ts_min.size + ts_max.size,), dtype=np.float64)
            ts_arr[0::2] = ts_min
            ts_arr[1::2] = ts_max

            vals_dict = {}
            for tag in selected_tags:
                min_col, max_col = f"{tag}_min", f"{tag}_max"
                if min_col in target_df.columns and max_col in target_df.columns:
                    y_min = target_df[min_col].to_numpy()
                    y_max = target_df[max_col].to_numpy()
                    y_arr = np.empty((y_min.size + y_max.size,), dtype=np.float64)
                    y_arr[0::2] = y_min
                    y_arr[1::2] = y_max
                    vals_dict[tag] = y_arr
            return ts_arr, vals_dict

        # --- 3. RAW EXTRACTION (Disk) ---
        # < 2 Hours: Route to raw Parquet files on SSD
        chunks = glob.glob(os.path.join(self.log_dir, "**/chunk_*.parquet"), recursive=True)
        days = glob.glob(os.path.join(self.log_dir, "**/day_*.parquet"), recursive=True)
        raws = glob.glob(os.path.join(self.log_dir, "**/*_raw.parquet"), recursive=True)

        target_files = chunks + raws + [f for f in days if "macro" not in f]
        if not target_files: return np.array([]), {}

        fetch_cols = ["timestamp"] + selected_tags

        try:
            df = pl.scan_parquet(target_files) \
                .filter((pl.col("timestamp") >= start_ts) & (pl.col("timestamp") <= end_ts))

            available_cols = df.collect_schema().names()
            valid_cols = [c for c in fetch_cols if c in available_cols]

            df_collected = df.select(valid_cols).sort("timestamp").collect()

            if df_collected.height == 0: return np.array([]), {}

            ts_arr = df_collected["timestamp"].to_numpy()
            vals_dict = {tag: df_collected[tag].to_numpy() for tag in selected_tags if tag in df_collected.columns}
            return ts_arr, vals_dict

        except Exception as e:
            print(f"[Engine] Raw Disk Query Error: {e}")
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