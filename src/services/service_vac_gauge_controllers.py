import socket
import time
import zmq
import json
import os
from typing import Dict, Any, Optional
import orjson
from src.core.event_helper import EventHelper

# Force Windows high-resolution timers (1ms precision)
if os.name == 'nt':
    import ctypes

    ctypes.windll.winmm.timeBeginPeriod(1)

from src.core.network_config import (
    ZMQ_PORT_VACUUM_PUB, ZMQ_PORT_VACUUM_CMD, TOPIC_VACUUM_DATA,
    NOISY_RACK_WAVESHARE_IP, NOISY_RACK_WAVESHARE_PORT,
    ZMQ_PORT_HEARTBEAT)

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

VG_MAP = {
    (10, 1): "VG1", (10, 2): "VG2", (10, 3): "VG3",
    (20, 1): "VG4", (20, 2): "VG5", (20, 3): "VG6"
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

        self.pressure_history: Dict[str, Dict[str, float]] = {}

        # --- Strict 1D Flat Payload ---
        self.state: Dict[str, Any] = {
            "timestamp": 0.0,
            "system.connected": 0.0,
            "system.cycle_time_ms": 0.0
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

        self.hb_socket = self.context.socket(zmq.PUB)
        self.hb_socket.connect(ZMQ_PORT_HEARTBEAT)
        self.last_hb_time = 0.0

        self.events = EventHelper("service_vac_gauge_controllers")

    def _load_config(self) -> dict:
        config_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../config/vac_gauges_config.json'))
        try:
            with open(config_path, "r") as f:
                return json.load(f)
        except Exception as e:
            self.events.log_general(f"CRITICAL: Failed to load config: {e}")
            return {}

    def _build_tag_map(self) -> dict:
        mapping = {}
        for node_str, node_data in self.config.items():
            if not node_str.isdigit():
                continue
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
            self.state["system.connected"] = 1.0
            self.events.log_general(f"Connected to {NOISY_RACK_WAVESHARE_IP}:{NOISY_RACK_WAVESHARE_PORT}")
        except Exception as e:
            self.connected = False
            self.sock = None
            self.state["system.connected"] = 0.0
            self.events.log_general(f"Connection failed: {e}")

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
            self.state["system.connected"] = 0.0
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
            print(f"TIMEOUT: Node {node_id}, Grp {param_group}, Param {param_no} failed after 0.3s")
            return None
        except Exception:
            self.connected = False
            self.state["system.connected"] = 0.0
            print(f"FAULT: _read_transaction error - {e}")
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
            self.state["system.connected"] = 0.0
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
        try:
            while True:
                msg = self.sub_socket.recv_json(flags=zmq.NOBLOCK)
                tag = msg.get("tag", "")
                value = msg.get("value")
                ts = msg.get("ts", 0.0)

                age = time.time() - ts
                if age > MAX_CMD_AGE:
                    self.events.log_general(f"WARNING: Dropped stale command for '{tag}'")
                    continue

                parts = tag.split('.')
                if len(parts) < 4:
                    continue

                if "controller_" in parts[2] and "relay_" in parts[3]:
                    try:
                        node_id = int(parts[2].replace("controller_", ""))
                        relay_str_parts = parts[3].split('_')
                        relay_id = int(relay_str_parts[1])
                        cmd_type = relay_str_parts[2]

                        if relay_id in RELAY_PARAMS:
                            param_str = RELAY_PARAMS[relay_id][cmd_type]
                            target_val_str = f"{float(value):.1E}"

                            self._write_transaction_with_retry(node_id, "4", param_str, target_val_str)
                            self.events.log_general(
                                f"Executed Relay Command: Node {node_id}, Relay {relay_id} {cmd_type.upper()} -> {target_val_str}")
                    except (ValueError, IndexError):
                        pass

                elif "vacuum_gauge_" in parts[2] and parts[3] == "cmd_name":
                    pass

        except zmq.Again:
            pass

    def _read_static_data(self):
        self.events.log_general("Reading static sensor profiles...")
        for node in NODE_IDS:
            val = self._read_transaction_with_retry(node, "5", str(PARAM_SERIAL), 50)
            if val:
                self.state[f"ion_beam.vacuum.controller_{node}.serial_number"] = val

            for ch in CHANNELS:
                name_val = self._read_transaction_with_retry(node, str(ch), str(PARAM_NAME), 50)
                type_val = self._read_transaction_with_retry(node, str(ch), str(PARAM_TYPE), 50)

                tag_prefix = self.tag_map.get((node, ch))

                if tag_prefix:
                    if name_val:
                        self.state[f"{tag_prefix}.name"] = name_val
                    if type_val:
                        self.state[f"{tag_prefix}.sensor_type"] = type_val

    def _enforce_startup_config(self):
        if not self.config:
            self.events.log_general("WARNING: No config loaded. Skipping enforcement.")
            return

        try:
            for node_str, node_data in self.config.items():
                if not node_str.isdigit():
                    continue
                node_id = int(node_str)
                if node_id not in NODE_IDS: continue

                if "channels" in node_data:
                    for ch_str, ch_params in node_data["channels"].items():
                        ch = int(ch_str)
                        target_name = str(ch_params.get("name", ""))[:10].strip()

                        if target_name:
                            curr_name = self._read_transaction_with_retry(node_id, str(ch), "5", 3)
                            self.hb_socket.send_json({"service": "service_vac_gauge_controllers", "ts": time.time()})

                            if curr_name != target_name:
                                self.events.log_general(f"-> Node {node_id} Ch {ch}: Name mismatch. Updating to '{target_name}'...")
                                self._write_transaction_with_retry(node_id, str(ch), "5", target_name)
                                self.hb_socket.send_json(
                                    {"service": "service_vac_gauge_controllers", "ts": time.time()})
                                time.sleep(0.2)

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
                        self.hb_socket.send_json({"service": "service_vac_gauge_controllers", "ts": time.time()})
                        curr_on = self._read_transaction_with_retry(node_id, "4", p_on, 3)
                        self.hb_socket.send_json({"service": "service_vac_gauge_controllers", "ts": time.time()})
                        curr_off = self._read_transaction_with_retry(node_id, "4", p_off, 3)
                        self.hb_socket.send_json({"service": "service_vac_gauge_controllers", "ts": time.time()})

                        match_found = False
                        if curr_ch == target_ch_str and curr_on is not None and curr_off is not None:
                            try:
                                if abs(float(curr_on) - float(on_val)) < 1e-12 and abs(
                                        float(curr_off) - float(off_val)) < 1e-12:
                                    match_found = True
                            except ValueError:
                                pass

                        if match_found:
                            continue

                        on_success = False
                        self.events.log_general(f"-> Node {node_id} Relay {relay_id}: Mismatch found! Overwriting...")
                        success = self._write_transaction_with_retry(node_id, "4", p_ch, target_ch_str)
                        self.hb_socket.send_json({"service": "service_vac_gauge_controllers", "ts": time.time()})
                        time.sleep(0.2)

                        if success:
                            current_off_float = float(curr_off) if curr_off else 1000.0
                            if float(on_val) < current_off_float:
                                on_success = self._write_transaction_with_retry(node_id, "4", p_on, target_on_str)
                                self.hb_socket.send_json(
                                    {"service": "service_vac_gauge_controllers", "ts": time.time()})
                                time.sleep(0.2)
                                if on_success: self._write_transaction_with_retry(node_id, "4", p_off, target_off_str)
                                self.hb_socket.send_json(
                                    {"service": "service_vac_gauge_controllers", "ts": time.time()})
                            else:
                                on_success = self._write_transaction_with_retry(node_id, "4", p_off, target_off_str)
                                self.hb_socket.send_json(
                                    {"service": "service_vac_gauge_controllers", "ts": time.time()})
                                time.sleep(0.2)
                                if on_success: self._write_transaction_with_retry(node_id, "4", p_on, target_on_str)
                                self.hb_socket.send_json(
                                    {"service": "service_vac_gauge_controllers", "ts": time.time()})
                            time.sleep(0.2)

                        if not (success and on_success):
                            self.events.log_general("Aborted Relay Config due to NACK on writes.\n")

            self.events.log_general("--- VERIFICATION COMPLETE ---\n")
        except Exception as e:
            self.events.log_general(f"Failed to enforce startup config: {e}")

    def _evaluate_gv_permissive(self):
        is_safe = False
        try:
            def has_fault(vg):
                return any(self.state.get(f"ion_beam.gauges.status.stat_{vg}_{f}", 0.0) == 1.0
                           for f in ["not_found", "mismatch", "above_sp", "rapid_rise"])

            vg1_has_fault = has_fault("vg1")
            vg2_has_fault = has_fault("vg2")
            comms_fail = not self.connected

            if not comms_fail and not vg1_has_fault and not vg2_has_fault:
                is_safe = True

        except Exception:
            is_safe = False

        self.state["ion_beam.vacuum.gv_permissive_ready"] = 1.0 if is_safe else 0.0

    def run(self):
        self.events.log_general("Daemon starting...")
        global_rise_limit = float(self.config.get("system_interlocks", {}).get("rapid_rise_thresh_mb_s", 5.0e-5))

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

            slow_task_name, slow_task_target = self.slow_tasks[self._slow_task_idx]

            for node in NODE_IDS:
                for ch in CHANNELS:
                    raw_p = self._read_transaction(node, str(ch), str(PARAM_PRESSURE))
                    tag_prefix = self.tag_map.get((node, ch))
                    vg_prefix = VG_MAP.get((node, ch))

                    if raw_p is not None and tag_prefix and vg_prefix:
                        try:
                            pressure_val = float(raw_p)
                            self.state[f"{tag_prefix}.pressure"] = pressure_val

                            current_time = time.time()
                            is_rapid_rise = False

                            if vg_prefix in self.pressure_history:
                                prev_p = self.pressure_history[vg_prefix]["pressure"]
                                prev_t = self.pressure_history[vg_prefix]["time"]
                                dt = current_time - prev_t

                                if dt > 0:
                                    dp_dt = (pressure_val - prev_p) / dt
                                    if dp_dt > global_rise_limit and pressure_val > 1.0e-6:
                                        if "vacuum_gauge_4" not in tag_prefix: #VG4 is extremely noisy and has spikes that cause false-triggers, VG6 monitors the same zone so safe to skip VG4
                                            is_rapid_rise = True

                            self.state[
                                f"ion_beam.gauges.status.stat_{vg_prefix.lower()}_rapid_rise"] = 1.0 if is_rapid_rise else 0.0
                            self.pressure_history[vg_prefix] = {"pressure": pressure_val, "time": current_time}

                        except ValueError:
                            pass
                    time.sleep(POLL_INTERVAL)

                val = None
                if slow_task_name == "gauge_status":
                    val = self._read_transaction(node, str(slow_task_target), str(PARAM_STATUS))
                    tag_prefix = self.tag_map.get((node, slow_task_target))
                    vg_prefix = VG_MAP.get((node, slow_task_target))

                    if val and tag_prefix and vg_prefix:
                        try:
                            status_code = int(val)
                            self.state[f"{tag_prefix}.status"] = float(status_code)

                            not_found_key = f"ion_beam.gauges.status.stat_{vg_prefix.lower()}_not_found"
                            mismatch_key = f"ion_beam.gauges.status.stat_{vg_prefix.lower()}_mismatch"

                            if status_code == 5:
                                self.state[not_found_key] = 1.0
                                self.state[mismatch_key] = 0.0
                            elif status_code == 6:
                                self.state[not_found_key] = 0.0
                                self.state[mismatch_key] = 1.0
                            elif status_code != 0:
                                self.state[not_found_key] = 0.0
                                self.state[mismatch_key] = 1.0
                            else:
                                self.state[not_found_key] = 0.0
                                self.state[mismatch_key] = 0.0

                        except ValueError:
                            pass

                elif slow_task_name == "relay_status":
                    p = RELAY_PARAMS[slow_task_target]["status"]
                    val = self._read_transaction(node, "4", p)
                    if val:
                        status_int = int(val)
                        self.state[f"ion_beam.vacuum.controller_{node}.relay_{slow_task_target}_status"] = float(
                            status_int)

                        if slow_task_target in CHANNELS:
                            vg_prefix = VG_MAP.get((node, slow_task_target))
                            if vg_prefix:
                                is_above_sp = (status_int == 0)
                                self.state[
                                    f"ion_beam.gauges.status.stat_{vg_prefix.lower()}_above_sp"] = 1.0 if is_above_sp else 0.0

                elif slow_task_name == "relay_on":
                    p = RELAY_PARAMS[slow_task_target]["on"]
                    val = self._read_transaction(node, "4", p)
                    if val:
                        self.state[f"ion_beam.vacuum.controller_{node}.relay_{slow_task_target}_on_sp"] = float(val)

                elif slow_task_name == "relay_off":
                    p = RELAY_PARAMS[slow_task_target]["off"]
                    val = self._read_transaction(node, "4", p)
                    if val:
                        self.state[f"ion_beam.vacuum.controller_{node}.relay_{slow_task_target}_off_sp"] = float(val)

                elif slow_task_name == "relay_ch":
                    p = RELAY_PARAMS[slow_task_target]["ch"]
                    val = self._read_transaction(node, "4", p)
                    if val:
                        self.state[f"ion_beam.vacuum.controller_{node}.relay_{slow_task_target}_assigned_ch"] = float(
                            val)

                time.sleep(POLL_INTERVAL)

            self._slow_task_idx = (self._slow_task_idx + 1) % len(self.slow_tasks)

            comms_fault_active = 1.0 if not self.connected else 0.0
            self.state["ion_beam.gauges.status.stat_graphix1_comms_fail"] = comms_fault_active
            self.state["ion_beam.gauges.status.stat_graphix2_comms_fail"] = comms_fault_active

            self._evaluate_gv_permissive()

            self.state["timestamp"] = time.time()
            elapsed = time.perf_counter() - cycle_start
            self.state["system.cycle_time_ms"] = elapsed * 1000

            try:
                topic = TOPIC_VACUUM_DATA if isinstance(TOPIC_VACUUM_DATA, bytes) else TOPIC_VACUUM_DATA.encode('utf-8')
                self.pub_socket.send_multipart([topic, orjson.dumps(self.state)])
            except Exception as e:
                self.events.log_general(f"ZMQ Publish Error: {e}")

            current_time = time.time()
            if current_time - self.last_hb_time >= 0.5:
                self.hb_socket.send_json({"service": "service_vac_gauge_controllers", "ts": current_time})
                self.last_hb_time = current_time

            time.sleep(max(0.0, 0.05 - elapsed))


if __name__ == "__main__":
    service = VacuumMicroservice()
    service.run()