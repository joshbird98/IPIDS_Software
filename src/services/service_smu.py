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

POLL_INTERVAL = 0.1  # 10Hz target loop (will stretch automatically for high NPLC)
MAX_CMD_AGE = 0.5
PLC_WATCHDOG_AGE = 1.0
DEBUG_VERBOSE = False

# Keysight B2900A series standard current ranges
CURRENT_RANGES = [10e-9, 100e-9, 1e-6, 10e-6, 100e-6, 1e-3]
UP_RANGE_THRESH = 0.95
DOWN_RANGE_THRESH = 0.08
KEYSIGHT_OVERLOAD_VAL = 1e37 # Catch SCPI IEEE infinity return

class SmuScpiProtocol:
    def __init__(self, ip: str, port: int = 5025):
        self.ip = ip
        self.port = port
        self.sock = None
        self.connected = False
        self.terminator = b'\n'
        self.output_state = True

        # State memory for reconnects
        self.current_nplc = 1.0
        self.cached_auto_range = False
        self.cached_range_val = 10e-9
        self.cached_azer = False

        self.timeout_counter = 0

    def connect(self) -> bool:
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

        try:
            if DEBUG_VERBOSE: print(f"[SYS] Attempting connection to {self.ip}:{self.port}...")
            self.sock.connect((self.ip, self.port))
            self.connected = True
            print(f"[SMU Driver] Raw SCPI Socket Connected to {self.ip}:{self.port}")

            # Allow enough time for standard initialization
            self.sock.settimeout(5.0)

            self.send_cmd("*RST")
            self.send_cmd("*CLS")
            self.query("*OPC?")

            self.send_cmd(":DISP:ENAB ON")
            self.send_cmd(":DISP:VIEW SING1")
            self.send_cmd(":DISP:DIG 6")
            self.send_cmd(":DISP:ZOOM OFF")

            self.send_cmd(":SOUR:VOLT:PROT 200")
            self.send_cmd(":SOUR:FUNC:MODE VOLT")
            self.send_cmd(f":SENS:CURR:RANG:AUTO OFF")
            self.send_cmd(f":SENS:CURR:RANG {CURRENT_RANGES[0]:.2e}")
            self.send_cmd(":SOUR:VOLT:MODE AUTO")
            self.send_cmd(":SENS:CURR:PROT 1e-4")

            self.send_cmd(":SENS:FUNC:OFF:ALL")
            self.send_cmd(":SENS:FUNC \"VOLT\",\"CURR\"")

            self.send_cmd(":SENS:REM OFF")

            # Restore cached AZER memory
            azer_str = "ON" if self.cached_azer else "OFF"
            self.send_cmd(f":SYST:AZER {azer_str}")

            # Restore cached NPLC memory
            self.set_nplc(self.current_nplc)

            self.send_cmd(":FORM:ELEM:SENS VOLT,CURR")
            self.query("*OPC?")

            self.timeout_counter = 0
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
            self.timeout_counter = 0  # Reset strike counter on successful reply
            if DEBUG_VERBOSE: print(f"[SCPI RX] {resp_str}")
            return resp_str
        except socket.timeout:
            self.timeout_counter += 1
            if DEBUG_VERBOSE: print(f"[SCPI ERR] Query timeout ({self.timeout_counter}/3) for command: {cmd}")
            if self.timeout_counter >= 3:
                self.connected = False
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

    def set_nplc(self, nplc: float):
        """Adjusts the aperture integration time and perfectly scales the network socket timeout."""
        if not self.connected: return
        # Valid B2900A NPLC range is typically 0.001 to 100
        nplc = max(0.001, min(100.0, float(nplc)))
        self.send_cmd(f":SENS:CURR:NPLC {nplc}")
        self.current_nplc = nplc

        # Generous safety margin to prevent timeout cascades during auto-ranging or long integrations
        required_timeout = 5.0 + (nplc * 0.1)
        self.sock.settimeout(required_timeout)
        if DEBUG_VERBOSE: print(
            f"[SMU Config] Aperture set to {nplc} NPLC. Socket timeout adjusted to {required_timeout:.2f}s")

    def read_telemetry(self) -> Optional[Tuple[float, float, bool]]:
        if not self.connected: return None
        try:
            # Atomic command sequence executed by instrument
            resp = self.query(":MEAS:VOLT?;:MEAS:CURR?")
            if resp is None: return None

            parts = resp.split(';')
            v_rb = float(parts[0])
            i_rb = float(parts[1])

            return v_rb, i_rb, self.output_state

        except Exception as e:
            if DEBUG_VERBOSE: print(f"[SCPI ERR] Telemetry parse failed: {e}")
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
        self.registry_limits = self._load_registry_limits()
        self.hw = SmuScpiProtocol(SMU_IP, SMU_PORT)

        self.stat_safety_relay = False
        self.last_plc_ts = 0.0
        self.safety_tripped = True
        self._last_printed_safety_state = True
        self._safety_lockout_active = False

        self.active_ctrl_mode = 0
        self.sp_cache: Dict[str, float] = {}
        self.state: Dict[str, Any] = {"timestamp": 0.0, "system.cycle_time_ms": 0.0, "system.safe_to_run": 0.0}

        self.hb_socket = self.context.socket(zmq.PUB)
        self.hb_socket.connect(ZMQ_PORT_HEARTBEAT)
        self.last_hb_time = 0.0

        self.events = EventHelper("service_smu")

        self.range_idx = 0  # Default to 10nA
        self.state["ion_beam.beamline.faraday.smu.stat_range_settled"] = 0.0

    def _load_config(self) -> dict:
        config_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../config/smu_config.json'))
        default_config = {
            "compliance_current": 100e-6
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
                    if data.get("source") == "service_faraday_smu" and data.get("auto_controllable"):
                        limits[tag] = {"min_val": data.get("min_val"), "max_val": data.get("max_val")}
        except Exception:
            pass
        return limits

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
            print(f"[SAFETY] State changed. Tripped: {self.safety_tripped}")
            self._last_printed_safety_state = self.safety_tripped

    def _process_commands(self):
        if not self.hw.connected: return

        if self.safety_tripped:
            is_physically_on = self.state.get("ion_beam.beamline.faraday.smu.stat_enabled", 0.0) == 1.0
            rb_voltage = self.state.get("ion_beam.beamline.faraday.smu.rb_voltage", 0.0)

            if not self._safety_lockout_active or is_physically_on or abs(rb_voltage) > 0.1:
                if DEBUG_VERBOSE: print("[SAFETY] Hardware violation detected. Forcing Output OFF and 0V.")
                self.hw.set_voltage(0.0)
                self.hw.set_output(False)
                self._safety_lockout_active = True

            try:
                while True: self.sub_socket.recv_json(flags=zmq.NOBLOCK)
            except zmq.Again:
                pass
            return

        self._safety_lockout_active = False

        try:
            while True:
                msg = self.sub_socket.recv_json(flags=zmq.NOBLOCK)
                tag = msg.get("tag", "")
                raw_value = msg.get("value", 0)
                ts = msg.get("ts", 0.0)
                origin = msg.get("origin", "hmi")

                if time.time() - ts > MAX_CMD_AGE: continue

                if tag == "ion_beam.beamline.faraday.smu.cmd_ctrl_mode":
                    try:
                        self.active_ctrl_mode = int(raw_value)
                        self.events.log_general(f"Arbitration: SMU mode set to {self.active_ctrl_mode}")
                    except:
                        pass
                    continue

                if self.active_ctrl_mode > 0 and origin != "optimizer" and origin != "mass_scan":
                    continue

                parts = tag.split('.')
                if len(parts) < 3 or parts[1] != "beamline": continue
                cmd_type = parts[-1]

                try:
                    value = float(raw_value)
                except:
                    continue

                if cmd_type == "sp_requested_voltage":
                    if self.state.get("ion_beam.beamline.faraday.smu.stat_enabled", 0.0) == 1.0:
                        tag_limits = self.registry_limits.get(tag, {})
                        min_v = tag_limits.get("min_val")
                        max_v = tag_limits.get("max_val")

                        if min_v is not None and min_v <= value <= max_v:
                            self.hw.set_voltage(value)
                            self.sp_cache["ion_beam.beamline.faraday.smu.sp_actual_voltage"] = value
                    else:
                        self.hw.set_output(False)

                elif cmd_type == "cmd_enable":
                    self.hw.set_output(bool(value))

                elif cmd_type == "cmd_nplc":
                    self.hw.set_nplc(value)
                    self.state["ion_beam.beamline.faraday.smu.rb_nplc"] = self.hw.current_nplc

                elif cmd_type == "cmd_range_val":
                    self.hw.cached_range_val = float(value)
                    if self.hw.connected and not self.hw.cached_auto_range:
                        self.hw.send_cmd(f":SENS:CURR:RANG {self.hw.cached_range_val:.2e}")

                elif cmd_type == "cmd_azer":
                    self.hw.cached_azer = bool(value)
                    if self.hw.connected:
                        self.hw.send_cmd(f":SYST:AZER {'ON' if value else 'OFF'}")

        except zmq.Again:
            pass

    def _poll_device(self):
        if not self.hw.connected: return
        telemetry_data = self.hw.read_telemetry()
        comms_fail = telemetry_data is None

        self.state["ion_beam.beamline.faraday.smu.stat_comms_fail"] = 1.0 if comms_fail else 0.0
        if comms_fail: return

        v_rb, i_rb, outp_enabled = telemetry_data

        # --- Software Auto-Range Logic ---
        current_range_val = CURRENT_RANGES[self.range_idx]
        is_overload = abs(i_rb) > KEYSIGHT_OVERLOAD_VAL

        range_changed = False

        if is_overload or abs(i_rb) > (UP_RANGE_THRESH * current_range_val):
            if self.range_idx < len(CURRENT_RANGES) - 1:
                self.range_idx += 1
                range_changed = True
        elif abs(i_rb) < (DOWN_RANGE_THRESH * current_range_val):
            if self.range_idx > 0:
                self.range_idx -= 1
                range_changed = True

        if range_changed:
            new_range = CURRENT_RANGES[self.range_idx]
            self.hw.send_cmd(f":SENS:CURR:RANG {new_range:.2e}")
            self.state["ion_beam.beamline.faraday.smu.stat_range_settled"] = 0.0

            # Discard telemetry; instrument is switching and settling
            return

        self.state["ion_beam.beamline.faraday.smu.stat_range_settled"] = 1.0
        # ---------------------------------

        self.state["ion_beam.beamline.faraday.smu.rb_voltage"] = round(v_rb, 4)
        self.state["ion_beam.beamline.faraday.smu.rb_current"] = i_rb
        self.state["ion_beam.beamline.faraday.smu.stat_enabled"] = 1.0 if outp_enabled else 0.0
        self.state["ion_beam.beamline.faraday.smu.rb_ctrl_mode"] = float(self.active_ctrl_mode)
        self.state["ion_beam.beamline.faraday.smu.rb_nplc"] = self.hw.current_nplc

        if "ion_beam.beamline.faraday.smu.sp_actual_voltage" in self.sp_cache:
            self.state["ion_beam.beamline.faraday.smu.sp_actual_voltage"] = self.sp_cache[
                "ion_beam.beamline.faraday.smu.sp_actual_voltage"]

    def run(self):
        self.events.log_general("[SMU Service] Daemon Starting...")
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
                            self.hw.send_cmd(f":SENS:CURR:PROT {comp_i:.6e}")

                            last_v = self.sp_cache.get("ion_beam.beamline.faraday.smu.sp_actual_voltage")
                            if last_v is not None:
                                self.hw.set_voltage(last_v)
                                self.hw.set_output(True)
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
                except Exception:
                    pass

                if time.time() - self.last_hb_time >= 0.5:
                    self.hb_socket.send_json({"service": "service_smu", "ts": time.time()})
                    self.last_hb_time = time.time()

                # If NPLC is high, the loop will naturally stretch past 0.1s due to SCPI blocking.
                # This math ensures it gracefully catches up to the next available 10Hz interval boundary.
                while next_tick <= time.perf_counter():
                    next_tick += POLL_INTERVAL

                sleep_time = next_tick - time.perf_counter()
                if sleep_time > 0.002: time.sleep(sleep_time - 0.002)
                while time.perf_counter() < next_tick: pass

        except KeyboardInterrupt:
            print("\n[SYS] Keyboard interrupt received. Shutting down...")
        finally:
            self.hw.disconnect()
            self.pub_socket.close()
            self.sub_socket.close()
            self.plc_socket.close()
            self.context.term()


if __name__ == "__main__":
    harden_windows_process()
    SmuMicroservice().run()