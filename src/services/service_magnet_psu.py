import time
import zmq
import json
import os
import struct
from typing import Dict, Any, Optional, Tuple

from pymodbus.client import ModbusTcpClient
from pymodbus.framer import FramerType

from src.core.network_config import (
    MAGNET_IP, MAGNET_PORT, ZMQ_PORT_MAGNET_PUB, ZMQ_PORT_MAGNET_CMD, TOPIC_MAGNET_DATA, ZMQ_PORT_PLC_PUB
)

# Performance & Safety Constants
POLL_INTERVAL = 0.05  # 50ms polling loop
MAX_CMD_AGE = 0.5  # Max age for UI commands
PLC_WATCHDOG_AGE = 1.0  # Max age for PLC safety permissives


class MagnetModbusProtocol:
    def __init__(self, ip: str, port: int = 502):
        self.ip = ip
        self.port = port
        self.client = None
        self.connected = False

        # Hardware Ratings (Populated dynamically on connect)
        self.nom_v = 1.0
        self.nom_i = 1.0
        self.nom_p = 1.0

    def connect(self) -> bool:
        # 1. Purge the dead socket completely
        if self.client:
            try:
                self.client.close()
            except Exception:
                pass

        # 2. Re-instantiate the client for a 100% clean slate
        self.client = ModbusTcpClient(self.ip, port=self.port, framer=FramerType.RTU, timeout=0.5)
        try:
            if self.client.connect():
                self.connected = True
                print(f"[Magnet Driver] Modbus TCP Connected to {self.ip}:{self.port}")

                res = self.client.read_holding_registers(address=121, count=6)
                if not res.isError():
                    regs = res.registers
                    self.nom_v = struct.unpack('>f', struct.pack('>HH', regs[0], regs[1]))[0]
                    self.nom_i = struct.unpack('>f', struct.pack('>HH', regs[2], regs[3]))[0]
                    self.nom_p = struct.unpack('>f', struct.pack('>HH', regs[4], regs[5]))[0]
                    print(f"[Magnet Driver] Hardware Ratings Detected: {self.nom_v}V, {self.nom_i}A, {self.nom_p}W")

                # 2. Take Remote Control (Coil 402)
                self.client.write_coil(402, True)

                # 3. Clear existing alarms (Coil 411)
                self.client.write_coil(411, True)

                return True
            else:
                return False

        except Exception as e:
            print(f"[Magnet Driver] Initialization failed: {e}")
            self.connected = False
            return False

    def disconnect(self):
        """Forces the output off and releases remote control on shutdown."""
        if self.connected:
            print("[Magnet Driver] Executing graceful hardware shutdown...")
            try:
                self.client.write_coil(405, False)  # Output OFF
                self.client.write_coil(402, False)  # Remote Control OFF
            except:
                pass
            self.client.close()
            self.connected = False

    def _scale_to_modbus(self, value: float, nominal: float) -> int:
        """Translates real floating values to EA Modbus 0-100% scale (0-52428)"""
        if nominal <= 0: return 0
        scaled = int((value * 52428.0) / nominal)
        return max(0, min(52428, scaled))  # Clamp safely within 0x0000 - 0xCCCC

    def _scale_from_modbus(self, hex_val: int, nominal: float) -> float:
        """Translates EA Modbus percentages back to real floats"""
        return (hex_val * nominal) / 52428.0

    def set_voltage(self, v_set: float):
        if self.connected:
            val = self._scale_to_modbus(v_set, self.nom_v)
            self.client.write_register(500, val)

    def set_current(self, i_set: float):
        if self.connected:
            val = self._scale_to_modbus(i_set, self.nom_i)
            self.client.write_register(501, val)

    def set_output(self, state: bool):
        if self.connected:
            self.client.write_coil(405, state)

    def clear_alarms(self):
        if self.connected:
            self.client.write_coil(411, True)

    def read_telemetry(self) -> Optional[Tuple[float, float, bool, dict]]:
        """
        Reads a contiguous block (Registers 505-508) containing:
        - Device Status 1 (32-bit: 505 & 506)
        - Actual Voltage (16-bit: 507)
        - Actual Current (16-bit: 508)
        """
        if not self.connected: return None

        try:
            res = self.client.read_holding_registers(address=505, count=4)
            if res.isError():
                self.connected = False
                return None

            regs = res.registers

            # Combine registers 505 and 506 for the 32-bit Status Word
            status_word = (regs[0] << 16) | regs[1]

            # Decode Actuals
            v_act = self._scale_from_modbus(regs[2], self.nom_v)
            i_act = self._scale_from_modbus(regs[3], self.nom_i)

            # Decode Status & Alarms
            outp_enabled = bool(status_word & 0x00000080)  # Bit 7

            alarms = {
                "OVP": bool(status_word & 0x00004000),  # Bit 14
                "OCP": bool(status_word & 0x00008000),  # Bit 15
                "OPP": bool(status_word & 0x00010000),  # Bit 16
                "OT": bool(status_word & 0x00020000),  # Bit 17
                "PF": bool(status_word & 0x00800000)  # Bit 23
            }

            return v_act, i_act, outp_enabled, alarms

        except Exception:
            self.connected = False
            return None


class MagnetMicroservice:
    def __init__(self):
        self.context = zmq.Context()

        self.pub_socket = self.context.socket(zmq.PUB)
        self.pub_socket.setsockopt(zmq.SNDHWM, 5)
        self.pub_socket.bind(ZMQ_PORT_MAGNET_PUB)

        self.sub_socket = self.context.socket(zmq.SUB)
        self.sub_socket.setsockopt(zmq.RCVHWM, 5)
        self.sub_socket.bind(ZMQ_PORT_MAGNET_CMD)
        self.sub_socket.setsockopt_string(zmq.SUBSCRIBE, "")

        self.plc_socket = self.context.socket(zmq.SUB)
        self.plc_socket.connect(ZMQ_PORT_PLC_PUB)
        self.plc_socket.setsockopt_string(zmq.SUBSCRIBE, "")

        self.limits = self._load_config()
        self.hw = MagnetModbusProtocol(MAGNET_IP, MAGNET_PORT)

        self.stat_safety_relay = False
        self.last_plc_ts = 0.0
        self.safety_tripped = True
        self.last_setpoint_ts = 0.0

        self.state: Dict[str, Any] = {
            "timestamp": 0.0,
            "system": {"cycle_time_ms": 0.0, "safe_to_run": False},
            "telemetry": {},
            "faults": {}
        }

    def _load_config(self) -> dict:
        config_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../config/magnet_config.json'))
        default_config = {
            "max_voltage": 10.0,
            "max_current": 30.0,
            "max_power": 200.0,
            "min_current_for_calc": 2.0,
            "nominal_resistance": 0.16,
            "resistance_tolerance": 0.05,
            "short_circuit_threshold": 0.05,
            "open_circuit_threshold": 5.0
        }
        try:
            with open(config_path, "r") as f:
                loaded = json.load(f)
                default_config.update(loaded)
        except Exception as e:
            print(f"[Magnet Service] CRITICAL: config load failed, using defaults. {e}")

        return default_config

    def _update_safety_permissives(self):
        try:
            while True:
                topic, msg = self.plc_socket.recv_multipart(flags=zmq.NOBLOCK)
                payload = json.loads(msg.decode('utf-8'))

                self.last_plc_ts = time.time()
                stat_safety_relay = payload.get("telemetry", {}).get("ion_beam.facilities.safety_relay_active")
                if stat_safety_relay is not None:
                    self.stat_safety_relay = stat_safety_relay

        except zmq.Again:
            pass

        comms_alive = (time.time() - self.last_plc_ts) < PLC_WATCHDOG_AGE
        self.safety_tripped = not (self.stat_safety_relay and comms_alive)
        self.state["system"]["safe_to_run"] = not self.safety_tripped

    def _process_commands(self):
        if not self.hw.connected:
            return

        if self.safety_tripped:
            self.hw.set_voltage(0.0)
            self.hw.set_current(0.0)
            self.hw.set_output(False)
            try:
                while True: self.sub_socket.recv_json(flags=zmq.NOBLOCK)
            except zmq.Again:
                pass
            return

        max_v = self.limits.get("max_voltage", 0.0)
        max_i = self.limits.get("max_current", 0.0)

        try:
            while True:
                msg = self.sub_socket.recv_json(flags=zmq.NOBLOCK)
                tag = msg.get("tag", "")
                raw_value = msg.get("value", 0)
                ts = msg.get("ts", 0.0)

                if time.time() - ts > MAX_CMD_AGE: continue
                parts = tag.split('.')
                if len(parts) < 3 or parts[1] != "magnet": continue
                cmd_type = parts[2]

                try:
                    value = float(raw_value)
                except (ValueError, TypeError):
                    print(f"[ZMQ CMD] WARN: Rejected '{tag}' - Invalid float type.")
                    continue

                if cmd_type == "voltage_sp":
                    if 0.0 <= value <= max_v:
                        self.hw.set_voltage(value)
                        self.last_setpoint_ts = time.time()
                    else:
                        print(f"[ZMQ CMD] Rejected V SP: {value} out of bounds")

                elif cmd_type == "current_sp":
                    if 0.0 <= value <= max_i:
                        self.hw.set_current(value)
                        self.last_setpoint_ts = time.time()
                    else:
                        print(f"[ZMQ CMD] Rejected I SP: {value} out of bounds")

                elif cmd_type == "cmd_enable":
                    self.hw.set_output(bool(value))
                    self.last_setpoint_ts = time.time()

        except zmq.Again:
            pass

    def _poll_device(self):
        if not self.hw.connected:
            return

        prefix = "ion_beam.magnet"
        telemetry_data = self.hw.read_telemetry()

        comms_fail = telemetry_data is None

        self.state["faults"]["Magnet_Comms_Fail"] = {
            "active": comms_fail, "severity": 1, "description": "Magnet PSU Modbus timeout."
        } if comms_fail else False

        if comms_fail: return

        v_rb, i_rb, outp_enabled, internal_alarms = telemetry_data

        self.state["telemetry"][f"{prefix}.voltage_rb"] = round(v_rb, 3)
        self.state["telemetry"][f"{prefix}.current_rb"] = round(i_rb, 3)
        self.state["telemetry"][f"{prefix}.stat_enabled"] = outp_enabled

        # Map Hardware Alarms
        self.state["faults"]["Magnet_PSU_OverTemp"] = {"active": internal_alarms["OT"], "severity": 1,
                                                       "description": "Magnet PSU Internal Overtemperature"} if \
        internal_alarms["OT"] else False
        self.state["faults"]["Magnet_PSU_PowerFail"] = {"active": internal_alarms["PF"], "severity": 1,
                                                        "description": "Magnet PSU AC Mains Power Fail"} if \
        internal_alarms["PF"] else False
        self.state["faults"]["Magnet_PSU_OVP"] = {"active": internal_alarms["OVP"], "severity": 1,
                                                  "description": "Magnet PSU Hardware Overvoltage Trip"} if \
        internal_alarms["OVP"] else False
        self.state["faults"]["Magnet_PSU_OCP"] = {"active": internal_alarms["OCP"], "severity": 1,
                                                  "description": "Magnet PSU Hardware Overcurrent Trip"} if \
        internal_alarms["OCP"] else False
        self.state["faults"]["Magnet_PSU_OPP"] = {"active": internal_alarms["OPP"], "severity": 1,
                                                  "description": "Magnet PSU Hardware Overpower Trip"} if \
        internal_alarms["OPP"] else False

        # Clear hardware alarm latches if any trip occurred
        if any(internal_alarms.values()):
            self.hw.clear_alarms()

        # Resistance Calculation and Fault Logic
        short_fault, open_fault, unexp_fault = False, False, False

        # Apply 250ms settling time check
        is_settled = (time.time() - self.last_setpoint_ts) > 0.250

        if outp_enabled and i_rb > self.limits["min_current_for_calc"] and is_settled:
            resistance = v_rb / i_rb
            self.state["telemetry"][f"{prefix}.resistance_rb"] = round(resistance, 3)

            if resistance < self.limits["short_circuit_threshold"]:
                short_fault = True
            elif resistance > self.limits["open_circuit_threshold"]:
                open_fault = True
            elif abs(resistance - self.limits["nominal_resistance"]) > self.limits["resistance_tolerance"]:
                unexp_fault = True
        else:
            self.state["telemetry"][f"{prefix}.resistance_rb"] = 0.0

        # Update Local Fault Dictionary
        self.state["faults"]["Magnet_Short_Circuit"] = {"active": short_fault, "severity": 1,
                                                        "description": "Magnet PSU appears short-circuited."} if short_fault else False
        self.state["faults"]["Magnet_Open_Circuit"] = {"active": open_fault, "severity": 1,
                                                       "description": "Magnet PSU appears open-circuited."} if open_fault else False
        self.state["faults"]["Magnet_Unexpected_Res"] = {"active": unexp_fault, "severity": 1,
                                                         "description": "Magnet has unexpected resistance value."} if unexp_fault else False

    def run(self):
        print("[Magnet Service] Daemon Starting (Modbus Architecture)...")

        # Ensure the user has configured the front panel safety setting
        print(
            "[Magnet Service] SAFETY NOTE: Ensure 'Output State after Remote' is set to OFF on the physical front panel menus.")

        last_connect_attempt = 0.0
        reconnect_interval = 0.5  # Wait 0.5 seconds between retry attempts

        try:
            while True:
                cycle_start = time.perf_counter()

                if not self.hw.connected:
                    if time.time() - last_connect_attempt > reconnect_interval:
                        last_connect_attempt = time.time()
                        self.hw.connect()
                    else:
                        time.sleep(0.05)
                        continue  # Skip polling until reconnected

                self._update_safety_permissives()

                if self.hw.connected:
                    self._process_commands()
                    self._poll_device()

                self.state["timestamp"] = time.time()
                elapsed = time.perf_counter() - cycle_start
                self.state["system"]["cycle_time_ms"] = round(elapsed * 1000, 2)

                try:
                    topic = TOPIC_MAGNET_DATA if isinstance(TOPIC_MAGNET_DATA, bytes) else TOPIC_MAGNET_DATA.encode(
                        'utf-8')
                    self.pub_socket.send_multipart([topic, json.dumps(self.state).encode('utf-8')])
                except Exception as e:
                    print(f"[Magnet Service] ZMQ Publish Error: {e}")

                time.sleep(max(0.0, POLL_INTERVAL - elapsed))

        except KeyboardInterrupt:
            print("\n[Magnet Service] Process interrupted by user.")
        finally:
            print("[Magnet Service] Cleaning up connections...")
            self.hw.disconnect()
            self.pub_socket.close()
            self.sub_socket.close()
            self.plc_socket.close()
            self.context.term()


if __name__ == "__main__":
    MagnetMicroservice().run()