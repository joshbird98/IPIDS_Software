import time
import zmq
import os
import struct
from typing import Dict, Any, Optional, Tuple
import orjson
import json
from src.core.event_helper import EventHelper
from src.core.os_helper import harden_windows_process

if os.name == 'nt':
    import ctypes

    ctypes.windll.winmm.timeBeginPeriod(1)

from pymodbus.client import ModbusTcpClient
from pymodbus.framer import FramerType

from src.core.network_map import (
    MAGNET_IP, MAGNET_PORT, ZMQ_PORT_MAGNET_PUB, ZMQ_PORT_MAGNET_CMD, TOPIC_MAGNET_DATA, ZMQ_PORT_PLC_PUB,
    ZMQ_PORT_HEARTBEAT)

POLL_INTERVAL = 0.05
MAX_CMD_AGE = 0.5
PLC_WATCHDOG_AGE = 1.0


class MagnetModbusProtocol:
    def __init__(self, ip: str, port: int = 502):
        self.ip = ip
        self.port = port
        self.client = None
        self.connected = False
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
                self.client.write_coil(402, True)
                self.client.write_coil(411, True)
                return True
            return False
        except Exception as e:
            self.connected = False
            return False

    def disconnect(self):
        if self.connected:
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
        if self.connected: self.client.write_register(500, self._scale_to_modbus(v_set, self.nom_v))

    def set_current(self, i_set: float):
        if self.connected: self.client.write_register(501, self._scale_to_modbus(i_set, self.nom_i))

    def set_output(self, state: bool):
        if self.connected: self.client.write_coil(405, state)

    def clear_alarms(self):
        if self.connected: self.client.write_coil(411, True)

    def read_telemetry(self) -> Optional[Tuple[float, float, bool, dict]]:
        if not self.connected: return None
        try:
            res = self.client.read_holding_registers(address=505, count=4)
            if res.isError():
                self.connected = False
                return None
            regs = res.registers
            status_word = (regs[0] << 16) | regs[1]
            alarms = {
                "OVP": bool(status_word & 0x00004000),
                "OCP": bool(status_word & 0x00008000),
                "OPP": bool(status_word & 0x00010000),
                "OT": bool(status_word & 0x00020000),
                "PF": bool(status_word & 0x00800000)
            }
            return self._scale_from_modbus(regs[2], self.nom_v), self._scale_from_modbus(regs[3], self.nom_i), bool(
                status_word & 0x00000080), alarms
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
        self.registry_limits = self._load_registry_limits()
        self.hw = MagnetModbusProtocol(MAGNET_IP, MAGNET_PORT)

        self.stat_safety_relay = False
        self.last_plc_ts = 0.0
        self.safety_tripped = True
        self.last_setpoint_ts = 0.0
        self.sp_cache: Dict[str, float] = {}

        # Arbitration State
        self.active_ctrl_mode = 0

        # Non-Blocking Degauss State Machine
        self.degauss_active = False
        self.degauss_step_idx = 0
        self.degauss_last_step_ts = 0.0

        self.state: Dict[str, Any] = {"timestamp": 0.0, "system.cycle_time_ms": 0.0, "system.safe_to_run": 0.0}

        self.hb_socket = self.context.socket(zmq.PUB)
        self.hb_socket.connect(ZMQ_PORT_HEARTBEAT)
        self.last_hb_time = 0.0

        self.events = EventHelper("service_magnet_psu")

    def _load_config(self) -> dict:
        config_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../config/magnet_config.json'))
        default_config = {
            "max_voltage": 10.0,
            "max_power": 200.0,
            "min_current_for_calc": 2.0,
            "nominal_resistance": 0.16,
            "resistance_tolerance": 0.05,
            "short_circuit_threshold": 0.05,
            "open_circuit_threshold": 5.0,
            "degauss_steps_amps": [30.0, 0.0, 20.0, 0.0, 10.0, 0.0, 5.0, 0.0],
            "degauss_step_time_sec": 1.5
        }
        try:
            with open(config_path, "r") as f:
                default_config.update(json.load(f))
        except Exception:
            pass
        return default_config

    def _load_registry_limits(self) -> dict:
        registry_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../config/system_tags.json'))
        limits = {}
        try:
            with open(registry_path, "r") as f:
                registry = json.load(f)
                for tag, data in registry.items():
                    if data.get("source") == "service_magnet" and data.get("auto_controllable"):
                        limits[tag] = {
                            "min_val": data.get("min_val"),
                            "max_val": data.get("max_val")
                        }
        except Exception as e:
            print(f"[Magnet Service] Failed to load registry limits: {e}")
        return limits

    def _apply_smart_setpoint(self, target_current: float):
        nom_res = self.limits.get("nominal_resistance", 0.16)
        max_v = self.limits.get("max_voltage", 10.0)

        # Calculate required voltage (Current * Resistance * 1.20 for 20% overhead)
        # Enforce an absolute minimum of 1.0V and cap at hardware maximum
        target_voltage = max(1.0, target_current * nom_res * 1.20)
        target_voltage = min(target_voltage, max_v)

        # Get the currently requested current to determine the direction of travel
        current_sp_i = self.sp_cache.get("ion_beam.beamline.magnet.sp_actual_current", 0.0)

        if target_current >= current_sp_i:
            # RAMPING UP: Raise Voltage limit FIRST, then push current
            self.hw.set_voltage(target_voltage)
            self.hw.set_current(target_current)
        else:
            # RAMPING DOWN: Drop current FIRST, then lower Voltage limit
            self.hw.set_current(target_current)
            self.hw.set_voltage(target_voltage)

        # Update cache to reflect the new hardware state
        self.sp_cache["ion_beam.beamline.magnet.sp_actual_voltage"] = target_voltage
        self.sp_cache["ion_beam.beamline.magnet.sp_actual_current"] = target_current

    def _update_safety_permissives(self):
        try:
            while True:
                topic, msg = self.plc_socket.recv_multipart(flags=zmq.NOBLOCK)
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

    def _execute_degauss_cycle(self):
        if not self.degauss_active: return

        now = time.time()
        step_delay = self.limits.get("degauss_step_time_sec", 1.5)
        steps = self.limits.get("degauss_steps_amps", [])

        if now - self.degauss_last_step_ts > step_delay:
            if self.degauss_step_idx < len(steps):
                target_amps = steps[self.degauss_step_idx]

                # Route degauss steps through the smart sequencer
                self._apply_smart_setpoint(target_amps)

                self.degauss_last_step_ts = now
                self.degauss_step_idx += 1
            else:
                self.degauss_active = False
                self.events.log_general("Degaussing cycle completed.")

    def _process_commands(self):
        if not self.hw.connected: return

        if self.safety_tripped:
            self.hw.set_voltage(0.0)
            self.hw.set_current(0.0)
            self.hw.set_output(False)
            self.degauss_active = False
            try:
                while True: self.sub_socket.recv_json(flags=zmq.NOBLOCK)
            except zmq.Again:
                pass
            return

        # Execute automated degauss sequence if active
        self._execute_degauss_cycle()

        try:
            while True:
                msg = self.sub_socket.recv_json(flags=zmq.NOBLOCK)
                tag = msg.get("tag", "")
                raw_value = msg.get("value", 0)
                ts = msg.get("ts", 0.0)
                origin = msg.get("origin", "hmi")

                if time.time() - ts > MAX_CMD_AGE: continue

                # --- 1. ARBITRATION: Handle Control Mode Changes ---
                if tag == "ion_beam.beamline.magnet.cmd_ctrl_mode":
                    try:
                        self.active_ctrl_mode = int(raw_value)
                        self.events.log_general(f"Arbitration: Magnet mode set to {self.active_ctrl_mode}")
                    except (ValueError, TypeError):
                        pass
                    continue

                # --- 2. ARBITRATION: Enforce Lockout ---
                if self.active_ctrl_mode > 0 and (origin not in ["optimizer", "mass_scan"]):
                    continue

                # --- 3. Normal Command Processing ---
                parts = tag.split('.')
                if len(parts) < 3 or parts[1] != "beamline": continue
                cmd_type = parts[-1]

                try:
                    value = float(raw_value)
                except (ValueError, TypeError):
                    continue

                if cmd_type == "sp_requested_voltage":
                    pass

                elif cmd_type == "sp_requested_current":
                    tag_limits = self.registry_limits.get(tag, {})
                    min_i = tag_limits.get("min_val")
                    max_i = tag_limits.get("max_val")
                    print(max_i)

                    # Fail safe: Do not execute if limits are undefined in the registry
                    if min_i is None or max_i is None:
                        continue

                    if min_i <= value <= max_i:
                        self.degauss_active = False
                        self._apply_smart_setpoint(value)
                        self.last_setpoint_ts = time.time()

                elif cmd_type == "cmd_enable":
                    self.hw.set_output(bool(value))
                    self.last_setpoint_ts = time.time()

                elif cmd_type == "cmd_degauss" and bool(value):
                    self.events.log_general("Initiating autonomous Degauss sequence...")
                    self.degauss_active = True
                    self.degauss_step_idx = 0
                    self.degauss_last_step_ts = 0.0
                    self.hw.set_output(True)

        except zmq.Again:
            pass

    def _poll_device(self):
        if not self.hw.connected: return
        telemetry_data = self.hw.read_telemetry()
        comms_fail = telemetry_data is None

        self.state["ion_beam.beamline.magnet.stat_comms_fail"] = 1.0 if comms_fail else 0.0
        if comms_fail: return

        v_rb, i_rb, outp_enabled, internal_alarms = telemetry_data

        self.state["ion_beam.beamline.magnet.rb_voltage"] = round(v_rb, 3)
        self.state["ion_beam.beamline.magnet.rb_current"] = round(i_rb, 3)
        self.state["ion_beam.beamline.magnet.stat_enabled"] = 1.0 if outp_enabled else 0.0
        self.state["ion_beam.beamline.magnet.stat_degaussing"] = 1.0 if self.degauss_active else 0.0

        # Publish active control mode
        self.state["ion_beam.beamline.magnet.rb_ctrl_mode"] = float(self.active_ctrl_mode)

        if "ion_beam.beamline.magnet.sp_actual_voltage" in self.sp_cache:
            self.state["ion_beam.beamline.magnet.sp_actual_voltage"] = self.sp_cache[
                "ion_beam.beamline.magnet.sp_actual_voltage"]
        if "ion_beam.beamline.magnet.sp_actual_current" in self.sp_cache:
            self.state["ion_beam.beamline.magnet.sp_actual_current"] = self.sp_cache[
                "ion_beam.beamline.magnet.sp_actual_current"]

        # Hardware Alarms - Flattened into Equipment Root
        self.state["ion_beam.beamline.magnet.stat_psu_overtemp"] = 1.0 if internal_alarms["OT"] else 0.0
        self.state["ion_beam.beamline.magnet.stat_psu_powerfail"] = 1.0 if internal_alarms["PF"] else 0.0
        self.state["ion_beam.beamline.magnet.stat_psu_ovp"] = 1.0 if internal_alarms["OVP"] else 0.0
        self.state["ion_beam.beamline.magnet.stat_psu_ovc"] = 1.0 if internal_alarms["OCP"] else 0.0

        if any(internal_alarms.values()): self.hw.clear_alarms()

        short_fault, open_fault, unexp_fault = False, False, False
        is_settled = (time.time() - self.last_setpoint_ts) > 1.500

        if outp_enabled and i_rb > self.limits["min_current_for_calc"] and is_settled:
            resistance = v_rb / i_rb
            self.state["ion_beam.beamline.magnet.rb_resistance"] = round(resistance, 3)
            if resistance < self.limits["short_circuit_threshold"]:
                short_fault = True
            elif resistance > self.limits["open_circuit_threshold"]:
                open_fault = True
            elif abs(resistance - self.limits["nominal_resistance"]) > self.limits["resistance_tolerance"]:
                unexp_fault = True
        else:
            self.state["ion_beam.beamline.magnet.rb_resistance"] = 0.0

        self.state["ion_beam.beamline.magnet.stat_short_circuit"] = 1.0 if short_fault else 0.0
        self.state["ion_beam.beamline.magnet.stat_open_circuit"] = 1.0 if open_fault else 0.0
        self.state["ion_beam.beamline.magnet.stat_unexpected_res"] = 1.0 if unexp_fault else 0.0

    def run(self):
        self.events.log_general("[Magnet Service] Daemon Starting (Modbus Architecture)...")
        last_connect_attempt = 0.0
        reconnect_interval = 0.5
        next_tick = time.perf_counter() + POLL_INTERVAL

        try:
            while True:
                cycle_start = time.perf_counter()

                if not self.hw.connected:
                    if time.time() - last_connect_attempt > reconnect_interval:
                        last_connect_attempt = time.time()
                        self.hw.connect()
                    else:
                        sleep_time = next_tick - time.perf_counter()
                        if sleep_time > 0.002: time.sleep(sleep_time - 0.002)
                        while time.perf_counter() < next_tick: pass
                        next_tick += POLL_INTERVAL
                        continue

                self._update_safety_permissives()

                if self.hw.connected:
                    self._process_commands()
                    self._poll_device()

                self.state["timestamp"] = time.time()
                self.state["system.cycle_time_ms"] = round((time.perf_counter() - cycle_start) * 1000, 2)

                try:
                    topic = TOPIC_MAGNET_DATA if isinstance(TOPIC_MAGNET_DATA, bytes) else TOPIC_MAGNET_DATA.encode(
                        'utf-8')
                    self.pub_socket.send_multipart([topic, orjson.dumps(self.state)])
                except Exception:
                    pass

                if time.time() - self.last_hb_time >= 0.5:
                    self.hb_socket.send_json({"service": "service_magnet_psu", "ts": time.time()})
                    self.last_hb_time = time.time()

                sleep_time = next_tick - time.perf_counter()
                if sleep_time > 0.002: time.sleep(sleep_time - 0.002)
                while time.perf_counter() < next_tick: pass
                next_tick += POLL_INTERVAL

        except KeyboardInterrupt:
            self.events.log_general("\n[Magnet Service] Process interrupted by user.")
        finally:
            self.hw.disconnect()
            self.pub_socket.close()
            self.sub_socket.close()
            self.plc_socket.close()
            self.context.term()


if __name__ == "__main__":
    harden_windows_process()
    MagnetMicroservice().run()