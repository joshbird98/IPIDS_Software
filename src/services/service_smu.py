import time
import zmq
import os
import socket
from typing import Dict, Any, Optional, Tuple
import orjson
import json

from src.core.event_helper import EventHelper
from src.core.os_helper import harden_windows_process

if os.name == 'nt':
    import ctypes

    ctypes.windll.winmm.timeBeginPeriod(1)

from src.core.network_map import (
    SMU_IP, SMU_PORT, ZMQ_PORT_SMU_PUB, ZMQ_PORT_SMU_CMD, TOPIC_SMU_DATA,
    ZMQ_PORT_PLC_PUB, ZMQ_PORT_HEARTBEAT
)

POLL_INTERVAL = 0.1  # 10Hz loop
MAX_CMD_AGE = 0.5
PLC_WATCHDOG_AGE = 1.0

# Set to False to silence standard operational prints
DEBUG_VERBOSE = False


class SmuScpiProtocol:
    def __init__(self, ip: str, port: int = 5025):
        self.ip = ip
        self.port = port
        self.sock = None
        self.connected = False
        self.timeout = 0.5
        self.terminator = b'\n'
        self.output_state = True

    def connect(self) -> bool:
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)

        try:
            if DEBUG_VERBOSE: print(f"[SYS] Attempting connection to {self.ip}:{self.port}...")
            self.sock.connect((self.ip, self.port))
            self.connected = True
            print(f"[SMU Driver] Raw SCPI Socket Connected to {self.ip}:{self.port}")

            self.sock.settimeout(5.0)

            # --- 1. Instrument initialization sequence ---
            self.send_cmd("*RST")
            self.send_cmd("*CLS")
            self.query("*OPC?")

            # --- 2. Front Panel Configuration ---
            self.send_cmd(":DISP:ENAB ON")
            self.send_cmd(":DISP:VIEW SING1")
            self.send_cmd(":DISP:DIG 6")

            # CHANGED: Turn Zoom OFF to reveal Compliance Limits and Setup Info
            self.send_cmd(":DISP:ZOOM OFF")

            # --- 3. Source Configuration ---
            self.send_cmd(":SOUR:VOLT:PROT 200")
            self.send_cmd(":SOUR:FUNC:MODE VOLT")
            self.send_cmd(":SOUR:VOLT:RANG:AUTO ON")
            self.send_cmd(":SOUR:VOLT:MODE AUTO")
            self.send_cmd(":SENS:CURR:PROT 1e-4")

            # --- 4. Measurement Configuration ---
            self.send_cmd(":SENS:FUNC:OFF:ALL")
            self.send_cmd(":SENS:FUNC \"VOLT\",\"CURR\"")
            self.send_cmd(":SENS:CURR:RANG:AUTO ON")
            self.send_cmd(":SENS:REM OFF")

            # --- 5. Telemetry Formatting ---
            # CHANGED: Removed the :SENS node. This strictly formats the global output string.
            self.send_cmd(":FORM:ELEM:SENS VOLT,CURR")

            # --- SYNCHRONIZATION ---
            self.query("*OPC?")

            self.sock.settimeout(self.timeout)
            return True

        except Exception as e:
            if DEBUG_VERBOSE: print(f"[SYS] Connection failed: {e}")
            self.connected = False
            return False

    def disconnect(self):
        if self.connected:
            try:
                if DEBUG_VERBOSE: print("[SYS] Disconnecting from SMU and forcing output OFF.")
                self.set_output(False)
                self.sock.close()
            except:
                pass
            self.connected = False

    def send_cmd(self, cmd: str):
        if not self.connected: return
        try:
            if DEBUG_VERBOSE: print(f"[SCPI TX] {cmd}")
            self.sock.sendall((cmd + '\n').encode('utf-8'))
        except Exception as e:
            if DEBUG_VERBOSE: print(f"[SCPI ERR] Send failed: {e}")
            self.connected = False

    def query(self, cmd: str) -> Optional[str]:
        if not self.connected: return None
        try:
            if DEBUG_VERBOSE: print(f"[SCPI TX] {cmd}")
            self.sock.sendall((cmd + '\n').encode('utf-8'))
            response = bytearray()
            while True:
                chunk = self.sock.recv(4096)
                if not chunk:
                    self.connected = False
                    return None
                response.extend(chunk)
                if self.terminator in chunk:
                    break

            resp_str = response.decode('utf-8').strip()
            if DEBUG_VERBOSE: print(f"[SCPI RX] {resp_str}")
            return resp_str
        except socket.timeout:
            if DEBUG_VERBOSE: print(f"[SCPI ERR] Query timeout for command: {cmd}")
            return None
        except Exception as e:
            if DEBUG_VERBOSE: print(f"[SCPI ERR] Query failed: {e}")
            self.connected = False
            return None

    def set_voltage(self, v_set: float):
        self.send_cmd(f":SOUR:VOLT {v_set:.6f}")

    def set_output(self, state: bool):
        if not self.connected: return

        if not state:
            self.send_cmd(":SOUR:VOLT 0.0")
        self.output_state = state



    def read_telemetry(self) -> Optional[Tuple[float, float, bool]]:
        """Returns (Voltage, Current, Output_State)"""
        if not self.connected: return None

        try:
            # Atomic fetch: Ask for everything in one hardware scan
            # This puts the SMU in 'Fetch' mode and collects all data in one pass
            resp = self.query(":MEAS:VOLT?;:MEAS:CURR?")

            if resp is None: return None

            parts = resp.split(';')

            v_rb = float(parts[0])
            i_rb = float(parts[1])

            return v_rb, i_rb, self.output_state

        except Exception as e:
            if 'DEBUG_VERBOSE' in globals() and DEBUG_VERBOSE:
                print(f"[SCPI ERR] Telemetry parse failed: {e}")
            return None

class SmuMicroservice:
    def __init__(self):
        self.context = zmq.Context()

        self.pub_socket = self.context.socket(zmq.PUB)
        self.pub_socket.setsockopt(zmq.SNDHWM, 5)
        self.pub_socket.bind(ZMQ_PORT_SMU_PUB)

        self.sub_socket = self.context.socket(zmq.SUB)
        self.sub_socket.setsockopt(zmq.RCVHWM, 5)
        self.sub_socket.bind(ZMQ_PORT_SMU_CMD)
        self.sub_socket.setsockopt_string(zmq.SUBSCRIBE, "")

        self.plc_socket = self.context.socket(zmq.SUB)
        self.plc_socket.connect(ZMQ_PORT_PLC_PUB)
        self.plc_socket.setsockopt_string(zmq.SUBSCRIBE, "")

        self.limits = self._load_config()
        self.hw = SmuScpiProtocol(SMU_IP, SMU_PORT)

        self.stat_safety_relay = False
        self.last_plc_ts = 0.0
        self.safety_tripped = True
        self._last_printed_safety_state = True
        self._safety_lockout_active = False

        self.sp_cache: Dict[str, float] = {}
        self.sp_intended_enable = False

        self.state: Dict[str, Any] = {"timestamp": 0.0, "system.cycle_time_ms": 0.0, "system.safe_to_run": 0.0}

        self.hb_socket = self.context.socket(zmq.PUB)
        self.hb_socket.connect(ZMQ_PORT_HEARTBEAT)
        self.last_hb_time = 0.0

        self.events = EventHelper("service_smu")

    def _load_config(self) -> dict:
        config_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../config/smu_config.json'))
        default_config = {
            "max_voltage": 200.0,
            "min_voltage": -200.0,
            "compliance_current": 100e-6  # 100uA
        }
        try:
            with open(config_path, "r") as f:
                default_config.update(json.load(f))
        except Exception:
            if DEBUG_VERBOSE: print("[SYS] Config file not found, using default hardcoded limits.")
        return default_config

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

        if DEBUG_VERBOSE and self.safety_tripped != self._last_printed_safety_state:
            print(
                f"[SAFETY] State changed. Tripped: {self.safety_tripped} (Relay: {self.stat_safety_relay}, PLC Comms Alive: {comms_alive})")
            self._last_printed_safety_state = self.safety_tripped

    def _process_commands(self):
        if not self.hw.connected: return

        if self.safety_tripped:
            # Read the latest known physical state from telemetry
            is_physically_on = self.state.get("ion_beam.beamline.faraday.smu.stat_enabled", 0.0) == 1.0
            rb_voltage = self.state.get("ion_beam.beamline.faraday.smu.rb_voltage", 0.0)

            self.sp_intended_enable = False

            # Enforce safety if we haven't yet, OR if the hardware state violates the interlock
            if not self._safety_lockout_active or is_physically_on or abs(rb_voltage) > 0.1:
                if DEBUG_VERBOSE: print("[SAFETY] Hardware violation detected. Forcing Output OFF and 0V.")
                self.hw.set_voltage(0.0)
                self.hw.set_output(False)
                self._safety_lockout_active = True

            try:
                # Flush incoming ZMQ commands during safety trip
                while True: self.sub_socket.recv_json(flags=zmq.NOBLOCK)
            except zmq.Again:
                pass
            return

        # Reset the lockout tracker when safety permissives are restored
        self._safety_lockout_active = False

        min_v = self.limits.get("min_voltage", -200.0)
        max_v = self.limits.get("max_voltage", 200.0)

        try:
            while True:
                msg = self.sub_socket.recv_json(flags=zmq.NOBLOCK)
                tag = msg.get("tag", "")
                raw_value = msg.get("value", 0)
                ts = msg.get("ts", 0.0)

                if time.time() - ts > MAX_CMD_AGE:
                    if DEBUG_VERBOSE: print(f"[ZMQ CMD] Dropped stale command: {tag}")
                    continue

                parts = tag.split('.')
                if len(parts) < 3 or parts[1] != "beamline": continue
                cmd_type = parts[-1]

                try:
                    value = float(raw_value)
                except (ValueError, TypeError):
                    continue

                if DEBUG_VERBOSE: print(f"[ZMQ CMD] Rx: {cmd_type} = {value} (Tag: {tag})")

                if cmd_type == "sp_requested_voltage":
                    if self.state["ion_beam.beamline.faraday.smu.stat_enabled"]:
                        if min_v <= value <= max_v:
                            self.hw.set_voltage(value)
                            self.sp_cache["ion_beam.beamline.faraday.smu.sp_actual_voltage"] = value
                        else:
                            if DEBUG_VERBOSE: print(f"[ZMQ CMD] Voltage {value} out of bounds [{min_v}, {max_v}]")
                    else:
                        self.hw.set_output(False)
                        if DEBUG_VERBOSE: print(f"[ZMQ CMD] Voltage set ignored, output off.")

                elif cmd_type == "cmd_enable":
                    self.hw.set_output(bool(value))

        except zmq.Again:
            pass

    def _poll_device(self):
        if not self.hw.connected: return
        telemetry_data = self.hw.read_telemetry()
        comms_fail = telemetry_data is None

        self.state["ion_beam.beamline.faraday.smu.stat_comms_fail"] = 1.0 if comms_fail else 0.0
        if comms_fail:
            if DEBUG_VERBOSE: print("[TELEMETRY] Comms failed during poll.")
            return

        v_rb, i_rb, outp_enabled = telemetry_data

        if DEBUG_VERBOSE:
            print(f"[TELEMETRY] V_rb: {v_rb:.4f} V, I_rb: {i_rb:.6e} A, Output: {'ON' if outp_enabled else 'OFF'}")

        self.state["ion_beam.beamline.faraday.smu.rb_voltage"] = round(v_rb, 4)
        self.state["ion_beam.beamline.faraday.smu.rb_current"] = i_rb
        self.state["ion_beam.beamline.faraday.smu.stat_enabled"] = 1.0 if outp_enabled else 0.0

        if "ion_beam.beamline.faraday.smu.sp_actual_voltage" in self.sp_cache:
            self.state["ion_beam.beamline.faraday.smu.sp_actual_voltage"] = self.sp_cache[
                "ion_beam.beamline.faraday.smu.sp_actual_voltage"]

    def run(self):
        self.events.log_general("[SMU Service] Daemon Starting (SCPI/Raw Socket Architecture)...")
        print("[SYS] Daemon Starting.")

        last_connect_attempt = 0.0
        reconnect_interval = 1.0
        next_tick = time.perf_counter() + POLL_INTERVAL

        try:
            while True:
                cycle_start = time.perf_counter()

                if not self.hw.connected:
                    if time.time() - last_connect_attempt > reconnect_interval:
                        last_connect_attempt = time.time()
                        self.hw.connect()

                        if self.hw.connected:
                            comp_i = self.limits.get("compliance_current", 100e-6)
                            if DEBUG_VERBOSE: print(
                                f"[SYS] Connection successful. Setting compliance to {comp_i:.6e} A")
                            self.hw.send_cmd(f":SENS:CURR:PROT {comp_i:.6e}")
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
                    topic = TOPIC_SMU_DATA if isinstance(TOPIC_SMU_DATA, bytes) else TOPIC_SMU_DATA.encode('utf-8')
                    self.pub_socket.send_multipart([topic, orjson.dumps(self.state)])
                except Exception as e:
                    if DEBUG_VERBOSE: print(f"[SYS ERR] ZMQ Publish failed: {e}")

                if time.time() - self.last_hb_time >= 0.5:
                    self.hb_socket.send_json({"service": "service_smu", "ts": time.time()})
                    self.last_hb_time = time.time()

                sleep_time = next_tick - time.perf_counter()
                if sleep_time > 0.002: time.sleep(sleep_time - 0.002)
                while time.perf_counter() < next_tick: pass
                next_tick += POLL_INTERVAL

        except KeyboardInterrupt:
            print("\n[SYS] Keyboard interrupt received. Shutting down...")
            self.events.log_general("\n[SMU Service] Process interrupted by user.")
        finally:
            self.hw.disconnect()
            self.pub_socket.close()
            self.sub_socket.close()
            self.plc_socket.close()
            self.context.term()


if __name__ == "__main__":
    harden_windows_process()
    SmuMicroservice().run()