import socket
import time
import zmq
import select
import os
import json
from typing import Dict, Any, Optional
import orjson
from src.core.event_helper import EventHelper

# Force Windows high-resolution timers (1ms precision)
if os.name == 'nt':
    import ctypes

    ctypes.windll.winmm.timeBeginPeriod(1)

from src.core.network_config import (
    ZMQ_PORT_SPELLMAN_PUB, ZMQ_PORT_SPELLMAN_CMD, ZMQ_PORT_PLC_PUB,
    ZMQ_PORT_HEARTBEAT
)

# Note: Ensure SPELLMAN_WAVESHARES is actually exposed in network_config,
# or load it directly from network_config.json dynamically below.

# Protocol Constants
STX = "\x02"
LF = "\x0A"
SOCKET_TIMEOUT = 1.0
POLL_INTERVAL = 0.1
MAX_CMD_AGE = 0.5

# MPD Commands
CMD_REQ_STATUS = "22"
CMD_REQ_KV = "19"
CMD_REQ_MA = "20"
CMD_SET_KV = "10"
CMD_SET_MA = "11"
CMD_HV_ENABLE = "99"


class SpellmanMPDProtocol:
    def __init__(self, ip: str, port: int, bus_id: str):
        self.ip = ip
        self.port = port
        self.bus_id = bus_id
        self.sock = None
        self.connected = False

    def connect(self):
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.sock.settimeout(SOCKET_TIMEOUT)
            self.sock.connect((self.ip, self.port))
            self.connected = True
            print(f"[MPD Driver] {self.bus_id} Connected to {self.ip}:{self.port}")
        except Exception as e:
            self.connected = False
            self.sock = None
            print(f"[MPD Driver] {self.bus_id} Connection failed: {e}")

    def _calculate_checksum(self, payload: str) -> str:
        ascii_sum = sum(ord(c) for c in payload)
        csum = 0x200 - ascii_sum
        csum = csum & 0xFF
        csum = csum & 0x7F
        csum = csum | 0x40
        return f"{csum:02X}"

    def _generate_frame(self, address: str, dev_type: str, cmd: str, operator: str = "", data: str = "") -> bytes:
        payload = f"{address}{dev_type}{cmd}{operator}{data}"
        csum = self._calculate_checksum(payload)
        return f"{STX}{payload}{csum}{LF}".encode('ascii')

    def transaction(self, address: str, dev_type: str, cmd: str, operator: str = "", data: str = "") -> Optional[str]:
        if not self.connected: return None
        try:
            while select.select([self.sock], [], [], 0.0)[0]:
                self.sock.recv(1024)

            frame = self._generate_frame(address, dev_type, cmd, operator, data)
            self.sock.sendall(frame)
            time.sleep(0.04)

            res = b""
            start_time = time.time()
            while not res.endswith(LF.encode('ascii')):
                chunk = self.sock.recv(1)
                if not chunk: break
                res += chunk
                if time.time() - start_time > SOCKET_TIMEOUT: break

            res_str = res.decode('ascii')
            if res_str.startswith(STX) and res_str.endswith(LF):
                return res_str[1:-3]
            return None
        except socket.timeout:
            return None
        except Exception:
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

        self.safety_relay_active = False
        self.config = self._load_config()
        self.net_config = self._load_net_config()
        self.buses: Dict[str, SpellmanMPDProtocol] = {}

        self.state: Dict[str, Any] = {"timestamp": 0.0, "system.cycle_time_ms": 0.0}

        # NEW: Cache setpoints to echo back to the GUI
        self.sp_cache: Dict[str, float] = {}

        self.last_hb_time = 0.0
        self.events = EventHelper("service_spellman_mpd")

    def _load_config(self) -> dict:
        config_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../config/mpd_config.json'))
        try:
            with open(config_path, "r") as f:
                return json.load(f)
        except Exception as e:
            self.events.log_general(f"[Spellman] CRITICAL: Failed to load mpd_config.json: {e}")
            return {"buses": []}

    def _load_net_config(self) -> dict:
        config_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../config/network_config.json'))
        try:
            with open(config_path, "r") as f:
                return json.load(f)
        except:
            return {}

    def _update_safety_permissives(self):
        try:
            while True:
                topic, msg = self.plc_socket.recv_multipart(flags=zmq.NOBLOCK)
                payload = orjson.loads(msg)
                relay_state = payload.get("ion_beam.facilities.safety_relay_active")
                if relay_state is not None:
                    self.safety_relay_active = bool(relay_state)
        except zmq.Again:
            pass

    def _manage_connections(self):
        ws_list = self.net_config.get("waveshares", {}).get("spellman", {})

        for bus in self.config.get("buses", []):
            bus_id = bus["bus_id"]
            if bus_id not in self.buses:
                # E.g. "beamline" or "chamber_waveshare" mappings
                target_net = ws_list.get(bus_id.replace("_waveshare", ""))
                if target_net:
                    self.buses[bus_id] = SpellmanMPDProtocol(target_net["ip"], target_net["port"], bus_id)
                else:
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

                if time.time() - ts > MAX_CMD_AGE: continue

                parts = tag.split('.')
                if len(parts) < 4 or parts[1] != "spellman": continue

                target_dev = parts[2]
                cmd_type = parts[3]

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
                    prefix = f"ion_beam.spellman.{target_dev}"

                    # NEW: Adjusted command routing to match GUI's standard suffixes
                    if cmd_type == "sp_requested_voltage":
                        if hw.transaction(addr, dtype, CMD_SET_KV, ",", str(value)) is not None:
                            self.sp_cache[f"{prefix}.sp_actual_voltage"] = float(value)
                    elif cmd_type == "sp_requested_current":
                        if hw.transaction(addr, dtype, CMD_SET_MA, ",", str(value)) is not None:
                            self.sp_cache[f"{prefix}.sp_actual_current"] = float(value)
                    elif cmd_type == "cmd_enable":
                        enable_str = "1" if value else "0"
                        hw.transaction(addr, dtype, CMD_HV_ENABLE, ",", enable_str)
        except zmq.Again:
            pass

    def _poll_device(self, bus_id: str, dev_name: str, dev_info: dict):
        addr, dtype = dev_info["address"], dev_info["dev_type"]
        plc_unit = dev_info.get("plc_unit", 1)

        hw = self.buses.get(bus_id)
        if not hw or not hw.connected: return

        prefix = f"ion_beam.spellman.{dev_name}"

        raw_kv = hw.transaction(addr, dtype, CMD_REQ_KV, "?")
        raw_ma = hw.transaction(addr, dtype, CMD_REQ_MA, "?")
        raw_status = hw.transaction(addr, dtype, CMD_REQ_STATUS, "?")

        comms_fail = raw_kv is None or raw_status is None
        self.state[f"ion_beam.spellman.status.stat_unit{plc_unit}_comms_fail"] = 1.0 if comms_fail else 0.0
        if comms_fail: return

        try:
            # NEW: Adjusted to GUI standards (rb_voltage instead of voltage_rb)
            if raw_kv: self.state[f"{prefix}.rb_voltage"] = float(raw_kv.split(',')[-1])
            if raw_ma: self.state[f"{prefix}.rb_current"] = float(raw_ma.split(',')[-1])

            # Echo cached setpoints
            if f"{prefix}.sp_actual_voltage" in self.sp_cache:
                self.state[f"{prefix}.sp_actual_voltage"] = self.sp_cache[f"{prefix}.sp_actual_voltage"]
            if f"{prefix}.sp_actual_current" in self.sp_cache:
                self.state[f"{prefix}.sp_actual_current"] = self.sp_cache[f"{prefix}.sp_actual_current"]

        except ValueError:
            pass

        if raw_status:
            status_parts = raw_status.split(',')
            if len(status_parts) >= 4:
                self.state[f"{prefix}.stat_enabled"] = 1.0 if status_parts[0] == "1" else 0.0
                self.state[f"ion_beam.spellman.status.stat_unit{plc_unit}_overcurrent"] = 1.0 if status_parts[
                                                                                                     1] == "1" else 0.0
                self.state[f"ion_beam.spellman.status.stat_unit{plc_unit}_undervoltage"] = 1.0 if status_parts[
                                                                                                      2] == "1" else 0.0
                self.state[f"ion_beam.spellman.status.stat_unit{plc_unit}_arc_exceeded"] = 1.0 if status_parts[
                                                                                                      3] == "1" else 0.0

    def run(self):
        self.events.log_general("[Spellman Service] Daemon Starting...")
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
                for hw in self.buses.values():
                    if hw.connected and hw.sock:
                        hw.sock.close()
                        hw.connected = False

            self.state["timestamp"] = time.time()
            self.state["system.cycle_time_ms"] = (time.perf_counter() - cycle_start) * 1000

            try:
                self.pub_socket.send_multipart([b"SPELLMAN", orjson.dumps(self.state)])
            except Exception as e:
                pass

            if time.time() - self.last_hb_time >= 0.5:
                self.hb_socket.send_json({"service": "service_spellman_mpd", "ts": time.time()})
                self.last_hb_time = time.time()

            sleep_time = next_tick - time.perf_counter()
            if sleep_time > 0.002: time.sleep(sleep_time - 0.002)
            while time.perf_counter() < next_tick: pass
            next_tick += POLL_INTERVAL


if __name__ == "__main__":
    SpellmanMicroservice().run()