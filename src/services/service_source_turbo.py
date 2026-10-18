import logging
import os
import socket
import time
from typing import Any, Dict

import orjson
import zmq

from src.core.commands import CommandServer
from src.core.event_helper import EventHelper
from src.core.log_setup import Throttle, setup_logging
from src.core.os_helper import harden_windows_process
from src.core.paths import simulation_enabled

# Force Windows high-resolution timers (1ms precision)
if os.name == 'nt':
    import ctypes

    ctypes.windll.winmm.timeBeginPeriod(1)

# Ensure these match your network_map.py
from src.core.network_map import (
    ZMQ_PORT_SRC_TURBO_PUB, ZMQ_PORT_SRC_TURBO_CMD, TOPIC_SRC_TURBO_DATA,
    SRC_TURBO_WAVESHARE_IP, SRC_TURBO_WAVESHARE_PORT,
    ZMQ_PORT_HEARTBEAT)

# --- USS PROTOCOL CONSTANTS ---
PUMP_ADDRESS = 0
SOCKET_TIMEOUT = 1.0
POLL_INTERVAL = 0.05  # 50ms loop
MAX_CMD_AGE = 0.5
RECONNECT_DELAYS = (1.0, 2.0, 5.0, 10.0)
BASE_TAG = "ion_beam.source.turbo_pump"

log = logging.getLogger("service_source_turbo")

# Task IDs (AK)
AK_READ = 1
AK_WRITE_16 = 2
AK_WRITE_FIELD_16 = 7

# Parameter Mapping
PNU_HW_VER = 1
PNU_SERIAL = 2
PNU_ACT_FREQ = 3  # Hz
PNU_ACT_VOLT = 4  # V
PNU_ACT_CURR = 5  # 0.1A
PNU_SAVE = 8
PNU_CONV_TEMP = 11  # °C
PNU_MAX_FREQ = 18  # Hz (Used for % calculation)
PNU_RELAY_X1 = 29  # Indexed
PNU_BEARING_TEMP = 125  # °C
PNU_X201_FUNC = 134  # Non-indexed for 350i
PNU_OP_HOURS = 184  # 0.01h
PNU_ERR_CODE = 171  # Latest error
PNU_WARN_BITS = 227  # Warning bitfield
PNU_VENT_ON_FREQ = 247
PNU_VENT_OFF_FREQ = 248
PNU_USS_WATCHDOG = 182  # Comms Timeout

TURBO_ERROR_DICT = {
    0: "No Error",
    1: "Overspeed warning",
    2: "Pass through time / Bearing temperature too high",
    4: "Short circuit in motor coil or converter",
    5: "Converter temperature error",
    6: "Run-up time error",
    7: "Motor temperature error",
    61: "Bearing temperature warning",
    83: "Motor undertemperature warning",
    84: "Motor temperature warning",
    85: "Converter overtemperature warning",
    86: "Pump temperature 6 warning",
    87: "Pump temperature 6 failure",
    94: "Pump temperature 4 warning",
    95: "Pump temperature 4 failure",
    96: "Pump temperature 5 warning",
    97: "Pump temperature 5 failure",
    101: "Overload warning (speed dropped)",
    103: "Supply voltage warning",
    106: "Overload Failure (speed below minimum)",
    111: "Motor undertemperature error",
    116: "Permanent overload error",
    117: "Motor current error",
    143: "Overspeed failure",
    213: "Supply voltage error (overvoltage)",
    221: "Checksum error 1",
    225: "Bearing run-in active",
    227: "Frequency converter collective error",
    228: "Frequency converter collective error",
    229: "Frequency converter collective error",
    230: "Frequency converter collective error",
    231: "Supply voltage error (overvoltage)",
    232: "Supply voltage error (undervoltage)",
    233: "Supply voltage error (overvoltage)",
    234: "Supply voltage error (undervoltage)",
    235: "Frequency converter collective error",
    236: "Startup failure (mechanical block/high gas load)",
    237: "Frequency converter collective error",
    238: "Frequency converter collective error",
    239: "Frequency converter collective error",
    240: "Checksum error 2",
    241: "Supply voltage is not 24V",
    242: "Supply voltage is not 48V",
    252: "Hardware plausibility error (Converter/Front-end mismatch)"
}

TURBO_WARNING_DICT = {
    0: "Pump temperature 1 too high",
    1: "Pump temperature 2 too high",
    2: "Pump temperature 3 too high",
    3: "Ambient temperature too low",
    6: "Overspeed warning",
    7: "Pump temperature 4 too high",
    11: "Overload warning",
    12: "Pump temperature 5 too high",
    13: "Pump temperature 6 too high",
    14: "Power supply voltage warning"
}


class TurbovacMicroservice:
    def __init__(self):
        self.context = zmq.Context()
        self.events = EventHelper("service_src_turbo")

        # 1. ZMQ Comms
        self.pub_socket = self.context.socket(zmq.PUB)
        self.pub_socket.setsockopt(zmq.SNDHWM, 5)
        self.pub_socket.bind(ZMQ_PORT_SRC_TURBO_PUB)

        self.commands = CommandServer(ZMQ_PORT_SRC_TURBO_CMD, "service_source_turbo", context=self.context,
                                      events=self.events, max_age=MAX_CMD_AGE)

        # Simulated pump (SINDIE_SIMULATE=1) replaces the Waveshare socket
        self._sim = None
        if simulation_enabled():
            from src.sim.turbo import shared_pump
            self._sim = shared_pump()
        self._throttle = Throttle(60.0)
        self._connect_failures = 0
        self._next_connect_at = 0.0
        self.running = True

        # 2. Hardware / Internal State
        self.sock = None
        self.connected = False
        self.comms_ok = False
        self.active_control_word = None

        # Arbitration State
        self.active_ctrl_mode = 0

        # Internal Memory (Not broadcasted directly)
        self._max_hz = 1000
        self._most_recent_error_desc = "No Error"
        self._most_recent_warning_desc = "None"

        # 3. Strict 1D Flat Payload
        self.state: Dict[str, Any] = {
            "timestamp": 0.0,
            "system.connected": 0.0,
            "system.cycle_time_ms": 0.0,
            "system.safety_synced": 0.0,
            "system.service_hours": 0.0
        }

        # 4. Slow Task Queue (Round-Robin)
        self.slow_tasks = [
            ("bearing_temp", PNU_BEARING_TEMP),
            ("conv_temp", PNU_CONV_TEMP),
            ("volts", PNU_ACT_VOLT),
            ("amps", PNU_ACT_CURR),
            ("op_hours", PNU_OP_HOURS),
            ("warnings", PNU_WARN_BITS),
            ("error", PNU_ERR_CODE)
        ]
        self._slow_idx = 0

        self.hb_socket = self.context.socket(zmq.PUB)
        self.hb_socket.connect(ZMQ_PORT_HEARTBEAT)
        self.last_hb_time = 0.0

    def stop(self):
        self.running = False

    def _heartbeat(self, force=False):
        now = time.time()
        if force or now - self.last_hb_time >= 0.5:
            self.hb_socket.send_json({"service": "service_source_turbo", "ts": now})
            self.last_hb_time = now

    # --- USS LOW LEVEL ---
    def _calculate_bcc(self, frame):
        bcc = 0
        for byte in frame: bcc ^= byte
        return bcc

    def _generate_frame(self, pnu, index, value=0, ak=AK_READ, control_word=0x0400):
        stx, lge, adr = 0x02, 22, PUMP_ADDRESS
        pke = (ak << 12) | pnu
        pwe = [(value >> 24) & 0xFF, (value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF]
        pzd1_hi, pzd1_lo = (control_word >> 8) & 0xFF, control_word & 0xFF

        frame = [stx, lge, adr, (pke >> 8) & 0xFF, pke & 0xFF, 0x00, index] + pwe + [pzd1_hi, pzd1_lo] + ([0x00] * 10)
        frame.append(self._calculate_bcc(frame))
        return bytes(frame)

    def _transaction(self, pnu, index, value=0, ak=AK_READ, control_word=0x0400):
        if not self.connected: return None, None, None
        if self._sim is not None:
            return self._sim.transaction(pnu, index, value, ak, control_word)

        try:
            self.sock.setblocking(False)
            try:
                while self.sock.recv(4096): pass
            except Exception:
                pass

            self.sock.setblocking(True)
            self.sock.settimeout(SOCKET_TIMEOUT)

            self.sock.sendall(self._generate_frame(pnu, index, value, ak, control_word))
            time.sleep(0.04)

            res = b""
            start_time = time.time()
            while len(res) < 24:
                chunk = self.sock.recv(24 - len(res))
                if not chunk: break
                res += chunk
                if time.time() - start_time > SOCKET_TIMEOUT: break

            if len(res) < 24:
                return None, None, None

            resp_pke = (res[3] << 8) | res[4]
            resp_ak = (resp_pke >> 12) & 0xF
            resp_pnu = resp_pke & 0x07FF

            if resp_pnu != pnu:
                if (n := self._throttle.ready("desync")) is not None:
                    self.events.log_general(f"USS desync: asked for PNU {pnu}, got PNU {resp_pnu} - "
                                            f"reconnecting{Throttle.suffix(n)}", severity="WARNING")
                self.connected = False
                return None, None, None

            res_pwe = (res[7] << 24) | (res[8] << 16) | (res[9] << 8) | res[10]
            zsw = (res[11] << 8) | res[12]

            return res_pwe, resp_ak, zsw

        except socket.timeout:
            return None, None, None
        except OSError as e:
            if (n := self._throttle.ready("socket")) is not None:
                self.events.log_general(f"Socket error: {e}{Throttle.suffix(n)}", severity="WARNING")
            self.connected = False
            return None, None, None

    # --- INITIALIZATION & HANDSHAKE ---
    def _connect_socket(self):
        if time.time() < self._next_connect_at:
            return
        if self._sim is not None:
            from src.sim import injected_faults
            if "turbo_disconnect" in injected_faults():
                err = "simulated: Waveshare bridge not reachable"
            else:
                self.connected = True
                self.state["system.connected"] = 1.0
                self.events.log_general("Connected to simulated TURBOVAC")
                self._connect_failures = 0
                return
        else:
            if self.sock:
                try:
                    self.sock.close()
                except OSError:
                    pass
            try:
                self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                self.sock.settimeout(SOCKET_TIMEOUT)
                self.sock.connect((SRC_TURBO_WAVESHARE_IP, SRC_TURBO_WAVESHARE_PORT))
                self.connected = True
                self.state["system.connected"] = 1.0
                self.events.log_general(f"Connected to {SRC_TURBO_WAVESHARE_IP}")
                self._connect_failures = 0
                self._throttle.reset("connect")
                return
            except OSError as e:
                err = e
        self.connected = False
        self.state["system.connected"] = 0.0
        self._connect_failures += 1
        self._next_connect_at = time.time() + RECONNECT_DELAYS[min(self._connect_failures - 1,
                                                                   len(RECONNECT_DELAYS) - 1)]
        if (n := self._throttle.ready("connect")) is not None:
            self.events.log_general(f"Connection to {SRC_TURBO_WAVESHARE_IP} failed: {err}{Throttle.suffix(n)}",
                                    severity="WARNING")

    def _verify_safety_strategy(self):
        raw_hz, ak_hz, zsw = self._transaction(PNU_ACT_FREQ, 0, control_word=0x0000)

        if raw_hz is not None and raw_hz > 0:
            self.events.log_general(f"Pump is already SPINNING at {raw_hz} Hz. Adopting LATCHED ON state.")
            self.active_control_word = 0x0401
        else:
            self.events.log_general("Pump is STOPPED. Adopting LATCHED OFF state.")
            self.active_control_word = 0x0400

        ser_val, _, _ = self._transaction(PNU_SERIAL, 0, control_word=self.active_control_word)
        hw_val, _, _ = self._transaction(PNU_HW_VER, 0, control_word=self.active_control_word)
        self.state["system.serial"] = ser_val
        self.state["system.hw_version"] = hw_val

        m_hz, ak_m, _ = self._transaction(PNU_MAX_FREQ, 0, control_word=self.active_control_word)
        if m_hz is not None and ak_m not in [7, 8]:
            self._max_hz = m_hz

        val_x201, ak_x201, _ = self._transaction(PNU_X201_FUNC, 0, ak=1, control_word=self.active_control_word)
        val_relay, ak_relay, _ = self._transaction(PNU_RELAY_X1, 0, ak=6, control_word=self.active_control_word)
        val_vent_on, ak_on, _ = self._transaction(PNU_VENT_ON_FREQ, 0, ak=1, control_word=self.active_control_word)
        val_vent_off, ak_off, _ = self._transaction(PNU_VENT_OFF_FREQ, 0, ak=1, control_word=self.active_control_word)
        val_wd, ak_wd, _ = self._transaction(PNU_USS_WATCHDOG, 0, ak=1, control_word=self.active_control_word)

        if 7 in [ak_x201, ak_relay, ak_on, ak_off]:
            self.events.log_general("[!] PUMP RETURNED NACK. Pump is likely saving to flash (Error 102).")
            self.state["system.safety_synced"] = 0.0
            return

        is_synced = (val_x201 == 19 and val_relay == 4 and val_vent_on == 500 and val_vent_off == 5 and val_wd == 0)
        self.state["system.safety_synced"] = 1.0 if is_synced else 0.0

        if not is_synced:
            if raw_hz == 0:
                self.events.log_general("Strategy out of sync. Correcting now...")
                self._transaction(PNU_X201_FUNC, 0, value=19, ak=AK_WRITE_16, control_word=self.active_control_word)
                time.sleep(0.1)
                self._transaction(PNU_RELAY_X1, 0, value=4, ak=AK_WRITE_FIELD_16, control_word=self.active_control_word)
                time.sleep(0.1)
                self._transaction(PNU_VENT_ON_FREQ, 0, value=500, ak=AK_WRITE_16, control_word=self.active_control_word)
                time.sleep(0.1)
                self._transaction(PNU_VENT_OFF_FREQ, 0, value=5, ak=AK_WRITE_16, control_word=self.active_control_word)
                time.sleep(0.1)
                self._transaction(PNU_USS_WATCHDOG, 0, value=0, ak=AK_WRITE_16, control_word=self.active_control_word)
                time.sleep(0.1)

                self.events.log_general("Saving to flash (P8=1)...")
                self._transaction(8, 0, value=1, ak=AK_WRITE_16, control_word=self.active_control_word)
                # 30 s for the pump to write its flash; keep heartbeating so the service
                # manager doesn't kill (and restart) us half way through.
                t_end = time.time() + 30
                while time.time() < t_end:
                    self._heartbeat()
                    time.sleep(0.25)
                self.events.log_general("Save complete.")
            else:
                self.events.log_general("[!] SAFETY WARNING: Pump is SPINNING but Argon strategy is not loaded!")
        else:
            self.events.log_general("Safety strategy verified and active.")

    # --- LOOP TASKS ---
    def _process_commands(self):
        for cmd in self.commands.poll():
            tag, value, origin = cmd.tag, cmd.value, cmd.origin

            if not tag.startswith(BASE_TAG + "."):
                cmd.reject("not a source turbo pump tag")
                continue

            # --- 1. ARBITRATION: Handle Control Mode Changes ---
            if tag.endswith(".cmd_ctrl_mode"):
                try:
                    self.active_ctrl_mode = int(value)
                except (ValueError, TypeError):
                    cmd.reject(f"invalid control mode {value!r}")
                    continue
                self.events.log_general(f"Arbitration: Turbo mode set to {self.active_ctrl_mode}")
                cmd.ok()
                continue

            # --- 2. ARBITRATION: Enforce Lockout ---
            if self.active_ctrl_mode > 0 and origin != "optimizer":
                cmd.reject(f"locked out: turbo is in automatic control mode {self.active_ctrl_mode}")
                continue

            if not self.connected:
                cmd.reject("turbo controller not connected")
                continue

            if tag.endswith(".cmd_enable"):
                # (was `value is True`, which treated a numeric 1 from a recipe as STOP)
                if bool(value):
                    if self._transaction(3, 0, control_word=0x0480)[0] is None:  # Reset to clear any faults
                        cmd.fail("no reply from pump to the fault-reset/start request")
                        continue
                    self.active_control_word = 0x0401
                    cmd.ok("start requested")
                else:
                    self.active_control_word = 0x0400
                    cmd.ok("stop requested")

            elif tag.endswith(".cmd_reset"):
                if not bool(value):
                    cmd.ok("nothing to do")
                elif self._transaction(3, 0, control_word=0x0480)[0] is None:
                    cmd.fail("no reply from pump")
                else:
                    cmd.ok("fault reset sent")
            else:
                cmd.reject(f"unsupported command '{tag.split('.')[-1]}'")
        self.commands.finish()

    def _publish_unknown(self):
        for key in ("rb_speed_hz", "rb_speed_pct", "stat_ready", "stat_turning", "stat_error", "stat_warning",
                    "stat_trip", "rb_temp_bearing", "rb_temp_converter", "rb_voltage", "rb_current"):
            self.state[f"{BASE_TAG}.{key}"] = None
        self.state[f"{BASE_TAG}.stat_comms_fail"] = 1.0
        self.state[f"{BASE_TAG}.rb_ctrl_mode"] = float(self.active_ctrl_mode)

    def run(self):
        self.events.log_general("Starting USS Daemon (ISA-95 Architecture)..."
                                + (" (SIMULATED PUMP)" if self._sim is not None else ""))

        current_time_pc = time.perf_counter()
        next_tick = current_time_pc + POLL_INTERVAL
        base_tag = BASE_TAG

        while self.running:
            cycle_start = time.perf_counter()

            if not self.connected:
                self._connect_socket()
                if self.connected:
                    self._verify_safety_strategy()
                else:
                    # Keep answering commands, publishing 'unknown' and heartbeating while the
                    # controller is unreachable (silence here made the manager restart us in a loop)
                    self._process_commands()
                    self._publish_unknown()
                    self.state["timestamp"] = time.time()
                    try:
                        self.pub_socket.send_multipart([TOPIC_SRC_TURBO_DATA, orjson.dumps(self.state)])
                    except zmq.ZMQError as e:
                        log.debug("publish failed: %s", e)
                    self._heartbeat()
                    sleep_time = next_tick - time.perf_counter()
                    if sleep_time > 0.002:
                        time.sleep(sleep_time - 0.002)
                    while time.perf_counter() < next_tick:
                        pass
                    next_tick += POLL_INTERVAL
                    if next_tick < time.perf_counter() - 1.0:
                        next_tick = time.perf_counter() + POLL_INTERVAL
                    continue

            # 1. High-Priority Poll (Frequency & Status Word)
            raw_hz, ak_hz, zsw = self._transaction(PNU_ACT_FREQ, 0, control_word=self.active_control_word)

            if raw_hz is not None and ak_hz not in [7, 8]:
                self.comms_ok = True

                ready = bool(zsw & (1 << 0))
                error_active = bool(zsw & (1 << 3))
                turning = bool(zsw & (1 << 11))
                warning_active = bool(zsw & (1 << 14))
                trip_active = error_active and not ready

                self.state[f"{base_tag}.rb_speed_hz"] = float(raw_hz)
                self.state[f"{base_tag}.stat_ready"] = 1.0 if ready else 0.0
                self.state[f"{base_tag}.stat_turning"] = 1.0 if turning else 0.0
                self.state[f"{base_tag}.stat_error"] = 1.0 if error_active else 0.0
                self.state[f"{base_tag}.stat_warning"] = 1.0 if warning_active else 0.0
                self.state[f"{base_tag}.stat_trip"] = 1.0 if trip_active else 0.0

                if self._max_hz > 0:
                    self.state[f"{base_tag}.rb_speed_pct"] = round((raw_hz / self._max_hz) * 100, 1)

            elif ak_hz in [7, 8]:
                self.comms_ok = True
            else:
                self.comms_ok = False

            comms_fail = not self.connected or not self.comms_ok
            self.state[f"{base_tag}.stat_comms_fail"] = 1.0 if comms_fail else 0.0
            if comms_fail:
                self._publish_unknown()

            # Publish active control mode
            self.state[f"{base_tag}.rb_ctrl_mode"] = float(self.active_ctrl_mode)

            # 2. Process Incoming ZMQ Commands
            self._process_commands()

            # 3. Slow-Priority Poll (Round Robin)
            task_name, pnu = self.slow_tasks[self._slow_idx]
            val, ak_val, _ = self._transaction(pnu, 0, control_word=self.active_control_word)

            if val is not None and ak_val not in [7, 8]:
                if task_name == "bearing_temp":
                    self.state[f"{base_tag}.rb_temp_bearing"] = float(val)
                elif task_name == "conv_temp":
                    self.state[f"{base_tag}.rb_temp_converter"] = float(val)
                elif task_name == "volts":
                    self.state[f"{base_tag}.rb_voltage"] = float(val)
                elif task_name == "amps":
                    self.state[f"{base_tag}.rb_current"] = val * 0.1
                elif task_name == "op_hours":
                    self.state["system.service_hours"] = round(val * 0.01, 2)
                elif task_name == "warnings":
                    active_warnings = [desc for bit, desc in TURBO_WARNING_DICT.items() if val & (1 << bit)]
                    self._most_recent_warning_desc = ", ".join(active_warnings) if active_warnings else "None"
                elif task_name == "error":
                    self.state[f"{base_tag}.stat_error_code"] = float(val)
                    self._most_recent_error_desc = TURBO_ERROR_DICT.get(val, f"Unknown Error ({val})")

            self._slow_idx = (self._slow_idx + 1) % len(self.slow_tasks)

            # 4. Broadcast
            self.state["timestamp"] = time.time()
            elapsed = time.perf_counter() - cycle_start
            self.state["system.cycle_time_ms"] = elapsed * 1000

            try:
                self.pub_socket.send_multipart([TOPIC_SRC_TURBO_DATA, orjson.dumps(self.state)])
            except zmq.ZMQError as e:
                if (n := self._throttle.ready("publish")) is not None:
                    log.error("ZMQ publish failed: %s%s", e, Throttle.suffix(n))

            self._heartbeat()

            # Strict Spin-Wait OS Scheduling
            sleep_time = next_tick - time.perf_counter()
            if sleep_time > 0.002:
                time.sleep(sleep_time - 0.002)

            while time.perf_counter() < next_tick:
                pass

            next_tick += POLL_INTERVAL
            if next_tick < time.perf_counter() - 1.0:
                next_tick = time.perf_counter() + POLL_INTERVAL

        self.commands.close()
        for sock in (self.pub_socket, self.hb_socket):
            sock.close(0)


if __name__ == "__main__":
    setup_logging("service_source_turbo")
    harden_windows_process()
    TurbovacMicroservice().run()