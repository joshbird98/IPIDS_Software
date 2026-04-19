import time
import os
import zmq
import json
import snap7
from snap7.util import get_bool, get_int, get_dint, get_real, get_dword, set_bool, set_int, set_real
from typing import Dict, Any

from src.core.network_config import (
    ZMQ_PORT_PLC_PUB, ZMQ_PORT_PLC_CMD, TOPIC_PLC_DATA, PLC_IP, PLC_RACK, PLC_SLOT, DB_INTERFACE_NUM
)

POLL_INTERVAL = 0.1  # 100ms cycle (10Hz)
HEARTBEAT_INTERVAL = 1.0  # 1Hz Watchdog
MAX_CMD_AGE = 0.5  # TTL: Reject incoming commands older than 500ms


class PlcMicroservice:
    def __init__(self):
        self.context = zmq.Context()

        # --- ZMQ Setup (Defense in Depth: Transport Layer) ---
        self.pub_socket = self.context.socket(zmq.PUB)
        self.pub_socket.setsockopt(zmq.SNDHWM, 5)  # Drop outbound if queue exceeds 5
        self.pub_socket.bind(ZMQ_PORT_PLC_PUB)

        self.sub_socket = self.context.socket(zmq.SUB)
        self.sub_socket.setsockopt(zmq.RCVHWM, 5)  # Drop inbound if queue exceeds 5
        self.sub_socket.bind(ZMQ_PORT_PLC_CMD)
        self.sub_socket.setsockopt_string(zmq.SUBSCRIBE, "")

        # --- Snap7 Setup ---
        self.client = snap7.client.Client()
        self.connected = False
        self.watchdog_state = False
        self.last_heartbeat = time.time()
        self.plc_cycle_count = 0

        # --- Tag Registry ---
        self.tags = self._load_tag_registry()
        self.db_interface_size = self._calculate_db_size(DB_INTERFACE_NUM)

        self.fault_map = self._load_fault_map()

        # Unified Payload Structure
        self.state: Dict[str, Any] = {
            "timestamp": 0.0,
            "system": {
                "connected": False,
                "cpu_state": "UNKNOWN",
                "cycle_time_ms": POLL_INTERVAL * 1000
            },
            "telemetry": {},
            "faults": {}
        }

    def _load_fault_map(self) -> dict:
        config_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../config/fault_map.json'))
        try:
            with open(config_path, "r") as f:
                raw_json = json.load(f)

            # Build a dynamic reverse-lookup dictionary keyed by the registry tag name
            dynamic_map = {}
            for udt_name, data in raw_json.items():
                target_tag = data.get("registry_tag")
                if target_tag:
                    dynamic_map[target_tag] = data.get("bits", {})

            print(f"[PLC Service] Mapped {len(dynamic_map)} dynamic fault structures.")
            return dynamic_map

        except Exception as e:
            print(f"[PLC Service] CRITICAL: Failed to load fault map: {e}")
            return {}

    def _load_tag_registry(self) -> dict:
        current_dir = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.dirname(os.path.dirname(current_dir))
        registry_path = os.path.join(project_root, "config", "system_tags.json")
        try:
            with open(registry_path, "r") as f:
                all_tags = json.load(f)

            plc_tags = {tag: meta for tag, meta in all_tags.items() if meta.get("source") == "service_plc_snap7"}
            print(f"[PLC Service] Loaded {len(plc_tags)} tags for Snap7 monitoring.")
            return plc_tags
        except FileNotFoundError:
            print(f"[PLC Service] WARNING: {registry_path} not found. Operating blind.")
            return {}

    def _calculate_db_size(self, db_num: int) -> int:
        max_byte = 0
        type_sizes = {"BOOL": 1, "INT": 2, "UINT": 2, "DINT": 4, "UDINT": 4, "REAL": 4, "DWORD": 4, "TIME": 4}

        for tag, meta in self.tags.items():
            if meta.get("db_number") == db_num:
                offset = meta.get("byte_offset", 0)
                dtype = meta.get("datatype", "UNKNOWN")
                tag_end = offset + type_sizes.get(dtype, 2)
                if tag_end > max_byte:
                    max_byte = tag_end

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
        watchdog_tag = "ion_beam.system.plc_watchdog"
        if watchdog_tag not in self.tags:
            return
        self.watchdog_state = not self.watchdog_state
        self._write_tag(watchdog_tag, self.watchdog_state)

    def _parse_bytearray(self, data: bytearray, byte_idx: int, bit_idx: int, dtype: str):
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

    def _parse_fault_struct(self, raw_db_bytes: bytearray, byte_offset: int, tag_name: str):
        """Dynamically unpacks boolean fault states based on the registry tag map."""
        # Extract the specific 4 bytes for this DWORD
        struct_bytes = raw_db_bytes[byte_offset: byte_offset + 4]

        # Iterate through the mapped bits for this specific tag
        for offset_str, fault_meta in self.fault_map[tag_name].items():
            byte_idx, bit_idx = map(int, offset_str.split('.'))

            is_active = get_bool(struct_bytes, byte_idx, bit_idx)
            fault_name = fault_meta["name"]

            if is_active:
                self.state["faults"][fault_name] = {
                    "active": True,
                    "severity": fault_meta["severity"],
                    "description": fault_meta["description"]
                }
            else:
                self.state["faults"][fault_name] = False

    def _read_and_parse_dbs(self):
        try:
            raw_interface = self.client.db_read(DB_INTERFACE_NUM, 0, self.db_interface_size)

            for tag, meta in self.tags.items():
                if meta.get("db_number") != DB_INTERFACE_NUM:
                    continue

                if "From_PC" in tag:
                    continue

                byte_offset = meta.get("byte_offset", 0)

                # If the tag exists in our dynamic fault map, parse it as a struct
                if tag in self.fault_map:
                    self._parse_fault_struct(raw_interface, byte_offset, tag)

                # Otherwise, parse it as standard telemetry
                else:
                    val = self._parse_bytearray(
                        raw_interface,
                        byte_offset,
                        meta.get("bit_offset", 0),
                        meta.get("datatype", "UNKNOWN")
                    )
                    self.state["telemetry"][tag] = val

            # CPU State Evaluation
            current_count = self.state["telemetry"].get("ion_beam.system.plc_cycle_count")
            if current_count is not None:
                self.state["system"]["cpu_state"] = "RUN" if current_count != self.plc_cycle_count else "STOP"
                self.plc_cycle_count = current_count

        except Exception as e:
            print(f"[PLC Service] Cycle aborted due to error: {e}")
            self.state["system"]["cpu_state"] = "DISCONNECTED"
            self.connected = False

    def _write_tag(self, tag: str, new_value: Any) -> bool:
        if tag not in self.tags:
            return False

        meta = self.tags[tag]
        if not meta.get("writable", True):
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
        """Defense in Depth: Application Layer TTL Verification"""
        try:
            while True:
                msg = self.sub_socket.recv_json(flags=zmq.NOBLOCK)

                tag = msg.get("tag")
                value = msg.get("value")
                timestamp = msg.get("ts", 0.0)

                # Time-To-Live Check
                age = time.time() - timestamp
                if age > MAX_CMD_AGE:
                    print(f"[PLC Service] WARNING: Dropped stale command '{tag}' (Age: {age:.2f}s)")
                    continue

                if tag and value is not None:
                    self._write_tag(tag, value)

        except zmq.Again:
            pass

    def run(self):
        print("[PLC Service] Starting Daemon...")
        while True:
            cycle_start = time.time()

            if not self.connected:
                self._connect_plc()
                if not self.connected:
                    time.sleep(1.0)
                    continue

            self._read_and_parse_dbs()
            self._process_commands()

            current_time = time.time()
            if current_time - self.last_heartbeat >= HEARTBEAT_INTERVAL:
                self._toggle_watchdog()
                self.last_heartbeat = current_time

            self.state["timestamp"] = current_time

            try:
                topic = TOPIC_PLC_DATA if isinstance(TOPIC_PLC_DATA, bytes) else TOPIC_PLC_DATA.encode('utf-8')
                self.pub_socket.send_multipart([topic, json.dumps(self.state).encode('utf-8')])
            except Exception as e:
                print(f"[PLC Service] Publish Error: {e}")

            # Dynamic sleep to maintain strict 10Hz timing
            elapsed = time.time() - cycle_start
            sleep_time = max(0, POLL_INTERVAL - elapsed)
            time.sleep(sleep_time)


if __name__ == "__main__":
    PlcMicroservice().run()