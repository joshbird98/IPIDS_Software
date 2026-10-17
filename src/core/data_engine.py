import os
import glob
import logging
import numpy as np
import polars as pl
import json
import warnings
import time
import re
from datetime import datetime

log = logging.getLogger("data_engine")

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
        self._unreadable_chunks = set()

    RAW_MAX_SPAN = 86400            # <= this: raw rows
    MACRO_1M_MAX_SPAN = 20 * 86400  # <= this: 1-minute rollups from disk
    RAW_FALLBACK_SPAN = 7 * 86400   # rollups missing for window: raw if span <= this
    PRELOAD_1M_MART = False
    RAW_BIN_ABOVE_SPAN = 600        # raw windows longer than this are min/max binned in polars
                                    # (numpy nanmin/nanmax on the worker thread holds the GIL)
    RAW_BINS = 4000                 # -> 8000 points, matching StatelessQueryWorker.TARGET_POINTS

    # Hoisting chunk files into RAM is OFF by default. Each 15-min chunk is ~0.2 MB
    # on disk but ~22 MB as a dense 317-column frame, so 68 chunks cost ~1.4 GB RSS.
    # Reading them lazily from disk (query_stateless already falls back to
    # pl.scan_parquet) costs only ~10-110 ms extra per query, and queries run on
    # the StatelessQueryWorker thread, not the GUI thread.
    HOIST_CHUNKS_TO_RAM = False

    def sync_active_chunks(self):
        """Background worker that preemptively hoists today's chunks into RAM."""
        if not self.HOIST_CHUNKS_TO_RAM:
            self.chunk_mart.clear()
            return
        chunk_files = glob.glob(os.path.join(self.data_root_dir, "**/chunk_*.parquet"), recursive=True)

        # 1. Ignore temporary files that the logger is currently writing to
        valid_chunks = [f for f in chunk_files if "_temp" not in f]

        # 2. Hoist new chunks into RAM
        for f in valid_chunks:
            if f not in self.chunk_mart:
                try:
                    self.chunk_mart[f] = pl.read_parquet(f)
                    self._unreadable_chunks.discard(f)
                except (OSError, pl.exceptions.PolarsError) as e:
                    # Usually locked by the OS for a moment (retried next loop); say so once per file
                    # so a corrupt chunk doesn't go unnoticed
                    if f not in self._unreadable_chunks:
                        self._unreadable_chunks.add(f)
                        log.warning("Chunk %s not readable (will keep retrying): %s", f, e)

        # 3. Garbage Collection: Free RAM if the 2:00 AM Compactor deleted the file from disk
        stale_keys = [k for k in self.chunk_mart.keys() if k not in valid_chunks]
        for k in stale_keys:
            del self.chunk_mart[k]

    def _get_files(self):
        """Prevents hammering the OS filesystem. Only rescans the folder every 10 seconds.

        Lists the two folders the logger/compactor actually write to
        (data/parquet_logs, data/daily_logs) with one os.scandir each, instead of
        four recursive '**' globs over all of data/ (exports, scans, ...). The glob
        ran on the query worker thread as pure Python (~70 ms here, more on a big
        Windows tree) and held the GIL, stalling the GUI thread. Falls back to the
        old recursive search if those folders don't exist.
        """
        if time.time() - self.last_glob_time > 10.0:
            known = [os.path.join(self.data_root_dir, d) for d in ("parquet_logs", "daily_logs")]
            known = [d for d in known if os.path.isdir(d)]
            if known:
                names = []
                for d in known:
                    with os.scandir(d) as it:
                        names += [e.path for e in it if e.name.endswith(".parquet")]
            else:
                names = glob.glob(os.path.join(self.data_root_dir, "**/*.parquet"), recursive=True)
            base = os.path.basename
            # (skip the logger's *_temp.parquet files: they are renamed into place once complete)
            self.cached_files["chunks"] = [f for f in names if base(f).startswith("chunk_") and "_temp" not in base(f)]
            self.cached_files["days"] = [f for f in names if base(f).startswith("day_")]
            self.cached_files["raws"] = [f for f in names if f.endswith("_raw.parquet")]
            self.cached_files["macro_1m"] = [f for f in names if f.endswith("macro_1m.parquet")]
            self.last_glob_time = time.time()
        return self.cached_files

    def _load_master_schema(self) -> list:
        from src.core.paths import REGISTRY_PATH as registry_path
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

    def _query_raw(self, start_ts, end_ts, selected_tags, pruned_chunks, pruned_days, raws):
        """Raw-row route. Schema-aware per file (cached), so each file only decodes
        the timestamp + selected columns, and files lacking a tag don't break the
        query."""
        empty = (np.array([], dtype=np.float64), {})
        target_files = [f for f in pruned_chunks if "_temp" not in f] + raws + \
                       [f for f in pruned_days if "macro" not in f]
        if not target_files:
            return empty
        fetch_cols = ["timestamp"] + list(selected_tags)
        lfs = []
        for f in target_files:
            try:
                if f in self.chunk_mart:
                    df = self.chunk_mart[f]
                    lfs.append(df.lazy().select([c for c in fetch_cols if c in df.columns]))
                else:
                    lfs.append(self.get_safe_scan(f, fetch_cols))
            except Exception:
                pass
        if not lfs:
            return empty
        lf = pl.concat(lfs, how="diagonal_relaxed") \
            .filter((pl.col("timestamp") >= start_ts) & (pl.col("timestamp") <= end_ts))
        present = [t for t in selected_tags if t in lf.collect_schema().names()]

        if (end_ts - start_ts) > self.RAW_BIN_ABOVE_SPAN:
            # Min/max binning inside polars (Rust, multi-threaded) instead of pulling
            # every row into numpy and thinning it afterwards: ~30% faster and
            # ~170 MB less transient RAM for a 24 h window. Output matches
            # downsample_minmax(): per bin (t_first, min), (t_last, max).
            bin_ms = max(1, int((end_ts - start_ts) * 1000 / self.RAW_BINS))
            aggs = [pl.col("timestamp").min().alias("_t0"), pl.col("timestamp").max().alias("_t1")]
            aggs += [pl.col(t).cast(pl.Float64).fill_nan(None).min().alias(f"{t}__min") for t in present]
            aggs += [pl.col(t).cast(pl.Float64).fill_nan(None).max().alias(f"{t}__max") for t in present]
            df = lf.with_columns(pl.from_epoch(pl.col("timestamp"), time_unit="s").alias("_dt")) \
                .sort("_dt").group_by_dynamic("_dt", every=f"{bin_ms}ms").agg(aggs).collect()
            if df.height == 0:
                return empty
            n = df.height
            ts_arr = np.empty(2 * n, dtype=np.float64)
            ts_arr[0::2] = df["_t0"].to_numpy()
            ts_arr[1::2] = df["_t1"].to_numpy()
            vals = {}
            for t in present:
                v = np.empty(2 * n, dtype=np.float64)
                v[0::2] = df[f"{t}__min"].to_numpy()
                v[1::2] = df[f"{t}__max"].to_numpy()
                vals[t] = v
            return ts_arr, vals

        df = lf.select(["timestamp"] + present).sort("timestamp").collect()
        if df.height == 0:
            return empty
        ts_arr = df["timestamp"].to_numpy()
        vals = {t: df[t].cast(pl.Float64).to_numpy() for t in present}
        return ts_arr, vals

    def _scan_macro_1m(self, start_ts, end_ts, selected_tags, files_1m):
        """1-minute rollups for [start, end] read lazily from the per-day
        day_YYYYMMDD_macro_1m.parquet files (~25-55 ms for 2-20 days)."""
        sel = []
        for f in files_1m:
            m = re.search(r'day_(\d{8})', f)
            if m:
                try:
                    day_ts = datetime.strptime(m.group(1), "%Y%m%d").timestamp()
                    if day_ts > end_ts + 86400 or day_ts + 2 * 86400 < start_ts:
                        continue
                except ValueError:
                    pass
            sel.append(f)
        if not sel:
            return None
        cols = ["timestamp_min", "timestamp_max"] + [f"{t}_{s}" for t in selected_tags for s in ("min", "max")]
        lfs = [self.get_safe_scan(f, cols) for f in sel]
        return pl.concat(lfs, how="diagonal_relaxed") \
            .filter((pl.col("timestamp_min") >= start_ts) & (pl.col("timestamp_min") <= end_ts)) \
            .sort("timestamp_min").collect()

    def populate_macro_marts(self):
        """Silently loads macro rollups into RAM for instantaneous slicing."""
        print("[Engine] Hoisting In-Memory Data Marts...")

        # Glob searches the root recursively, finding macros inside daily_logs
        files_1m = glob.glob(os.path.join(self.data_root_dir, "**/*macro_1m.parquet"), recursive=True)
        files_20m = glob.glob(os.path.join(self.data_root_dir, "**/*macro_20m.parquet"), recursive=True)

        try:
            if files_20m:
                lfs_20m = [pl.scan_parquet(f) for f in files_20m]
                # glob() order is filesystem order, not time order: sort, or the
                # plotted x goes backwards (breaks clipToView / downsampling).
                self.mart_20m = pl.concat(lfs_20m, how="diagonal").sort("timestamp_min").collect()
                print(f"[Engine] 20m Mart online. Rows: {self.mart_20m.height}")

            # The 1m tier is now read lazily from disk per query (_scan_macro_1m), so it
            # is no longer preloaded (saves up to ~230 MB for 30 days of 336 tags).
            if files_1m and self.PRELOAD_1M_MART:
                cutoff_ts = time.time() - (30 * 86400)
                lfs_1m = [pl.scan_parquet(f) for f in files_1m]
                self.mart_1m = pl.concat(lfs_1m, how="diagonal") \
                    .filter(pl.col("timestamp_min") >= cutoff_ts) \
                    .sort("timestamp_min") \
                    .collect()
                print(f"[Engine] 1m Mart online (30-day window). Rows: {self.mart_1m.height}")
        except Exception as e:
            print(f"[Engine] Data Mart population failed: {e}")

    @staticmethod
    def _prune_files(all_chunks, all_days, start_ts, end_ts):
        """Keep only chunk/day files whose filename date can overlap [start, end]
        (HDD optimisation: avoids opening files that cannot contain the window)."""
        pruned_chunks = []
        for f in all_chunks:
            match = re.search(r'chunk_(\d{8})_(\d{6})', f)
            if match:
                try:
                    chunk_ts = datetime.strptime(match.group(1) + match.group(2), "%Y%m%d%H%M%S").timestamp()
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
        return sorted(pruned_chunks), sorted(pruned_days)

    def iter_exact_frames(self, start_ts, end_ts, selected_tags, refresh_listing=False):
        """Yield every logged row in [start, end] as polars DataFrames, one per
        source file (bounded memory for long exports): columns 'timestamp' plus
        each selected tag present in that file (Float64), sorted by time.
        Uses raw day/chunk files only - never the min/max rollups."""
        if refresh_listing:
            self.last_glob_time = 0
        files = self._get_files()
        chunks, days = self._prune_files(files.get("chunks", []), files.get("days", []), start_ts, end_ts)
        targets = [f for f in days if "macro" not in f] + files.get("raws", []) + \
                  [f for f in chunks if "_temp" not in f]
        cols = ["timestamp"] + list(selected_tags)
        for f in targets:
            try:
                lf = self.get_safe_scan(f, cols)
                df = lf.filter((pl.col("timestamp") >= start_ts) & (pl.col("timestamp") <= end_ts)) \
                       .sort("timestamp").collect()
            except Exception as e:
                print(f"[Engine] export: skipped {os.path.basename(f)}: {e}")
                continue
            if df.height:
                yield df.with_columns([pl.col(c).cast(pl.Float64) for c in df.columns])

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

            pruned_chunks, pruned_days = self._prune_files(all_chunks, all_days, start_ts, end_ts)

            # --- 2. ROUTE SELECTION ---
            #   span <= 24 h          -> raw rows from chunk/day files
            #   24 h < span <= 20 d   -> 1-minute rollups, read lazily from disk
            #   span > 20 d           -> 20-minute rollups (in RAM)
            # Previously >24 h always used the in-RAM 1m mart, which only holds the
            # last 30 days from *now*, so older data jumped straight from ~40 s/pt to
            # 20 min/pt at the 24 h boundary ("have to zoom a long way in").
            have_1m = bool(files.get("macro_1m"))
            have_20m = self.mart_20m is not None
            if span <= self.RAW_MAX_SPAN or not (have_1m or have_20m):
                route_taken = "RAW_DISK"
                return self._query_raw(start_ts, end_ts, selected_tags, pruned_chunks, pruned_days, raws)

            # --- 3. MACRO ROUTING WITH GAP STITCHING (> 24 Hours) ---
            else:
                if have_1m and (span <= self.MACRO_1M_MAX_SPAN or not have_20m):
                    route_taken = "MART_1M_DISK"
                    df_macro = self._scan_macro_1m(start_ts, end_ts, selected_tags, files["macro_1m"])
                else:
                    route_taken = "MART_20M"
                    df_macro = self.mart_20m.filter(
                        (pl.col("timestamp_min") >= start_ts) & (pl.col("timestamp_min") <= end_ts))

                if (df_macro is None or df_macro.height == 0) and span <= self.RAW_FALLBACK_SPAN:
                    # No rollups for this window yet (e.g. not compacted): use raw rows.
                    route_taken = "RAW_FALLBACK"
                    return self._query_raw(start_ts, end_ts, selected_tags, pruned_chunks, pruned_days, raws)
                if df_macro is None:
                    return safe_empty_ts, safe_empty_vals

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
                    n_macro = ts_arr.size
                    chunk_parts = {}
                    if pruned_chunks:
                        # CRITICAL FIX: Only stitch chunks that are within the last 24 hours
                        # This prevents the RAM explosion when zoomed out to months
                        today_cutoff = time.time() - 86400
                        relevant_chunks = [f for f in pruned_chunks if self._get_chunk_ts(f) >= today_cutoff]

                        if relevant_chunks:
                            try:
                                lfs = []
                                stitch_cols = ["timestamp"] + list(selected_tags)
                                for f in pruned_chunks:
                                    if "_temp" in f: continue
                                    try:
                                        if f in self.chunk_mart:
                                            _df = self.chunk_mart[f]
                                            lfs.append(_df.lazy().select([c for c in stitch_cols if c in _df.columns]))
                                        else:
                                            # schema-aware projection (cached), as in _query_raw
                                            lfs.append(self.get_safe_scan(f, stitch_cols))
                                    except Exception:
                                        pass

                                df_chunks_lazy = pl.concat(lfs, how="diagonal_relaxed") \
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

                                    # 1. Cast timestamp using safe expression syntax.
                                    # group_by_dynamic REQUIRES sorted input; chunk files are
                                    # concatenated in directory order, so without this sort the
                                    # stitch raised, was caught as "Stitching skipped", and today's
                                    # data vanished from every view longer than 24 h.
                                    df_chunks_lazy = df_chunks_lazy.sort("timestamp").with_columns(
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

                                                chunk_parts[tag] = y_chunks
                                        vals_dict = self._stitch_parts(vals_dict, chunk_parts, selected_tags,
                                                                       n_macro, ts_chunks.size)
                                    else:
                                        # Standard raw stitch
                                        ts_chunks = df_chunks["timestamp"].to_numpy()
                                        ts_arr = np.concatenate((ts_arr, ts_chunks)) if ts_arr.size else ts_chunks
                                        for tag in valid_tags:
                                            if tag in df_chunks.columns:
                                                chunk_parts[tag] = df_chunks[tag].to_numpy()
                                        vals_dict = self._stitch_parts(vals_dict, chunk_parts, selected_tags,
                                                                       n_macro, ts_chunks.size)

                            except Exception as stitch_err:
                                print(f"[Engine] Stitching skipped: {stitch_err}")

                return self._sort_by_time(ts_arr, vals_dict)

        except Exception as e:
            print(f"[Engine] Fatal Query Error: {e}")
            return safe_empty_ts, safe_empty_vals

    @staticmethod
    def _stitch_parts(macro_vals, chunk_parts, tags, n_macro, n_chunk):
        """Join macro + chunk segments per tag, NaN-padding whichever side a tag
        is missing from, so every value array matches the stitched timestamp
        array. (Tags added/renamed over time exist in only one side; unequal
        lengths make pyqtgraph raise inside a Qt slot, which aborts the app.)"""
        out = {}
        for tag in tags:
            m = macro_vals.get(tag)
            if m is None or len(m) != n_macro:
                m = np.full(n_macro, np.nan)
            c = chunk_parts.get(tag)
            if c is None or len(c) != n_chunk:
                c = np.full(n_chunk, np.nan)
            out[tag] = np.concatenate((np.asarray(m, dtype=np.float64), np.asarray(c, dtype=np.float64)))
        return out

    @staticmethod
    def _sort_by_time(ts_arr, vals_dict):
        """Guarantee monotonic time (clipToView and downsample_minmax assume it)
        and drop any value array whose length doesn't match the timestamps."""
        n = len(ts_arr)
        clean = {k: v for k, v in vals_dict.items() if len(v) == n}
        if n > 1 and np.any(np.diff(ts_arr) < 0):
            order = np.argsort(ts_arr, kind="stable")
            ts_arr = ts_arr[order]
            clean = {k: v[order] for k, v in clean.items()}
        return ts_arr, clean

    def _get_chunk_ts(self, filepath):
        """Extracts timestamp from filename for filtering."""
        match = re.search(r'chunk_(\d{8})_(\d{6})', filepath)
        if match:
            try:
                return datetime.strptime(match.group(1) + match.group(2), "%Y%m%d%H%M%S").timestamp()
            except ValueError:  # malformed date in a hand-renamed file
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