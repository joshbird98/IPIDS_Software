import numpy as np
import re


class RingBufferHistory:
    def __init__(self, plc_interface, sample_rate_hz=50):
        """
        Pre-allocates memory for the entire session.
        channel_names: List of raw tag strings (e.g. ["system.voltages.v1", ...])
        """
        self.plc = plc_interface
        self.channel_names = list(self.plc.tags.keys())
        # 1. Calculate Size
        max_duration_hours = 1.0
        self.max_points = int(max_duration_hours * 3600 * sample_rate_hz)
        self.ptr = 0
        self.is_full = False
        self.last_update_time = 0.0

        # 2. Pre-allocate Arrays
        self.timestamps = np.zeros(self.max_points, dtype=np.float64)

        self.data_buffers = {}
        for name in self.channel_names:
            # Initialize with NaN so we can distinguish "0.0" from "Missing Data"
            self.data_buffers[name] = np.full(self.max_points, np.nan, dtype=np.float64)

        # Optimization: cache the keys list for the update loop
        self.keys = list(self.channel_names)
        self.time_pattern = re.compile(r'(\d+):(\d+):(\d+):([\d\.]+)')

    def _log_msg(self, msg):
        """Helper to print to console and GUI if available"""
        print(msg)
        if self.plc and hasattr(self.plc, 'gui_messages'):
            self.plc.gui_messages.append(msg)

    def _safe_convert(self, val):
        if val is None: return np.nan
        if isinstance(val, bool): return 1.0 if val else 0.0
        if isinstance(val, (int, float)): return float(val)
        if isinstance(val, str):
            try:
                return float(val)
            except ValueError:
                pass
            match = self.time_pattern.match(val)
            if match:
                d, h, m, s = match.groups()
                total_seconds = (int(d) * 86400) + (int(h) * 3600) + (int(m) * 60) + float(s)
                return total_seconds
        return np.nan

    def _insert_gap(self, approximate_timestamp):

        self.timestamps[self.ptr] = approximate_timestamp
        for key in self.keys:
            self.data_buffers[key][self.ptr] = np.nan

        # Advance pointer
        self.ptr += 1
        if self.ptr >= self.max_points:
            self.ptr = 0
            self.is_full = True

    def update(self, timestamp, tags_dict):

        if getattr(self, 'is_loading_history', False):
            return

        GAP_THRESHOLD = 0.5  # Recommended: increase safety margin

        # If we missed data for > 2 seconds, break the line
        if self.last_update_time > 0 and (timestamp - self.last_update_time) > GAP_THRESHOLD:
            #print(f"   ⚠️ INSERTING GAP! Delta was {timestamp - self.last_update_time:.4f}s")
            gap_time = self.last_update_time + 0.001
            self._insert_gap(gap_time)

        self.last_update_time = timestamp
        # -------------------------------

        # 1. Update Time
        self.timestamps[self.ptr] = timestamp

        # 2. Update Data
        # We iterate over the channels we decided to track (which is now ALL of them)
        for name in self.keys:
            tag_data = tags_dict.get(name)
            val = np.nan
            if tag_data:
                val = self._safe_convert(tag_data.get('value'))

            self.data_buffers[name][self.ptr] = val

        # 3. Increment Pointer
        self.ptr += 1
        if self.ptr >= self.max_points:
            self.ptr = 0
            self.is_full = True

    def get_current_view(self, start_time=None):
        """
        Returns (times, slices) for the plotting loop.
        """
        # 1. Identify Valid Region
        if not self.is_full:
            # Data is from 0 to ptr
            if self.ptr == 0: return np.array([]), []
            raw_slices = [slice(0, self.ptr)]
            times = self.timestamps[raw_slices[0]]
        else:
            # Wrapped: Oldest [ptr:] ... Newest [:ptr]
            raw_slices = [
                slice(self.ptr, self.max_points),
                slice(0, self.ptr)
            ]
            times = np.concatenate((
                self.timestamps[raw_slices[0]],
                self.timestamps[raw_slices[1]]
            ))

        # 2. Filter out Zeros (Startup artifacts)
        # If the buffer has 0.0 timestamps, it ruins the zoom.
        valid_mask = times > 1000000  # Filter for valid epoch timestamps (> year 1970)


        # 3. Apply Zoom (Time Window)
        if start_time is not None:
            # Find index in the LOGICAL array
            start_idx = np.searchsorted(times, start_time)

            # Optimization: If requested time is ahead of all data, return empty
            if start_idx >= len(times):
                return np.array([]), []

            if start_idx > 0:
                start_idx -= 1  # Include one point before for continuity

            # Adjust the Logical View
            times = times[start_idx:]

            # Adjust the Physical Slices
            final_slices = []
            remaining_skip = start_idx

            for s in raw_slices:
                length = s.stop - s.start
                if remaining_skip >= length:
                    remaining_skip -= length
                    continue  # This entire slice is skipped
                else:
                    # Partial slice
                    new_slice = slice(s.start + remaining_skip, s.stop)
                    final_slices.append(new_slice)
                    remaining_skip = 0  # Done skipping

            return times, final_slices

        return times, raw_slices

    def load_log_file(self, log_directory="SystemLogsBin", keep_alive_func=None):
        import os
        import numpy as np
        from datetime import datetime, timedelta
        import gc

        self.is_loading_history = True

        try:
            current_dir = os.path.dirname(os.path.abspath(__file__))
            project_root = os.path.dirname(current_dir)
            now = datetime.now()

            # 1. Find Binary Files (Today and Yesterday)
            files_to_load = []
            for days_back in [0, 1]:
                d = now - timedelta(days=days_back)
                fname = os.path.join(project_root, log_directory, f"hmi_data_{d.strftime('%Y-%m-%d')}.npz")
                if os.path.exists(fname):
                    files_to_load.append(fname)

            if not files_to_load:
                print("⚠️ No binary logs found.")
                return False

            files_to_load.sort()

            total_restored = 0

            # 2. Load Directly to RAM
            for filepath in files_to_load:
                try:
                    print(f"⚡ Loading binary: {os.path.basename(filepath)}")

                    with np.load(filepath, allow_pickle=True) as data:
                        # Extract arrays
                        file_ts = data['timestamps']
                        file_vals = data['values']  # Shape (N, Columns)
                        file_keys = data['keys']  # Shape (Columns,)

                    if len(file_ts) == 0: continue

                    # 3. Filter Corrupt Timestamps (Just in case)
                    valid_mask = (file_ts > 1000000000) & np.isfinite(file_ts)
                    file_ts = file_ts[valid_mask]
                    file_vals = file_vals[valid_mask]

                    # 4. Map Columns
                    # Create map: { "voltage": 0, "current": 1 }
                    # We handle the case where the file has different keys than the current system
                    file_key_map = {k: i for i, k in enumerate(file_keys)}

                    # 5. Write to RingBuffer
                    count = len(file_ts)
                    limit = min(count, self.max_points - self.ptr)

                    # A. Timestamps
                    self.timestamps[self.ptr: self.ptr + limit] = file_ts[:limit]

                    # B. Values
                    for sys_key in self.keys:
                        if sys_key in file_key_map:
                            col_idx = file_key_map[sys_key]
                            # Direct Numpy Copy (Fastest possible method)
                            self.data_buffers[sys_key][self.ptr: self.ptr + limit] = file_vals[:limit, col_idx]
                        else:
                            # Key missing in file -> NaN
                            self.data_buffers[sys_key][self.ptr: self.ptr + limit] = np.nan

                    self.ptr += limit
                    total_restored += limit

                    # Cleanup large temp arrays immediately
                    del file_ts, file_vals, file_keys
                    gc.collect()

                    # Buffer full check
                    if self.ptr >= self.max_points:
                        self.ptr = 0
                        self.is_full = True
                        break  # Stop loading if buffer is full

                except Exception as e:
                    print(f"⚠️ Corrupt Log Found ({filepath}): {e}")

                    # FIX: Auto-delete bad files so we don't crash next time
                    try:
                        os.remove(filepath)
                        print(f"   🗑️ Deleted corrupt file. Starting fresh.")
                    except:
                        pass
                    continue  # Skip to next file

            # 6. Gap Logic (To separate history from live data)
            if self.last_update_time == 0 and self.ptr > 0:
                self.last_update_time = self.timestamps[self.ptr - 1]

            if (datetime.now().timestamp() - self.last_update_time) > 5.0:
                self._insert_gap(self.last_update_time + 0.001)

            print(f"✅ History Ready: {total_restored} points.")
            return True

        finally:
            self.is_loading_history = False

    def get_snapshot(self):
        times_view, slices = self.get_current_view()
        times_list = times_view.tolist()
        snapshot_data = {}
        for tag, buffer_array in self.data_buffers.items():
            pieces = [buffer_array[s] for s in slices]
            full_array = np.concatenate(pieces)
            snapshot_data[tag] = full_array.tolist()
        return times_list, snapshot_data

    def absorb_burst_data(self, burst_timestamps, burst_data_dict):
        """
        Injects a block of high-speed data into the RingBuffer.
        This allows faults (1ms resolution) to coexist with normal data (5s resolution).
        """
        # 1. Safety Locks
        if getattr(self, 'is_loading_history', False): return

        count = len(burst_timestamps)
        if count == 0: return

        # 2. Bulk Write (Much faster than looping)
        # We need to handle the case where the burst wraps around the end of the buffer

        # Calculate start and end indices
        start_idx = self.ptr
        end_idx = start_idx + count

        if end_idx <= self.max_points:
            # Case A: Fits without wrapping
            self.timestamps[start_idx: end_idx] = burst_timestamps

            for key, values in burst_data_dict.items():
                if key in self.data_buffers:
                    self.data_buffers[key][start_idx: end_idx] = values
        else:
            # Case B: Wraps around
            # Part 1: Fill to end
            remaining = self.max_points - start_idx
            self.timestamps[start_idx:] = burst_timestamps[:remaining]

            for key, values in burst_data_dict.items():
                if key in self.data_buffers:
                    self.data_buffers[key][start_idx:] = values[:remaining]

            # Part 2: Continue from start
            leftover = count - remaining
            self.timestamps[:leftover] = burst_timestamps[remaining:]

            for key, values in burst_data_dict.items():
                if key in self.data_buffers:
                    self.data_buffers[key][:leftover] = values[remaining:]

            self.is_full = True  # We definitely wrapped

        # 3. Advance Pointer
        self.ptr = (self.ptr + count) % self.max_points
        self.last_update_time = burst_timestamps[-1]