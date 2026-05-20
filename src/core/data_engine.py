import os
import glob
import numpy as np
import polars as pl
import json
import warnings


class TimeSeriesEngine:
    """
    Polars-backed engine for querying and decimating Parquet SCADA logs.
    """

    def __init__(self, log_directory):
        self.log_dir = log_directory
        self.channel_keys = self._load_master_schema()

    def _load_master_schema(self) -> list:
        """Loads the exact schema directly from system_tags.json."""
        registry_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../config/system_tags.json'))

        try:
            with open(registry_path, "r") as f:
                registry = json.load(f)
                return sorted(list(registry.keys()))
        except FileNotFoundError:
            print(f"[Engine] CRITICAL: Registry not found at {registry_path}.")
            return []

    def query(self, start_ts: float, end_ts: float, stride: int = 1):
        # Define both directories
        parquet_pattern = os.path.join(self.log_dir, "parquet_logs", "*.parquet")
        daily_pattern = os.path.join(self.log_dir, "daily_logs", "*.parquet")

        # Get actual files that exist to prevent Polars from throwing a "no files found" error
        available_files = glob.glob(parquet_pattern) + glob.glob(daily_pattern)
        #print(f"[Engine DEBUG] Query requested: start={start_ts}, end={end_ts}")
        #print(f"[Engine DEBUG] Found {len(available_files)} parquet files to scan.")

        if not available_files:
            return np.array([]), {}

        try:
            # 1. SCHEMA EVOLUTION FIX:
            # Load files individually and concatenate 'diagonally' to unify mismatched schemas
            lfs = [pl.scan_parquet(f) for f in available_files]
            lf = pl.concat(lfs, how="diagonal")

            # 2. Push down filters: Only load the specific time window
            lf = lf.filter((pl.col("timestamp") >= start_ts) & (pl.col("timestamp") <= end_ts))

            # 3. Ensure chronological order (in case fragment times overlapped)
            lf = lf.sort("timestamp")

            # 4. Decimate directly in the lazy query if requested
            if stride > 1:
                lf = lf.gather_every(stride)

            # 5. Execute the optimized query and pull into RAM
            #print("[Engine DEBUG] Executing lf.collect()...")
            df = lf.collect()
            #print(f"[Engine DEBUG] Collect successful. DataFrame shape: {df.shape}")

            if df.is_empty():
                #print("[Engine DEBUG] DataFrame is empty after filtering by start_ts and end_ts!")
                return np.array([]), {}

            # Debug datatypes before NumPy conversion
            null_cols = [c for c in df.columns if df[c].dtype == pl.Null]
            # null_cols:
                #print(f"[Engine DEBUG] WARNING: Found {len(null_cols)} columns with pl.Null datatype.")


            # 6. Convert to PyQtGraph-friendly NumPy arrays natively
            ts_array = df["timestamp"].to_numpy()
            #print(f"[Engine DEBUG] Extracted ts_array: length={len(ts_array)}, dtype={ts_array.dtype}")

            vals_dict = {}
            for col in df.columns:
                if col != "timestamp" and col in self.channel_keys:
                    vals_dict[col] = df[col].to_numpy()

            #print(f"[Engine DEBUG] Returning dictionary with {len(vals_dict)} channels.")
            return ts_array, vals_dict

        except Exception as e:
            #print(f"[Engine DEBUG] Parquet Query Error: {repr(e)}")
            return np.array([]), {}

    @staticmethod
    def downsample_minmax(times, values, target_points=2000):
        """
        Dynamically decimates a 1D array while perfectly preserving visual noise spikes
        (Min/Max envelope). Ideal for rendering millions of points to a fixed-width screen.
        """
        n_points = len(values)
        if n_points <= target_points:
            return times, values

        chunk_size = max(1, n_points // (target_points // 2))
        n_chunks = n_points // chunk_size
        n_usable = n_chunks * chunk_size

        data_view = values[:n_usable].reshape(n_chunks, chunk_size)
        time_view = times[:n_usable].reshape(n_chunks, chunk_size)

        with np.errstate(invalid='ignore'), warnings.catch_warnings():
            warnings.simplefilter("ignore", category=RuntimeWarning)
            mins = np.nanmin(data_view, axis=1)
            maxs = np.nanmax(data_view, axis=1)

        final_values = np.empty(n_chunks * 2, dtype=values.dtype)
        final_values[0::2] = mins
        final_values[1::2] = maxs

        final_times = np.empty(n_chunks * 2, dtype=times.dtype)
        final_times[0::2] = time_view[:, 0]
        final_times[1::2] = time_view[:, -1]

        return final_times, final_values

    def get_global_bounds(self):
        """Finds absolute time boundaries across both fragment and daily directories."""
        patterns = [
            os.path.join(self.log_dir, "parquet_logs", "*.parquet"),
            os.path.join(self.log_dir, "daily_logs", "*.parquet")
        ]

        available_files = []
        for p in patterns:
            available_files.extend(glob.glob(p))

        #print(f"[Engine DEBUG] get_global_bounds: Found {len(available_files)} files to scan for min/max ts.")
        if not available_files:
            return None, None

        try:
            # We scan all files but only look at the 'timestamp' column
            lf = pl.scan_parquet(available_files).select([
                pl.col("timestamp").min().alias("min_ts"),
                pl.col("timestamp").max().alias("max_ts")
            ])
            res = lf.collect()
            min_ts, max_ts = res["min_ts"][0], res["max_ts"][0]
            #print(f"[Engine DEBUG] get_global_bounds: Min={min_ts}, Max={max_ts}")
            return min_ts, max_ts
        except Exception as e:
            #print(f"[Engine DEBUG] Bounds error: {repr(e)}")
            return None, None