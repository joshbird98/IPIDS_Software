import socket
import time
import zmq
import os
from typing import Dict, Any
import orjson
from src.core.event_helper import EventHelper

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

        # 1. ZMQ Comms
        self.pub_socket = self.context.socket(zmq.PUB)
        self.pub_socket.setsockopt(zmq.SNDHWM, 5)
        self.pub_socket.bind(ZMQ_PORT_SRC_TURBO_PUB)

        self.sub_socket = self.context.socket(zmq.SUB)
        self.sub_socket.setsockopt(zmq.RCVHWM, 5)
        self.sub_socket.bind(ZMQ_PORT_SRC_TURBO_CMD)
        self.sub_socket.setsockopt_string(zmq.SUBSCRIBE, "")

        # 2. Hardware / Internal State
        self.sock = None
        self.connected = False
        self.comms_ok = False
        self.active_control_word = None

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

        self.events = EventHelper("service_src_turbo")

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
                self.events.log_general(f"[!] DESYNC DETECTED! Asked for PNU {pnu}, got PNU {resp_pnu}.")
                self.connected = False
                return None, None, None

            res_pwe = (res[7] << 24) | (res[8] << 16) | (res[9] << 8) | res[10]
            zsw = (res[11] << 8) | res[12]

            return res_pwe, resp_ak, zsw

        except socket.timeout:
            return None, None, None
        except Exception as e:
            self.events.log_general(f"Socket error: {e}")
            self.connected = False
            return None, None, None

    # --- INITIALIZATION & HANDSHAKE ---
    def _connect_socket(self):
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.sock.settimeout(SOCKET_TIMEOUT)
            self.sock.connect((SRC_TURBO_WAVESHARE_IP, SRC_TURBO_WAVESHARE_PORT))
            self.connected = True
            self.state["system.connected"] = 1.0
            self.events.log_general(f"Connected to {SRC_TURBO_WAVESHARE_IP}")
        except Exception as e:
            self.connected = False
            self.state["system.connected"] = 0.0
            self.events.log_general(f"Connection failed: {e}")

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
                time.sleep(30)
                self.events.log_general("Save complete.")
            else:
                self.events.log_general("[!] SAFETY WARNING: Pump is SPINNING but Argon strategy is not loaded!")
        else:
            self.events.log_general("Safety strategy verified and active.")

    # --- LOOP TASKS ---
    def _process_commands(self):
        try:
            while True:
                msg = self.sub_socket.recv_json(flags=zmq.NOBLOCK)

                tag = msg.get("tag", "")
                value = msg.get("value")
                ts = msg.get("ts", 0.0)

                if time.time() - ts > MAX_CMD_AGE:
                    continue

                if tag.endswith(".cmd_enable"):
                    if value is True:
                        self.active_control_word = 0x0401
                    else:
                        self.active_control_word = 0x0400

                elif tag.endswith(".cmd_reset") and value is True:
                    self._transaction(3, 0, control_word=0x0480)

        except zmq.Again:
            pass

    def run(self):
        self.events.log_general("Starting USS Daemon...")

        current_time_pc = time.perf_counter()
        next_tick = current_time_pc + POLL_INTERVAL

        while True:
            cycle_start = time.perf_counter()

            if not self.connected:
                self._connect_socket()
                if self.connected:
                    self._verify_safety_strategy()
                else:
                    sleep_time = next_tick - time.perf_counter()
                    if sleep_time > 0.002:
                        time.sleep(sleep_time - 0.002)
                    while time.perf_counter() < next_tick:
                        pass
                    next_tick += POLL_INTERVAL
                    continue

            # 1. High-Priority Poll (Frequency & Status Word)
            raw_hz, ak_hz, zsw = self._transaction(PNU_ACT_FREQ, 0, control_word=self.active_control_word)

            if raw_hz is not None and ak_hz not in [7, 8]:
                self.comms_ok = True

                ready = bool(zsw & (1 << 0))
                error_active = bool(zsw & (1 << 3))
                turning = bool(zsw & (1 << 11))
                warning_active = bool(zsw & (1 << 14))

                self.state["ion_beam.source.turbo_pump.speed_hz"] = float(raw_hz)
                self.state["ion_beam.source.turbo_pump.status_ready"] = 1.0 if ready else 0.0
                self.state["ion_beam.source.turbo_pump.status_turning"] = 1.0 if turning else 0.0
                self.state["ion_beam.source.turbo_pump.status_error"] = 1.0 if error_active else 0.0

                if self._max_hz > 0:
                    self.state["ion_beam.source.turbo_pump.speed_pct"] = round((raw_hz / self._max_hz) * 100, 1)

                trip_active = error_active and not ready

                # Mapped exactly to PLC payload expectations
                self.state["ion_beam.pump.status.stat_src_turbo_error"] = 1.0 if error_active else 0.0
                self.state["ion_beam.pump.status.stat_src_turbo_warning"] = 1.0 if warning_active else 0.0
                self.state["ion_beam.pump.status.stat_src_turbo_trip"] = 1.0 if trip_active else 0.0

            elif ak_hz in [7, 8]:
                self.comms_ok = True
            else:
                self.comms_ok = False

            comms_fail = not self.connected or not self.comms_ok
            self.state["ion_beam.pump.status.stat_src_turbo_comms_fail"] = 1.0 if comms_fail else 0.0

            # 2. Process Incoming ZMQ Commands
            self._process_commands()

            # 3. Slow-Priority Poll (Round Robin)
            task_name, pnu = self.slow_tasks[self._slow_idx]
            val, ak_val, _ = self._transaction(pnu, 0, control_word=self.active_control_word)

            if val is not None and ak_val not in [7, 8]:
                if task_name == "bearing_temp":
                    self.state["ion_beam.source.turbo_pump.temp_bearing"] = float(val)
                elif task_name == "conv_temp":
                    self.state["ion_beam.source.turbo_pump.temp_converter"] = float(val)
                elif task_name == "volts":
                    self.state["ion_beam.source.turbo_pump.voltage"] = float(val)
                elif task_name == "amps":
                    self.state["ion_beam.source.turbo_pump.current"] = val * 0.1
                elif task_name == "op_hours":
                    self.state["system.service_hours"] = round(val * 0.01, 2)
                elif task_name == "warnings":
                    active_warnings = [desc for bit, desc in TURBO_WARNING_DICT.items() if val & (1 << bit)]
                    self._most_recent_warning_desc = ", ".join(active_warnings) if active_warnings else "None"
                elif task_name == "error":
                    self.state["ion_beam.source.turbo_pump.error_code"] = float(val)
                    self._most_recent_error_desc = TURBO_ERROR_DICT.get(val, f"Unknown Error ({val})")

            self._slow_idx = (self._slow_idx + 1) % len(self.slow_tasks)

            # 4. Broadcast
            self.state["timestamp"] = time.time()
            elapsed = time.perf_counter() - cycle_start
            self.state["system.cycle_time_ms"] = elapsed * 1000

            try:
                topic = TOPIC_SRC_TURBO_DATA if isinstance(TOPIC_SRC_TURBO_DATA,
                                                           bytes) else TOPIC_SRC_TURBO_DATA.encode('utf-8')
                self.pub_socket.send_multipart([topic, orjson.dumps(self.state)])
            except Exception as e:
                self.events.log_general(f"ZMQ Publish Error: {e}")

            current_time = time.time()
            if current_time - self.last_hb_time >= 0.5:
                self.hb_socket.send_json({"service": "service_source_turbo", "ts": current_time})
                self.last_hb_time = current_time

            # Strict Spin-Wait OS Scheduling
            sleep_time = next_tick - time.perf_counter()
            if sleep_time > 0.002:
                time.sleep(sleep_time - 0.002)

            while time.perf_counter() < next_tick:
                pass

            next_tick += POLL_INTERVAL


if __name__ == "__main__":
    TurbovacMicroservice().run()