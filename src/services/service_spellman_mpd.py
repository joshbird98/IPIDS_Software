import socket
import time
import zmq
import json
import select
import os
from typing import Dict, Any, Optional

from src.core.network_config import (
    ZMQ_PORT_SPELLMAN_PUB, ZMQ_PORT_SPELLMAN_CMD, TOPIC_SPELLMAN_DATA, ZMQ_PORT_PLC_PUB,
    SPELLMAN_WAVESHARES,
    ZMQ_PORT_HEARTBEAT)

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
        if not self.connected:
            return None
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
        except Exception as e:
            self.connected = False
            return None


class SpellmanMicroservice:
    def __init__(self):
        self.context = zmq.Context()

        # 1. ZMQ Setup
        self.pub_socket = self.context.socket(zmq.PUB)
        self.pub_socket.setsockopt(zmq.SNDHWM, 5)
        self.pub_socket.bind(ZMQ_PORT_SPELLMAN_PUB)

        self.plc_socket = self.context.socket(zmq.SUB)
        self.plc_socket.connect(ZMQ_PORT_PLC_PUB)
        self.plc_socket.setsockopt_string(zmq.SUBSCRIBE, "")

        self.safety_relay_active = False  # Assume unsafe until proven otherwise

        self.sub_socket = self.context.socket(zmq.SUB)
        self.sub_socket.setsockopt(zmq.RCVHWM, 5)
        self.sub_socket.bind(ZMQ_PORT_SPELLMAN_CMD)
        self.sub_socket.setsockopt_string(zmq.SUBSCRIBE, "")

        # 2. Dynamic Hardware Topology
        self.config = self._load_config()
        self.buses: Dict[str, SpellmanMPDProtocol] = {}

        # 3. Unified Payload Structure
        self.state: Dict[str, Any] = {
            "timestamp": 0.0,
            "system": {
                "cycle_time_ms": 0.0
            },
            "telemetry": {},
            "faults": {}
        }

        # Setup the Heartbeat Publisher
        self.hb_socket = self.context.socket(zmq.PUB)
        self.hb_socket.connect(ZMQ_PORT_HEARTBEAT)
        self.last_hb_time = 0.0

    def _update_safety_permissives(self):
        """Listens to the PLC telemetry to see if physical power is available."""
        try:
            while True:
                topic, msg = self.plc_socket.recv_multipart(flags=zmq.NOBLOCK)
                payload = json.loads(msg.decode('utf-8'))

                # Extract the safety relay boolean from the PLC's telemetry stream
                relay_state = payload.get("telemetry", {}).get("ion_beam.facilities.safety_relay_active")
                if relay_state is not None:
                    self.safety_relay_active = relay_state

        except zmq.Again:
            pass

    def _load_config(self) -> dict:
        config_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../config/mpd_config.json'))
        try:
            with open(config_path, "r") as f:
                return json.load(f)
        except Exception as e:
            print(f"[Spellman Service] CRITICAL: Failed to load mpd_config.json: {e}")
            return {"buses": []}

    def _manage_connections(self):
        """Ensures all Waveshare TCP connections defined in config are alive."""
        for bus in self.config.get("buses", []):
            bus_id = bus["bus_id"]

            # If we haven't created a socket for this bus yet
            if bus_id not in self.buses:
                # Look up the IP/Port from network_config.py's SPELLMAN_WAVESHARES dict
                ws_config = SPELLMAN_WAVESHARES.get(bus_id, {})
                ip = ws_config.get("ip")
                port = ws_config.get("port")

                if ip and port:
                    self.buses[bus_id] = SpellmanMPDProtocol(ip, port, bus_id)
                else:
                    print(f"[Spellman Service] WARNING: {bus_id} IP/Port not found in network_map.json")
                    continue

            # Reconnect if dropped
            if not self.buses[bus_id].connected:
                self.buses[bus_id].connect()

    def _process_commands(self):
        try:
            while True:
                msg = self.sub_socket.recv_json(flags=zmq.NOBLOCK)

                # Extract standard tag format
                tag = msg.get("tag", "")
                value = msg.get("value", 0)
                ts = msg.get("ts", 0.0)

                if time.time() - ts > MAX_CMD_AGE:
                    continue

                    # Parse the tag: "ion_beam.spellman.beamline_einzel.voltage_sp"
                parts = tag.split('.')
                if len(parts) < 4 or parts[1] != "spellman":
                    continue

                target_dev = parts[2]  # e.g., "beamline_einzel"
                cmd_type = parts[3]  # e.g., "voltage_sp"

                # Find which bus and what address this device lives on
                target_bus = None
                dev_info = None
                for bus in self.config.get("buses", []):
                    if target_dev in bus.get("devices", {}):
                        target_bus = bus["bus_id"]
                        dev_info = bus["devices"][target_dev]
                        break

                if target_bus and self.buses.get(target_bus):
                    hw = self.buses[target_bus]
                    addr = dev_info["address"]
                    dtype = dev_info["dev_type"]

                    # Map the tag ending to the specific hardware command
                    if cmd_type == "voltage_sp":
                        hw.transaction(addr, dtype, CMD_SET_KV, ",", str(value))
                    elif cmd_type == "current_sp":
                        hw.transaction(addr, dtype, CMD_SET_MA, ",", str(value))
                    elif cmd_type == "cmd_enable":
                        # Convert boolean True/False to "1" or "0"
                        enable_str = "1" if value else "0"
                        hw.transaction(addr, dtype, CMD_HV_ENABLE, ",", enable_str)

        except zmq.Again:
            pass

    def _poll_device(self, bus_id: str, dev_name: str, dev_info: dict):
        # 1. Hardware mapping extraction
        addr = dev_info["address"]
        dtype = dev_info["dev_type"]
        plc_unit = dev_info.get("plc_unit", 1)  # Default to 1 if missing

        hw = self.buses.get(bus_id)
        if not hw or not hw.connected:
            return

        prefix = f"ion_beam.spellman.{dev_name}"

        # 2. Query Data
        raw_kv = hw.transaction(addr, dtype, CMD_REQ_KV, "?")
        raw_ma = hw.transaction(addr, dtype, CMD_REQ_MA, "?")
        raw_status = hw.transaction(addr, dtype, CMD_REQ_STATUS, "?")

        comms_fail = raw_kv is None or raw_status is None

        # 3. Output the exact string the PLC Data Broker expects!
        self.state["faults"][f"Unit{plc_unit}_Comms_Fail"] = {
            "active": comms_fail, "severity": 2, "description": f"RS485 Timeout on {dev_name}"
        } if comms_fail else False

        if comms_fail: return

        # 4. Parse Analog Values
        try:
            if raw_kv:
                self.state["telemetry"][f"{prefix}.voltage_rb"] = float(raw_kv.split(',')[-1])
            if raw_ma:
                self.state["telemetry"][f"{prefix}.current_rb"] = float(raw_ma.split(',')[-1])
        except ValueError:
            pass

        # 5. Parse Status Words
        if raw_status:
            # IMPORTANT: Update string slicing per your Spellman manual
            status_parts = raw_status.split(',')

            if len(status_parts) >= 4:
                hv_enabled = status_parts[0] == "1"
                overcurrent = status_parts[1] == "1"
                undervoltage = status_parts[2] == "1"
                arc_exceeded = status_parts[3] == "1"

                self.state["telemetry"][f"{prefix}.stat_enabled"] = hv_enabled

                self.state["faults"][f"Unit{plc_unit}_OverCurrent"] = {
                    "active": overcurrent, "severity": 1, "description": "Overcurrent limit reached."
                } if overcurrent else False

                self.state["faults"][f"Unit{plc_unit}_UnderVoltage"] = {
                    "active": undervoltage, "severity": 2, "description": "Regulation error / Undervoltage."
                } if undervoltage else False

                self.state["faults"][f"Unit{plc_unit}_Arc_Exceeded"] = {
                    "active": arc_exceeded, "severity": 1, "description": "Maximum arc rate exceeded."
                } if arc_exceeded else False

    def run(self):
        print("[Spellman Service] Daemon Starting...")
        while True:
            cycle_start = time.perf_counter()

            self._update_safety_permissives()

            if self.safety_relay_active:
                # 1. Maintain TCP connections to all buses
                self._manage_connections()
                # 2. Process Commands
                self._process_commands()

                # 3. Poll all devices across all buses
                for bus in self.config.get("buses", []):
                    bus_id = bus["bus_id"]
                    for dev_name, dev_info in bus.get("devices", {}).items():
                        self._poll_device(bus_id, dev_name, dev_info)
                        time.sleep(0.01)  # Brief RS485 turnaround pause

            else:
                # If E-Stop is pressed, gracefully close connections so they don't hang
                for bus_id, hw in self.buses.items():
                    if hw.connected and hw.sock:
                        hw.sock.close()
                        hw.connected = False

            # 4. Broadcast
            self.state["timestamp"] = time.time()
            elapsed = time.perf_counter() - cycle_start
            self.state["system"]["cycle_time_ms"] = elapsed * 1000

            try:
                topic = TOPIC_SPELLMAN_DATA if isinstance(TOPIC_SPELLMAN_DATA, bytes) else TOPIC_SPELLMAN_DATA.encode(
                    'utf-8')
                self.pub_socket.send_multipart([topic, json.dumps(self.state).encode('utf-8')])
            except Exception as e:
                print(f"[Spellman Service] ZMQ Publish Error: {e}")

            # Pulse the heartbeat twice per second
            current_time = time.time()
            if current_time - self.last_hb_time >= 0.5:
                # Ensure the "service" string exactly matches the key in SERVICES_CONFIG
                self.hb_socket.send_json({"service": "service_logger", "ts": current_time})
                self.last_hb_time = current_time

            time.sleep(max(0.0, POLL_INTERVAL - elapsed))


if __name__ == "__main__":
    SpellmanMicroservice().run()