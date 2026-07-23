import socket
import time
from errno import WSAEWOULDBLOCK

import zmq
import json
import os
from typing import Dict, Any, Optional
import orjson
import re
from src.core.event_helper import EventHelper
from src.core.os_helper import harden_windows_process

# Force Windows high-resolution timers (1ms precision)
if os.name == 'nt':
    import ctypes

    ctypes.windll.winmm.timeBeginPeriod(1)

from src.core.network_map import (
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
        self.registry_limits = self._load_registry_limits()

        # Arbitration State
        self.active_ctrl_mode = 0

        self.pressure_history: Dict[str, Dict[str, float]] = {}

        # --- Dynamic Configuration Mapping Lookup Structures ---
        self.relay_to_channel_map = {}
        self.channel_to_relay_map = {}
        self.relay_thresholds = {}
        self._build_relay_configuration_maps()

        # --- Strict 1D Flat Payload ---
        self.state: Dict[str, Any] = {
            "timestamp": 0.0,
            "system.connected": 0.0,
            "system.cycle_time_ms": 0.0
        }

        # Initialize all gauge status flags to consistent default states in their vertical ISA-95 paths
        for (node, ch), base_tag in self.tag_map.items():
            self.state[f"{base_tag}.stat_not_found"] = 0.0
            self.state[f"{base_tag}.stat_mismatch"] = 0.0
            self.state[f"{base_tag}.stat_rapid_rise"] = 0.0
            self.state[f"{base_tag}.stat_above_sp"] = 0.0
            self.state[f"{base_tag}.stat_approaching_sp"] = 0.0
            self.state[f"{base_tag}.stat_relay_active"] = 0.0
            self.state[f"{base_tag}.stat_comms_fail"] = 0.0

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

        self.stats_time = time.time()
        self.stats_read_attempts = 0
        self.stats_read_fails = 0
        self.stats_loop_times = []
        self.stats_detailed = {}

        self.events = EventHelper("service_vac_gauge_controllers")

    def _load_config(self) -> dict:
        config_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../config/vac_gauges_config.json'))
        try:
            with open(config_path, "r") as f:
                return json.load(f)
        except Exception as e:
            self.events.log_general(f"CRITICAL: Failed to load config: {e}")
            return {}

    def _load_registry_limits(self) -> dict:
        registry_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../config/system_tags.json'))
        limits = {}
        try:
            with open(registry_path, "r") as f:
                registry = json.load(f)
                for tag, data in registry.items():
                    if data.get("source") == "service_vacuum" and data.get("auto_controllable"):
                        limits[tag] = {
                            "min_val": data.get("min_val"),
                            "max_val": data.get("max_val")
                        }
        except Exception as e:
            self.events.log_general(f"Failed to load registry limits: {e}")
        return limits

    def _build_tag_map(self) -> dict:
        # Explicitly map the physical (Node, Channel) matrix to the ISA-95 Functional Area
        mapping = {
            (10, 1): "ion_beam.source.vacuum_gauge_1",
            (10, 2): "ion_beam.beamline.vacuum_gauge_2",
            (10, 3): "ion_beam.beamline.vacuum_gauge_3",
            (20, 1): "ion_beam.endstation.vacuum_gauge_4",
            (20, 2): "ion_beam.loadlock.vacuum_gauge_5",
            (20, 3): "ion_beam.endstation.vacuum_gauge_6"
        }
        return mapping

    def _build_relay_configuration_maps(self):
        """Extracts and maps relations between relays, channels, and thresholds from configuration."""
        for node_str, node_data in self.config.items():
            if not node_str.isdigit():
                continue
            node_id = int(node_str)
            if "relays" in node_data:
                for relay_str, params in node_data["relays"].items():
                    relay_id = int(relay_str)
                    channel = params.get("channel")
                    on_val = params.get("on_val")
                    off_val = params.get("off_val")
                    if channel is not None:
                        ch_id = int(channel)
                        self.relay_to_channel_map[(node_id, relay_id)] = ch_id
                        if (node_id, ch_id) not in self.channel_to_relay_map:
                            self.channel_to_relay_map[(node_id, ch_id)] = []
                        self.channel_to_relay_map[(node_id, ch_id)].append(relay_id)
                        try:
                            self.relay_thresholds[(node_id, relay_id)] = {
                                "on_val": float(on_val),
                                "off_val": float(off_val)
                            }
                        except (ValueError, TypeError):
                            pass

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
        except (BlockingIOError, OSError):
            pass
        except Exception as e:
            self.connected = False
            self.state["system.connected"] = 0.0
            print(f"[NET DISCONNECT] Stale buffer clear error: {e}")
        finally:
            if self.sock:
                self.sock.settimeout(SOCKET_TIMEOUT)

    def _read_transaction(self, node_id: int, param_group: str, param_no: str) -> Optional[str]:

        stat_key = f"Node{node_id}_Grp{param_group}"
        if stat_key not in self.stats_detailed:
            self.stats_detailed[stat_key] = {"attempts": 0, "timeouts": 0, "garbage": 0}

        self.stats_detailed[stat_key]["attempts"] += 1

        if not self.connected or not self.sock:
            self.stats_detailed[stat_key]["timeouts"] += 1
            return None

        self._clear_stale_buffer()
        payload = self._generate_read_frame(node_id, param_group, param_no)

        try:
            self.sock.sendall(payload)

            buffer = b""
            start_recv = time.perf_counter()
            while b"\x04" not in buffer:
                chunk = self.sock.recv(128)
                if not chunk:
                    break
                buffer += chunk
                if (time.perf_counter() - start_recv) > SOCKET_TIMEOUT:
                    raise socket.timeout

            if b"\x04" in buffer:
                eot_idx = buffer.find(b"\x04")

                if b"\x06" in buffer:
                    ack_idx = buffer.find(b"\x06")
                    result = buffer[ack_idx + 1:eot_idx - 1].decode('ascii', errors='ignore').strip()
                    time.sleep(0.02)
                    return result
                else:
                    if param_group in ["1", "2", "3"] and param_no == str(PARAM_PRESSURE):
                        raw_tail = buffer[:eot_idx - 1]
                        decoded_tail = raw_tail.decode('ascii', errors='ignore').strip()

                        scavenged_str = ""
                        for i in range(len(decoded_tail)):
                            test_str = decoded_tail[i:].strip()
                            if len(test_str) > 0 and all(32 <= ord(c) <= 126 for c in test_str):
                                scavenged_str = test_str
                                break

                        if re.match(r'^\d\.\d{2}[eE][+-]\d{2}>?$', scavenged_str):
                            scavenged_str = scavenged_str.replace(">", "")
                            time.sleep(0.02)
                            return scavenged_str

                    self.stats_detailed[stat_key]["garbage"] += 1

        except socket.timeout:
            self.stats_detailed[stat_key]["timeouts"] += 1
        except Exception:
            self.connected = False
            self.state["system.connected"] = 0.0
            self.stats_detailed[stat_key]["garbage"] += 1

        time.sleep(0.02)
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

        status = "ERROR"
        try:
            self.sock.sendall(payload)
            response = self.sock.recv(1024)
            if b"\x06" in response:
                status = "ACK"
            elif b"\x15" in response:
                status = "NACK"
        except Exception:
            self.connected = False
            self.state["system.connected"] = 0.0

        time.sleep(0.02)
        return status

    def _read_transaction_with_retry(self, node_id, param_group, param_no, max_retries=5) -> Optional[str]:
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
                origin = msg.get("origin", "hmi")

                age = time.time() - ts
                if age > MAX_CMD_AGE:
                    self.events.log_general(f"WARNING: Dropped stale command for '{tag}'")
                    continue

                # --- 1. ARBITRATION: Handle Control Mode Changes ---
                if tag.endswith(".cmd_ctrl_mode"):
                    try:
                        self.active_ctrl_mode = int(value)
                        self.events.log_general(f"Arbitration: Vacuum mode set to {self.active_ctrl_mode}")
                    except (ValueError, TypeError):
                        pass
                    continue

                # --- 2. ARBITRATION: Enforce Lockout ---
                if self.active_ctrl_mode > 0 and origin != "optimizer":
                    continue

                parts = tag.split('.')
                if len(parts) < 4:
                    continue

                if "controller_" in parts[2] and "relay_" in parts[3]:
                    try:
                        # Fail-Safe check using registry limits (if configured in system_tags.json)
                        if tag in self.registry_limits:
                            min_val = self.registry_limits[tag]["min_val"]
                            max_val = self.registry_limits[tag]["max_val"]
                            if min_val is None or max_val is None or not (min_val <= float(value) <= max_val):
                                continue

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
                                self.events.log_general(
                                    f"-> Node {node_id} Ch {ch}: Name mismatch. Updating to '{target_name}'...")
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
            vg1 = self.tag_map.get((10, 1))
            vg2 = self.tag_map.get((10, 2))

            def has_fault(base_tag):
                if not base_tag: return True
                return any(self.state.get(f"{base_tag}.stat_{f}", 0.0) == 1.0
                           for f in ["not_found", "mismatch", "above_sp", "rapid_rise"])

            vg1_has_fault = has_fault(vg1)
            vg2_has_fault = has_fault(vg2)
            comms_fail = not self.connected

            if not comms_fail and not vg1_has_fault and not vg2_has_fault:
                is_safe = True

        except Exception:
            is_safe = False

        self.state["ion_beam.vacuum.gv_permissive_ready"] = 1.0 if is_safe else 0.0

    def run(self):
        self.events.log_general("Daemon starting (ISA-95 Architecture)...")
        global_rise_limit = float(self.config.get("system_interlocks", {}).get("rapid_rise_thresh_mb_s", 5.0e-5))

        global POLL_INTERVAL
        POLL_INTERVAL = 0.005

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
                    base_tag = self.tag_map.get((node, ch))

                    if raw_p is not None and base_tag:
                        try:
                            pressure_val = float(raw_p)
                            self.state[f"{base_tag}.rb_pressure"] = pressure_val

                            # Evaluate dynamic setpoint warning conditions (Approaching Setpoint threshold limit ratio)
                            is_approaching = False
                            relay_ids = self.channel_to_relay_map.get((node, ch), [])
                            for r_id in relay_ids:
                                thresholds = self.relay_thresholds.get((node, r_id))
                                if thresholds:
                                    on_threshold = thresholds["on_val"]
                                    if on_threshold < pressure_val <= (on_threshold / 0.8):
                                        is_approaching = True
                                        break

                            self.state[f"{base_tag}.stat_approaching_sp"] = 1.0 if is_approaching else 0.0



                            current_time = time.time()
                            is_rapid_rise = False

                            if base_tag in self.pressure_history:
                                prev_p = self.pressure_history[base_tag]["pressure"]
                                prev_t = self.pressure_history[base_tag]["time"]
                                dt = current_time - prev_t

                                if dt > 0.5:
                                    dp_dt = (pressure_val - prev_p) / dt
                                    if dp_dt > global_rise_limit and pressure_val > 1.0e-6:
                                        if "vacuum_gauge_4" not in base_tag:
                                            is_rapid_rise = True

                                    self.pressure_history[base_tag] = {"pressure": pressure_val, "time": current_time}
                            else:
                                self.pressure_history[base_tag] = {"pressure": pressure_val, "time": current_time}

                            self.state[f"{base_tag}.stat_rapid_rise"] = 1.0 if is_rapid_rise else 0.0
                            self.state[f"{base_tag}.stat_comms_fail"] = 0.0


                        except ValueError:
                            pass
                    elif base_tag:
                        # If a read fails on a connected network, flag the individual gauge comms offline
                        self.state[f"{base_tag}.stat_comms_fail"] = 1.0

                val = None
                if slow_task_name == "gauge_status":
                    val = self._read_transaction(node, str(slow_task_target), str(PARAM_STATUS))
                    base_tag = self.tag_map.get((node, slow_task_target))

                    if val and base_tag:
                        try:
                            # Handle string literals returned by the hardware
                            if val.strip().upper() == "OK":
                                status_code = 0
                            else:
                                status_code = int(val)

                            self.state[f"{base_tag}.stat_error_code"] = float(status_code)

                            not_found_key = f"{base_tag}.stat_not_found"
                            mismatch_key = f"{base_tag}.stat_mismatch"

                            if status_code == 5:
                                self.state[not_found_key], self.state[mismatch_key] = 1.0, 0.0
                            elif status_code == 6:
                                self.state[not_found_key], self.state[mismatch_key] = 0.0, 1.0
                            elif status_code != 0:
                                self.state[not_found_key], self.state[mismatch_key] = 0.0, 1.0
                            else:
                                self.state[not_found_key], self.state[mismatch_key] = 0.0, 0.0


                        except ValueError:
                            pass

                elif slow_task_name == "relay_status":
                    p = RELAY_PARAMS[slow_task_target]["status"]
                    val = self._read_transaction(node, "4", p)
                    if val:
                        try:
                            status_int = int(val)
                            self.state[f"ion_beam.vacuum.controller_{node}.relay_{slow_task_target}_status"] = float(
                                status_int)

                            # Map relay state flags dynamically back to assigned target vacuum gauges based on active configuration data
                            ch_id = self.relay_to_channel_map.get((node, slow_task_target))
                            if ch_id:
                                base_tag = self.tag_map.get((node, ch_id))
                                if base_tag:
                                    is_active = 1.0 if status_int == 1 else 0.0
                                    is_above = 1.0 if status_int == 0 else 0.0
                                    self.state[f"{base_tag}.stat_relay_active"] = is_active
                                    self.state[f"{base_tag}.stat_above_sp"] = is_above


                        except ValueError:
                            pass

                elif slow_task_name in ["relay_on", "relay_off", "relay_ch"]:
                    p = RELAY_PARAMS[slow_task_target][slow_task_name.split("_")[1]]
                    val = self._read_transaction(node, "4", p)
                    if val:
                        try:
                            self.state[
                                f"ion_beam.vacuum.controller_{node}.relay_{slow_task_target}_{slow_task_name.split('_')[1]}_sp"] = float(
                                val)
                        except ValueError:
                            pass

            self._slow_task_idx = (self._slow_task_idx + 1) % len(self.slow_tasks)

            comms_fault_active = 1.0 if not self.connected else 0.0

            # Map Master Controller comms faults to their new ISA-95 locations
            self.state["ion_beam.facilities.graphix1.stat_comms_fail"] = comms_fault_active
            self.state["ion_beam.facilities.graphix2.stat_comms_fail"] = comms_fault_active

            # Broadcast the active control mode
            self.state["ion_beam.vacuum.rb_ctrl_mode"] = float(self.active_ctrl_mode)

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

                self.stats_time = current_time
                self.stats_detailed.clear()
                self.stats_loop_times.clear()

            time.sleep(max(0.0, 0.05 - elapsed))


if __name__ == "__main__":
    harden_windows_process()
    service = VacuumMicroservice()
    service.run()