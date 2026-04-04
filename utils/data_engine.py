import os
import numpy as np


class TimeSeriesEngine:
    """
    Backend engine for indexing, querying, and decimating SCADA .npz log files.
    """

    def __init__(self, log_directory):
        self.log_dir = log_directory
        self.manifest = []  # Stores dicts: {'path': str, 'start': float, 'end': float}
        self.channel_keys = []

        self._scan_directory()

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
            if not filename.endswith(".npz"):
                continue

            filepath = os.path.join(self.log_dir, filename)

            try:
                # np.load is lazy. It doesn't load the massive 'values' array into RAM
                # until you explicitly ask for it. This makes scanning hundreds of files very fast.
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

                    # Capture the channel keys from the very first valid file we read
                    if not self.channel_keys and 'keys' in data:
                        self.channel_keys = list(data['keys'])

            except Exception as e:
                print(f"[Engine] Failed to index {filename}: {e}")

        # Sort manifest chronologically by start time
        self.manifest.sort(key=lambda x: x['start'])
        print(f"[Engine] Indexed {len(self.manifest)} files.")

    def query(self, start_ts, end_ts):
        """
        Loads and stitches all files that intersect the requested time window.
        Returns: (stitched_timestamps, stitched_values)
        """
        # 1. Find overlapping files
        files_to_load = []
        for file_info in self.manifest:
            # Check for overlap: File starts before window ends AND File ends after window starts
            if file_info['start'] <= end_ts and file_info['end'] >= start_ts:
                files_to_load.append(file_info['path'])

        if not files_to_load:
            return np.array([]), np.array([])

        print(f"[Engine] Query hits {len(files_to_load)} files. Stitching...")

        # 2. Load and merge arrays
        ts_chunks = []
        val_chunks = []

        for filepath in files_to_load:
            try:
                with np.load(filepath, allow_pickle=True) as data:
                    ts_chunks.append(data['timestamps'])
                    val_chunks.append(data['values'])
            except Exception as e:
                print(f"[Engine] Error loading {filepath} during query: {e}")

        if not ts_chunks:
            return np.array([]), np.array([])

        # Concatenate all chunks
        raw_ts = np.concatenate(ts_chunks)
        raw_vals = np.vstack(val_chunks)

        # 3. Sort chronologically
        # (Crucial because a Burst file and a Daily file might have overlapping timestamps)
        sort_indices = np.argsort(raw_ts)
        sorted_ts = raw_ts[sort_indices]
        sorted_vals = raw_vals[sort_indices]

        # 4. Hard-slice to the exact requested window boundaries
        # np.searchsorted is a highly optimized binary search
        start_idx = np.searchsorted(sorted_ts, start_ts, side='left')
        end_idx = np.searchsorted(sorted_ts, end_ts, side='right')

        final_ts = sorted_ts[start_idx:end_idx]
        final_vals = sorted_vals[start_idx:end_idx]

        return final_ts, final_vals

    @staticmethod
    def downsample_minmax(times, values, target_points=2000):
        """
        Dynamically decimates a 1D array while perfectly preserving visual noise spikes
        (Min/Max envelope). Ideal for rendering millions of points to a fixed-width screen.
        """
        n_points = len(values)

        # If we have less points than the screen width, just return the raw data
        if n_points <= target_points:
            return times, values

        # Calculate chunk size (e.g., if we have 1,000,000 points and want 2,000, chunk size is 500)
        # We divide by 2 because each chunk yields TWO points (a min and a max)
        chunk_size = max(1, n_points // (target_points // 2))

        # Truncate arrays so they divide evenly into the chunk size
        n_chunks = n_points // chunk_size
        n_usable = n_chunks * chunk_size

        data_view = values[:n_usable].reshape(n_chunks, chunk_size)
        time_view = times[:n_usable].reshape(n_chunks, chunk_size)

        # Suppress warnings if a chunk is entirely NaNs
        with np.errstate(invalid='ignore'):
            mins = np.nanmin(data_view, axis=1)
            maxs = np.nanmax(data_view, axis=1)

        # Interleave the mins and maxs
        final_values = np.empty(n_chunks * 2, dtype=values.dtype)
        final_values[0::2] = mins
        final_values[1::2] = maxs

        # Interleave the times (start of chunk for min, end of chunk for max)
        final_times = np.empty(n_chunks * 2, dtype=times.dtype)
        final_times[0::2] = time_view[:, 0]
        final_times[1::2] = time_view[:, -1]

        return final_times, final_values

    def get_global_bounds(self):
        """Returns the absolute earliest and latest timestamps in the log directory."""
        if not self.manifest:
            return None, None
        return self.manifest[0]['start'], self.manifest[-1]['end']