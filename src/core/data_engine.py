import os
import glob
import numpy as np
import polars as pl
import json
import warnings
import time
import re
from datetime import datetime

class TimeSeriesEngine:
    def __init__(self, data_root_dir):
        self.data_root_dir = data_root_dir
        self.channel_keys = self._load_master_schema()
        self._schema_cache = {}
        self.mart_1m = None
        self.mart_20m = None

        # Glob Caching
        self.last_glob_time = 0
        self.cached_files = {"chunks": [], "days": [], "raws": []}

        # NEW: The Active Chunk Mart
        self.chunk_mart = {}

    def sync_active_chunks(self):
        """Background worker that preemptively hoists today's chunks into RAM."""
        chunk_files = glob.glob(os.path.join(self.data_root_dir, "**/chunk_*.parquet"), recursive=True)

        # 1. Ignore temporary files that the logger is currently writing to
        valid_chunks = [f for f in chunk_files if "_temp" not in f]

        # 2. Hoist new chunks into RAM
        for f in valid_chunks:
            if f not in self.chunk_mart:
                try:
                    self.chunk_mart[f] = pl.read_parquet(f)
                except Exception as e:
                    pass  # File might be locked by the OS briefly, we will catch it next loop

        # 3. Garbage Collection: Free RAM if the 2:00 AM Compactor deleted the file from disk
        stale_keys = [k for k in self.chunk_mart.keys() if k not in valid_chunks]
        for k in stale_keys:
            del self.chunk_mart[k]

    def _get_files(self):
        """Prevents hammering the OS filesystem. Only rescans the folder every 10 seconds."""
        if time.time() - self.last_glob_time > 10.0:
            self.cached_files["chunks"] = glob.glob(os.path.join(self.data_root_dir, "**/chunk_*.parquet"),
                                                    recursive=True)
            self.cached_files["days"] = glob.glob(os.path.join(self.data_root_dir, "**/day_*.parquet"), recursive=True)
            self.cached_files["raws"] = glob.glob(os.path.join(self.data_root_dir, "**/*_raw.parquet"), recursive=True)
            self.last_glob_time = time.time()
        return self.cached_files

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

        # Glob searches the root recursively, finding macros inside daily_logs
        files_1m = glob.glob(os.path.join(self.data_root_dir, "**/*macro_1m.parquet"), recursive=True)
        files_20m = glob.glob(os.path.join(self.data_root_dir, "**/*macro_20m.parquet"), recursive=True)

        try:
            if files_20m:
                lfs_20m = [pl.scan_parquet(f) for f in files_20m]
                self.mart_20m = pl.concat(lfs_20m, how="diagonal").collect()
                print(f"[Engine] 20m Mart online. Rows: {self.mart_20m.height}")

            if files_1m:
                cutoff_ts = time.time() - (30 * 86400)
                lfs_1m = [pl.scan_parquet(f) for f in files_1m]
                self.mart_1m = pl.concat(lfs_1m, how="diagonal") \
                    .filter(pl.col("timestamp_min") >= cutoff_ts) \
                    .collect()
                print(f"[Engine] 1m Mart online (30-day window). Rows: {self.mart_1m.height}")
        except Exception as e:
            print(f"[Engine] Data Mart population failed: {e}")

    def query_stateless(self, start_ts: float, end_ts: float, selected_tags: list):
        # 1. GUARANTEED FALLBACKS
        safe_empty_ts = np.array([], dtype=np.float64)
        safe_empty_vals = {}
        if not selected_tags:
            return safe_empty_ts, safe_empty_vals

        t0 = time.perf_counter()
        span = end_ts - start_ts
        route_taken = ""

        try:
            files = self._get_files()
            all_chunks = files.get("chunks", [])
            all_days = files.get("days", [])
            raws = files.get("raws", [])

            # --- AST BLOAT PRE-FILTER (HDD Optimization) ---
            pruned_chunks = []
            for f in all_chunks:
                match = re.search(r'chunk_(\d{8})_(\d{6})', f)
                if match:
                    try:
                        dt_str = match.group(1) + match.group(2)
                        chunk_ts = datetime.strptime(dt_str, "%Y%m%d%H%M%S").timestamp()
                        if chunk_ts <= (end_ts + 3600) and (chunk_ts + 3600) >= start_ts:
                            pruned_chunks.append(f)
                    except ValueError:
                        pruned_chunks.append(f)
                else:
                    pruned_chunks.append(f)

            pruned_days = []
            for f in all_days:
                match = re.search(r'day_(\d{8})', f)
                if match:
                    try:
                        day_ts = datetime.strptime(match.group(1), "%Y%m%d").timestamp()
                        if day_ts <= (end_ts + 86400) and (day_ts + 172800) >= start_ts:
                            pruned_days.append(f)
                    except ValueError:
                        pruned_days.append(f)
                else:
                    pruned_days.append(f)

            # --- 2. RAW ROUTING (< 24 Hours) ---
            if span <= 86400 or self.mart_1m is None:
                route_taken = "RAW_DISK"
                target_files = pruned_chunks + raws + [f for f in pruned_days if "macro" not in f]

                if not target_files:
                    return safe_empty_ts, safe_empty_vals

                fetch_cols = ["timestamp"] + selected_tags

                lfs = []
                for f in target_files:
                    if "chunk_" in f and "_temp" not in f:
                        # Pull instantly from RAM, or fallback to HDD if it's brand new
                        if f in self.chunk_mart:
                            lfs.append(self.chunk_mart[f].lazy())
                        else:
                            try:
                                lfs.append(pl.scan_parquet(f))
                            except Exception:
                                pass
                    else:
                        try:
                            lfs.append(pl.scan_parquet(f))
                        except Exception:
                            pass

                df_lazy = pl.concat(lfs, how="diagonal") \
                    .filter((pl.col("timestamp") >= start_ts) & (pl.col("timestamp") <= end_ts))

                avail_cols = df_lazy.collect_schema().names()
                valid_cols = [c for c in fetch_cols if c in avail_cols]

                df = df_lazy.select(valid_cols).sort("timestamp").collect()

                if df.height == 0:
                    #self._log_perf(route_taken, span, 0, t0)
                    return safe_empty_ts, safe_empty_vals

                ts_arr = df["timestamp"].to_numpy()
                vals_dict = {tag: df[tag].to_numpy() for tag in selected_tags if tag in df.columns}

                #self._log_perf(route_taken, span, ts_arr, t0)
                return ts_arr, vals_dict

            # --- 3. MACRO ROUTING WITH GAP STITCHING (> 24 Hours) ---
            else:
                is_20m = span > (30 * 86400)
                mart = self.mart_20m if is_20m and self.mart_20m is not None else self.mart_1m
                route_taken = "MART_20M" if is_20m else "MART_1M"

                if mart is None:
                    return safe_empty_ts, safe_empty_vals

                df_macro = mart.filter((pl.col("timestamp_min") >= start_ts) & (pl.col("timestamp_min") <= end_ts))

                ts_arr = safe_empty_ts
                vals_dict = {tag: np.array([], dtype=np.float64) for tag in selected_tags}

                if df_macro.height > 0:
                    ts_min = df_macro["timestamp_min"].to_numpy()
                    ts_max = df_macro["timestamp_max"].to_numpy()
                    ts_arr = np.empty((ts_min.size + ts_max.size,), dtype=np.float64)
                    ts_arr[0::2] = ts_min
                    ts_arr[1::2] = ts_max

                    for tag in selected_tags:
                        min_col, max_col = f"{tag}_min", f"{tag}_max"
                        if min_col in df_macro.columns and max_col in df_macro.columns:
                            y_min = df_macro[min_col].to_numpy()
                            y_max = df_macro[max_col].to_numpy()

                            # Create an empty array double the size
                            y_arr = np.empty((y_min.size * 2,), dtype=np.float64)

                            # The Alternating Ribbon Weave
                            # Even bins go up: Min -> Max
                            y_arr[0::4] = y_min[0::2]
                            y_arr[1::4] = y_max[0::2]

                            # Odd bins go down: Max -> Min
                            y_arr[2::4] = y_max[1::2]
                            y_arr[3::4] = y_min[1::2]

                            vals_dict[tag] = y_arr

                    # Stitch the "Today Gap"
                    if pruned_chunks:
                        # CRITICAL FIX: Only stitch chunks that are within the last 24 hours
                        # This prevents the RAM explosion when zoomed out to months
                        today_cutoff = time.time() - 86400
                        relevant_chunks = [f for f in pruned_chunks if self._get_chunk_ts(f) >= today_cutoff]

                        if relevant_chunks:
                            try:
                                lfs = []
                                for f in pruned_chunks:
                                    if "_temp" in f: continue
                                    if f in self.chunk_mart:
                                        lfs.append(self.chunk_mart[f].lazy())
                                    else:
                                        try:
                                            lfs.append(pl.scan_parquet(f))
                                        except Exception:
                                            pass

                                df_chunks_lazy = pl.concat(lfs, how="diagonal") \
                                    .filter((pl.col("timestamp") >= start_ts - 3600) & (pl.col("timestamp") <= end_ts + 3600))

                                # 1. Get tags actually present in THIS specific chunk batch
                                available_cols = df_chunks_lazy.collect_schema().names()
                                valid_tags = [t for t in selected_tags if t in available_cols]

                                if span > 7200 and valid_tags:
                                    # 2. Build the aggregation list dynamically based on schema
                                    aggregations = []
                                    for c in valid_tags:
                                        aggregations.append(pl.col(c).min().alias(f"{c}_min"))
                                        aggregations.append(pl.col(c).max().alias(f"{c}_max"))

                                    # 1. Cast timestamp using safe expression syntax
                                    df_chunks_lazy = df_chunks_lazy.with_columns(
                                        (pl.from_epoch(pl.col("timestamp"))).alias("ts_dt")
                                    )

                                    # 2. Aggregation: Use standard math operators
                                    if valid_tags:
                                        aggregations = []
                                        for c in valid_tags:
                                            aggregations.append(pl.col(c).min().alias(f"{c}_min"))
                                            aggregations.append(pl.col(c).max().alias(f"{c}_max"))

                                        df_chunks_lazy = df_chunks_lazy.group_by_dynamic("ts_dt", every="60s").agg(
                                            aggregations)

                                    # 3. Cast back to epoch float
                                    df_chunks_lazy = df_chunks_lazy.with_columns(
                                        (pl.col("ts_dt").dt.epoch(time_unit="ms") / 1000.0).alias("timestamp")
                                    )

                                # Collect and sort
                                df_chunks = df_chunks_lazy.sort("timestamp").collect()
                                # Debug the stitch

                                if df_chunks.height > 0:
                                    route_taken += " + STITCH"

                                    if span > 7200:
                                        # If macro, we must stitch the min/max envelope format
                                        ts_min = df_chunks["timestamp"].to_numpy()
                                        # Use min/max naming from Mart structure
                                        ts_chunks = np.empty((ts_min.size * 2,), dtype=np.float64)
                                        ts_chunks[0::2] = ts_min
                                        ts_chunks[1::2] = ts_min  # Vertical bar constraint

                                        ts_arr = np.concatenate((ts_arr, ts_chunks)) if ts_arr.size else ts_chunks

                                        for tag in valid_tags:
                                            min_col, max_col = f"{tag}_min", f"{tag}_max"
                                            if min_col in df_chunks.columns and max_col in df_chunks.columns:
                                                y_min = df_chunks[min_col].to_numpy()
                                                y_max = df_chunks[max_col].to_numpy()

                                                # Ribbon Weave
                                                y_chunks = np.empty((y_min.size * 2,), dtype=np.float64)
                                                y_chunks[0::4] = y_min[0::2]
                                                y_chunks[1::4] = y_max[0::2]
                                                y_chunks[2::4] = y_max[1::2]
                                                y_chunks[3::4] = y_min[1::2]

                                                if tag in vals_dict and vals_dict[tag].size:
                                                    vals_dict[tag] = np.concatenate((vals_dict[tag], y_chunks))
                                                else:
                                                    vals_dict[tag] = y_chunks
                                    else:
                                        # Standard raw stitch
                                        ts_chunks = df_chunks["timestamp"].to_numpy()
                                        ts_arr = np.concatenate((ts_arr, ts_chunks)) if ts_arr.size else ts_chunks
                                        for tag in valid_tags:
                                            if tag in df_chunks.columns:
                                                y_chunks = df_chunks[tag].to_numpy()
                                                if tag in vals_dict and vals_dict[tag].size:
                                                    vals_dict[tag] = np.concatenate((vals_dict[tag], y_chunks))
                                                else:
                                                    vals_dict[tag] = y_chunks

                            except Exception as stitch_err:
                                print(f"[Engine] Stitching skipped: {stitch_err}")

                return ts_arr, vals_dict

        except Exception as e:
            print(f"[Engine] Fatal Query Error: {e}")
            return safe_empty_ts, safe_empty_vals

    def _get_chunk_ts(self, filepath):
        """Extracts timestamp from filename for filtering."""
        match = re.search(r'chunk_(\d{8})_(\d{6})', filepath)
        if match:
            try:
                return datetime.strptime(match.group(1) + match.group(2), "%Y%m%d%H%M%S").timestamp()
            except:
                pass
        return 0

    def _log_perf(self, route: str, span_sec: float, ts_arr: np.ndarray, t0: float):
        """Outputs a clean benchmark including mathematical \u0394t and visual density."""
        elapsed = (time.perf_counter() - t0) * 1000
        span_hrs = span_sec / 3600.0
        pts = len(ts_arr)

        # 1. Calculate Empirical Time Interval (\u0394t)
        dt_sec = 0.0
        if pts > 2:
            if "MART" in route:
                # Macros have duplicated X values [t0, t0, t1, t1...] to draw vertical lines
                # Slicing [0::2] gets the unique timestamps [t0, t1, t2...]
                unique_ts = ts_arr[0::2]
                if len(unique_ts) > 1:
                    dt_sec = np.median(np.diff(unique_ts))
            else:
                dt_sec = np.median(np.diff(ts_arr))

        # 2. Calculate Visual Density (Assuming a ~1500px wide ViewBox)
        density = pts / 1500.0

        print(
            f"[Engine] {route:<18} | Span: {span_hrs:>5.1f}h | Pts: {pts:>7} | \u0394t: {dt_sec:>5.1f}s | Density: {density:>5.1f} pts/px | Time: {elapsed:>5.1f}ms")

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