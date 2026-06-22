import socket
import time
import zmq
import select
import os
import json
from typing import Dict, Any, Optional
import orjson
from src.core.event_helper import EventHelper
from src.core.os_helper import harden_windows_process

# Force Windows high-resolution timers (1ms precision)
if os.name == 'nt':
    import ctypes

    ctypes.windll.winmm.timeBeginPeriod(1)

from src.core.network_map import (
    ZMQ_PORT_SPELLMAN_PUB, ZMQ_PORT_SPELLMAN_CMD, ZMQ_PORT_PLC_PUB,
    ZMQ_PORT_HEARTBEAT, TOPIC_SPELLMAN_DATA
)

# Protocol Constants
STX = "\x02"
LF = "\x0A"
SOCKET_TIMEOUT = 1.0
POLL_INTERVAL = 0.1
MAX_CMD_AGE = 0.5

# MPD Commands (per Manual 48113-21 Issue 3, Section 3.2)
CMD_REQ_STATUS = "SR"
CMD_REQ_KV = "M0"
CMD_REQ_MA = "M1"
CMD_SET_KV = "V1"
CMD_SET_MA = "I1"
CMD_HV_ENABLE = "EN"


class SpellmanMPDProtocol:
    def __init__(self, ip: str, port: int, bus_id: str):
        self.ip = ip
        self.port = port
        self.bus_id = bus_id
        self.sock = None
        self.connected = False

    def connect(self):
        print(f"[MPD_DEBUG][{self.bus_id}] Attempting TCP connection to {self.ip}:{self.port}...")
        if self.sock:
            try:
                print(f"[MPD_DEBUG][{self.bus_id}] Closing existing stale socket before reconnect.")
                self.sock.close()
            except Exception as e:
                print(f"[MPD_DEBUG][{self.bus_id}] Error closing stale socket: {e}")
                pass
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.sock.settimeout(SOCKET_TIMEOUT)
            self.sock.bind(('192.168.1.100', 0))
            self.sock.connect((self.ip, self.port))
            self.connected = True
            print(f"[MPD Driver] {self.bus_id} Connected successfully to {self.ip}:{self.port}")
        except Exception as e:
            self.connected = False
            self.sock = None
            print(f"[MPD Driver] {self.bus_id} Connection failed. Exception type: {type(e).__name__}, Args: {e.args}")

    def _calculate_checksum(self, payload: str) -> str:
        ascii_sum = sum(ord(c) for c in payload)
        csum = 0x200 - ascii_sum
        csum = csum & 0xFF
        csum = csum & 0x7F
        csum = csum | 0x40
        calc_str = f"{csum:02X}"
        return calc_str

    def _generate_frame(self, address: str, dev_type: str, cmd: str, operator: str = "", data: str = "") -> bytes:
        payload = f"{address}{dev_type}{cmd}{operator}{data}"
        csum = self._calculate_checksum(payload)
        frame = f"{STX}{payload}{csum}{LF}".encode('ascii')
        return frame

    def transaction(self, address: str, dev_type: str, cmd: str, operator: str = "", data: str = "") -> Optional[str]:
        if not self.connected:
            print(f"[MPD_DEBUG][{self.bus_id}] Transaction aborted: Hardware not connected.")
            return None

        try:
            # Clear stale data from RX buffer
            stale_cleared = 0
            while select.select([self.sock], [], [], 0.0)[0]:
                dump = self.sock.recv(1024)
                stale_cleared += len(dump)

            frame = self._generate_frame(address, dev_type, cmd, operator, data)
            self.sock.sendall(frame)
            time.sleep(0.04)

            res = b""
            start_time = time.time()
            while not res.endswith(LF.encode('ascii')):
                chunk = self.sock.recv(1)
                if not chunk:
                    break
                res += chunk
                if time.time() - start_time > SOCKET_TIMEOUT:
                    break

            res_str = res.decode('ascii')

            if res_str.startswith(STX) and res_str.endswith(LF):
                parsed_data = res_str[1:-3]
                return parsed_data
            else:
                return None

        except socket.timeout:
            return None
        except Exception as e:
            print(f"[MPD_DEBUG][{self.bus_id}] Unexpected transaction exception: {type(e).__name__} -> {e}")
            self.connected = False
            return None


class SpellmanMicroservice:
    def __init__(self):
        self.context = zmq.Context()

        self.pub_socket = self.context.socket(zmq.PUB)
        self.pub_socket.setsockopt(zmq.SNDHWM, 5)
        self.pub_socket.bind(ZMQ_PORT_SPELLMAN_PUB)

        self.plc_socket = self.context.socket(zmq.SUB)
        self.plc_socket.connect(ZMQ_PORT_PLC_PUB)
        self.plc_socket.setsockopt_string(zmq.SUBSCRIBE, "")

        self.sub_socket = self.context.socket(zmq.SUB)
        self.sub_socket.setsockopt(zmq.RCVHWM, 5)
        self.sub_socket.bind(ZMQ_PORT_SPELLMAN_CMD)
        self.sub_socket.setsockopt_string(zmq.SUBSCRIBE, "")

        self.hb_socket = self.context.socket(zmq.PUB)
        self.hb_socket.connect(ZMQ_PORT_HEARTBEAT)

        self.events = EventHelper("service_spellman_mpd")

        # --- ISA-95 Physical Routing Map ---
        self.location_routing = {
            "source_einzel": "ion_beam.source.einzel",
            "beamline_einzel": "ion_beam.beamline.einzel",
            "neutral_trap_pos": "ion_beam.beamline.neutral_trap_pos",
            "neutral_trap_neg": "ion_beam.beamline.neutral_trap_neg"
        }

        self.safety_relay_active = False
        self.config = self._load_config()
        self.net_config = self._load_net_config()
        self.registry_limits = self._load_registry_limits()
        self.buses: Dict[str, SpellmanMPDProtocol] = {}

        self.state: Dict[str, Any] = {"timestamp": 0.0, "system.cycle_time_ms": 0.0}
        self.sp_cache: Dict[str, float] = {}
        self.last_hb_time = 0.0

        # --- Arbitration State per device ---
        self.active_ctrl_modes: Dict[str, int] = {prefix: 0 for prefix in self.location_routing.values()}

        # --- Software Arc Detection Memory ---
        self.dev_settle_timers: Dict[str, float] = {}
        self.arc_fault_latches: Dict[str, bool] = {}

    def _load_config(self) -> dict:
        config_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../config/mpd_config.json'))
        print(f"[SRV_DEBUG] Attempting to load MPD config from: {config_path}")
        try:
            with open(config_path, "r") as f:
                data = json.load(f)
                print(f"[SRV_DEBUG] MPD config loaded successfully. Identified {len(data.get('buses', []))} buses.")
                return data
        except Exception as e:
            self.events.log_general(f"[Spellman] CRITICAL: Failed to load mpd_config.json: {e}")
            print(f"[SRV_DEBUG] MPD config load exception: {e}")
            return {"buses": []}

    def _load_net_config(self) -> dict:
        config_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../config/network_config.json'))
        print(f"[SRV_DEBUG] Attempting to load Network config from: {config_path}")
        try:
            with open(config_path, "r") as f:
                return json.load(f)
        except Exception as e:
            print(f"[SRV_DEBUG] Network config load exception: {e}")
            return {}

    def _load_registry_limits(self) -> dict:
        registry_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../config/system_tags.json'))
        limits = {}
        try:
            with open(registry_path, "r") as f:
                registry = json.load(f)
                for tag, data in registry.items():
                    if data.get("source") == "service_spellman" and data.get("auto_controllable"):
                        limits[tag] = {
                            "min_val": data.get("min_val"),
                            "max_val": data.get("max_val")
                        }
        except Exception as e:
            print(f"[SRV_DEBUG] Failed to load registry limits: {e}")
        return limits

    def _update_safety_permissives(self):
        try:
            while True:
                topic, msg = self.plc_socket.recv_multipart(flags=zmq.NOBLOCK)
                payload = orjson.loads(msg)
                relay_state = payload.get("ion_beam.facilities.safety_relay_active")
                if relay_state is not None:
                    if self.safety_relay_active != bool(relay_state):
                        print(
                            f"[SRV_DEBUG] Safety Relay state transition: {self.safety_relay_active} -> {bool(relay_state)}")
                    self.safety_relay_active = bool(relay_state)
        except zmq.Again:
            pass

    def _manage_connections(self):
        ws_list = self.net_config.get("waveshares", {}).get("spellman", {})

        for bus in self.config.get("buses", []):
            bus_id = bus["bus_id"]
            if bus_id not in self.buses:
                target_net = ws_list.get(bus_id.replace("_waveshare", ""))
                if target_net:
                    print(
                        f"[SRV_DEBUG] Initializing bus '{bus_id}' mapping to IP: {target_net['ip']}:{target_net['port']}")
                    self.buses[bus_id] = SpellmanMPDProtocol(target_net["ip"], target_net["port"], bus_id)
                else:
                    print(f"[SRV_DEBUG] WARNING: Bus '{bus_id}' not found in network_config under waveshares.spellman.")
                    continue

            if not self.buses[bus_id].connected:
                self.buses[bus_id].connect()

    def _process_commands(self):
        try:
            while True:
                msg = self.sub_socket.recv_json(flags=zmq.NOBLOCK)
                tag = msg.get("tag", "")
                value = msg.get("value", 0)
                ts = msg.get("ts", 0.0)
                origin = msg.get("origin", "hmi")

                age = time.time() - ts
                if age > MAX_CMD_AGE:
                    print(f"[SRV_DEBUG] Rejecting stale command '{tag}' (Age: {age:.3f}s > {MAX_CMD_AGE}s)")
                    continue

                # --- GLOBAL FAULT RESET ---
                if tag == "ion_beam.system.cmd_fault_reset" and bool(value):
                    print("[SRV_DEBUG] Global Fault Reset received. Clearing MPD hardware and software latches...")
                    for key in self.arc_fault_latches.keys():
                        self.arc_fault_latches[key] = False
                    for bus in self.config.get("buses", []):
                        hw = self.buses.get(bus["bus_id"])
                        if hw and hw.connected:
                            for dev_info in bus.get("devices", {}).values():
                                hw.transaction(dev_info["address"], dev_info["dev_type"], "CF", "=", "1")
                    continue

                # --- Route to specific equipment module ---
                target_dev = None
                cmd_type = None
                prefix = None

                for dev_name, loc_prefix in self.location_routing.items():
                    if tag.startswith(loc_prefix):
                        target_dev = dev_name
                        prefix = loc_prefix
                        cmd_type = tag.split('.')[-1]
                        break

                if not target_dev:
                    continue

                # --- 1. ARBITRATION: Handle Control Mode Changes ---
                if cmd_type == "cmd_ctrl_mode":
                    try:
                        self.active_ctrl_modes[prefix] = int(value)
                        self.events.log_general(f"Arbitration: {target_dev} mode set to {int(value)}")
                    except (ValueError, TypeError):
                        pass
                    continue

                # --- 2. ARBITRATION: Enforce Lockout per Device ---
                active_mode = self.active_ctrl_modes.get(prefix, 0)
                if active_mode > 0 and origin != "optimizer":
                    continue

                target_bus, dev_info = None, None
                for bus in self.config.get("buses", []):
                    if target_dev in bus.get("devices", {}):
                        target_bus = bus["bus_id"]
                        dev_info = bus["devices"][target_dev]
                        break

                if target_bus and self.buses.get(target_bus):
                    hw = self.buses[target_bus]
                    addr = dev_info["address"]
                    dtype = dev_info["dev_type"]

                    try:
                        value_float = float(value)
                    except (ValueError, TypeError):
                        continue

                    if cmd_type == "sp_requested_voltage":
                        tag_limits = self.registry_limits.get(tag, {})
                        min_v = tag_limits.get("min_val")
                        max_v = tag_limits.get("max_val")

                        if min_v is None or max_v is None or not (min_v <= value_float <= max_v):
                            continue

                        volts_req = value_float * 1000.0
                        formatted_v = f"{volts_req:07.1f}"

                        print(f"[SRV_DEBUG] Translating {value_float}kV -> {formatted_v}V for {target_dev}")
                        res = hw.transaction(addr, dtype, CMD_SET_KV, "=", formatted_v)

                        if res and "*" not in res:
                            self.sp_cache[f"{prefix}.sp_actual_voltage"] = value_float
                        else:
                            print(f"[SRV_DEBUG] FAILED to set voltage. Hardware rejected command. Response: {res}")


                    elif cmd_type == "sp_requested_current":
                        tag_limits = self.registry_limits.get(tag, {})
                        min_i = tag_limits.get("min_val")
                        max_i = tag_limits.get("max_val")

                        if min_i is None or max_i is None or not (min_i <= value_float <= max_i):
                            continue

                        ua_req = value_float if value_float > 0.0 else 0.0
                        formatted_i = f"{ua_req:07.1f}"
                        print(f"[SRV_DEBUG] Forwarding {value_float}uA -> {formatted_i}uA for {target_dev}")
                        res = hw.transaction(addr, dtype, CMD_SET_MA, "=", formatted_i)

                        if res and "*" not in res:
                            self.sp_cache[f"{prefix}.sp_actual_current"] = value_float
                        else:
                            print(
                                f"[SRV_DEBUG] FAILED to set current limit. Hardware rejected command. Response: {res}")

                    elif cmd_type == "cmd_enable":
                        enable_str = "1" if value else "0"
                        print(f"[SRV_DEBUG] Attempting HV Enable/Disable ({enable_str}) for {target_dev}")
                        if hw.transaction(addr, dtype, CMD_HV_ENABLE, "=", enable_str) is not None:
                            if value:
                                self.dev_settle_timers[target_dev] = time.time()
                else:
                    print(f"[SRV_DEBUG] Hardware mapping failed for command targeting '{target_dev}'.")

        except zmq.Again:
            pass

    def _poll_device(self, bus_id: str, dev_name: str, dev_info: dict):
        addr, dtype = dev_info["address"], dev_info["dev_type"]

        hw = self.buses.get(bus_id)
        if not hw or not hw.connected:
            return

        prefix = self.location_routing.get(dev_name, f"ion_beam.source.{dev_name}")

        # Publish active control mode
        self.state[f"{prefix}.rb_ctrl_mode"] = float(self.active_ctrl_modes.get(prefix, 0))

        # 1. Poll live readbacks
        raw_kv = hw.transaction(addr, dtype, CMD_REQ_KV, "?")
        raw_ma = hw.transaction(addr, dtype, CMD_REQ_MA, "?")
        raw_status = hw.transaction(addr, dtype, CMD_REQ_STATUS, "?")

        # 2. Poll hardware-verified setpoints
        raw_sp_v = hw.transaction(addr, dtype, CMD_SET_KV, "?")
        raw_sp_i = hw.transaction(addr, dtype, CMD_SET_MA, "?")

        comms_fail = raw_kv is None or raw_status is None
        self.state[f"{prefix}.stat_comms_fail"] = 1.0 if comms_fail else 0.0

        if comms_fail:
            print(f"[SRV_DEBUG] Polling failed for '{dev_name}'. KV_Response: {raw_kv}, Status_Response: {raw_status}")
            return

        try:
            # Parse live readbacks (M0 / M1)
            if raw_kv:
                volts_rb = float(raw_kv.split('=')[-1])
                self.state[f"{prefix}.rb_voltage"] = volts_rb / 1000.0
            if raw_ma:
                ua_rb = float(raw_ma.split('=')[-1])
                self.state[f"{prefix}.rb_current"] = ua_rb

            # Parse Hardware Setpoints (V1 / I1)
            if raw_sp_v:
                sp_volts = float(raw_sp_v.split('=')[-1])
                self.state[f"{prefix}.sp_actual_voltage"] = sp_volts / 1000.0
            elif f"{prefix}.sp_actual_voltage" in self.sp_cache:
                self.state[f"{prefix}.sp_actual_voltage"] = self.sp_cache[f"{prefix}.sp_actual_voltage"]

            if raw_sp_i:
                sp_ua = float(raw_sp_i.split('=')[-1])
                self.state[f"{prefix}.sp_actual_current"] = sp_ua
            elif f"{prefix}.sp_actual_current" in self.sp_cache:
                self.state[f"{prefix}.sp_actual_current"] = self.sp_cache[f"{prefix}.sp_actual_current"]

        except ValueError as e:
            print(
                f"[SRV_DEBUG] Telemetry Float Casting Error for '{dev_name}': {e}. Raw V1: '{raw_sp_v}', Raw I1: '{raw_sp_i}'")

        if raw_status:
            try:
                status_hex_str = raw_status.split('=')[-1]
                status_word = int(status_hex_str, 16)

                is_enabled = bool(status_word & 0x01)
                self.state[f"{prefix}.stat_enabled"] = 1.0 if is_enabled else 0.0

                # Map directly to ISA-95 Equipment flags
                self.state[f"{prefix}.stat_overvoltage"] = 1.0 if (status_word & 0x04) else 0.0
                self.state[f"{prefix}.stat_overcurrent"] = 1.0 if (status_word & 0x08) else 0.0

                # Combine Over-temp and Power-rail collapse into generic Hardware Fail
                self.state[f"{prefix}.stat_fail"] = 1.0 if (status_word & 0x30) else 0.0

                # --- TEMPORARY RAW STATUS DEBUG ---
                self.state[f"{prefix}.debug_raw_status"] = raw_status
                self.state[f"{prefix}.debug_general_fault"] = 1.0 if (status_word & 0x02) else 0.0
                self.state[f"{prefix}.debug_hw_enable"] = 1.0 if (status_word & 0x40) else 0.0
                self.state[f"{prefix}.debug_sw_enable"] = 1.0 if (status_word & 0x80) else 0.0
                # ----------------------------------

                # --- AUTONOMOUS SOFTWARE ARC DETECTION ---
                active_sp_v = self.state.get(f"{prefix}.sp_actual_voltage", 0.0)
                is_settled = (time.time() - self.dev_settle_timers.get(dev_name, time.time())) > 3.0

                if is_settled and is_enabled and active_sp_v > 0.5:
                    if volts_rb < (active_sp_v * 0.25) or ua_rb > 500.0:
                        if not self.arc_fault_latches.get(dev_name, False):
                            print(
                                f"[SRV_DEBUG] ⚡ ARC DETECTED on {dev_name}! V_rb: {volts_rb / 1000.0}kV, I_rb: {ua_rb}µA")
                        self.arc_fault_latches[dev_name] = True

                self.state[f"{prefix}.stat_arc_exceeded"] = 1.0 if self.arc_fault_latches.get(dev_name, False) else 0.0

            except Exception as e:
                print(f"[SRV_DEBUG] Failed to parse Status Register for '{dev_name}'. Error: {e}. Raw: '{raw_status}'")

    def run(self):
        self.events.log_general("[Spellman Service] Daemon Starting (ISA-95 Architecture)...")
        next_tick = time.perf_counter() + POLL_INTERVAL

        while True:
            cycle_start = time.perf_counter()
            self._update_safety_permissives()

            if self.safety_relay_active:
                self._manage_connections()
                self._process_commands()
                for bus in self.config.get("buses", []):
                    for dev_name, dev_info in bus.get("devices", {}).items():
                        self._poll_device(bus["bus_id"], dev_name, dev_info)
                        time.sleep(0.01)
            else:
                for bus_id, hw in self.buses.items():
                    if hw.connected and hw.sock:
                        print(f"[SRV_DEBUG] Safety Relay OFF. Severing connection to bus '{bus_id}'.")
                        hw.sock.close()
                        hw.connected = False

            self.state["timestamp"] = time.time()
            self.state["system.cycle_time_ms"] = (time.perf_counter() - cycle_start) * 1000

            try:
                self.pub_socket.send_multipart([TOPIC_SPELLMAN_DATA, orjson.dumps(self.state)])
            except Exception as e:
                print(f"[SRV_DEBUG] ZMQ Publish Exception: {e}")

            if time.time() - self.last_hb_time >= 0.5:
                self.hb_socket.send_json({"service": "service_spellman_mpd", "ts": time.time()})
                self.last_hb_time = time.time()

            sleep_time = next_tick - time.perf_counter()
            if sleep_time > 0.002: time.sleep(sleep_time - 0.002)
            while time.perf_counter() < next_tick: pass
            next_tick += POLL_INTERVAL


if __name__ == "__main__":
    harden_windows_process()
    SpellmanMicroservice().run()