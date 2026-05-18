import time
import zmq
import os
import struct
from typing import Dict, Any, Optional, Tuple
import orjson
import json
from src.core.event_helper import EventHelper

# Force Windows high-resolution timers (1ms precision)
if os.name == 'nt':
    import ctypes

    ctypes.windll.winmm.timeBeginPeriod(1)

from pymodbus.client import ModbusTcpClient
from pymodbus.framer import FramerType

from src.core.network_config import (
    MAGNET_IP, MAGNET_PORT, ZMQ_PORT_MAGNET_PUB, ZMQ_PORT_MAGNET_CMD, TOPIC_MAGNET_DATA, ZMQ_PORT_PLC_PUB,
    ZMQ_PORT_HEARTBEAT)

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

        # Hardware Ratings
        self.nom_v = 1.0
        self.nom_i = 1.0
        self.nom_p = 1.0

    def connect(self) -> bool:
        if self.client:
            try:
                self.client.close()
            except Exception:
                pass

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

                self.client.write_coil(402, True)
                self.client.write_coil(411, True)
                return True
            else:
                return False
        except Exception as e:
            print(f"[Magnet Driver] Initialization failed: {e}")
            self.connected = False
            return False

    def disconnect(self):
        if self.connected:
            print("[Magnet Driver] Executing graceful hardware shutdown...")
            try:
                self.client.write_coil(405, False)
                self.client.write_coil(402, False)
            except:
                pass
            self.client.close()
            self.connected = False

    def _scale_to_modbus(self, value: float, nominal: float) -> int:
        if nominal <= 0: return 0
        scaled = int((value * 52428.0) / nominal)
        return max(0, min(52428, scaled))

    def _scale_from_modbus(self, hex_val: int, nominal: float) -> float:
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
        if not self.connected: return None

        try:
            res = self.client.read_holding_registers(address=505, count=4)
            if res.isError():
                self.connected = False
                return None

            regs = res.registers
            status_word = (regs[0] << 16) | regs[1]

            v_act = self._scale_from_modbus(regs[2], self.nom_v)
            i_act = self._scale_from_modbus(regs[3], self.nom_i)
            outp_enabled = bool(status_word & 0x00000080)

            alarms = {
                "OVP": bool(status_word & 0x00004000),
                "OCP": bool(status_word & 0x00008000),
                "OPP": bool(status_word & 0x00010000),
                "OT": bool(status_word & 0x00020000),
                "PF": bool(status_word & 0x00800000)
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

        # Strict 1D Flat Payload
        self.state: Dict[str, Any] = {
            "timestamp": 0.0,
            "system.cycle_time_ms": 0.0,
            "system.safe_to_run": 0.0
        }

        self.hb_socket = self.context.socket(zmq.PUB)
        self.hb_socket.connect(ZMQ_PORT_HEARTBEAT)
        self.last_hb_time = 0.0

        self.events = EventHelper("service_magnet_psu")

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
            self.events.log_general(f"[Magnet Service] CRITICAL: config load failed, using defaults. {e}")
        return default_config

    def _update_safety_permissives(self):
        try:
            while True:
                topic, msg = self.plc_socket.recv_multipart(flags=zmq.NOBLOCK)
                # Decode 1D Payload from PLC
                payload = orjson.loads(msg)

                self.last_plc_ts = time.time()
                stat_safety_relay = payload.get("ion_beam.facilities.safety_relay_active")
                if stat_safety_relay is not None:
                    self.stat_safety_relay = bool(stat_safety_relay)

        except zmq.Again:
            pass

        comms_alive = (time.time() - self.last_plc_ts) < PLC_WATCHDOG_AGE
        self.safety_tripped = not (self.stat_safety_relay and comms_alive)
        self.state["system.safe_to_run"] = 0.0 if self.safety_tripped else 1.0

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
                    continue

                if cmd_type == "voltage_sp":
                    if 0.0 <= value <= max_v:
                        self.hw.set_voltage(value)
                        self.last_setpoint_ts = time.time()
                elif cmd_type == "current_sp":
                    if 0.0 <= value <= max_i:
                        self.hw.set_current(value)
                        self.last_setpoint_ts = time.time()
                elif cmd_type == "cmd_enable":
                    self.hw.set_output(bool(value))
                    self.last_setpoint_ts = time.time()

        except zmq.Again:
            pass

    def _poll_device(self):
        if not self.hw.connected:
            return

        telemetry_data = self.hw.read_telemetry()
        comms_fail = telemetry_data is None

        self.state["ion_beam.magnet.status.stat_comms_fail"] = 1.0 if comms_fail else 0.0

        if comms_fail: return

        v_rb, i_rb, outp_enabled, internal_alarms = telemetry_data

        self.state["ion_beam.magnet.voltage_rb"] = round(v_rb, 3)
        self.state["ion_beam.magnet.current_rb"] = round(i_rb, 3)
        self.state["ion_beam.magnet.stat_enabled"] = 1.0 if outp_enabled else 0.0

        # Map Hardware Alarms specifically for the PLC broker
        self.state["ion_beam.magnet.status.stat_psu_overtemp"] = 1.0 if internal_alarms["OT"] else 0.0
        self.state["ion_beam.magnet.status.stat_psu_powerfail"] = 1.0 if internal_alarms["PF"] else 0.0
        self.state["ion_beam.magnet.status.stat_psu_ovp"] = 1.0 if internal_alarms["OVP"] else 0.0
        self.state["ion_beam.magnet.status.stat_psu_ovc"] = 1.0 if internal_alarms["OCP"] else 0.0

        if any(internal_alarms.values()):
            self.hw.clear_alarms()

        short_fault, open_fault, unexp_fault = False, False, False
        is_settled = (time.time() - self.last_setpoint_ts) > 0.250

        if outp_enabled and i_rb > self.limits["min_current_for_calc"] and is_settled:
            resistance = v_rb / i_rb
            self.state["ion_beam.magnet.resistance_rb"] = round(resistance, 3)

            if resistance < self.limits["short_circuit_threshold"]:
                short_fault = True
            elif resistance > self.limits["open_circuit_threshold"]:
                open_fault = True
            elif abs(resistance - self.limits["nominal_resistance"]) > self.limits["resistance_tolerance"]:
                unexp_fault = True
        else:
            self.state["ion_beam.magnet.resistance_rb"] = 0.0

        self.state["ion_beam.magnet.status.stat_short_circuit"] = 1.0 if short_fault else 0.0
        self.state["ion_beam.magnet.status.stat_open_circuit"] = 1.0 if open_fault else 0.0
        self.state["ion_beam.magnet.status.stat_unexpected_res"] = 1.0 if unexp_fault else 0.0

    def run(self):
        self.events.log_general("[Magnet Service] Daemon Starting (Modbus Architecture)...")
        print(
            "[Magnet Service] SAFETY NOTE: Ensure 'Output State after Remote' is set to OFF on the physical front panel menus.")

        last_connect_attempt = 0.0
        reconnect_interval = 0.5

        current_time_pc = time.perf_counter()
        next_tick = current_time_pc + POLL_INTERVAL

        try:
            while True:
                cycle_start = time.perf_counter()

                if not self.hw.connected:
                    if time.time() - last_connect_attempt > reconnect_interval:
                        last_connect_attempt = time.time()
                        self.hw.connect()
                    else:
                        sleep_time = next_tick - time.perf_counter()
                        if sleep_time > 0.002:
                            time.sleep(sleep_time - 0.002)
                        while time.perf_counter() < next_tick:
                            pass
                        next_tick += POLL_INTERVAL
                        continue

                self._update_safety_permissives()

                if self.hw.connected:
                    self._process_commands()
                    self._poll_device()

                self.state["timestamp"] = time.time()
                elapsed = time.perf_counter() - cycle_start
                self.state["system.cycle_time_ms"] = round(elapsed * 1000, 2)

                try:
                    topic = TOPIC_MAGNET_DATA if isinstance(TOPIC_MAGNET_DATA, bytes) else TOPIC_MAGNET_DATA.encode(
                        'utf-8')
                    self.pub_socket.send_multipart([topic, orjson.dumps(self.state)])
                except Exception as e:
                    self.events.log_general(f"[Magnet Service] ZMQ Publish Error: {e}")

                current_time = time.time()
                if current_time - self.last_hb_time >= 0.5:
                    self.hb_socket.send_json({"service": "service_magnet_psu", "ts": current_time})
                    self.last_hb_time = current_time

                # Strict Spin-Wait OS Scheduling
                sleep_time = next_tick - time.perf_counter()
                if sleep_time > 0.002:
                    time.sleep(sleep_time - 0.002)

                while time.perf_counter() < next_tick:
                    pass

                next_tick += POLL_INTERVAL

        except KeyboardInterrupt:
            self.events.log_general("\n[Magnet Service] Process interrupted by user.")
        finally:
            self.events.log_general("[Magnet Service] Cleaning up connections...")
            self.hw.disconnect()
            self.pub_socket.close()
            self.sub_socket.close()
            self.plc_socket.close()
            self.context.term()


if __name__ == "__main__":
    MagnetMicroservice().run()