import os
import numpy as np
import threading
import queue
import time
from datetime import datetime


class UnifiedDailyLogger:
    def __init__(self, plc_interface, log_dir_name="SystemLogsBin"):
        """
        BINARY HYBRID LOGGER:
        - Normal Operation: Saves snapshot to disk every 5 minutes (Efficient).
        - Fault Operation: Captures high-speed bursts and enables High-Res recovery mode.
        """
        self.plc = plc_interface

        # 1. Setup Path
        base_path = os.getcwd()
        if "utils" in base_path:
            base_path = os.path.dirname(base_path)

        self.log_dir = os.path.join(base_path, log_dir_name)
        os.makedirs(self.log_dir, exist_ok=True)

        # 2. State Tracking
        self.last_save_time = time.time()
        self.save_interval = 300  # Auto-save RAM to Disk every 5 minutes

        # 3. High Speed Mode Tracker
        self.high_speed_until = 0.0  # Timestamp when high-res mode ends

        # 4. Background Thread (Handles heavy disk I/O)
        self.write_queue = queue.Queue()
        self.running = True
        self.worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
        self.worker_thread.start()

    # --- MAIN INTERFACE (Called by PLC Loop) ---

    @property
    def is_high_speed_requested(self):
        """
        Returns TRUE if we are in the 5-minute post-fault recovery window.
        Your Main Loop should check this to decide sampling rate.
        """
        return time.time() < self.high_speed_until

    def log_snapshot(self, data_dict):
        """
        Periodic check.
        1. Checks if it's time to dump RAM to Disk (Auto-Save).
        2. Does NOT record data itself (The RingBuffer handles that).
        """
        if not self.running: return

        # Auto-Save RAM to Disk every 5 minutes
        # (This persists whatever data is currently in the buffer)
        if time.time() - self.last_save_time > self.save_interval:
            self.trigger_save()
            self.last_save_time = time.time()

    def log_high_speed_burst(self, timestamps=None, data_store=None):
        """
        CRITICAL EVENT TRIGGER:
        1. (Optional) Injects external burst data.
        2. Activates 'High Speed Mode' for the next 5 minutes.
        3. Triggers an immediate disk save.
        """
        if not self.running: return

        # A. Inject Burst (Only if provided)
        # If we are just triggering a save of current history, we skip this.
        if timestamps is not None and data_store is not None:
            try:
                self.plc.history.absorb_burst_data(timestamps, data_store)
            except Exception as e:
                print(f"Error merging burst data: {e}")

        # B. Activate High-Res Recovery Mode (5 Minutes)
        self.high_speed_until = time.time() + 300.0
        print("🚨 FAULT LOGGED: Switching to High-Res Mode for 5 minutes.")

        # C. Force Immediate Save to Disk
        # The worker thread will read the history directly, so we don't need to pass it.
        self.trigger_save()

    def trigger_save(self):
        """Manually force a disk dump"""
        if self.running:
            self.write_queue.put("SAVE")

    def close(self):
        """Safe Shutdown"""
        self.running = False
        self.write_queue.put("CLOSE")
        if self.worker_thread.is_alive():
            self.worker_thread.join(timeout=2.0)

    # --- BACKGROUND WORKER (Disk I/O) ---

    def _worker_loop(self):
        while self.running:
            try:
                task = self.write_queue.get(timeout=1.0)

                if task == "SAVE":
                    self._do_save_binary()
                elif task == "CLOSE":
                    self._do_save_binary()
                    break

                self.write_queue.task_done()
            except queue.Empty:
                continue
            except Exception as e:
                print(f"Logger Thread Error: {e}")

    def _do_save_binary(self):
        """
        ATOMIC SAVE (Fixed for Numpy Extension Behavior):
        1. Save to '..._temp.npz' (Satisfies Numpy)
        2. Rename to '....npz' (Atomic Replace)
        """
        try:
            hist = self.plc.history
            if hist.ptr == 0 and not hist.is_full: return

            # 1. Setup Absolute Paths
            now = datetime.now()
            abs_log_dir = os.path.abspath(self.log_dir)

            # Ensure directory exists (just in case)
            if not os.path.exists(abs_log_dir):
                os.makedirs(abs_log_dir, exist_ok=True)

            base_name = f"hmi_data_{now.strftime('%Y-%m-%d')}"
            final_path = os.path.join(abs_log_dir, base_name + ".npz")

            # FIX: Use .npz extension for temp file too, so Numpy doesn't add it double
            temp_path = os.path.join(abs_log_dir, base_name + "_temp.npz")

            # 2. Prepare Data
            keys = sorted(list(hist.data_buffers.keys()))
            if hist.is_full:
                ts = np.roll(hist.timestamps, -hist.ptr)
                n_points = hist.max_points
                vals = np.zeros((n_points, len(keys)), dtype=np.float32)
                for i, key in enumerate(keys):
                    vals[:, i] = np.roll(hist.data_buffers[key], -hist.ptr)
            else:
                n_points = hist.ptr
                ts = hist.timestamps[:n_points]
                vals = np.zeros((n_points, len(keys)), dtype=np.float32)
                for i, key in enumerate(keys):
                    vals[:, i] = hist.data_buffers[key][:n_points]

            # 3. Write Temp File
            np.savez_compressed(temp_path, timestamps=ts, values=vals, keys=keys)

            # Verify creation
            if not os.path.exists(temp_path):
                print(f"❌ Error: Temp file missing: {temp_path}")
                return

            # 4. Atomic Rename
            import time
            max_retries = 3
            for i in range(max_retries):
                try:
                    if os.path.exists(final_path):
                        os.remove(final_path)
                    os.rename(temp_path, final_path)
                    break
                except OSError:
                    if i == max_retries - 1: raise
                    time.sleep(0.1)

        except Exception as e:
            print(f"❌ Error saving binary log: {e}")
            # Cleanup
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except:
                    pass