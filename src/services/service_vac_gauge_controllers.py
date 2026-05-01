import socket
import time
import zmq
import json
import os
from typing import Dict, Any, Optional

from src.core.network_config import (
    ZMQ_PORT_VACUUM_PUB, ZMQ_PORT_VACUUM_CMD, TOPIC_VACUUM_DATA,
    NOISY_RACK_WAVESHARE_IP, NOISY_RACK_WAVESHARE_PORT
)

# --- CONFIGURATION ---
SOCKET_TIMEOUT = 0.3
POLL_INTERVAL = 0.05
MAX_CMD_AGE = 0.5  # TTL

NODE_IDS = [10, 20]
CHANNELS = [1, 2, 3]
RELAYS = [1, 2, 3, 4, 5, 6]

PARAM_PRESSURE = 29
PARAM_STATUS = 24
PARAM_NAME = 5
PARAM_TYPE = 4
PARAM_SERIAL = 2

RELAY_PARAMS = {
    1: {"ch": "1", "on": "2", "off": "3", "status": "4"},
    2: {"ch": "5", "on": "6", "off": "7", "status": "8"},
    3: {"ch": "9", "on": "10", "off": "11", "status": "12"},
    4: {"ch": "13", "on": "14", "off": "15", "status": "16"},
    5: {"ch": "17", "on": "18", "off": "19", "status": "20"},
    6: {"ch": "21", "on": "22", "off": "23", "status": "24"}
}


class VacuumMicroservice:
    def __init__(self):
        self.context = zmq.Context()

        # --- ZMQ Setup (Defense in Depth) ---
        self.pub_socket = self.context.socket(zmq.PUB)
        self.pub_socket.setsockopt(zmq.SNDHWM, 5)
        self.pub_socket.bind(ZMQ_PORT_VACUUM_PUB)

        self.sub_socket = self.context.socket(zmq.SUB)
        self.sub_socket.setsockopt(zmq.RCVHWM, 5)
        self.sub_socket.bind(ZMQ_PORT_VACUUM_CMD)
        self.sub_socket.setsockopt_string(zmq.SUBSCRIBE, "")

        # --- Hardware State ---
        self.sock = None
        self.connected = False
        self.config = self._load_config()
        self.tag_map = self._build_tag_map()

        # --- Unified Payload Structure ---
        self.state: Dict[str, Any] = {
            "timestamp": 0.0,
            "system": {
                "connected": False,
                "cycle_time_ms": 0.0
            },
            "telemetry": {},
            "faults": {}
        }

        # --- Round-Robin Queue ---
        self.slow_tasks = []
        for ch in CHANNELS:
            self.slow_tasks.append(("gauge_status", ch))
        for sp in RELAYS:
            self.slow_tasks.append(("relay_status", sp))
            self.slow_tasks.append(("relay_on", sp))
            self.slow_tasks.append(("relay_off", sp))
            self.slow_tasks.append(("relay_ch", sp))
        self._slow_task_idx = 0

    def _load_config(self) -> dict:
        config_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../config/vac_gauges_config.json'))
        try:
            with open(config_path, "r") as f:
                return json.load(f)
        except Exception as e:
            print(f"[Vacuum Service] CRITICAL: Failed to load config: {e}")
            return {}

    def _build_tag_map(self) -> dict:
        """Pre-computes the telemetry string prefixes for fast O(1) loop lookups."""
        mapping = {}
        for node_str, node_data in self.config.items():
            if "channels" in node_data:
                for ch_str, ch_data in node_data["channels"].items():
                    subsystem = ch_data.get("subsystem", "unknown")
                    device = ch_data.get("device", f"gauge_{ch_str}")
                    mapping[(int(node_str), int(ch_str))] = f"ion_beam.{subsystem}.{device}"
        return mapping

    def _connect_socket(self):
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.settimeout(SOCKET_TIMEOUT)
            self.sock.connect((NOISY_RACK_WAVESHARE_IP, NOISY_RACK_WAVESHARE_PORT))
            self.connected = True
            self.state["system"]["connected"] = True
            print(f"[Vacuum Service] Connected to {NOISY_RACK_WAVESHARE_IP}:{NOISY_RACK_WAVESHARE_PORT}")
        except Exception as e:
            self.connected = False
            self.sock = None
            self.state["system"]["connected"] = False
            print(f"[Vacuum Service] Connection failed: {e}")

    # --- LEYBOLD HARDWARE PROTOCOL ---
    def _generate_read_frame(self, node_id: int, param_group: str, param_no: str) -> bytes:
        address = f"{node_id:02X}".encode('ascii')
        body = b'\x0f' + param_group.encode('ascii') + b';' + param_no.encode('ascii')
        crc_val = 255 - (sum(body) % 256)
        if crc_val < 32: crc_val += 32
        return address + body + bytes([crc_val]) + b'\x04'

    def _clear_stale_buffer(self):
        if not self.connected or not self.sock: return
        try:
            self.sock.settimeout(0.0)
            while True:
                discarded = self.sock.recv(1024)
                if not discarded: break
        except BlockingIOError:
            pass
        except Exception:
            self.connected = False
            self.state["system"]["connected"] = False
        finally:
            if self.sock: self.sock.settimeout(SOCKET_TIMEOUT)

    def _read_transaction(self, node_id: int, param_group: str, param_no: str) -> Optional[str]:
        if not self.connected: return None
        self._clear_stale_buffer()
        payload = self._generate_read_frame(node_id, param_group, param_no)
        try:
            self.sock.sendall(payload)
            response = self.sock.recv(1024)
            if b"\x06" in response:
                ack_idx = response.find(b"\x06")
                return response[ack_idx + 1:-2].decode('ascii', errors='ignore').strip()
            return None
        except socket.timeout:
            return None
        except Exception:
            self.connected = False
            self.state["system"]["connected"] = False
            return None

    def _write_transaction(self, node_id: int, param_group: str, param_no: str, value: str) -> str:
        if not self.connected: return "ERROR"
        self._clear_stale_buffer()
        address = f"{node_id:02X}".encode('ascii')
        body = b'\x0e' + param_group.encode('ascii') + b';' + param_no.encode('ascii') + b';' + value.encode(
            'ascii') + b' '
        crc_val = 255 - (sum(body) % 256)
        if crc_val < 32: crc_val += 32
        payload = address + body + bytes([crc_val]) + b'\x04'
        try:
            self.sock.sendall(payload)
            response = self.sock.recv(1024)
            if b"\x06" in response:
                return "ACK"
            elif b"\x15" in response:
                return "NACK"
            else:
                return "ERROR"
        except Exception:
            self.connected = False
            self.state["system"]["connected"] = False
            return "ERROR"

    def _read_transaction_with_retry(self, node_id, param_group, param_no, max_retries=50) -> Optional[str]:
        for _ in range(max_retries):
            val = self._read_transaction(node_id, param_group, param_no)
            if val is not None: return val
            time.sleep(POLL_INTERVAL)
        return None

    def _write_transaction_with_retry(self, node_id: int, param_group: str, param_no: str, value: str,
                                      max_retries: int = 10) -> bool:
        for _ in range(max_retries):
            status = self._write_transaction(node_id, param_group, param_no, value)
            if status == "ACK":
                return True
            elif status == "NACK":
                return False
            time.sleep(POLL_INTERVAL)
        return False

    def _process_commands(self):
        """TTL-verified, Tag-Based command processor."""
        try:
            while True:
                msg = self.sub_socket.recv_json(flags=zmq.NOBLOCK)
                tag = msg.get("tag", "")
                value = msg.get("value")
                ts = msg.get("ts", 0.0)

                age = time.time() - ts
                if age > MAX_CMD_AGE:
                    print(f"[Vacuum Service] WARNING: Dropped stale command for '{tag}'")
                    continue

                parts = tag.split('.')
                if len(parts) < 4:
                    continue

                # Handle Relay Setpoints (e.g., ion_beam.vacuum.controller_10.relay_1_on_sp)
                if "controller_" in parts[2] and "relay_" in parts[3]:
                    try:
                        node_id = int(parts[2].replace("controller_", ""))

                        # Extract relay number and command type ('on_sp' or 'off_sp')
                        relay_str_parts = parts[3].split('_')
                        relay_id = int(relay_str_parts[1])
                        cmd_type = relay_str_parts[2]  # 'on' or 'off'

                        if relay_id in RELAY_PARAMS:
                            param_str = RELAY_PARAMS[relay_id][cmd_type]
                            target_val_str = f"{float(value):.1E}"  # Leybold scientific notation

                            self._write_transaction_with_retry(node_id, "4", param_str, target_val_str)
                            print(
                                f"[Vacuum Service] Executed Relay Command: Node {node_id}, Relay {relay_id} {cmd_type.upper()} -> {target_val_str}")

                    except (ValueError, IndexError):
                        pass

                # Handle Channel Naming (e.g., ion_beam.source.vacuum_gauge_1.cmd_name)
                elif "vacuum_gauge_" in parts[2] and parts[3] == "cmd_name":
                    pass  # Insert your existing name-writing logic here if desired

        except zmq.Again:
            pass

    def _read_static_data(self):
        """One-off initialization reads, routed into the flattened telemetry payload."""
        print("[Vacuum Service] Reading static sensor profiles...")
        for node in NODE_IDS:
            # 1. Controller Serial Number
            val = self._read_transaction_with_retry(node, "5", str(PARAM_SERIAL), 50)
            if val:
                # The base controller doesn't have a channel prefix, so we define one
                self.state["telemetry"][f"ion_beam.vacuum.controller_{node}.serial_number"] = val

            # 2. Channel Names and Types
            for ch in CHANNELS:
                name_val = self._read_transaction_with_retry(node, str(ch), str(PARAM_NAME), 50)
                type_val = self._read_transaction_with_retry(node, str(ch), str(PARAM_TYPE), 50)

                # Fetch the exact dynamic string prefix (e.g., "ion_beam.source.vacuum_gauge_1")
                tag_prefix = self.tag_map.get((node, ch))

                if tag_prefix:
                    if name_val:
                        self.state["telemetry"][f"{tag_prefix}.name"] = name_val
                    if type_val:
                        self.state["telemetry"][f"{tag_prefix}.sensor_type"] = type_val

    def _enforce_startup_config(self):
        """Reads the config, enforces Names and Relay setpoints if mismatched."""

        # We already loaded the JSON into self.config during __init__!
        if not self.config:
            print("[Vacuum Service] WARNING: No config loaded. Skipping enforcement.")
            return

        print("\n[Vacuum Service] --- VERIFYING STARTUP CONFIG ---")
        try:
            config_data = self.config

            for node_str, node_data in config_data.items():
                node_id = int(node_str)
                if node_id not in NODE_IDS: continue

                # --- 1. Enforce Channel Names ---
                if "channels" in node_data:
                    for ch_str, ch_params in node_data["channels"].items():
                        ch = int(ch_str)
                        target_name = str(ch_params.get("name", ""))[:10].strip()  # Leybold limit is 10 chars

                        if target_name:
                            # Group = Channel, Param = 5 (Name)
                            curr_name = self._read_transaction_with_retry(node_id, str(ch), "5", 3)
                            if curr_name != target_name:
                                print(f"-> Node {node_id} Ch {ch}: Name mismatch. Updating to '{target_name}'...")
                                self._write_transaction_with_retry(node_id, str(ch), "5", target_name)
                                time.sleep(0.2)

                # --- 2. Enforce Relay Interlocks ---
                if "relays" in node_data:
                    for relay_str, params in node_data["relays"].items():
                        relay_id = int(relay_str)
                        if relay_id not in RELAY_PARAMS: continue

                        channel = params.get("channel")
                        on_val = params.get("on_val")
                        off_val = params.get("off_val")

                        if None in (channel, on_val, off_val): continue

                        p_ch = RELAY_PARAMS[relay_id]["ch"]
                        p_on = RELAY_PARAMS[relay_id]["on"]
                        p_off = RELAY_PARAMS[relay_id]["off"]

                        target_ch_str = str(int(channel))
                        target_on_str = f"{float(on_val):.1E}"
                        target_off_str = f"{float(off_val):.1E}"

                        curr_ch = self._read_transaction_with_retry(node_id, "4", p_ch, 3)
                        curr_on = self._read_transaction_with_retry(node_id, "4", p_on, 3)
                        curr_off = self._read_transaction_with_retry(node_id, "4", p_off, 3)

                        match_found = False
                        if curr_ch == target_ch_str and curr_on is not None and curr_off is not None:
                            try:
                                if abs(float(curr_on) - float(on_val)) < 1e-12 and abs(
                                        float(curr_off) - float(off_val)) < 1e-12:
                                    match_found = True
                            except ValueError:
                                pass

                        if match_found:
                            continue  # OK, skip writes

                        on_success = False
                        print(f"-> Node {node_id} Relay {relay_id}: Mismatch found! Overwriting...")
                        success = self._write_transaction_with_retry(node_id, "4", p_ch, target_ch_str)
                        time.sleep(0.2)

                        if success:

                            current_off_float = float(curr_off) if curr_off else 1000.0
                            if float(on_val) < current_off_float:
                                on_success = self._write_transaction_with_retry(node_id, "4", p_on, target_on_str)
                                time.sleep(0.2)
                                if on_success: self._write_transaction_with_retry(node_id, "4", p_off, target_off_str)
                            else:
                                on_success = self._write_transaction_with_retry(node_id, "4", p_off, target_off_str)
                                time.sleep(0.2)
                                if on_success: self._write_transaction_with_retry(node_id, "4", p_on, target_on_str)
                            time.sleep(0.2)

                        if not (success and on_success):
                            print("[Vacuum Service] Aborted Relay Config due to NACK on writes.\n")

            print("[Vacuum Service] --- VERIFICATION COMPLETE ---\n")
        except Exception as e:
            print(f"[Vacuum Service] Failed to enforce startup config: {e}")

    def run(self):
        print("[Vacuum Service] Daemon starting...")
        while True:
            cycle_start = time.perf_counter()

            if not self.connected:
                self._connect_socket()
                if self.connected:
                    self._read_static_data()
                    self._enforce_startup_config()
                else:
                    time.sleep(2.0)
                    continue

            self._process_commands()

            # Identify slow task
            slow_task_name, slow_task_target = self.slow_tasks[self._slow_task_idx]

            for node in NODE_IDS:
                # 1. High-Priority: Pressures
                for ch in CHANNELS:
                    raw_p = self._read_transaction(node, str(ch), str(PARAM_PRESSURE))
                    tag_prefix = self.tag_map.get((node, ch))

                    if raw_p is not None and tag_prefix:
                        try:
                            pressure_val = float(raw_p)
                            self.state["telemetry"][f"{tag_prefix}.pressure"] = pressure_val
                        except ValueError:
                            pass
                    time.sleep(POLL_INTERVAL)

                # 2. Low-Priority: Interlaced Status Tasks
                val = None
                if slow_task_name == "gauge_status":
                    val = self._read_transaction(node, str(slow_task_target), str(PARAM_STATUS))
                    tag_prefix = self.tag_map.get((node, slow_task_target))
                    if val and tag_prefix:
                        try:
                            status_code = int(val)
                            self.state["telemetry"][f"{tag_prefix}.status"] = status_code

                            # --- Explicit Fault Mapping for PLC ---
                            # Map (Node, Channel) to the exact VG prefix from fault_map.json
                            vg_map = {
                                (10, 1): "VG1", (10, 2): "VG2", (10, 3): "VG3",
                                (20, 1): "VG4", (20, 2): "VG5", (20, 3): "VG6"
                            }
                            vg_prefix = vg_map.get((node, slow_task_target))

                            if vg_prefix:
                                fault_not_found = f"{vg_prefix}_Not_Found"
                                fault_mismatch = f"{vg_prefix}_Type_Mismatch"

                                # Leybold typical statuses: 5 = No Sensor, 6 = ID Error
                                if status_code == 5:
                                    self.state["faults"][fault_not_found] = {"active": True, "severity": 2,
                                                                             "description": "Gauge disconnected."}
                                    self.state["faults"][fault_mismatch] = False
                                elif status_code == 6:
                                    self.state["faults"][fault_not_found] = False
                                    self.state["faults"][fault_mismatch] = {"active": True, "severity": 2,
                                                                            "description": "Gauge type mismatch."}
                                elif status_code != 0:
                                    # For other general errors (underrange, etc), just flag mismatch for safety
                                    self.state["faults"][fault_not_found] = False
                                    self.state["faults"][fault_mismatch] = {"active": True, "severity": 2,
                                                                            "description": f"Gauge error code: {status_code}"}
                                else:
                                    self.state["faults"][fault_not_found] = False
                                    self.state["faults"][fault_mismatch] = False
                        except ValueError:
                            pass

                elif slow_task_name == "relay_status":
                    p = RELAY_PARAMS[slow_task_target]["status"]
                    val = self._read_transaction(node, "4", p)
                    if val:
                        self.state["telemetry"][
                            f"ion_beam.vacuum.controller_{node}.relay_{slow_task_target}_status"] = int(val)

                elif slow_task_name == "relay_on":
                    p = RELAY_PARAMS[slow_task_target]["on"]
                    val = self._read_transaction(node, "4", p)
                    if val:
                        self.state["telemetry"][
                            f"ion_beam.vacuum.controller_{node}.relay_{slow_task_target}_on_sp"] = float(val)

                elif slow_task_name == "relay_off":
                    p = RELAY_PARAMS[slow_task_target]["off"]
                    val = self._read_transaction(node, "4", p)
                    if val:
                        self.state["telemetry"][
                            f"ion_beam.vacuum.controller_{node}.relay_{slow_task_target}_off_sp"] = float(val)

                elif slow_task_name == "relay_ch":
                    p = RELAY_PARAMS[slow_task_target]["ch"]
                    val = self._read_transaction(node, "4", p)
                    if val:
                        self.state["telemetry"][
                            f"ion_beam.vacuum.controller_{node}.relay_{slow_task_target}_assigned_ch"] = int(val)

                time.sleep(POLL_INTERVAL)

            self._slow_task_idx = (self._slow_task_idx + 1) % len(self.slow_tasks)

            # --- Enforce Comms Failures ---
            comms_fault_active = not self.connected
            self.state["faults"]["Graphix_1_Comms_Fail"] = {
                "active": comms_fault_active, "severity": 2, "description": "TCP connection lost to Node 10."
            } if comms_fault_active else False

            self.state["faults"]["Graphix_2_Comms_Fail"] = {
                "active": comms_fault_active, "severity": 2, "description": "TCP connection lost to Node 20."
            } if comms_fault_active else False

            # 3. Broadcast Unified Payload
            self.state["timestamp"] = time.time()
            elapsed = time.perf_counter() - cycle_start
            self.state["system"]["cycle_time_ms"] = elapsed * 1000

            try:
                topic = TOPIC_VACUUM_DATA if isinstance(TOPIC_VACUUM_DATA, bytes) else TOPIC_VACUUM_DATA.encode('utf-8')
                self.pub_socket.send_multipart([topic, json.dumps(self.state).encode('utf-8')])
            except Exception as e:
                print(f"[Vacuum Service] ZMQ Publish Error: {e}")

            time.sleep(max(0.0, 0.05 - elapsed))


if __name__ == "__main__":
    service = VacuumMicroservice()
    service.run()