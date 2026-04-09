import os
import numpy as np
import json


class TimeSeriesEngine:
    """
    Backend engine for indexing, querying, and decimating SCADA .npz log files.
    """

    def __init__(self, log_directory):
        self.log_dir = log_directory
        self.manifest = []  # Stores dicts: {'path': str, 'start': float, 'end': float}

        # 1. Load schema from central registry immediately
        self.channel_keys = self._load_master_schema()

        self._scan_directory()

    def _load_master_schema(self) -> list:
        """Loads the exact schema directly from system_tags.json."""
        current_dir = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.dirname(current_dir)
        registry_path = os.path.join(project_root, "system_tags.json")

        try:
            with open(registry_path, "r") as f:
                registry = json.load(f)
                # Sort alphabetically to guarantee index matching with the Logger
                return sorted(list(registry.keys()))
        except FileNotFoundError:
            print(f"[Engine] CRITICAL: Registry not found at {registry_path}.")
            return []

    def _scan_directory(self):
        """
        Scans the log directory and builds a lightweight RAM index of all available files
        and their exact UNIX time boundaries.
        """
        if not os.path.exists(self.log_dir):
            print(f"[Engine] Directory not found: {self.log_dir}")
            return

        print(f"[Engine] Scanning directory: {self.log_dir}...")
        self.manifest.clear()

        for filename in os.listdir(self.log_dir):
            if not filename.endswith(".npz") or filename.endswith("_temp.npz"):
                continue

            filepath = os.path.join(self.log_dir, filename)

            try:
                # np.load is lazy. It doesn't load the massive 'values' array into RAM
                with np.load(filepath, allow_pickle=True) as data:
                    timestamps = data['timestamps']

                    if len(timestamps) == 0:
                        continue

                    t_start = float(timestamps[0])
                    t_end = float(timestamps[-1])

                    self.manifest.append({
                        'path': filepath,
                        'start': t_start,
                        'end': t_end
                    })

            except Exception as e:
                print(f"[Engine] Failed to index {filename}: {e}")

        # Sort manifest chronologically by start time
        self.manifest.sort(key=lambda x: x['start'])
        print(f"[Engine] Indexed {len(self.manifest)} files.")

    def query(self, start_ts, end_ts, stride=1):
        """
        Loads and stitches all files that intersect the requested time window.
        Returns: (stitched_timestamps, stitched_values)
        """
        # 1. Find overlapping files
        files_to_load = []
        for file_info in self.manifest:
            if file_info['start'] <= end_ts and file_info['end'] >= start_ts:
                files_to_load.append(file_info['path'])

        if not files_to_load:
            return np.array([]), np.array([])

        print(f"[Engine] Query hits {len(files_to_load)} files. Stitching...")

        # 2. Load and merge arrays
        ts_chunks = []
        val_chunks = []

        master_keys = self.channel_keys
        num_cols = len(master_keys)

        for filepath in files_to_load:
            try:
                with np.load(filepath, allow_pickle=True) as data:
                    file_ts = data['timestamps']
                    file_vals = data['values']

                    # --- THE FIX: Safely extract and decode keys from NumPy ---
                    if 'keys' in data:
                        raw_keys = data['keys']
                        # Force any weird byte-strings into standard Python strings
                        file_keys = [k.decode('utf-8') if isinstance(k, bytes) else str(k) for k in raw_keys]
                    else:
                        file_keys = []

                    print(f"[DEBUG File] Loaded {os.path.basename(filepath)}. Found {len(file_keys)} keys. Sample: {file_keys[:3]}")

                    # Create a blank slate of NaNs shaped to match the CURRENT master schema
                    padded_vals = np.full((len(file_ts), num_cols), np.nan, dtype=np.float32)

                    # Map the old columns into their correct positions in the new schema
                    for i, key in enumerate(file_keys):
                        if key in master_keys:
                            master_idx = master_keys.index(key)
                            padded_vals[:, master_idx] = file_vals[:, i]

                    ts_chunks.append(file_ts)
                    val_chunks.append(padded_vals)

            except Exception as e:
                print(f"[Engine] Error loading {filepath} during query: {e}")

        if not ts_chunks:
            return np.array([]), np.array([])

        raw_ts = np.concatenate(ts_chunks)
        raw_vals = np.vstack(val_chunks)

        # 3. Sort chronologically
        sort_indices = np.argsort(raw_ts, kind='mergesort')
        sorted_ts = raw_ts[sort_indices]
        sorted_vals = raw_vals[sort_indices]

        # 4. Filter duplicates
        unique_ts, unique_idx = np.unique(sorted_ts, return_index=True)
        sorted_ts = unique_ts
        sorted_vals = sorted_vals[unique_idx]

        # 5. Hard-slice to the requested window
        start_idx = np.searchsorted(sorted_ts, start_ts, side='left')
        end_idx = np.searchsorted(sorted_ts, end_ts, side='right')

        final_ts = sorted_ts[start_idx:end_idx]
        final_vals = sorted_vals[start_idx:end_idx]

        if stride > 1:
            final_ts = final_ts[::stride]
            final_vals = final_vals[::stride]

        print(f"[DEBUG Engine] Query Complete. Handing over {len(final_ts)} rows with shape {final_vals.shape} to the background thread.")

        return final_ts, final_vals

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

        with np.errstate(invalid='ignore'):
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
        """Returns the absolute earliest and latest timestamps in the log directory."""
        if not self.manifest:
            return None, None
        return self.manifest[0]['start'], self.manifest[-1]['end']