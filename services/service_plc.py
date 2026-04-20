import time
import os
import zmq
import json
import ctypes
import snap7
from snap7.util import *
from snap7.type import S7DataItem, Area, WordLen
from typing import Dict, Any

from network_config import (
    ZMQ_PORT_PLC_PUB, ZMQ_PORT_PLC_CMD, TOPIC_PLC_DATA, TOPIC_PLC_FAULTS,
    PLC_IP, PLC_RACK, PLC_SLOT, DB_INTERFACE_NUM, DB_RETAIN_NUM
)

POLL_INTERVAL = 0.016  # Gives ~50Hz cycle
HEARTBEAT_INTERVAL = 1.0  # 1Hz Watchdog

MAX_CMD_AGE = 0.2  # 200ms expiration


class PlcMicroservice:
    def __init__(self):
        self.context = zmq.Context()

        # --- ZMQ Setup ---
        self.pub_socket = self.context.socket(zmq.PUB)
        self.pub_socket.bind(ZMQ_PORT_PLC_PUB)

        self.sub_socket = self.context.socket(zmq.SUB)
        self.sub_socket.setsockopt(zmq.RCVHWM, 5)  # Drop inbound messages if buffer exceeds 5
        self.sub_socket.bind(ZMQ_PORT_PLC_CMD)
        self.sub_socket.setsockopt_string(zmq.SUBSCRIBE, "")

        # --- Snap7 Setup ---
        self.client = snap7.client.Client()
        self.connected = False
        self.watchdog_state = False
        self.last_heartbeat = time.time()
        self.plc_cycle_count = 0

        # --- Tag Registry ---
        # Note: In production, load this dictionary from your build_registry.py JSON output.
        # Format: "tag_name": {"db": int, "offset": float, "type": str, "writable": bool}
        self.tags = self._load_tag_registry()

        # Calculate block sizes dynamically to minimize network payload
        self.db_interface_size = self._calculate_db_size(DB_INTERFACE_NUM)
        self.db_retain_size = self._calculate_db_size(DB_RETAIN_NUM)

        self.last_retain_read = 0.0
        self.raw_retain_cache = bytearray(self.db_retain_size)

        self.state: Dict[str, Any] = {
            "system": {
                "connected": False,
                "cpu_state": "UNKNOWN"
            },
            "data": {},
            "faults": {}
        }

    def _load_tag_registry(self) -> dict:
        current_dir = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.dirname(current_dir)
        registry_path = os.path.join(project_root, "system_tags.json")
        try:
            with open(registry_path, "r") as f:
                all_tags = json.load(f)

            plc_tags = {}
            for tag, meta in all_tags.items():
                # STRICT FILTER: Only load tags meant for this specific microservice
                if meta.get("source") == "service_plc_snap7":
                    plc_tags[tag] = meta

            print(f"[PLC Service] Loaded {len(plc_tags)} tags for Snap7 monitoring.")
            return plc_tags

        except FileNotFoundError:
            print("[PLC Service] WARNING: system_tags.json not found. Operating blind.")
            return {}

    def _calculate_db_size(self, db_num: int) -> int:
        """Calculate exact DB size needed based on the highest offset and its datatype."""
        max_byte = 0

        # Siemens data type byte sizes
        type_sizes = {
            "BOOL": 1,
            "INT": 2,
            "UINT": 2,
            "DINT": 4,
            "UDINT": 4,
            "REAL": 4,
            "DWORD": 4,
            "TIME": 4
        }

        for tag, meta in self.tags.items():
            if meta.get("db_number") == db_num:
                offset = meta.get("byte_offset", 0)
                dtype = meta.get("datatype", "UNKNOWN")

                # Calculate the final byte this specific tag occupies
                tag_end = offset + type_sizes.get(dtype, 2)

                if tag_end > max_byte:
                    max_byte = tag_end

        # Siemens ALWAYS pads Data Blocks to an even number of bytes (Word alignment)
        if max_byte % 2 != 0:
            max_byte += 1

        return max_byte

    def _connect_plc(self):
        try:
            if self.client.get_connected():
                self.client.disconnect()

            self.client.connect(PLC_IP, PLC_RACK, PLC_SLOT)
            self.connected = True
            self.state["system"]["connected"] = True
            print(f"[PLC Service] Connected to S7-1200 at {PLC_IP}")
        except Exception as e:
            self.connected = False
            self.state["system"]["connected"] = False
            self.state["system"]["cpu_state"] = "DISCONNECTED"
            print(f"[PLC Service] Connection failed: {e}")

    def _toggle_watchdog(self):
        """Inverts the Watchdog bit to prevent the PLC from executing comms-loss failsafes."""
        watchdog_tag = "ion_beam.system.plc_watchdog"  # Update string to match your registry
        if watchdog_tag not in self.tags:
            return

        self.watchdog_state = not self.watchdog_state
        self._write_tag(watchdog_tag, self.watchdog_state)

    def _parse_bytearray(self, data: bytearray, byte_idx: int, bit_idx: int, dtype: str):
        """Extracts native Python types from the raw Siemens bytearray."""
        if dtype == "BOOL":
            return get_bool(data, byte_idx, bit_idx)
        elif dtype == "INT":
            return get_int(data, byte_idx)
        elif dtype == "DINT":
            return get_dint(data, byte_idx)
        elif dtype == "REAL":
            return get_real(data, byte_idx)
        elif dtype == "DWORD":
            return get_dword(data, byte_idx)
        return None

    def _read_and_parse_dbs(self):
        """Executes sequential reads, caching the Retain DB to save network bandwidth."""
        try:
            current_time = time.time()

            # 1. Interface Read (Every Cycle)
            raw_interface = self.client.db_read(DB_INTERFACE_NUM, 0, self.db_interface_size)

            # 2. Retain Read (Only at 1Hz)
            if current_time - self.last_retain_read >= 1.0:
                self.raw_retain_cache = self.client.db_read(DB_RETAIN_NUM, 0, self.db_retain_size)
                self.last_retain_read = current_time

            raw_retain = self.raw_retain_cache

            # 3. Parse Data
            for tag, meta in self.tags.items():
                db_num = meta.get("db_number")

                if db_num not in [DB_INTERFACE_NUM, DB_RETAIN_NUM]:
                    continue

                db_target = raw_interface if db_num == DB_INTERFACE_NUM else raw_retain

                val = self._parse_bytearray(
                    db_target,
                    meta.get("byte_offset", 0),
                    meta.get("bit_offset", 0),
                    meta.get("datatype", "UNKNOWN")
                )

                if db_num == DB_RETAIN_NUM and "fault" in tag.lower():
                    self.state["faults"][tag] = val
                else:
                    self.state["data"][tag] = val

            # 4. CPU State Evaluation
            current_count = self.state["data"].get("ion_beam.system.plc_cycle_count")

            if current_count is not None:
                if current_count != self.plc_cycle_count:
                    self.state["system"]["cpu_state"] = "RUN"
                else:
                    self.state["system"]["cpu_state"] = "STOP"
                self.plc_cycle_count = current_count

        except Exception as e:
            print(f"[PLC Service] Cycle aborted due to error: {e}")
            self.state["system"]["cpu_state"] = "DISCONNECTED"
            self.connected = False

    def _write_tag(self, tag: str, new_value: Any) -> bool:
        """Executes a targeted byte-write to the PLC."""
        if tag not in self.tags:
            print(f"[PLC Service] Tag '{tag}' not in registry.")
            return False

        meta = self.tags[tag]

        # If your registry adds a writable flag later, this supports it.
        # Defaults to True for now.
        if not meta.get("writable", True):
            print(f"[PLC Service] Tag '{tag}' is read-only.")
            return False

        byte_offset = meta.get("byte_offset", 0)
        bit_offset = meta.get("bit_offset", 0)
        dtype = meta.get("datatype", "UNKNOWN")
        db_num = meta.get("db_number")

        try:
            if dtype == "BOOL":
                data = self.client.db_read(db_num, byte_offset, 1)
                set_bool(data, 0, bit_offset, bool(new_value))
                self.client.db_write(db_num, byte_offset, data)

            elif dtype == "REAL":
                data = bytearray(4)
                set_real(data, 0, float(new_value))
                self.client.db_write(db_num, byte_offset, data)

            elif dtype == "INT":
                data = bytearray(2)
                set_int(data, 0, int(new_value))
                self.client.db_write(db_num, byte_offset, data)

            return True

        except Exception as e:
            print(f"[PLC Service] Write failed for '{tag}': {e}")
            self.connected = False
            return False

    def _process_commands(self):
        """Non-blocking check for incoming GUI commands."""
        try:
            while True:
                msg = self.sub_socket.recv_json(flags=zmq.NOBLOCK)
                tag = msg.get("tag")
                value = msg.get("value")
                timestamp = msg.get("ts", 0.0)


                # Time-To-Live Verification
                age = time.time() - timestamp
                if age > MAX_CMD_AGE:
                    print(f"[SAFETY] Dropped stale command '{tag}': {value} from {timestamp}. Age: {age:.2f}s")
                    continue

                # Execute
                if tag and value is not None:
                    self._write_tag(tag, value)
                    print(f"[PLC Service] Executed command '{tag}': {value} from {timestamp}")

        except zmq.Again:
            pass


    def run(self):
        print("[PLC Service] Starting Daemon...")
        while True:
            cycle_start = time.perf_counter()

            if not self.connected:
                self._connect_plc()
                if not self.connected:
                    time.sleep(1.0)
                    continue

            # 1. Fetch State
            self._read_and_parse_dbs()

            # 2. Watchdog Heartbeat
            current_time = time.time()
            if current_time - self.last_heartbeat >= HEARTBEAT_INTERVAL:
                self._toggle_watchdog()
                self.last_heartbeat = current_time

            # 3. Process Inbound Writes
            self._process_commands()

            # 4. Broadcast
            try:
                topic = TOPIC_PLC_DATA if isinstance(TOPIC_PLC_DATA, bytes) else TOPIC_PLC_DATA.encode('utf-8')
                self.pub_socket.send_multipart([topic, json.dumps(self.state).encode('utf-8')])
            except Exception as e:
                print(f"[PLC Service] Publish Error: {e}")

            # 5. Maintain Strict Target Frequency
            elapsed = time.perf_counter() - cycle_start
            sleep_time = max(0.0, POLL_INTERVAL - elapsed)
            time.sleep(sleep_time)


if __name__ == "__main__":
    PlcMicroservice().run()