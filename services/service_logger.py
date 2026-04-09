# services/service_logger.py
import zmq
import json
import time
import os
import queue
import threading
import numpy as np
from datetime import datetime

from network_config import ZMQ_PORT_PLC_PUB, ZMQ_PORT_VACUUM_PUB, ZMQ_PORT_LOGGER_CMD, TOPIC_PLC_DATA, TOPIC_VACUUM_DATA
from hmi_config import DEFAULT_LOG_DIRECTORY


class LoggerMicroservice:
    def __init__(self):
        # 1. ZMQ Setup
        self.context = zmq.Context()

        self.sub_socket = self.context.socket(zmq.SUB)
        self.sub_socket.connect(ZMQ_PORT_PLC_PUB)
        self.sub_socket.connect(ZMQ_PORT_VACUUM_PUB)

        self.sub_socket.setsockopt(zmq.SUBSCRIBE, TOPIC_PLC_DATA)
        self.sub_socket.setsockopt(zmq.SUBSCRIBE, TOPIC_VACUUM_DATA)

        self.current_state = {}

        self.cmd_socket = self.context.socket(zmq.PULL)
        self.cmd_socket.bind(ZMQ_PORT_LOGGER_CMD)

        self.poller = zmq.Poller()
        self.poller.register(self.sub_socket, zmq.POLLIN)
        self.poller.register(self.cmd_socket, zmq.POLLIN)

        # 2. Disk I/O Background Thread
        os.makedirs(DEFAULT_LOG_DIRECTORY, exist_ok=True)
        self.write_queue = queue.Queue()
        self.running = True
        self.io_thread = threading.Thread(target=self._disk_writer_loop, daemon=True)
        self.io_thread.start()

        # 3. Buffer Configurations (Deterministic)
        self.keys = self._generate_deterministic_keys()
        self.num_keys = len(self.keys)

        self.vacuum_hw_map = self._build_vacuum_hw_map()

        # High Res: 10Hz for 5 mins = 3000 points
        self.high_res_max = 3000
        self.high_res_ptr = 0
        self.high_res_full = False
        self.high_res_buffer = np.zeros((self.high_res_max, self.num_keys), dtype=np.float32)
        self.high_res_ts = np.zeros(self.high_res_max, dtype=np.float64)

        # Low Res: 0.5Hz for 24 hours = 43200 points
        self.low_res_max = 43200
        self.low_res_ptr = 0
        self.low_res_full = False
        self.low_res_buffer = np.zeros((self.low_res_max, self.num_keys), dtype=np.float32)
        self.low_res_ts = np.zeros(self.low_res_max, dtype=np.float64)

        # Timing state
        self.last_low_res_log = 0.0
        self.low_res_interval = 2.0  # Seconds
        self.last_disk_flush = time.time()
        self.flush_interval = 15.0  # 5 minutes

        # Burst State Machine
        self.burst_active = False
        self.burst_end_time = 0.0
        self.post_fault_duration = 10.0  # 5 minutes of post-fault data
        self.burst_id = ""

        self.burst_new_ts = []
        self.burst_new_data = []
        self.burst_metadata = []

        # --- Internal Clock ---
        self.tick_rate = 0.100  # 10Hz target
        self.next_tick = time.time() + self.tick_rate

    def _build_vacuum_hw_map(self) -> dict:
        """Creates a lookup table mapping (Node, Channel) -> Registry Base Path."""
        current_dir = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.dirname(current_dir)
        registry_path = os.path.join(project_root, "system_tags.json")

        hw_map = {}
        try:
            with open(registry_path, "r") as f:
                registry = json.load(f)

            for full_tag, metadata in registry.items():
                if metadata.get("source") == "service_vacuum":
                    node = str(metadata.get("hw_node"))
                    ch = str(metadata.get("hw_channel"))

                    if node and ch and node != "None" and ch != "None":
                        # full_tag is e.g. "vacuum.controller_10.vg1_source.pressure"
                        # We slice off the ".pressure" to get the base path
                        base_path = full_tag.rsplit('.', 1)[0]
                        hw_map[(node, ch)] = base_path
        except Exception as e:
            print(f"[Logger] Failed to build HW map: {e}")

        return hw_map

    def _generate_deterministic_keys(self) -> list:
        """Loads keys from the central registry to lock array sizes."""
        # Dynamically locate the project root (one directory up from /services)
        current_dir = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.dirname(current_dir)
        registry_path = os.path.join(project_root, "system_tags.json")

        try:
            with open(registry_path, "r") as f:
                registry = json.load(f)
                # Sort them alphabetically to guarantee array index matching across services
                return sorted(list(registry.keys()))
        except FileNotFoundError:
            print(f"CRITICAL: Registry not found at {registry_path}. Run build_registry.py first.")
            return []

    def _flatten_vacuum_data(self, data: dict) -> dict:
        """Maps incoming hardware data to the registry using pure hardware addresses."""
        flat_data = {}

        STATUS_MAP = {
            "OK": 0.0, "NO-SEN": 1.0, "RANGE?": 2.0, "S-OFF": 3.0,
            "ERROR-H": 4.0, "ERROR-L": 5.0, "ERROR-S": 6.0
        }

        for node_id_str, node_data in data.items():
            for ch_id_str, ch_data in node_data.get("channels", {}).items():

                # 1. Ask the lookup table what the exact string is supposed to be
                base_path = self.vacuum_hw_map.get((node_id_str, ch_id_str))

                if not base_path:
                    continue  # If it's not in the registry, ignore it

                # 2. Map Pressure
                p = ch_data.get("pressure")
                if p is not None:
                    flat_data[f"{base_path}.pressure"] = float(p)

                # 3. Map Status
                s = ch_data.get("status")
                if s is not None:
                    clean_status = str(s).strip().upper()
                    flat_data[f"{base_path}.status"] = STATUS_MAP.get(clean_status, -1.0)

        return flat_data

    def _update_state_cache(self, topic: bytes, payload_bytes: bytes):
        """Routes payload processing based on topic and merges to RAM cache."""
        data = json.loads(payload_bytes.decode('utf-8'))

        if topic == TOPIC_VACUUM_DATA:
            self.current_state.update(self._flatten_vacuum_data(data))
        else:
            self.current_state.update(data)

    def _log_current_state(self):
        """Writes the current RAM cache to Numpy arrays."""
        current_ts = time.time()
        row = np.array([float(self.current_state.get(k, 0.0)) for k in self.keys], dtype=np.float32)

        # 1. Update High-Res Ring Buffer (10Hz)
        self.high_res_ts[self.high_res_ptr] = current_ts
        self.high_res_buffer[self.high_res_ptr, :] = row

        self.high_res_ptr += 1
        if self.high_res_ptr >= self.high_res_max:
            self.high_res_ptr = 0
            self.high_res_full = True

        # 2. Update Low-Res Buffer (2s interval)
        # ---> UN-INDENTED THIS ENTIRE BLOCK <---
        if current_ts - self.last_low_res_log >= self.low_res_interval:
            self.low_res_ts[self.low_res_ptr] = current_ts
            self.low_res_buffer[self.low_res_ptr, :] = row

            self.low_res_ptr += 1
            if self.low_res_ptr >= self.low_res_max:
                self.low_res_ptr = 0
                self.low_res_full = True

            self.last_low_res_log = current_ts

        # 3. Periodic Disk Flush of Low-Res Data
        if current_ts - self.last_disk_flush >= self.flush_interval:
            self._queue_low_res_flush()
            self.last_disk_flush = current_ts

        # 4. Handle Active Burst Accumulation
        if self.burst_active:
            self.burst_new_ts.append(current_ts)
            self.burst_new_data.append(row)

            if current_ts > self.burst_end_time:
                self._finalize_burst()

    def _queue_low_res_flush(self):
        """Extracts valid low-res data and sends to I/O thread."""
        if not self.low_res_full and self.low_res_ptr == 0:
            return

        filename = f"daily_trend_{datetime.now().strftime('%Y%m%d')}.npz"
        filepath = os.path.join(DEFAULT_LOG_DIRECTORY, filename)

        if self.low_res_full:
            # Unravel the circular buffer so time goes strictly from old -> new
            ts_copy = np.roll(self.low_res_ts, -self.low_res_ptr)
            data_copy = np.roll(self.low_res_buffer, -self.low_res_ptr, axis=0)
        else:
            ts_copy = self.low_res_ts[:self.low_res_ptr].copy()
            data_copy = self.low_res_buffer[:self.low_res_ptr, :].copy()

        self.write_queue.put(('SAVE', filepath, ts_copy, data_copy, self.keys))

    def _trigger_burst(self, metadata=None):
        if metadata is None: metadata = {}
        reason = metadata.get("reason", "Unknown Trigger")
        current_time = time.time()

        # 1. Reset the countdown timer & store the reason
        self.burst_end_time = current_time + self.post_fault_duration
        self.burst_metadata.append(metadata)

        # 2. First Trigger Logic: Freeze history and save PRE file immediately
        if not self.burst_active:
            print(f"[Logger] Burst started. Reason: {reason}")
            self.burst_active = True

            # Generate a unique ID (down to the millisecond) to link PRE and POST files
            self.burst_id = datetime.now().strftime('%Y%m%d_%H%M%S_%f')[:-3]

            # Extract Pre-history
            if self.high_res_full:
                pre_ts = np.roll(self.high_res_ts, -self.high_res_ptr)
                pre_data = np.roll(self.high_res_buffer, -self.high_res_ptr, axis=0)
            else:
                pre_ts = self.high_res_ts[:self.high_res_ptr].copy()
                pre_data = self.high_res_buffer[:self.high_res_ptr, :].copy()

            # Clear accumulators for the storm
            self.burst_new_ts = []
            self.burst_new_data = []

            # --- BOOKEND PART 1: Save PRE file ---
            filepath_pre = os.path.join(DEFAULT_LOG_DIRECTORY, f"burst_{self.burst_id}_PRE.npz")
            meta_pre = json.dumps([metadata])  # Record the initial trigger
            self.write_queue.put(('SAVE_BURST', filepath_pre, pre_ts, pre_data, self.keys, meta_pre))

        else:
            print(f"[Logger] Burst extended. Additional reason: {reason}")

    def _finalize_burst(self):
        print(f"[Logger] Burst window closed. Saving POST data to disk...")
        self.burst_active = False

        # Convert accumulated lists to Numpy arrays
        post_ts = np.array(self.burst_new_ts, dtype=np.float64)
        post_data = np.array(self.burst_new_data, dtype=np.float32)

        # Package all triggers that happened during this storm
        meta_post = json.dumps(self.burst_metadata)

        # --- BOOKEND PART 2: Save POST file ---
        # Notice we use the EXACT SAME self.burst_id so the files match
        filepath_post = os.path.join(DEFAULT_LOG_DIRECTORY, f"burst_{self.burst_id}_POST.npz")
        self.write_queue.put(('SAVE_BURST', filepath_post, post_ts, post_data, self.keys, meta_post))

        # Clear RAM
        self.burst_new_ts = []
        self.burst_new_data = []
        self.burst_metadata = []

    def _disk_writer_loop(self):
        while self.running:
            try:
                # 1. Try to get a task from the queue
                task = self.write_queue.get(timeout=1.0)
                cmd = task[0]

                # 2. Attempt the specific disk I/O operations
                try:
                    if cmd == 'SAVE':
                        # Standard daily trend save
                        _, filepath, ts, data, keys = task
                        temp_path = filepath.replace(".npz", "_temp.npz")
                        np.savez_compressed(temp_path, timestamps=ts, values=data, keys=keys)
                        LoggerMicroservice._atomic_rename(temp_path, filepath)
                        print("")
                        print(f"[Logger] Successfully saved trend: {os.path.basename(filepath)}")

                    elif cmd == 'SAVE_BURST':
                        # Burst save (PRE or POST)
                        _, filepath, ts, data, keys, meta_str = task
                        temp_path = filepath.replace(".npz", "_temp.npz")
                        np.savez_compressed(temp_path, timestamps=ts, values=data, keys=keys, metadata=np.array(meta_str))
                        LoggerMicroservice._atomic_rename(temp_path, filepath)
                        print(f"[Logger] Successfully saved burst: {os.path.basename(filepath)}")

                except Exception as io_err:
                    # If the write fails, safely extract the filepath to see exactly what broke
                    failed_file = task[1] if len(task) > 1 else "Unknown File"
                    print(f"[Logger I/O Error] Failed to write {os.path.basename(failed_file)}: {io_err}")

                finally:
                    # No matter what happens (success or crash), tell the queue we are done
                    # with this task so the thread doesn't deadlock.
                    self.write_queue.task_done()

            except queue.Empty:
                # Normal timeout, just loop again
                continue
            except Exception as e:
                # Catch-all for any weird queue or tuple unpacking errors
                print(f"[Logger Loop Error] Critical worker failure: {e}")

    @staticmethod
    def _atomic_rename(temp_path, final_path):
        if os.path.exists(temp_path):
            if os.path.exists(final_path):
                os.remove(final_path)
            os.rename(temp_path, final_path)

    def run(self):
        print("[Logger] Service started.")
        while True:
            try:
                # Calculate time remaining until the next 10Hz tick
                current_time = time.time()
                time_to_next_tick = max(0.0, self.next_tick - current_time)
                timeout_ms = int(time_to_next_tick * 1000)

                socks = dict(self.poller.poll(timeout_ms))

                # 1. Process Network Data (Updates the cache instantly)
                if self.sub_socket in socks and socks[self.sub_socket] == zmq.POLLIN:
                    topic, payload = self.sub_socket.recv_multipart()
                    self._update_state_cache(topic, payload)

                # 2. Process Command Stream (Triggers)
                if self.cmd_socket in socks and socks[self.cmd_socket] == zmq.POLLIN:
                    cmd_msg = self.cmd_socket.recv_json()
                    if cmd_msg.get("action") == "TRIGGER_BURST":
                        self._trigger_burst(cmd_msg)

                # 3. Process the Internal Clock Tick
                current_time = time.time()
                if current_time >= self.next_tick:
                    self._log_current_state()
                    self.next_tick = current_time + self.tick_rate
                    print(".", end="", flush=True)

            except KeyboardInterrupt:
                break
            except Exception as e:
                print(f"[Logger Loop Error] {e}")

        # Shutdown sequence
        self.running = False
        self._queue_low_res_flush()
        self.io_thread.join(timeout=2.0)
        print("[Logger] Shutdown complete.")


if __name__ == "__main__":
    svc = LoggerMicroservice()
    svc.run()