import json
import logging
import os
import struct
import time
from typing import Any, Dict, Optional, Tuple

import orjson
import zmq

from src.core.commands import CommandServer
from src.core.event_helper import EventHelper
from src.core.log_setup import Throttle, setup_logging
from src.core.os_helper import harden_windows_process
from src.core.paths import REGISTRY_PATH, simulation_enabled

if os.name == 'nt':
    import ctypes

    ctypes.windll.winmm.timeBeginPeriod(1)

from pymodbus.client import ModbusTcpClient
from pymodbus.framer import FramerType

from src.core.network_map import (
    STEERER_MAGNET_IP, STEERER_MAGNET_PORT, ZMQ_PORT_STEERER_MAGNET_PUB, ZMQ_PORT_STEERER_MAGNET_CMD,
    TOPIC_STEERER_MAGNET_DATA, ZMQ_PORT_HEARTBEAT, ZMQ_PORT_PLC_PUB)

POLL_INTERVAL = 0.05
MAX_CMD_AGE = 0.5
PLC_WATCHDOG_AGE = 1.0      # relay state counts as unknown (= off) if the PLC goes quiet this long
FORCE_SAFE_REPEAT_S = 1.0   # while interlocked, re-send 'output off' this often
RECONNECT_DELAYS = (0.5, 1.0, 2.0, 5.0)
BASE = "ion_beam.beamline.steerer_magnet"

log = logging.getLogger("service_steerer_magnet_psu")


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
            except Exception as e:
                log.debug("close before reconnect: %s", e)

        self.client = ModbusTcpClient(self.ip, port=self.port, framer=FramerType.RTU, timeout=0.5)
        try:
            if self.client.connect():
                self.connected = True
                log.info("Modbus TCP connected to %s:%s", self.ip, self.port)
                res = self.client.read_holding_registers(address=121, count=6)
                if not res.isError():
                    regs = res.registers
                    self.nom_v = struct.unpack('>f', struct.pack('>HH', regs[0], regs[1]))[0]
                    self.nom_i = struct.unpack('>f', struct.pack('>HH', regs[2], regs[3]))[0]
                    self.nom_p = struct.unpack('>f', struct.pack('>HH', regs[4], regs[5]))[0]
                else:
                    log.warning("Could not read nominal ratings (%s); setpoint scaling may be wrong", res)
                self.client.write_coil(402, True)
                self.client.write_coil(411, True)
                return True
            return False
        except Exception as e:
            log.debug("connect failed: %s", e)
            self.connected = False
            return False

    def disconnect(self):
        if self.connected:
            try:
                self.client.write_coil(405, False)
                self.client.write_coil(402, False)
            except Exception as e:
                log.warning("Could not switch output off while disconnecting: %s", e)
            self.client.close()
            self.connected = False

    def _scale_to_modbus(self, value: float, nominal: float) -> int:
        if nominal <= 0: return 0
        scaled = int((value * 52428.0) / nominal)
        return max(0, min(52428, scaled))

    def _scale_from_modbus(self, hex_val: int, nominal: float) -> float:
        return (hex_val * nominal) / 52428.0

    def _checked(self, res, what):
        if res is not None and hasattr(res, "isError") and res.isError():
            raise IOError(f"PSU refused {what}: {res}")
        return True

    def set_voltage(self, v_set: float):
        if not self.connected: raise ConnectionError("PSU not connected")
        return self._checked(self.client.write_register(500, self._scale_to_modbus(v_set, self.nom_v)), "voltage")

    def set_current(self, i_set: float):
        if not self.connected: raise ConnectionError("PSU not connected")
        return self._checked(self.client.write_register(501, self._scale_to_modbus(i_set, self.nom_i)), "current")

    def set_output(self, state: bool):
        if not self.connected: raise ConnectionError("PSU not connected")
        return self._checked(self.client.write_coil(405, state), "output enable")

    def clear_alarms(self):
        if not self.connected: raise ConnectionError("PSU not connected")
        return self._checked(self.client.write_coil(411, True), "alarm clear")

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
        except Exception as e:
            log.warning("Telemetry read failed: %s", e)
            self.connected = False
            return None


def _make_driver():
    if simulation_enabled():
        from src.sim.magnet import FakeMagnetModbus
        return FakeMagnetModbus(STEERER_MAGNET_IP, STEERER_MAGNET_PORT, name="steerer", resistance=0.3,
                                nom_v=40.0, nom_i=80.0)
    return MagnetModbusProtocol(STEERER_MAGNET_IP, STEERER_MAGNET_PORT)


class SteererMagnetMicroservice:
    def __init__(self):
        self.context = zmq.Context()
        self.events = EventHelper("service_steerer_magnet_psu")

        self.pub_socket = self.context.socket(zmq.PUB)
        self.pub_socket.setsockopt(zmq.SNDHWM, 5)
        self.pub_socket.bind(ZMQ_PORT_STEERER_MAGNET_PUB)

        self.commands = CommandServer(ZMQ_PORT_STEERER_MAGNET_CMD, "service_steerer_magnet_psu",
                                      context=self.context, events=self.events, max_age=MAX_CMD_AGE)

        # Software safety interlock: the steerer PSU is not on the safety relay circuit, so the
        # service holds its output off whenever the PLC reports the relay de-energised (or is silent)
        self.plc_socket = self.context.socket(zmq.SUB)
        self.plc_socket.connect(ZMQ_PORT_PLC_PUB)
        self.plc_socket.setsockopt_string(zmq.SUBSCRIBE, "")
        self.stat_safety_relay = False
        self.last_plc_ts = 0.0
        self.safety_tripped = True
        self._last_force_safe = 0.0

        self.limits = self._load_config()
        self.registry_limits = self._load_registry_limits()
        self.hw = _make_driver()

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

        self._throttle = Throttle(60.0)
        self._connect_failures = 0
        self._next_connect_at = 0.0
        self.running = True

    def stop(self):
        self.running = False

    def _load_config(self) -> dict:
        config_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../config/steerer_magnet_config.json'))
        default_config = {
            "max_voltage": 10.0,
            "max_power": 200.0,
            "min_current_for_calc": 2.0,
            "nominal_resistance": 1.6,
            "resistance_tolerance": 0.2,
            "short_circuit_threshold": 0.5,
            "open_circuit_threshold": 5.0,
            "degauss_steps_amps": [50.0, 0.0, 40.0, 0.0, 30.0, 0.0, 20.0, 0.0, 10.0, 0.0, 5.0, 0.0],
            "degauss_step_time_sec": 1.5
        }
        try:
            with open(config_path, "r") as f:
                default_config.update(json.load(f))
        except (OSError, ValueError) as e:
            self.events.log_general(f"steerer_magnet_config.json not loaded, using built-in defaults: {e}",
                                    severity="WARNING")
        return default_config

    def _load_registry_limits(self) -> dict:
        registry_path = REGISTRY_PATH
        limits = {}
        try:
            with open(registry_path, "r") as f:
                registry = json.load(f)
                for tag, data in registry.items():
                    if data.get("source") == "service_steerer_magnet" and data.get("auto_controllable"):
                        limits[tag] = {
                            "min_val": data.get("min_val"),
                            "max_val": data.get("max_val")
                        }
        except (OSError, ValueError) as e:
            self.events.log_general(f"Failed to load registry limits (setpoints will be refused): {e}",
                                    severity="ERROR")
        return limits

    def _apply_smart_setpoint(self, target_current: float):
        nom_res = self.limits.get("nominal_resistance", 0.16)
        max_v = self.limits.get("max_voltage", 10.0)

        # Calculate required voltage (Current * Resistance * 1.20 for 20% overhead)
        # Enforce an absolute minimum of 1.0V and cap at hardware maximum
        target_voltage = max(1.0, target_current * nom_res * 1.20)
        target_voltage = min(target_voltage, max_v)

        # Get the currently requested current to determine the direction of travel
        current_sp_i = self.sp_cache.get("ion_beam.beamline.steerer_magnet.sp_actual_current", 0.0)

        if target_current >= current_sp_i:
            # RAMPING UP: Raise Voltage limit FIRST, then push current
            self.hw.set_voltage(target_voltage)
            self.hw.set_current(target_current)
        else:
            # RAMPING DOWN: Drop current FIRST, then lower Voltage limit
            self.hw.set_current(target_current)
            self.hw.set_voltage(target_voltage)

        # Update cache to reflect the new hardware state
        self.sp_cache["ion_beam.beamline.steerer_magnet.sp_actual_voltage"] = target_voltage
        self.sp_cache["ion_beam.beamline.steerer_magnet.sp_actual_current"] = target_current

    def _update_safety_permissives(self):
        try:
            while True:
                topic, msg = self.plc_socket.recv_multipart(flags=zmq.NOBLOCK)
                try:
                    payload = orjson.loads(msg)
                except ValueError:
                    continue
                self.last_plc_ts = time.time()
                # null = PLC disconnected = relay state unknown -> treat as off
                self.stat_safety_relay = bool(payload.get("ion_beam.facilities.safety_relay_active"))
        except zmq.Again:
            pass

        comms_alive = (time.time() - self.last_plc_ts) < PLC_WATCHDOG_AGE
        was_tripped = self.safety_tripped
        self.safety_tripped = not (self.stat_safety_relay and comms_alive)
        if self.safety_tripped != was_tripped:
            if self.safety_tripped:
                why = "safety relay de-energised" if comms_alive else "no PLC telemetry"
                self.events.log_general(f"Steerer output forced off: {why}", severity="WARNING")
                self._last_force_safe = 0.0   # act now, not on the next repeat
            else:
                self.events.log_general("Steerer safety permissive restored (output stays off until enabled)")
        self.state["system.safe_to_run"] = 0.0 if self.safety_tripped else 1.0

    def _force_safe_outputs(self):
        """Interlocked: hold the PSU at 0 A, 0 V, output off. Sent when the interlock trips, then
        every FORCE_SAFE_REPEAT_S, and at once if the PSU reports its output on anyway."""
        self.degauss_active = False
        if not self.hw.connected:
            return
        output_on = self.state.get(f"{BASE}.stat_enabled") == 1.0
        if not output_on and time.time() - self._last_force_safe < FORCE_SAFE_REPEAT_S:
            return
        try:
            self.hw.set_current(0.0)
            self.hw.set_voltage(0.0)
            self.hw.set_output(False)
            self.sp_cache[f"{BASE}.sp_actual_voltage"] = 0.0
            self.sp_cache[f"{BASE}.sp_actual_current"] = 0.0
            self._last_force_safe = time.time()
        except Exception as e:
            if (n := self._throttle.ready("force-safe")) is not None:
                self.events.log_general(f"Could not force steerer outputs off: {e}{Throttle.suffix(n)}",
                                        severity="ERROR")
            self.hw.connected = False

    def _execute_degauss_cycle(self):
        if not self.degauss_active: return

        now = time.time()
        step_delay = self.limits.get("degauss_step_time_sec", 10)
        steps = self.limits.get("degauss_steps_amps", [])

        if now - self.degauss_last_step_ts > step_delay:
            if self.degauss_step_idx < len(steps):
                target_amps = steps[self.degauss_step_idx]
                log.info("Degauss step %d/%d: %.1f A", self.degauss_step_idx + 1, len(steps), target_amps)

                # Route degauss steps through the smart sequencer
                try:
                    self._apply_smart_setpoint(target_amps)
                except Exception as e:
                    self.degauss_active = False
                    self.events.log_general(f"Degauss aborted - PSU write failed: {e}", severity="ERROR")
                    return

                self.degauss_last_step_ts = now
                self.degauss_step_idx += 1
            else:
                self.degauss_active = False
                self.events.log_general("Degaussing cycle completed.")

    def _process_commands(self):
        for cmd in self.commands.poll():
            tag, raw_value, origin = cmd.tag, cmd.value, cmd.origin

            # --- 1. ARBITRATION: Handle Control Mode Changes (no hardware access, always allowed) ---
            if tag == "ion_beam.beamline.steerer_magnet.cmd_ctrl_mode":
                try:
                    self.active_ctrl_mode = int(raw_value)
                except (ValueError, TypeError):
                    cmd.reject(f"invalid control mode {raw_value!r}")
                    continue
                self.events.log_general(f"Arbitration: Steerer Magnet mode set to {self.active_ctrl_mode}")
                cmd.ok()
                continue

            # --- 2. ARBITRATION: Enforce Lockout ---
            if self.active_ctrl_mode > 0 and (origin not in ["optimizer", "mass_scan"]):
                cmd.reject(f"locked out: steerer is in automatic control mode {self.active_ctrl_mode}")
                continue

            # --- 3. Normal Command Processing ---
            parts = tag.split('.')
            if len(parts) < 3 or not tag.startswith(BASE + "."):
                cmd.reject("not a steerer magnet tag")
                continue
            cmd_type = parts[-1]

            try:
                value = float(raw_value)
            except (ValueError, TypeError):
                cmd.reject(f"value {raw_value!r} is not a number")
                continue

            if not self.hw.connected:
                cmd.reject("steerer PSU not connected")
                continue
            if self.safety_tripped:
                if cmd_type == "cmd_enable" and not value:
                    cmd.ok("already off: safety interlock holds the output off")
                else:
                    cmd.reject("safety relay de-energised (or no PLC telemetry) - output held off")
                continue

            try:
                if cmd_type == "sp_requested_voltage":
                    cmd.reject("voltage is set automatically from the current setpoint")

                elif cmd_type == "sp_requested_current":
                    tag_limits = self.registry_limits.get(tag, {})
                    min_i = tag_limits.get("min_val")
                    max_i = tag_limits.get("max_val")

                    # Fail safe: Do not execute if limits are undefined in the registry
                    if min_i is None or max_i is None:
                        cmd.reject("no min/max limits configured for this tag in system_tags.json")
                    elif not (min_i <= value <= max_i):
                        cmd.reject(f"{value:g} A is outside the allowed range [{min_i:g}, {max_i:g}] A")
                    else:
                        self.degauss_active = False
                        self._apply_smart_setpoint(value)
                        self.last_setpoint_ts = time.time()
                        cmd.ok()

                elif cmd_type == "cmd_enable":
                    self.hw.set_output(bool(value))
                    self.last_setpoint_ts = time.time()
                    cmd.ok("output on" if value else "output off")

                elif cmd_type == "cmd_degauss":
                    if bool(value):
                        self.events.log_general("Initiating autonomous Degauss sequence...")
                        self.degauss_active = True
                        self.degauss_step_idx = 0
                        self.degauss_last_step_ts = 0.0
                        self.hw.set_output(True)
                        cmd.ok("degauss started")
                    else:
                        cmd.ok("nothing to do")
                else:
                    cmd.reject(f"unsupported command '{cmd_type}'")
            except Exception as e:  # PSU write failed part-way (connection lost / Modbus exception)
                cmd.fail(f"PSU write failed: {e}")
                log.warning("Command %s failed: %s", cmd, e)
                self.hw.connected = False
        self.commands.finish()

    def _publish_unknown(self):
        self.state[f"{BASE}.stat_comms_fail"] = 1.0
        for key in ("rb_voltage", "rb_current", "stat_enabled", "rb_resistance", "stat_psu_overtemp",
                    "stat_psu_powerfail", "stat_psu_ovp", "stat_psu_ovc", "stat_short_circuit",
                    "stat_open_circuit", "stat_unexpected_res"):
            self.state[f"{BASE}.{key}"] = None
        self.state[f"{BASE}.stat_degaussing"] = 1.0 if self.degauss_active else 0.0
        self.state[f"{BASE}.rb_ctrl_mode"] = float(self.active_ctrl_mode)

    def _poll_device(self):
        if not self.hw.connected:
            self._publish_unknown()
            return
        telemetry_data = self.hw.read_telemetry()
        if telemetry_data is None:
            self._publish_unknown()
            return
        self.state["ion_beam.beamline.steerer_magnet.stat_comms_fail"] = 0.0

        v_rb, i_rb, outp_enabled, internal_alarms = telemetry_data

        self.state["ion_beam.beamline.steerer_magnet.rb_voltage"] = round(v_rb, 3)
        self.state["ion_beam.beamline.steerer_magnet.rb_current"] = round(i_rb, 3)
        self.state["ion_beam.beamline.steerer_magnet.stat_enabled"] = 1.0 if outp_enabled else 0.0
        self.state["ion_beam.beamline.steerer_magnet.stat_degaussing"] = 1.0 if self.degauss_active else 0.0

        # Publish active control mode
        self.state["ion_beam.beamline.steerer_magnet.rb_ctrl_mode"] = float(self.active_ctrl_mode)

        if "ion_beam.beamline.steerer_magnet.sp_actual_voltage" in self.sp_cache:
            self.state["ion_beam.beamline.steerer_magnet.sp_actual_voltage"] = self.sp_cache[
                "ion_beam.beamline.steerer_magnet.sp_actual_voltage"]
        if "ion_beam.beamline.steerer_magnet.sp_actual_current" in self.sp_cache:
            self.state["ion_beam.beamline.steerer_magnet.sp_actual_current"] = self.sp_cache[
                "ion_beam.beamline.steerer_magnet.sp_actual_current"]

        # Hardware Alarms - Flattened into Equipment Root
        self.state["ion_beam.beamline.steerer_magnet.stat_psu_overtemp"] = 1.0 if internal_alarms["OT"] else 0.0
        self.state["ion_beam.beamline.steerer_magnet.stat_psu_powerfail"] = 1.0 if internal_alarms["PF"] else 0.0
        self.state["ion_beam.beamline.steerer_magnet.stat_psu_ovp"] = 1.0 if internal_alarms["OVP"] else 0.0
        self.state["ion_beam.beamline.steerer_magnet.stat_psu_ovc"] = 1.0 if internal_alarms["OCP"] else 0.0

        if any(internal_alarms.values()):
            active = ", ".join(k for k, v in internal_alarms.items() if v)
            if (n := self._throttle.ready(f"alarm-{active}")) is not None:
                self.events.log_general(f"Steerer PSU alarm: {active} (auto-clearing){Throttle.suffix(n)}",
                                        severity="WARNING")
            try:
                self.hw.clear_alarms()
            except Exception as e:
                log.warning("Alarm clear failed: %s", e)

        short_fault, open_fault, unexp_fault = False, False, False
        is_settled = (time.time() - self.last_setpoint_ts) > 1.500

        if outp_enabled and i_rb > self.limits["min_current_for_calc"] and is_settled:
            resistance = v_rb / i_rb
            # (was published under the misspelt tag 'ion_beam.beamline.steerer_.rb_resistance')
            self.state["ion_beam.beamline.steerer_magnet.rb_resistance"] = round(resistance, 3)
            if resistance < self.limits["short_circuit_threshold"]:
                short_fault = True
            elif resistance > self.limits["open_circuit_threshold"]:
                open_fault = True
            elif abs(resistance - self.limits["nominal_resistance"]) > self.limits["resistance_tolerance"]:
                unexp_fault = True
        else:
            self.state["ion_beam.beamline.steerer_magnet.rb_resistance"] = 0.0

        self.state["ion_beam.beamline.steerer_magnet.stat_short_circuit"] = 1.0 if short_fault else 0.0
        self.state["ion_beam.beamline.steerer_magnet.stat_open_circuit"] = 1.0 if open_fault else 0.0
        self.state["ion_beam.beamline.steerer_magnet.stat_unexpected_res"] = 1.0 if unexp_fault else 0.0

    def _try_connect(self):
        if time.time() < self._next_connect_at:
            return
        if self.hw.connect():
            if self._connect_failures:
                self.events.log_general(f"Steerer PSU reconnected after {self._connect_failures} failed attempt(s)")
            else:
                self.events.log_general(f"Connected to steerer PSU at {STEERER_MAGNET_IP}:{STEERER_MAGNET_PORT}")
            self._connect_failures = 0
            self._throttle.reset("connect")
        else:
            self._connect_failures += 1
            self._next_connect_at = time.time() + RECONNECT_DELAYS[
                min(self._connect_failures - 1, len(RECONNECT_DELAYS) - 1)]
            if (n := self._throttle.ready("connect")) is not None:
                self.events.log_general(
                    f"Steerer PSU at {STEERER_MAGNET_IP}:{STEERER_MAGNET_PORT} not reachable{Throttle.suffix(n)}",
                    severity="WARNING")

    def run(self):
        self.events.log_general("[Steerer Magnet Service] Daemon Starting (Modbus Architecture)..."
                                + (" (SIMULATED PSU)" if simulation_enabled() else ""))
        next_tick = time.perf_counter() + POLL_INTERVAL

        try:
            while self.running:
                cycle_start = time.perf_counter()

                if not self.hw.connected:
                    self._try_connect()

                self._update_safety_permissives()
                if self.safety_tripped:
                    self._force_safe_outputs()
                elif self.hw.connected:
                    self._execute_degauss_cycle()

                self._process_commands()   # always answered, with a reason when refused
                self._poll_device()

                self.state["timestamp"] = time.time()
                self.state["system.cycle_time_ms"] = round((time.perf_counter() - cycle_start) * 1000, 2)

                try:
                    self.pub_socket.send_multipart([TOPIC_STEERER_MAGNET_DATA, orjson.dumps(self.state)])
                except zmq.ZMQError as e:
                    if (n := self._throttle.ready("publish")) is not None:
                        log.error("ZMQ publish failed: %s%s", e, Throttle.suffix(n))

                if time.time() - self.last_hb_time >= 0.5:
                    self.hb_socket.send_json({"service": "service_steerer_magnet_psu", "ts": time.time()})
                    self.last_hb_time = time.time()

                sleep_time = next_tick - time.perf_counter()
                if sleep_time > 0.002: time.sleep(sleep_time - 0.002)
                while time.perf_counter() < next_tick: pass
                next_tick += POLL_INTERVAL
                if next_tick < time.perf_counter() - 1.0:
                    next_tick = time.perf_counter() + POLL_INTERVAL

        except KeyboardInterrupt:
            self.events.log_general("[Steerer Magnet Service] Process interrupted by user.")
        finally:
            self.hw.disconnect()
            self.commands.close()
            self.pub_socket.close(0)
            self.plc_socket.close(0)
            self.hb_socket.close(0)


if __name__ == "__main__":
    setup_logging("service_steerer_magnet_psu")
    harden_windows_process()
    SteererMagnetMicroservice().run()
